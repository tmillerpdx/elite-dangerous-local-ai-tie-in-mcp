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
from src.utils.raw_journal import iter_journal_events
from src.utils.wmm import (
    build_wmm_history,
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
        assert [s["system"] for s in result["systems"]] == [
            "Mbutas", "Paemara", "Col 285 Sector VF-E c12-5",
        ]

    async def test_reputation_accepts_a_system_list(self, store):
        tools = MCPTools(store, journal_reader=lambda days, event_types: [])
        result = await tools.get_faction_reputation("Sol, Lave")
        assert [s["system"] for s in result["systems"]] == ["Sol", "Lave"]

    async def test_reader_failure_returns_error_object(self, store):
        def reader(days, event_types):
            raise OSError("journal folder unreadable")

        result = await MCPTools(store, journal_reader=reader).get_wmm_stack()
        assert "journal folder unreadable" in result["error"]


class TestExpansionSystem:
    """PTN's October 2026 expansion WMM system and its fifth commodity.

    Missions there read "Mining Expansion Support needs N Units of X" and are
    stacked at Bismarck Legacy and Sei Point in Col 285 Sector VF-E c12-5.
    Gallite joined the supported commodities; Bromellite and Indium did not.
    """

    SYSTEM = "Col 285 Sector VF-E c12-5"

    def _expansion(self, mission_id, commodity, count, reward, station, ts):
        event = _accepted(mission_id, commodity, count, reward, faction="Azimuth Biotech",
                          expiry="2026-10-25T20:00:00Z", ts=ts)
        event["LocalisedName"] = "Mining Expansion Support needs %d Units of %s" % (count, commodity)
        return event

    def _events(self):
        return [
            _docked("Sei Point", self.SYSTEM, ts="2026-10-18T20:00:00Z"),
            self._expansion(1, "Bertrandite", 1000, 60000000, "Sei Point", "2026-10-18T20:01:00Z"),
            self._expansion(2, "Gallite", 1076, 55000000, "Sei Point", "2026-10-18T20:02:00Z"),
            _docked("Bismarck Legacy", self.SYSTEM, ts="2026-10-18T21:00:00Z"),
            self._expansion(3, "Silver", 360, 30000000, "Bismarck Legacy", "2026-10-18T21:01:00Z"),
        ]

    def test_expansion_stations_and_gallite_count_as_wmm(self):
        stack = build_wmm_stack(self._events(), datetime(2026, 10, 19, 1, 0, tzinfo=timezone.utc))
        assert [m["flags"] for m in stack["missions"]] == [[], [], []]
        assert stack["wmm_count"] == 3
        assert stack["total_reward"] == 145000000
        assert stack["totals_by_commodity"]["Gallite"]["tons_required"] == 1076

    def test_bromellite_and_indium_stay_rejected_in_expansion_system(self):
        events = [
            _docked("Sei Point", self.SYSTEM),
            _accepted(1, "Bromellite", 700),
            _accepted(2, "Indium", 700),
        ]
        stack = build_wmm_stack(events, NOW)
        assert [m["flags"] for m in stack["missions"]] == [["wrong_commodity"], ["wrong_commodity"]]

    def test_totals_by_station(self):
        now = datetime(2026, 10, 19, 1, 0, tzinfo=timezone.utc)
        stack = build_wmm_stack(self._events(), now, cargo_capacity=0)
        by_station = stack["totals_by_station"]
        assert set(by_station) == {"Sei Point", "Bismarck Legacy"}
        assert by_station["Sei Point"] == {
            "system": self.SYSTEM, "missions": 2, "tons_required": 2076,
            "tons_delivered": 0, "tons_remaining": 2076, "reward": 115000000,
        }
        assert by_station["Bismarck Legacy"]["tons_required"] == 360

    def test_flagged_missions_stay_out_of_station_totals(self):
        events = [_docked(), _accepted(1, "Bromellite", 700)]
        assert build_wmm_stack(events, NOW)["totals_by_station"] == {}


def _completed(mission_id, commodity="Gold", count=200, reward=40000000,
               faction="Mbutas Systems", ts="2026-10-02T10:00:00Z"):
    return {
        "timestamp": ts, "event": "MissionCompleted", "Faction": faction,
        "Name": "Mission_Mining_Wing_name", "MissionID": mission_id,
        "Commodity": "$%s_Name;" % commodity, "Commodity_Localised": commodity,
        "Count": count, "Reward": reward, "FactionEffects": [],
    }


class TestBuildHistory:
    """Completed wing mining missions and trends over a date range."""

    START = datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc)
    END = datetime(2026, 10, 12, 23, 59, 59, tzinfo=timezone.utc)

    def _events(self):
        return [
            _docked(),
            _accepted(1, "Gold", 200, 40000000, ts="2026-09-30T10:05:00Z"),
            _accepted(2, "Silver", 400, 45000000, ts="2026-09-30T10:06:00Z"),
            _docked("Rukavishnikov Terminal", "Paemara", ts="2026-10-05T09:00:00Z"),
            _accepted(3, "Bertrandite", 300, 50000000, faction="Paemara Order",
                      ts="2026-10-05T09:05:00Z"),
            _accepted(4, "Gold", 100, 20000000, faction="Paemara Order",
                      ts="2026-10-05T09:06:00Z"),
            _completed(1, "Gold", 200, 41000000, ts="2026-10-02T10:05:00Z"),
            _completed(2, "Silver", 400, 45000000, ts="2026-10-03T16:06:00Z"),
            _completed(3, "Bertrandite", 300, 50000000, faction="Paemara Order",
                       ts="2026-10-07T21:05:00Z"),
            {"timestamp": "2026-10-08T00:00:00Z", "event": "MissionAbandoned", "MissionID": 4},
        ]

    def test_rows_pair_completion_with_acceptance(self):
        history = build_wmm_history(self._events(), self.START, self.END)
        assert history["completed_count"] == 3
        assert history["abandoned_or_failed_count"] == 1
        rows = history["missions"]
        assert [r["mission_id"] for r in rows] == [1, 2, 3]  # completion order
        gold = rows[0]
        assert gold["faction"] == "Mbutas Systems"
        assert gold["station"] == "Burkin Orbital"
        assert gold["system"] == "Mbutas"
        assert gold["commodity"] == "Gold"
        assert gold["tons"] == 200
        assert gold["reward"] == 41000000  # the reward actually paid
        assert gold["wing"] is True
        assert gold["accepted_at"] == "2026-09-30T10:05:00+00:00"
        assert gold["completed_at"] == "2026-10-02T10:05:00+00:00"
        assert gold["hours_to_complete"] == 48.0
        assert gold["iso_week"] == "2026-W40"
        assert history["total_tons"] == 900
        assert history["total_reward"] == 136000000
        assert history["average_hours_to_complete"] == round((48.0 + 78.0 + 60.0) / 3, 1)

    def test_totals_by_week_commodity_faction_and_station(self):
        history = build_wmm_history(self._events(), self.START, self.END)
        assert history["totals_by_week"] == {
            "2026-W40": {"week_start": "2026-09-28", "missions": 2, "tons": 600,
                         "reward": 86000000, "average_hours_to_complete": 63.0},
            "2026-W41": {"week_start": "2026-10-05", "missions": 1, "tons": 300,
                         "reward": 50000000, "average_hours_to_complete": 60.0},
        }
        assert history["totals_by_commodity"]["Gold"] == {
            "missions": 1, "tons": 200, "reward": 41000000, "average_hours_to_complete": 48.0,
        }
        assert set(history["totals_by_faction"]) == {"Mbutas Systems", "Paemara Order"}
        assert history["totals_by_faction"]["Mbutas Systems"]["missions"] == 2
        assert history["totals_by_station"]["Rukavishnikov Terminal"] == {
            "system": "Paemara", "missions": 1, "tons": 300, "reward": 50000000,
            "average_hours_to_complete": 60.0,
        }

    def test_group_by_keeps_only_that_grouping(self):
        history = build_wmm_history(self._events(), self.START, self.END, group_by="faction")
        assert "totals_by_faction" in history
        for other in ("totals_by_week", "totals_by_commodity", "totals_by_station"):
            assert other not in history

    def test_unknown_group_by_is_an_error(self):
        history = build_wmm_history(self._events(), self.START, self.END, group_by="pilot")
        assert "error" in history
        assert history["valid_group_by"] == ["week", "commodity", "faction", "station"]

    def test_range_filters_on_completion_time_only(self):
        start = datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc)
        history = build_wmm_history(self._events(), start, self.END)
        # Mission 2 was accepted before the range but completed inside it.
        assert [r["mission_id"] for r in history["missions"]] == [2, 3]
        assert history["abandoned_or_failed_count"] == 1
        history = build_wmm_history(self._events(), self.START,
                                    datetime(2026, 10, 4, 0, 0, tzinfo=timezone.utc))
        assert [r["mission_id"] for r in history["missions"]] == [1, 2]
        assert history["abandoned_or_failed_count"] == 0

    def test_missions_that_break_the_rules_are_counted_but_not_totalled(self):
        events = [
            _docked(),
            _accepted(1, "Bromellite", 700, ts="2026-09-30T10:05:00Z"),
            _accepted(2, "Gold", 200, wing=False, name="Mission_Mining", ts="2026-09-30T10:06:00Z"),
            _completed(1, "Bromellite", 700, ts="2026-10-01T10:00:00Z"),
            _completed(2, "Gold", 200, ts="2026-10-01T11:00:00Z"),
            _completed(9, "Gold", 200, ts="2026-10-01T12:00:00Z"),  # never accepted here
        ]
        history = build_wmm_history(events, self.START, self.END)
        assert history["completed_count"] == 0
        assert history["missions"] == []
        assert history["flagged_completed_count"] == 2
        assert history["unmatched_completed_count"] == 1
        assert history["totals_by_commodity"] == {}
        assert history["total_tons"] == 0

    def test_non_cargo_completions_are_ignored(self):
        events = [
            _docked(),
            {"timestamp": "2026-09-30T10:06:00Z", "event": "MissionAccepted",
             "Faction": "Mbutas Systems", "Name": "Mission_AltruismCredits",
             "Donation": "1000000", "Expiry": "2026-10-06T10:00:00Z", "Wing": False,
             "MissionID": 50},
            {"timestamp": "2026-09-30T10:07:00Z", "event": "MissionCompleted",
             "Faction": "Mbutas Systems", "Name": "Mission_AltruismCredits_name",
             "MissionID": 50, "Donated": 1000000},
        ]
        history = build_wmm_history(events, self.START, self.END)
        assert history["completed_count"] == 0
        assert history["flagged_completed_count"] == 0
        assert history["unmatched_completed_count"] == 0

    def test_empty_range_has_zero_totals_and_no_average(self):
        history = build_wmm_history([], self.START, self.END)
        assert history["completed_count"] == 0
        assert history["average_hours_to_complete"] is None
        assert history["totals_by_week"] == {}
        assert history["date_range"] == {
            "start": "2026-09-28T00:00:00+00:00", "end": "2026-10-12T23:59:59+00:00",
        }


