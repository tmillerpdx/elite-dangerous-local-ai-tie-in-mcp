"""Wing mining mission (WMM) stack and faction reputation, read from the journal.

Pure functions over raw journal event dicts, plus a small reader that pulls
only the wanted event types out of recent journal files. The data store keeps
about a day of events, which is too short for a mission stack that lives up
to a week or a reputation snapshot from the last visit to a system, so these
tools read the journal files directly.

Journal facts used here:
- MissionAccepted carries MissionID, Name, Faction, Commodity, Count, Reward,
  Expiry and the Wing flag, but not the station it was taken at; that comes
  from the Docked or Location event before it.
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
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

MISSION_LIMIT = 20
EXPIRY_ALERT_HOURS = 48.0
WMM_COMMODITIES: Set[str] = {"Gold", "Silver", "Bertrandite", "Indite"}
WMM_STATIONS: Dict[str, Set[str]] = {
    "mbutas": {"burkin orbital", "darlton port"},
    "paemara": {"rukavishnikov terminal"},
}
WMM_SYSTEMS: List[str] = ["Mbutas", "Paemara"]
FACTIONS_WITHOUT_WMM: Set[str] = {"paemara gold posse"}

STACK_EVENT_TYPES: Set[str] = {
    "Docked", "Location", "MissionAccepted", "MissionCompleted", "MissionAbandoned",
    "MissionFailed", "Missions", "CargoDepot",
}
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
        if kind == "Docked" or (kind == "Location" and event.get("Docked")):
            station = event.get("StationName")
            system = event.get("StarSystem")
        elif kind == "Location":
            station = None
            system = event.get("StarSystem")
        elif kind == "MissionAccepted" and event.get("MissionID") is not None:
            count = event.get("Count")
            commodity = _commodity_name(event)
            active[event["MissionID"]] = {
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
                "wing": bool(event.get("Wing")),
                "is_cargo": commodity is not None and count is not None,
                "details_known": True,
            }
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
        "hauling_plan": hauling,
        "missions": missions,
        "board_refresh": {
            "next_refresh_utc": boundary.isoformat(),
            "seconds_until": int((boundary - now).total_seconds()),
        },
    }


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
