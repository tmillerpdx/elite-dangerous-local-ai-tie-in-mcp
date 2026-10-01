"""
Tests for game-state defects found while verifying location tracking.

1. Status flags were read only from key_data, which never carries them, so
   every Status event reset docked/landed/supercruise to False.
2. Status flag bit positions above bit 4 did not match the journal manual.
3. Coordinates were read from StarPosX/Y/Z, which the journal never writes;
   it writes "StarPos": [x, y, z].
4. Docked did not update the current system.
"""

from datetime import datetime, timezone

from src.journal.events import EventProcessor
from src.utils.data_store import DataStore

NOW = "2026-10-01T00:00:00Z"

# Real value from a commander docked on a fleet carrier:
# Docked | LandingGear | ShieldsUp | FsdMassLocked | InMainShip
DOCKED_ON_CARRIER_FLAGS = 16842765


def _store(*events):
    store = DataStore()
    processor = EventProcessor()
    for event in events:
        store.store_event(processor.process_event(event))
    return store


def _status(flags, flags2=None):
    event = {"timestamp": NOW, "event": "Status", "Flags": flags}
    if flags2 is not None:
        event["Flags2"] = flags2
    return event


class TestStatusFlags:
    def test_docked_flag_is_read_from_the_status_event(self):
        state = _store(_status(DOCKED_ON_CARRIER_FLAGS)).get_game_state()
        assert state.docked is True
        assert state.in_main_ship is True
        assert state.supercruise is False

    def test_high_bits_follow_the_journal_manual(self):
        # Bit 6 is "hardpoints deployed" and bit 5 is "flight assist off".
        # Neither means the FSD is charging or cooling down.
        state = _store(_status(0x01000000 | 0x20 | 0x40)).get_game_state()
        assert state.fsd_charging is False
        assert state.fsd_cooldown is False
        assert state.in_main_ship is True

    def test_fsd_low_fuel_and_srv_bits(self):
        flags = 0x00020000 | 0x00080000 | 0x04000000
        state = _store(_status(flags)).get_game_state()
        assert state.fsd_charging is True
        assert state.low_fuel is True
        assert state.in_srv is True
        assert state.in_main_ship is False

    def test_empty_status_does_not_clear_journal_state(self):
        # With the game closed Status.json has no flags. That must not undo
        # what the journal established.
        docked = {"timestamp": NOW, "event": "Docked", "StationName": "W3W-T6Z",
                  "StarSystem": "Hollatja"}
        for empty in ({"timestamp": NOW, "event": "Status"}, _status(0), _status(0, 0)):
            state = _store(docked, empty).get_game_state()
            assert state.docked is True
            assert state.current_station == "W3W-T6Z"

    def test_on_foot_status_is_applied(self):
        # On foot, Flags is 0 and Flags2 carries the state.
        docked = {"timestamp": NOW, "event": "Docked", "StationName": "W3W-T6Z",
                  "StarSystem": "Hollatja"}
        state = _store(docked, _status(0, 1)).get_game_state()
        assert state.docked is False


class TestCoordinates:
    def test_fsd_jump_sets_coordinates_from_star_pos(self):
        jump = {"timestamp": NOW, "event": "FSDJump", "StarSystem": "Mizar",
                "StarPos": [-36.25, 72.8125, -15.46875]}
        state = _store(jump).get_game_state()
        assert state.coordinates == {"x": -36.25, "y": 72.8125, "z": -15.46875}

    def test_location_and_carrier_jump_set_coordinates(self):
        location = {"timestamp": NOW, "event": "Location", "StarSystem": "Mizar",
                    "StarPos": [1.0, 2.0, 3.0]}
        carrier = {"timestamp": NOW, "event": "CarrierJump", "StarSystem": "Hollatja",
                   "StarPos": [10.0, 20.0, 30.0], "Docked": True, "StationName": "W3W-T6Z"}
        assert _store(location).get_game_state().coordinates == {"x": 1.0, "y": 2.0, "z": 3.0}
        assert _store(location, carrier).get_game_state().coordinates == {
            "x": 10.0, "y": 20.0, "z": 30.0}

    def test_jump_without_position_leaves_coordinates_unset(self):
        jump = {"timestamp": NOW, "event": "FSDJump", "StarSystem": "Mizar"}
        assert _store(jump).get_game_state().coordinates is None


class TestHistoricalQueryLeavesStateAlone:
    """A historical search once moved the commander back to where they were
    six weeks earlier, because old events were replayed through live state."""

    def _journal(self, directory, name, events):
        import json
        path = directory / name
        path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        return path

    def test_historical_query_does_not_move_the_commander(self, tmp_path):
        self._journal(tmp_path, "Journal.2026-08-18T060000.01.log", [
            {"timestamp": "2026-08-18T06:24:45Z", "event": "Location",
             "StarSystem": "Arietis Sector XZ-P b5-0", "StationName": "Haffkine Starport",
             "Docked": True, "StarPos": [-59.9, -100.8, -101.9]},
            {"timestamp": "2026-08-18T06:30:00Z", "event": "Statistics",
             "Bank_Account": {"Current_Wealth": 229334704}},
        ])
        store = DataStore(journal_path=tmp_path)
        processor = EventProcessor()
        store.store_event(processor.process_event(
            {"timestamp": "2026-09-30T22:45:10Z", "event": "CarrierJump", "StarSystem": "Hollatja",
             "StationName": "W3W-T6Z", "Docked": True, "StarPos": [25.3, -32.1, 35.8]}))

        found = store.query_historical_events(start_date="2026-08-14", end_date="2026-08-19")

        assert found["total_count"] == 2
        state = store.get_game_state()
        assert state.current_system == "Hollatja"
        assert state.current_station == "W3W-T6Z"
        assert state.coordinates == {"x": 25.3, "y": -32.1, "z": 35.8}

    def test_store_event_can_skip_state_updates(self):
        store = DataStore()
        processor = EventProcessor()
        event = processor.process_event(
            {"timestamp": NOW, "event": "FSDJump", "StarSystem": "Sol", "StarPos": [0, 0, 0]})
        store.store_event(event, update_state=False)
        assert store.get_game_state().current_system is None
        assert len(store.get_events_by_type("FSDJump", limit=5)) == 1


class TestDockedSetsSystem:
    def test_docked_updates_a_stale_system(self):
        location = {"timestamp": NOW, "event": "Location", "StarSystem": "Mizar",
                    "StarPos": [1.0, 2.0, 3.0]}
        docked = {"timestamp": NOW, "event": "Docked", "StationName": "W3W-T6Z",
                  "StarSystem": "Hollatja"}
        state = _store(location, docked).get_game_state()
        assert state.current_system == "Hollatja"
        # The old position belongs to Mizar and must not be kept.
        assert state.coordinates is None

    def test_docked_in_the_same_system_keeps_coordinates(self):
        location = {"timestamp": NOW, "event": "Location", "StarSystem": "Mizar",
                    "StarPos": [1.0, 2.0, 3.0]}
        docked = {"timestamp": NOW, "event": "Docked", "StationName": "Somewhere",
                  "StarSystem": "Mizar"}
        state = _store(location, docked).get_game_state()
        assert state.coordinates == {"x": 1.0, "y": 2.0, "z": 3.0}