class TestIterJournalEvents:
    def test_reads_only_wanted_types_in_file_order(self, tmp_path):
        first = tmp_path / "Journal.2026-09-29T000000.01.log"
        first.write_text(
            json.dumps(_docked()) + "\n" + json.dumps({"event": "Music"}) + "\nnot json\n",
            encoding="utf-8")
        second = tmp_path / "Journal.2026-09-30T000000.01.log"
        second.write_text(json.dumps(_accepted(1)) + "\n", encoding="utf-8")
        events = list(iter_journal_events(tmp_path, {"Docked", "MissionAccepted"}))
        assert [e["event"] for e in events] == ["Docked", "MissionAccepted"]

    def test_start_skips_files_that_end_before_it(self, tmp_path):
        old = tmp_path / "Journal.2026-08-01T000000.01.log"
        old.write_text(json.dumps(_docked("Old Port", ts="2026-08-01T01:00:00Z")) + "\n",
                       encoding="utf-8")
        new = tmp_path / "Journal.2026-09-30T000000.01.log"
        new.write_text(json.dumps(_docked()) + "\n", encoding="utf-8")
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        events = list(iter_journal_events(tmp_path, {"Docked"}, start=start))
        assert [e["StationName"] for e in events] == ["Burkin Orbital"]

    def test_missing_folder_yields_nothing(self, tmp_path):
        assert list(iter_journal_events(tmp_path / "nope", {"Docked"})) == []
        assert list(iter_journal_events(None, {"Docked"})) == []


