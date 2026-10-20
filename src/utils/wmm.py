"""Wing mining mission (WMM) stack, history and faction reputation, from the journal.

Pure functions over raw journal event dicts, plus a small reader that pulls
only the wanted event types out of recent journal files. The data store keeps
about a day of events, which is too short for a mission stack that lives up
to a week or a reputation snapshot from the last visit to a system, so these
tools read the journal files directly.

Journal facts used here:
- MissionAccepted carries MissionID, Name, Faction, Commodity, Count, Reward,
  Expiry and the Wing flag, but not the station it was taken at; that comes
  from the Docked or Location event before it.
- MissionCompleted carries MissionID, Faction, Commodity, Count and the
  Reward actually paid, but not the station or the Wing flag.
- CargoDepot carries ItemsDelivered and TotalItemsToDeliver per MissionID.
- Missions is written at game start and lists every active mission.
- FSDJump, Location and CarrierJump carry Factions[] with MyReputation
  (-100 to 100) for the system being entered.
"""

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

MISSION_LIMIT = 20
EXPIRY_ALERT_HOURS = 48.0
# PTN wing mining mission programme. PTN rotates systems, so these lists are
# the one place to edit when it does. Station names are lower-case.
WMM_COMMODITIES: Set[str] = {"Gold", "Silver", "Bertrandite", "Indite", "Gallite"}
WMM_STATIONS: Dict[str, Set[str]] = {
    "mbutas": {"burkin orbital", "darlton port"},
    "paemara": {"rukavishnikov terminal"},
    # Expansion-based WMM test, announced 17 Oct 2026.
    "col 285 sector vf-e c12-5": {"bismarck legacy", "sei point"},
}
WMM_SYSTEMS: List[str] = ["Mbutas", "Paemara", "Col 285 Sector VF-E c12-5"]
FACTIONS_WITHOUT_WMM: Set[str] = {"paemara gold posse"}

STACK_EVENT_TYPES: Set[str] = {
    "Docked", "Location", "MissionAccepted", "MissionCompleted", "MissionAbandoned",
    "MissionFailed", "Missions", "CargoDepot",
}
HISTORY_EVENT_TYPES: Set[str] = {
    "Docked", "Location", "MissionAccepted", "MissionCompleted", "MissionAbandoned",
    "MissionFailed",
}
HISTORY_GROUPINGS: List[str] = ["week", "commodity", "faction", "station"]
REPUTATION_EVENT_TYPES: Set[str] = {"FSDJump", "Location", "CarrierJump"}

_UNKNOWN_DETAIL_HINTS = ("mining", "collect", "wing")


