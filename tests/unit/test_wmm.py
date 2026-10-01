"""
Tests for the wing mining mission (WMM) stack and faction reputation tools.

Events are plain journal dicts; no test reads the real journal folder except
through tmp_path.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.elite_mcp.mcp_tools import MCPTools
from src.utils.data_store import DataStore, GameState
from src.utils.wmm import (
    build_wmm_stack,
    faction_reputation,
    read_recent_journal_events,
    standing_for,
)

NOW = datetime(2026, 10, 1, 12, 3, 0, tzinfo=timezone.utc)


def _docked(station="Burkin Orbital", system="Mbutas", ts="2026-09-30T10:00:00Z"):
    return {"timestamp": ts, "event": "Docked", "StationName": station, "StarSystem": system}


def _accepted(mission_id, commodity="Gold", count=200, reward=40000000, wing=True,
              name="Mission_Mining_Wing", faction="Mbutas Systems",
              expiry="2026-10-06T10:00:00Z", ts="2026-09-30T10:05:00Z"):
    return {
        "timestamp": ts, "event": "MissionAccepted", "Faction": faction, "Name": name,
        "LocalisedName": "Mine %d units of %s" % (count, commodity),
        "Commodity": "$%s_Name;" % commodity, "Commodity_Localised": commodity,
        "Count": count, "Reward": reward, "Expiry": expiry, "Wing": wing,
        "Influence": "++", "Reputation": "++", "MissionID": mission_id,
    }


def _depot(mission_id, delivered, total, ts="2026-09-30T12:00:00Z"):
    return {"timestamp": ts, "event": "CargoDepot", "MissionID": mission_id,
            "UpdateType": "Deliver", "ItemsCollected": 0, "ItemsDelivered": delivered,
            "TotalItemsToDeliver": total, "Progress": 0.0}


class TestStanding:
    @pytest.mark.parametrize("value,label", [
        (100.0, "Allied"), (90.0, "Allied"), (89.9, "Friendly"), (35.0, "Friendly"),
        (34.9, "Cordial"), (4.0, "Cordial"), (0.0, "Neutral"), (-35.0, "Neutral"),
        (-36.0, "Unfriendly"), (-90.0, "Unfriendly"), (-95.0, "Hostile"),
    ])
    def test_thresholds(self, value, label):
        assert standing_for(value) == label


class TestBuildStack:
    def test_tracks_station_progress_and_totals(self):
        events = [
            _docked(),
            _accepted(1, "Gold", 200, 40000000),
            _accepted(2, "Gold", 150, 35000000, expiry="2026-10-05T08:03:00Z"),
            _accepted(3, "Silver", 400, 45000000),
            _depot(1, 120, 200),
        ]
        stack = build_wmm_stack(events, NOW)
        assert stack["wmm_count"] == 3
        assert stack["active_mission_count"] == 3
        assert stack["mission_slots_left"] == 17
        first = stack["missions"][0]
        assert first["mission_id"] == 2  # soonest expiry first
        gold = next(m for m in stack["missions"] if m["mission_id"] == 1)
        assert gold["station"] == "Burkin Orbital"
        assert gold["system"] == "Mbutas"
        assert gold["faction"] == "Mbutas Systems"
        assert gold["commodity"] == "Gold"
        assert gold["tons_required"] == 200
        assert gold["tons_delivered"] == 120
        assert gold["tons_remaining"] == 80
        assert gold["reward"] == 40000000
        assert gold["wing"] is True
        assert gold["flags"] == []
        assert stack["totals_by_commodity"]["Gold"] == {
            "missions": 2, "tons_required": 350, "tons_delivered": 120,
            "tons_remaining": 230, "reward": 75000000,
        }
        assert stack["total_reward"] == 120000000
        assert stack["earliest_expiry"]["mission_id"] == 2
        assert stack["earliest_expiry"]["hours_left"] == 92.0
        assert stack["earliest_expiry"]["alert"] is False

    def test_expiry_alert_within_48_hours(self):
        events = [_docked(), _accepted(1, expiry="2026-10-03T00:00:00Z")]
        assert build_wmm_stack(events, NOW)["earliest_expiry"]["alert"] is True

    def test_finished_missions_leave_the_stack(self):
        events = [
            _docked(),
            _accepted(1), _accepted(2), _accepted(3), _accepted(4),
            {"timestamp": "2026-09-30T13:00:00Z", "event": "MissionCompleted", "MissionID": 1},
            {"timestamp": "2026-09-30T13:01:00Z", "event": "MissionAbandoned", "MissionID": 2},
            {"timestamp": "2026-09-30T13:02:00Z", "event": "MissionFailed", "MissionID": 3},
        ]
        stack = build_wmm_stack(events, NOW)
        assert [m["mission_id"] for m in stack["missions"]] == [4]

    def test_expired_missions_are_dropped(self):
        events = [_docked(), _accepted(1, expiry="2026-09-30T23:00:00Z")]
        assert build_wmm_stack(events, NOW)["missions"] == []

    def test_bad_missions_are_flagged_and_kept_out_of_totals(self):
        events = [
            _docked(),
            _accepted(1, "Bromellite", 700),
            _accepted(2, "Indium", 700),
            _accepted(3, "Gold", 200, wing=False, name="Mission_Mining"),
            _accepted(4, "Gold", 200, name="Mission_CollectWing"),
            _docked("Some Other Port", "Mbutas", ts="2026-09-30T11:00:00Z"),
            _accepted(5, "Gold", 200, ts="2026-09-30T11:05:00Z"),
        ]
        stack = build_wmm_stack(events, NOW)
        flags = {m["mission_id"]: m["flags"] for m in stack["missions"]}
        assert flags[1] == ["wrong_commodity"]
        assert flags[2] == ["wrong_commodity"]
        assert flags[3] == ["not_wing"]
        assert flags[4] == ["source_and_return"]
        assert flags[5] == ["unsupported_station"]
        assert stack["wmm_count"] == 0
        assert stack["flagged_count"] == 5
        assert stack["totals_by_commodity"] == {}

    def test_missions_snapshot_removes_missing_and_keeps_unknown(self):
        events = [
            _docked(),
            _accepted(1), _accepted(2),
            {"timestamp": "2026-10-01T06:00:00Z", "event": "Missions", "Failed": [], "Complete": [],
             "Active": [
                 {"MissionID": 2, "Name": "Mission_Mining_Wing_name", "Expires": 400000},
                 {"MissionID": 9, "Name": "Mission_Mining_Wing_name", "Expires": 86400},
             ]},
        ]
        stack = build_wmm_stack(events, NOW)
        ids = [m["mission_id"] for m in stack["missions"]]
        assert 1 not in ids and 2 in ids and 9 in ids
        unknown = next(m for m in stack["missions"] if m["mission_id"] == 9)
        assert unknown["flags"] == ["details_unknown"]
        assert unknown["commodity"] is None
        assert unknown["expiry"] == "2026-10-02T06:00:00+00:00"

    def test_non_cargo_missions_only_count_toward_the_limit(self):
        donation = {"timestamp": "2026-09-30T10:06:00Z", "event": "MissionAccepted",
                    "Faction": "Mbutas Systems", "Name": "Mission_AltruismCredits",
                    "Donation": "1000000", "Expiry": "2026-10-06T10:00:00Z", "Wing": False,
                    "MissionID": 50}
        stack = build_wmm_stack([_docked(), donation, _accepted(1)], NOW)
        assert [m["mission_id"] for m in stack["missions"]] == [1]
        assert stack["active_mission_count"] == 2
        assert stack["mission_slots_left"] == 18

    def test_hauling_plan(self):
        events = [_docked(), _accepted(1, "Gold", 200), _accepted(2, "Silver", 400),
                  _depot(2, 100, 400)]
        stack = build_wmm_stack(events, NOW, cargo_capacity=128)
        assert stack["hauling_plan"] == {
            "cargo_capacity_t": 128,
            "loads_by_commodity": {"Gold": 2, "Silver": 3},
            "total_tons_remaining": 500,
            "total_loads": 4,
        }

    def test_no_hauling_plan_without_capacity(self):
        assert build_wmm_stack([_docked(), _accepted(1)], NOW)["hauling_plan"] is None

    def test_board_refresh_is_next_ten_minute_boundary(self):
        board = build_wmm_stack([], NOW)["board_refresh"]
        assert board["next_refresh_utc"] == "2026-10-01T12:10:00+00:00"
        assert board["seconds_until"] == 420


def _jump(system, factions, ts):
    return {"timestamp": ts, "event": "FSDJump", "StarSystem": system, "Factions": [
        {"Name": name, "MyReputation": rep, "FactionState": "None", "Influence": 0.2}
        for name, rep in factions
    ]}


class TestFactionReputation:
    def test_latest_visit_wins_and_not_allied_is_listed(self):
        events = [
            _jump("Paemara", [("Paemara Order", 10.0)], "2026-09-10T00:00:00Z"),
            _jump("Paemara", [("Paemara Order", 95.0), ("Dijkstra PLC", 68.8),
                              ("Paemara Gold Posse", 0.0)], "2026-09-30T00:00:00Z"),
        ]
        result = faction_reputation(events, ["Paemara"], NOW)
        system = result["systems"][0]
        assert system["system"] == "Paemara"
        assert system["as_of"] == "2026-09-30T00:00:00+00:00"
        assert system["age_days"] == 1.5
        by_name = {f["faction"]: f for f in system["factions"]}
        assert by_name["Paemara Order"]["standing"] == "Allied"
        assert by_name["Paemara Order"]["allied"] is True
        assert by_name["Dijkstra PLC"]["standing"] == "Friendly"
        assert by_name["Paemara Gold Posse"]["offers_wmm"] is False
        assert system["not_allied"] == ["Dijkstra PLC"]
        assert system["not_at_full_reputation"] == ["Paemara Order", "Dijkstra PLC"]

    def test_system_never_visited_is_reported(self):
        result = faction_reputation([], ["Mbutas"], NOW)
        assert result["systems"][0] == {
            "system": "Mbutas", "as_of": None,
            "note": "No visit to this system was found in the journals searched.",
        }

    def test_system_name_matching_ignores_case(self):
        events = [_jump("Mbutas", [("Mbutas Systems", 100.0)], "2026-09-30T00:00:00Z")]
        result = faction_reputation(events, ["mbutas"], NOW)
        assert result["systems"][0]["not_allied"] == []
        assert result["systems"][0]["not_at_full_reputation"] == []


class TestReadRecentJournalEvents:
    def test_reads_wanted_events_from_recent_files_in_order(self, tmp_path):
        old = tmp_path / "Journal.2026-08-01T000000.01.log"
        old.write_text(json.dumps(_docked("Old Port")) + "\n", encoding="utf-8")
        stale = (NOW - timedelta(days=40)).timestamp()
        os.utime(old, (stale, stale))
        first = tmp_path / "Journal.2026-09-29T000000.01.log"
        first.write_text(
            json.dumps(_docked()) + "\n" + json.dumps({"event": "Music"}) + "\nnot json\n",
            encoding="utf-8")
        second = tmp_path / "Journal.2026-09-30T000000.01.log"
        second.write_text(json.dumps(_accepted(1)) + "\n", encoding="utf-8")
        day = (NOW - timedelta(days=2)).timestamp()
        os.utime(first, (day, day))
        os.utime(second, (day + 3600, day + 3600))
        (tmp_path / "Status.json").write_text("{}", encoding="utf-8")

        events = read_recent_journal_events(
            tmp_path, 14, {"Docked", "MissionAccepted"}, NOW)
        assert [e["event"] for e in events] == ["Docked", "MissionAccepted"]
        assert events[0]["StationName"] == "Burkin Orbital"

    def test_missing_folder_gives_no_events(self, tmp_path):
        assert read_recent_journal_events(tmp_path / "nope", 14, {"Docked"}, NOW) == []
        assert read_recent_journal_events(None, 14, {"Docked"}, NOW) == []


class TestWmmTools:
    @pytest.fixture
    def store(self):
        store = Mock(spec=DataStore)
        store.get_game_state.return_value = GameState(
            current_system="Mbutas", last_updated=datetime.now(timezone.utc))
        store.get_events_by_type.return_value = [SimpleNamespace(
            raw_event={"event": "Loadout", "CargoCapacity": 720},
            timestamp=datetime.now(timezone.utc))]
        return store

    def _future(self):
        return (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def test_stack_uses_journal_events_and_ship_capacity(self, store):
        seen = {}

        def reader(days, event_types):
            seen["days"] = days
            seen["types"] = event_types
            return [_docked(), _accepted(1, "Gold", 200, expiry=self._future())]

        result = await MCPTools(store, journal_reader=reader).get_wmm_stack()
        assert "MissionAccepted" in seen["types"] and "CargoDepot" in seen["types"]
        assert result["wmm_count"] == 1
        assert result["hauling_plan"]["cargo_capacity_t"] == 720
        assert result["hauling_plan"]["total_loads"] == 1

    async def test_reputation_defaults_to_the_wmm_systems(self, store):
        events = [_jump("Mbutas", [("Mbutas Systems", 100.0)], "2026-09-30T00:00:00Z")]
        tools = MCPTools(store, journal_reader=lambda days, event_types: events)
        result = await tools.get_faction_reputation()
        assert [s["system"] for s in result["systems"]] == ["Mbutas", "Paemara"]

    async def test_reputation_accepts_a_system_list(self, store):
        tools = MCPTools(store, journal_reader=lambda days, event_types: [])
        result = await tools.get_faction_reputation("Sol, Lave")
        assert [s["system"] for s in result["systems"]] == ["Sol", "Lave"]

    async def test_reader_failure_returns_error_object(self, store):
        def reader(days, event_types):
            raise OSError("journal folder unreadable")

        result = await MCPTools(store, journal_reader=reader).get_wmm_stack()
        assert "journal folder unreadable" in result["error"]
