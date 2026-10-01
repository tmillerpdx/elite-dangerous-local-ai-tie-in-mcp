"""
Tests for current-location tracking across carrier jumps and server restarts.

Reproduces a wrong-location report: the commander rode a fleet carrier from
Mizar to Hollatja, stayed docked, and the server kept answering "Mizar".

Three defects combined to cause it:
1. Journal filename timestamps were timezone-naive, so comparing them with a
   UTC-aware cutoff raised TypeError and the startup history load aborted.
2. History files were replayed newest-first, so older state overwrote newer.
3. The CarrierJump event had no game-state handler.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from src.journal.events import EventProcessor
from src.journal.parser import JournalParser
from src.server import EliteDangerousServer
from src.utils.data_store import DataStore, reset_data_store


def _ts(hours_ago):
    moment = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_journal(directory, hours_ago, events):
    """Write a journal named the way the game does: local time, ISO-like."""
    local_start = datetime.now() - timedelta(hours=hours_ago)
    path = Path(directory) / ("Journal.%s.01.log" % local_start.strftime("%Y-%m-%dT%H%M%S"))
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def _location(hours_ago, system, station=None):
    event = {"timestamp": _ts(hours_ago), "event": "Location", "StarSystem": system,
             "Docked": station is not None, "StarPos": [1.0, 2.0, 3.0]}
    if station:
        event["StationName"] = station
    return event


def _carrier_jump(hours_ago, system, station="W3W-T6Z"):
    return {"timestamp": _ts(hours_ago), "event": "CarrierJump", "Docked": True,
            "StationName": station, "StationType": "FleetCarrier", "StarSystem": system,
            "SystemAddress": 5370319620976, "StarPos": [10.0, 20.0, 30.0], "Body": system}


class TestFilenameTimestamp:
    def test_iso_filename_timestamp_is_timezone_aware(self, tmp_path):
        parser = JournalParser(str(tmp_path))
        result = parser._extract_timestamp_from_filename(Path("Journal.2026-09-30T142736.01.log"))
        assert result.tzinfo is not None
        # Must be comparable with a UTC-aware datetime without raising.
        assert result < datetime.now(timezone.utc) + timedelta(days=36500)

    def test_filename_time_is_interpreted_as_local_time(self, tmp_path):
        parser = JournalParser(str(tmp_path))
        result = parser._extract_timestamp_from_filename(Path("Journal.2026-09-30T142736.01.log"))
        expected = datetime(2026, 9, 30, 14, 27, 36).astimezone(timezone.utc)
        assert result == expected

    def test_legacy_filename_timestamp_is_timezone_aware(self, tmp_path):
        parser = JournalParser(str(tmp_path))
        result = parser._extract_timestamp_from_filename(Path("Journal.20240115090000.01.log"))
        assert result.tzinfo is not None

    def test_unparseable_filename_falls_back_to_aware_epoch(self, tmp_path):
        parser = JournalParser(str(tmp_path))
        result = parser._extract_timestamp_from_filename(Path("Journal.garbage.log"))
        assert result == datetime.fromtimestamp(0, timezone.utc)


class TestCarrierJumpState:
    def test_carrier_jump_updates_current_system(self):
        store = DataStore()
        processor = EventProcessor()
        store.store_event(processor.process_event(_location(3, "Mizar", "W3W-T6Z")))
        store.store_event(processor.process_event(_carrier_jump(2, "Hollatja")))

        state = store.get_game_state()
        assert state.current_system == "Hollatja"
        assert state.current_station == "W3W-T6Z"
        assert state.current_body == "Hollatja"
        assert state.docked is True


class TestStartupHistoryLoad:
    def setup_method(self):
        reset_data_store()

    def teardown_method(self):
        reset_data_store()

    def _server(self, journal_dir):
        with patch("src.server.EliteConfig") as mock_config:
            config = Mock()
            config.journal_path = Path(journal_dir)
            mock_config.return_value = config
            return EliteDangerousServer()

    async def test_history_load_does_not_abort_on_iso_filenames(self, tmp_path):
        _write_journal(tmp_path, 3, [_location(3, "Sol")])
        _write_journal(tmp_path, 1, [{"timestamp": _ts(1), "event": "Fileheader"}])
        server = self._server(tmp_path)

        await server.load_historical_data(hours_back=24)

        assert server.data_store.get_game_state().current_system == "Sol"

    async def test_history_is_replayed_oldest_first(self, tmp_path):
        _write_journal(tmp_path, 5, [_location(5, "Sol")])
        _write_journal(tmp_path, 3, [_location(3, "Mizar"), _carrier_jump(2.5, "Hollatja")])
        _write_journal(tmp_path, 1, [{"timestamp": _ts(1), "event": "Fileheader"}])
        server = self._server(tmp_path)

        await server.load_historical_data(hours_back=24)

        assert server.data_store.get_game_state().current_system == "Hollatja"

    async def test_latest_journal_is_left_for_the_monitor(self, tmp_path):
        # The journal monitor replays the newest file itself. Loading it here
        # as well would store every one of its events twice.
        _write_journal(tmp_path, 3, [_location(3, "Sol")])
        _write_journal(tmp_path, 1, [{"timestamp": _ts(1), "event": "Music", "MusicTrack": "NoTrack"}])
        server = self._server(tmp_path)

        await server.load_historical_data(hours_back=24)

        assert server.data_store.get_events_by_type("Music", limit=10) == []
        assert len(server.data_store.get_events_by_type("Location", limit=10)) == 1

    async def test_looks_further_back_when_window_has_no_location(self, tmp_path):
        # Last real session was five days ago; today's file is only a
        # launch-and-quit. The commander is still where they were left.
        _write_journal(tmp_path, 24 * 9, [_location(24 * 9, "Sol")])
        _write_journal(tmp_path, 24 * 5, [_location(24 * 5, "Mizar"), _carrier_jump(24 * 5 - 1, "Hollatja")])
        _write_journal(tmp_path, 1, [{"timestamp": _ts(1), "event": "Shutdown"}])
        server = self._server(tmp_path)

        await server.load_historical_data(hours_back=24)

        assert server.data_store.get_game_state().current_system == "Hollatja"

    async def test_lookback_is_skipped_when_latest_journal_has_location(self, tmp_path):
        _write_journal(tmp_path, 24 * 5, [_location(24 * 5, "Sol")])
        _write_journal(tmp_path, 1, [_location(1, "Hollatja")])
        server = self._server(tmp_path)

        await server.load_historical_data(hours_back=24)

        # The old file is outside the window and not needed for location.
        assert server.data_store.get_events_by_type("Location", limit=10) == []