class TestWmmHistoryTool:
    @pytest.fixture
    def store(self, tmp_path):
        store = Mock(spec=DataStore)
        store.journal_path = tmp_path
        return store

    def _completed_wmm(self):
        return [
            _docked(ts="2026-09-30T10:00:00Z"),
            _accepted(1, "Gold", 200, ts="2026-09-30T10:05:00Z"),
            _completed(1, "Gold", 200, ts="2026-10-02T10:05:00Z"),
        ]

    async def test_defaults_to_the_last_90_days(self, store):
        seen = {}

        def reader(event_types, start, end):
            seen.update(types=event_types, start=start, end=end)
            return self._completed_wmm()

        before = datetime.now(timezone.utc)
        result = await MCPTools(store, history_reader=reader).get_wmm_history()
        assert {"MissionAccepted", "MissionCompleted", "Docked"} <= seen["types"]
        # The reader is asked for a lead window before the range so that
        # acceptances before the start are still paired.
        assert seen["start"] < before - timedelta(days=90)
        assert seen["end"] is None
        assert result["completed_count"] == 1
        assert result["date_range"]["end"] is not None
        assert "notes" in result

    async def test_explicit_dates_override_days(self, store):
        seen = {}

        def reader(event_types, start, end):
            seen.update(start=start, end=end)
            return self._completed_wmm()

        tools = MCPTools(store, history_reader=reader)
        result = await tools.get_wmm_history(start_date="2026-10-01", end_date="2026-10-03")
        assert result["date_range"] == {
            "start": "2026-10-01T00:00:00+00:00", "end": "2026-10-03T23:59:59.999999+00:00",
        }
        assert seen["end"] == datetime(2026, 10, 3, 23, 59, 59, 999999, tzinfo=timezone.utc)
        assert result["completed_count"] == 1

    async def test_bad_date_is_an_error_object(self, store):
        tools = MCPTools(store, history_reader=lambda *a: [])
        result = await tools.get_wmm_history(start_date="not a date")
        assert "error" in result

    async def test_group_by_is_passed_through(self, store):
        tools = MCPTools(store, history_reader=lambda *a: self._completed_wmm())
        result = await tools.get_wmm_history(group_by="week")
        assert "totals_by_week" in result and "totals_by_station" not in result

    async def test_missing_journal_folder_is_an_error_object(self):
        store = Mock(spec=DataStore)
        store.journal_path = None
        result = await MCPTools(store).get_wmm_history()
        assert "error" in result

    async def test_reads_real_files_through_the_raw_journal_scanner(self, store, tmp_path):
        path = tmp_path / "Journal.2026-09-30T000000.01.log"
        path.write_text("\n".join(json.dumps(e) for e in self._completed_wmm()) + "\n",
                        encoding="utf-8")
        result = await MCPTools(store).get_wmm_history(start_date="2026-10-01",
                                                      end_date="2026-10-03")
        assert result["completed_count"] == 1
        assert result["missions"][0]["station"] == "Burkin Orbital"
