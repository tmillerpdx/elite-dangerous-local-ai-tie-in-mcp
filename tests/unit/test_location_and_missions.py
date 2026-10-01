"""
Tests for two defects found while verifying live journal tailing.

1. get_mission_summary crashed with "unsupported operand type(s) for +=:
   'int' and 'NoneType'" as soon as it had a donation mission to count,
   because those carry no Reward.
2. location_timestamp came from the last Location event only, so after a jump
   and a docking it still showed the login time and looked stale.
"""

from datetime import datetime, timedelta, timezone

from src.elite_mcp.mcp_tools import MCPTools
from src.journal.events import EventProcessor
from src.utils.data_store import DataStore

PROCESSOR = EventProcessor()


def _ts(minutes_ago):
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _tools(*raw_events):
    store = DataStore()
    for raw in raw_events:
        store.store_event(PROCESSOR.process_event(raw))
    return MCPTools(store)


def _donation(minutes_ago, mission_id):
    accepted = {"timestamp": _ts(minutes_ago + 1), "event": "MissionAccepted",
                "Faction": "Paemara Order", "Name": "Mission_AltruismCredits_CivilUnrest",
                "LocalisedName": "Provide 1,000,000 Cr to Tackle Civil Unrest",
                "Donation": "1000000", "MissionID": mission_id}
    completed = {"timestamp": _ts(minutes_ago), "event": "MissionCompleted",
                 "Faction": "Paemara Order", "Name": "Mission_AltruismCredits_CivilUnrest_name",
                 "MissionID": mission_id, "Donation": "1000000", "Donated": 1000000}
    return accepted, completed


class TestMissionSummary:
    async def test_donation_missions_do_not_crash_the_summary(self):
        events = []
        for index in range(8):
            events.extend(_donation(10 + index * 2, 1067443002 + index))
        result = await _tools(*events).get_activity_summary("missions", 6)
        assert "error" not in result
        assert result["missions_accepted"] == 8
        assert result["missions_completed"] == 8
        assert result["total_rewards"] == 0
        assert result["total_donated"] == 8000000
        assert result["active_missions"] == []

    async def test_paid_missions_still_sum_rewards(self):
        paid = {"timestamp": _ts(5), "event": "MissionCompleted", "Faction": "LHS 3447 Cartel",
                "Name": "Mission_Courier_name", "MissionID": 939142956, "Reward": 30997}
        accepted, donated = _donation(10, 1)
        result = await _tools(accepted, donated, paid).get_activity_summary("missions", 6)
        assert result["missions_completed"] == 2
        assert result["total_rewards"] == 30997
        assert result["total_donated"] == 1000000


class TestLocationTimestamp:
    async def test_timestamp_follows_the_newest_placing_event(self):
        location = {"timestamp": _ts(60), "event": "Location", "StarSystem": "Hollatja",
                    "StationName": "W3W-T6Z", "Docked": True, "StarPos": [1.0, 2.0, 3.0]}
        jump = {"timestamp": _ts(20), "event": "FSDJump", "StarSystem": "Paemara",
                "StarPos": [100.5, 69.0, 84.25], "Population": 5000,
                "SystemAllegiance": "Independent", "SystemEconomy_Localised": "Industrial",
                "SystemGovernment_Localised": "Theocracy", "SystemSecurity_Localised": "Medium"}
        docked = {"timestamp": _ts(15), "event": "Docked", "StarSystem": "Paemara",
                  "StationName": "Rukavishnikov Terminal"}
        result = await _tools(location, jump, docked).get_current_location()

        assert result["current_system"] == "Paemara"
        assert result["current_station"] == "Rukavishnikov Terminal"
        assert result["location_event"] == "Docked"
        assert result["location_timestamp"].startswith(docked["timestamp"][:19])
        # System details come from the jump, which describes the new system.
        assert result["population"] == 5000
        assert result["allegiance"] == "Independent"
        assert result["economy"] == "Industrial"
        assert result["government"] == "Theocracy"
        assert result["security"] == "Medium"

    async def test_carrier_jump_counts_as_a_placing_event(self):
        location = {"timestamp": _ts(60), "event": "Location", "StarSystem": "Mizar"}
        carrier = {"timestamp": _ts(30), "event": "CarrierJump", "StarSystem": "Hollatja",
                   "StationName": "W3W-T6Z", "Docked": True}
        result = await _tools(location, carrier).get_current_location()
        assert result["location_event"] == "CarrierJump"
        assert result["location_timestamp"].startswith(carrier["timestamp"][:19])

    async def test_no_placing_events_gives_no_timestamp(self):
        result = await _tools().get_current_location()
        assert "location_timestamp" not in result
        assert result["current_system"] == "Unknown"