def _parse_time(value: Any) -> Optional[datetime]:
    """Parse a journal timestamp into an aware UTC datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def standing_for(reputation: float) -> str:
    """Name the standing for a MyReputation value (-100 to 100)."""
    if reputation >= 90:
        return "Allied"
    if reputation >= 35:
        return "Friendly"
    if reputation >= 4:
        return "Cordial"
    if reputation >= -35:
        return "Neutral"
    if reputation >= -90:
        return "Unfriendly"
    return "Hostile"


def _commodity_name(event: Dict[str, Any]) -> Optional[str]:
    localised = event.get("Commodity_Localised")
    if localised:
        return str(localised)
    raw = str(event.get("Commodity") or "")
    if raw.startswith("$") and raw.lower().endswith("_name;"):
        raw = raw[1:-6]
    return raw.title() if raw else None


def _mission_flags(mission: Dict[str, Any]) -> List[str]:
    """Reasons a cargo mission is not a good PTN wing mining mission."""
    flags: List[str] = []
    if mission["commodity"] not in WMM_COMMODITIES:
        flags.append("wrong_commodity")
    if "collect" in (mission["name"] or "").lower():
        flags.append("source_and_return")
    if not mission["wing"]:
        flags.append("not_wing")
    system = (mission["system"] or "").lower()
    station = (mission["station"] or "").lower()
    if station not in WMM_STATIONS.get(system, set()):
        flags.append("unsupported_station")
    return flags


def _docked_at(event: Dict[str, Any]) -> Optional[Tuple[Optional[str], Optional[str]]]:
    """(station, system) after a Docked or Location event, or None if not one."""
    kind = event.get("event")
    if kind == "Docked" or (kind == "Location" and event.get("Docked")):
        return event.get("StationName"), event.get("StarSystem")
    if kind == "Location":
        return None, event.get("StarSystem")
    return None


def _mission_from_accepted(
    event: Dict[str, Any], station: Optional[str], system: Optional[str]
) -> Dict[str, Any]:
    """The mission record build_wmm_stack keeps for a MissionAccepted event."""
    count = event.get("Count")
    commodity = _commodity_name(event)
    return {
        "mission_id": event["MissionID"],
        "name": event.get("Name"),
        "title": event.get("LocalisedName"),
        "faction": event.get("Faction"),
        "station": station,
        "system": system,
        "commodity": commodity,
        "tons_required": int(count) if count is not None else None,
        "tons_delivered": 0,
        "reward": int(event.get("Reward") or 0),
        "expiry_time": _parse_time(event.get("Expiry")),
        "accepted_time": _parse_time(event.get("timestamp")),
        "wing": bool(event.get("Wing")),
        "is_cargo": commodity is not None and count is not None,
        "details_known": True,
    }


def build_wmm_stack(
    events: Iterable[Dict[str, Any]], now: datetime, cargo_capacity: int = 0
) -> Dict[str, Any]:
    """Rebuild the active mission stack from journal events in time order.

    Cargo missions (those with a commodity and a count) are listed and
    checked against the PTN wing mining rules. Other active missions only
    count toward the 20-mission limit.
    """
    station: Optional[str] = None
    system: Optional[str] = None
    active: Dict[int, Dict[str, Any]] = {}

    for event in events:
        kind = event.get("event")
        location = _docked_at(event)
        if location is not None:
            station, system = location
        elif kind == "MissionAccepted" and event.get("MissionID") is not None:
            active[event["MissionID"]] = _mission_from_accepted(event, station, system)
        elif kind in ("MissionCompleted", "MissionAbandoned", "MissionFailed"):
            active.pop(event.get("MissionID"), None)
        elif kind == "CargoDepot":
            mission = active.get(event.get("MissionID"))
            if mission is not None and event.get("ItemsDelivered") is not None:
                mission["tons_delivered"] = int(event["ItemsDelivered"])
                if mission["tons_required"] is None and event.get("TotalItemsToDeliver"):
                    mission["tons_required"] = int(event["TotalItemsToDeliver"])
        elif kind == "Missions":
            taken = _parse_time(event.get("timestamp")) or now
            listed = {}
            for entry in (event.get("Active") or []) + (event.get("Complete") or []):
                if entry.get("MissionID") is not None:
                    listed[entry["MissionID"]] = entry
            for mission_id in [m for m in active if m not in listed]:
                del active[mission_id]
            for mission_id, entry in listed.items():
                if mission_id in active:
                    continue
                name = str(entry.get("Name") or "")
                expires = entry.get("Expires") or 0
                active[mission_id] = {
                    "mission_id": mission_id, "name": name, "title": None, "faction": None,
                    "station": None, "system": None, "commodity": None,
                    "tons_required": None, "tons_delivered": 0, "reward": 0,
                    "expiry_time": taken + timedelta(seconds=int(expires)) if expires else None,
                    "wing": "wing" in name.lower(),
                    "is_cargo": any(hint in name.lower() for hint in _UNKNOWN_DETAIL_HINTS),
                    "details_known": False,
                }

    live = [m for m in active.values() if m["expiry_time"] is None or m["expiry_time"] > now]
    missions: List[Dict[str, Any]] = []
    totals: Dict[str, Dict[str, int]] = {}
    by_station: Dict[str, Dict[str, Any]] = {}
    for mission in live:
        if not mission["is_cargo"]:
            continue
        flags = _mission_flags(mission) if mission["details_known"] else ["details_unknown"]
        required = mission["tons_required"]
        remaining = max(0, required - mission["tons_delivered"]) if required is not None else None
        expiry = mission["expiry_time"]
        missions.append({
            "mission_id": mission["mission_id"],
            "title": mission["title"] or mission["name"],
            "faction": mission["faction"],
            "station": mission["station"],
            "system": mission["system"],
            "commodity": mission["commodity"],
            "tons_required": required,
            "tons_delivered": mission["tons_delivered"],
            "tons_remaining": remaining,
            "reward": mission["reward"],
            "expiry": expiry.isoformat() if expiry else None,
            "hours_left": round((expiry - now).total_seconds() / 3600.0, 1) if expiry else None,
            "wing": mission["wing"],
            "flags": flags,
        })
        if not flags:
            total = totals.setdefault(mission["commodity"], {
                "missions": 0, "tons_required": 0, "tons_delivered": 0,
                "tons_remaining": 0, "reward": 0,
            })
            total["missions"] += 1
            total["tons_required"] += required
            total["tons_delivered"] += mission["tons_delivered"]
            total["tons_remaining"] += remaining
            total["reward"] += mission["reward"]
            station_total = by_station.setdefault(mission["station"], {
                "system": mission["system"], "missions": 0, "tons_required": 0,
                "tons_delivered": 0, "tons_remaining": 0, "reward": 0,
            })
            station_total["missions"] += 1
            station_total["tons_required"] += required
            station_total["tons_delivered"] += mission["tons_delivered"]
            station_total["tons_remaining"] += remaining
            station_total["reward"] += mission["reward"]

    missions.sort(key=lambda m: (m["expiry"] is None, m["expiry"] or "", m["mission_id"]))
    dated = [m for m in missions if m["expiry"] is not None]
    earliest = None
    if dated:
        first = dated[0]
        earliest = {
            "mission_id": first["mission_id"],
            "expiry": first["expiry"],
            "hours_left": first["hours_left"],
            "alert": first["hours_left"] < EXPIRY_ALERT_HOURS,
        }

    hauling = None
    capacity = int(cargo_capacity or 0)
    if capacity > 0:
        remaining_total = sum(t["tons_remaining"] for t in totals.values())
        hauling = {
            "cargo_capacity_t": capacity,
            "loads_by_commodity": {
                name: math.ceil(t["tons_remaining"] / capacity)
                for name, t in totals.items() if t["tons_remaining"] > 0
            },
            "total_tons_remaining": remaining_total,
            "total_loads": math.ceil(remaining_total / capacity),
        }

    boundary = now.replace(second=0, microsecond=0) + timedelta(minutes=10 - now.minute % 10)
    good = [m for m in missions if not m["flags"]]
    return {
        "wmm_count": len(good),
        "flagged_count": len(missions) - len(good),
        "active_mission_count": len(live),
        "mission_slots_left": max(0, MISSION_LIMIT - len(live)),
        "total_reward": sum(m["reward"] for m in good),
        "earliest_expiry": earliest,
        "totals_by_commodity": totals,
        "totals_by_station": by_station,
        "hauling_plan": hauling,
        "missions": missions,
        "board_refresh": {
            "next_refresh_utc": boundary.isoformat(),
            "seconds_until": int((boundary - now).total_seconds()),
        },
    }


def _in_range(moment: Optional[datetime], start: Optional[datetime],
              end: Optional[datetime]) -> bool:
    if moment is None:
        return False
    if start is not None and moment < start:
        return False
    if end is not None and moment > end:
        return False
    return True


def _new_total(**extra: Any) -> Dict[str, Any]:
    total: Dict[str, Any] = dict(extra)
    total.update({"missions": 0, "tons": 0, "reward": 0, "_hours": []})
    return total


def _add_to_total(total: Dict[str, Any], row: Dict[str, Any]) -> None:
    total["missions"] += 1
    total["tons"] += row["tons"]
    total["reward"] += row["reward"]
    if row["hours_to_complete"] is not None:
        total["_hours"].append(row["hours_to_complete"])


def _finish_totals(totals: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    for total in totals.values():
        hours = total.pop("_hours")
        total["average_hours_to_complete"] = (
            round(sum(hours) / len(hours), 1) if hours else None)
    return totals


def build_wmm_history(
    events: Iterable[Dict[str, Any]],
    start: Optional[datetime],
    end: Optional[datetime],
    group_by: str = "",
) -> Dict[str, Any]:
    """Completed wing mining missions, and trends, over a date range.

    Events must be in time order and should begin well before start, since a
    mission completed inside the range was accepted up to a week earlier.
    Each MissionCompleted is paired with its MissionAccepted by MissionID for
    faction, commodity, tonnage, the station it was taken at, the Wing flag
    and the time taken. Only missions that pass the get_wmm_stack rules are
    listed and totalled; the rest are counted. The range applies to the
    completion time.

    Args:
        events: Raw journal events of HISTORY_EVENT_TYPES, oldest first
        start, end: Inclusive bounds on completion time; None for no bound
        group_by: "", for every grouping, or one of HISTORY_GROUPINGS
    """
    grouping = (group_by or "").strip().lower()
    if grouping and grouping not in HISTORY_GROUPINGS:
        return {
            "error": "group_by must be one of %s, got '%s'" % (
                ", ".join(HISTORY_GROUPINGS), group_by),
            "valid_group_by": list(HISTORY_GROUPINGS),
        }

    station: Optional[str] = None
    system: Optional[str] = None
    accepted: Dict[Any, Dict[str, Any]] = {}
    rows: List[Dict[str, Any]] = []
    flagged = 0
    unmatched = 0
    abandoned = 0

    for event in events:
        kind = event.get("event")
        location = _docked_at(event)
        if location is not None:
            station, system = location
        elif kind == "MissionAccepted" and event.get("MissionID") is not None:
            accepted[event["MissionID"]] = _mission_from_accepted(event, station, system)
        elif kind in ("MissionAbandoned", "MissionFailed"):
            mission = accepted.pop(event.get("MissionID"), None)
            when = _parse_time(event.get("timestamp"))
            if (mission is not None and mission["is_cargo"] and not _mission_flags(mission)
                    and _in_range(when, start, end)):
                abandoned += 1
        elif kind == "MissionCompleted":
            mission = accepted.pop(event.get("MissionID"), None)
            when = _parse_time(event.get("timestamp"))
            if not _in_range(when, start, end):
                continue
            if mission is None:
                # Acceptance not in the events read. A cargo completion we
                # cannot judge; anything else (a donation, a courier run)
                # is not a WMM and is ignored.
                if _commodity_name(event) is not None and event.get("Count") is not None:
                    unmatched += 1
                continue
            if not mission["is_cargo"]:
                continue
            if _mission_flags(mission):
                flagged += 1
                continue
            taken = mission["accepted_time"]
            hours = (round((when - taken).total_seconds() / 3600.0, 1)
                     if taken is not None else None)
            year, week, _ = when.isocalendar()
            rows.append({
                "mission_id": mission["mission_id"],
                "title": mission["title"] or mission["name"],
                "faction": event.get("Faction") or mission["faction"],
                "station": mission["station"],
                "system": mission["system"],
                "commodity": mission["commodity"],
                "tons": int(event.get("Count") or mission["tons_required"] or 0),
                "reward": int(event.get("Reward") or mission["reward"] or 0),
                "wing": mission["wing"],
                "accepted_at": taken.isoformat() if taken else None,
                "completed_at": when.isoformat(),
                "hours_to_complete": hours,
                "iso_week": "%d-W%02d" % (year, week),
            })

    rows.sort(key=lambda r: (r["completed_at"], r["mission_id"]))

    by_week: Dict[str, Dict[str, Any]] = {}
    by_commodity: Dict[str, Dict[str, Any]] = {}
    by_faction: Dict[str, Dict[str, Any]] = {}
    by_station: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        completed = datetime.fromisoformat(row["completed_at"])
        week_start = (completed - timedelta(days=completed.weekday())).date().isoformat()
        _add_to_total(by_week.setdefault(row["iso_week"], _new_total(week_start=week_start)), row)
        _add_to_total(by_commodity.setdefault(row["commodity"], _new_total()), row)
        _add_to_total(by_faction.setdefault(row["faction"], _new_total()), row)
        _add_to_total(by_station.setdefault(
            row["station"], _new_total(system=row["system"])), row)

    hours = [r["hours_to_complete"] for r in rows if r["hours_to_complete"] is not None]
    result: Dict[str, Any] = {
        "date_range": {
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
        },
        "completed_count": len(rows),
        "total_tons": sum(r["tons"] for r in rows),
        "total_reward": sum(r["reward"] for r in rows),
        "average_hours_to_complete": round(sum(hours) / len(hours), 1) if hours else None,
        "abandoned_or_failed_count": abandoned,
        "flagged_completed_count": flagged,
        "unmatched_completed_count": unmatched,
    }
    groupings = {
        "week": ("totals_by_week", by_week),
        "commodity": ("totals_by_commodity", by_commodity),
        "faction": ("totals_by_faction", by_faction),
        "station": ("totals_by_station", by_station),
    }
    for name in ([grouping] if grouping else HISTORY_GROUPINGS):
        key, totals = groupings[name]
        result[key] = _finish_totals(totals)
    result["missions"] = rows
    return result


def faction_reputation(
    events: Iterable[Dict[str, Any]], systems: List[str], now: datetime
) -> Dict[str, Any]:
    """Reputation with each minor faction, from the last visit to each system."""
    wanted = {name.strip().lower(): name.strip() for name in systems if name.strip()}
    latest: Dict[str, Dict[str, Any]] = {}
    for event in events:
        key = str(event.get("StarSystem") or "").lower()
        if key not in wanted or not event.get("Factions"):
            continue
        seen = _parse_time(event.get("timestamp"))
        previous = latest.get(key)
        if previous is None or (seen and seen >= previous["seen"]):
            latest[key] = {"event": event, "seen": seen or now}

    results = []
    for key, given in wanted.items():
        found = latest.get(key)
        if found is None:
            results.append({
                "system": given, "as_of": None,
                "note": "No visit to this system was found in the journals searched.",
            })
            continue
        factions = []
        for faction in found["event"]["Factions"]:
            name = faction.get("Name")
            value = round(float(faction.get("MyReputation") or 0.0), 1)
            factions.append({
                "faction": name,
                "reputation": value,
                "standing": standing_for(value),
                "allied": value >= 90,
                "state": faction.get("FactionState"),
                "influence_percent": round(float(faction.get("Influence") or 0.0) * 100, 1),
                "offers_wmm": str(name or "").lower() not in FACTIONS_WITHOUT_WMM,
            })
        counted = [f for f in factions if f["offers_wmm"]]
        results.append({
            "system": found["event"].get("StarSystem"),
            "as_of": found["seen"].isoformat(),
            "age_days": round((now - found["seen"]).total_seconds() / 86400.0, 1),
            "factions": factions,
            "not_allied": [f["faction"] for f in counted if not f["allied"]],
            "not_at_full_reputation": [f["faction"] for f in counted if f["reputation"] < 100],
        })
    return {
        "systems": results,
        "notes": [
            "Reputation is the value the game wrote when the commander last entered or "
            "logged in to the system. It does not move until the next visit.",
            "Allied is taken as 90 or more (documented for superpowers, assumed for "
            "minor factions); 100 is the full bar.",
        ],
    }


def read_recent_journal_events(
    journal_path: Optional[Path], days: float, event_types: Set[str], now: datetime
) -> List[Dict[str, Any]]:
    """Read the wanted event types from journal files changed in the last days.

    Files are read oldest first, so events come back in time order.
    """
    if not journal_path:
        return []
    folder = Path(journal_path)
    if not folder.is_dir():
        return []
    cutoff = (now - timedelta(days=days)).timestamp()
    recent = []
    for path in folder.glob("Journal.*.log"):
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if modified >= cutoff:
            recent.append((modified, path))
    needles = ['"%s"' % name for name in event_types]
    events: List[Dict[str, Any]] = []
    for _, path in sorted(recent):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not any(needle in line for needle in needles):
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and event.get("event") in event_types:
                        events.append(event)
        except OSError as exc:
            logger.warning("Could not read journal file %s: %s", path, exc)
    return events
