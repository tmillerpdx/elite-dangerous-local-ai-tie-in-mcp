"""
Tests for live journal tailing.

Reproduces a report that the server went blind to a game session started after
the server: the commander jumped, docked and completed eight missions, and the
server still reported the previous system with zero missions.

Cause: the game keeps its journal open for the whole session and only flushes.
On Windows a file written that way produces no change notifications until the
writer closes it, so a file-watcher alone never sees the new lines. The new
journal file was detected, found empty, and never read again.

Fix: poll the newest journals on a timer and before each tool call, reading
only complete lines.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.journal.monitor import JournalMonitor
from src.journal.parser import JournalParser
from src.server import EliteDangerousServer
from src.utils.data_store import reset_data_store


def _line(event, **fields):
    payload = {"timestamp": "2026-10-01T22:39:00Z", "event": event}
    payload.update(fields)
    return json.dumps(payload) + "\n"


class _Collector:
    """Async callback that records every journal entry delivered."""

    def __init__(self):
        self.entries = []

    async def collect(self, data, event_type):
        if event_type == "journal_entries":
            self.entries.extend(data)

    def names(self):
        return [entry["event"] for entry in self.entries]


@pytest.fixture
def journal_dir(tmp_path):
    (tmp_path / "Journal.2026-10-01T060000.01.log").write_text(
        _line("Fileheader") + _line("Location", StarSystem="Hollatja"), encoding="utf-8")
    return tmp_path


class TestIncrementalReadTakesCompleteLinesOnly:
    def test_partial_trailing_line_is_left_for_the_next_read(self, tmp_path):
        path = tmp_path / "Journal.2026-10-01T151701.01.log"
        whole = _line("LoadGame")
        partial = _line("FSDJump", StarSystem="Paemara")
        path.write_bytes((whole + partial[:25]).encode("utf-8"))
        parser = JournalParser(str(tmp_path))

        entries, position = parser.read_journal_file_incremental(path, 0)
        assert [e["event"] for e in entries] == ["LoadGame"]
        assert position == len(whole.encode("utf-8"))

        path.write_bytes((whole + partial).encode("utf-8"))
        entries, position = parser.read_journal_file_incremental(path, position)
        assert [e["event"] for e in entries] == ["FSDJump"]
        assert entries[0]["StarSystem"] == "Paemara"
        assert position == len((whole + partial).encode("utf-8"))

    def test_nothing_new_returns_same_position(self, tmp_path):
        path = tmp_path / "Journal.2026-10-01T151701.01.log"
        path.write_text(_line("LoadGame"), encoding="utf-8")
        parser = JournalParser(str(tmp_path))
        _, position = parser.read_journal_file_incremental(path, 0)
        assert parser.read_journal_file_incremental(path, position) == ([], position)

    def test_position_beyond_file_end_restarts_from_the_top(self, tmp_path):
        path = tmp_path / "Journal.2026-10-01T151701.01.log"
        path.write_text(_line("LoadGame"), encoding="utf-8")
        parser = JournalParser(str(tmp_path))
        entries, _ = parser.read_journal_file_incremental(path, 99999)
        assert [e["event"] for e in entries] == ["LoadGame"]

    def test_non_ascii_text_keeps_byte_positions_right(self, tmp_path):
        path = tmp_path / "Journal.2026-10-01T151701.01.log"
        first = _line("ReceiveText", Message="caf\u00e9 \u2013 d\u00e9j\u00e0")
        second = _line("Docked", StationName="Rukavishnikov Terminal")
        path.write_bytes(first.encode("utf-8"))
        parser = JournalParser(str(tmp_path))
        _, position = parser.read_journal_file_incremental(path, 0)
        with open(path, "ab") as handle:
            handle.write(second.encode("utf-8"))
        entries, _ = parser.read_journal_file_incremental(path, position)
        assert [e["event"] for e in entries] == ["Docked"]


class TestPolling:
    async def test_lines_written_through_an_open_handle_are_picked_up(self, journal_dir):
        collector = _Collector()
        monitor = JournalMonitor(journal_dir, collector.collect, poll_interval=0)
        assert await monitor.start_monitoring()
        try:
            collector.entries.clear()
            newest = journal_dir / "Journal.2026-10-01T060000.01.log"
            # The game never closes its journal mid-session.
            with open(newest, "a", encoding="utf-8") as game:
                game.write(_line("FSDJump", StarSystem="Paemara"))
                game.flush()
                await monitor.poll_once()
                assert collector.names() == ["FSDJump"]

                game.write(_line("Docked", StationName="Rukavishnikov Terminal"))
                game.flush()
                await monitor.poll_once()
                assert collector.names() == ["FSDJump", "Docked"]
        finally:
            await monitor.stop_monitoring()

    async def test_journal_created_after_startup_is_followed(self, journal_dir):
        collector = _Collector()
        monitor = JournalMonitor(journal_dir, collector.collect, poll_interval=0)
        assert await monitor.start_monitoring()
        try:
            collector.entries.clear()
            new_file = journal_dir / "Journal.2026-10-01T151701.01.log"
            with open(new_file, "a", encoding="utf-8") as game:
                # Created empty, exactly as the game does, then written to.
                await monitor.poll_once()
                game.write(_line("LoadGame") + _line("Location", StarSystem="Hollatja"))
                game.flush()
                await monitor.poll_once()
                game.write(_line("FSDJump", StarSystem="Paemara"))
                game.flush()
                await monitor.poll_once()
            assert collector.names() == ["LoadGame", "Location", "FSDJump"]
        finally:
            await monitor.stop_monitoring()

    async def test_creation_notice_after_a_poll_does_not_deliver_twice(self, journal_dir):
        collector = _Collector()
        monitor = JournalMonitor(journal_dir, collector.collect, poll_interval=0)
        assert await monitor.start_monitoring()
        try:
            collector.entries.clear()
            new_file = journal_dir / "Journal.2026-10-01T151701.01.log"
            new_file.write_text(_line("LoadGame"), encoding="utf-8")
            await monitor.poll_once()
            # The file-watcher's "created" notice can arrive after the poll.
            await monitor.event_handler._handle_journal_creation(new_file)
            await monitor.poll_once()
            assert collector.names().count("LoadGame") == 1
        finally:
            await monitor.stop_monitoring()

    async def test_old_journals_are_not_replayed(self, journal_dir):
        (journal_dir / "Journal.2026-09-30T142736.01.log").write_text(
            _line("Location", StarSystem="Mizar"), encoding="utf-8")
        collector = _Collector()
        monitor = JournalMonitor(journal_dir, collector.collect, poll_interval=0)
        assert await monitor.start_monitoring()
        try:
            collector.entries.clear()
            await monitor.poll_once()
            assert collector.entries == []
        finally:
            await monitor.stop_monitoring()

    async def test_background_loop_polls_without_being_asked(self, journal_dir):
        collector = _Collector()
        monitor = JournalMonitor(journal_dir, collector.collect, poll_interval=0.05)
        assert await monitor.start_monitoring()
        try:
            collector.entries.clear()
            newest = journal_dir / "Journal.2026-10-01T060000.01.log"
            with open(newest, "a", encoding="utf-8") as game:
                game.write(_line("FSDJump", StarSystem="Paemara"))
                game.flush()
                for _ in range(40):
                    if collector.entries:
                        break
                    await asyncio.sleep(0.05)
                assert collector.names() == ["FSDJump"]
        finally:
            await monitor.stop_monitoring()

    async def test_poll_once_before_start_is_harmless(self, journal_dir):
        monitor = JournalMonitor(journal_dir, _Collector().collect, poll_interval=0)
        assert await monitor.poll_once() == 0

    async def test_stop_cancels_the_background_loop(self, journal_dir):
        monitor = JournalMonitor(journal_dir, _Collector().collect, poll_interval=0.05)
        assert await monitor.start_monitoring()
        task = monitor._poll_task
        assert task is not None and not task.done()
        await monitor.stop_monitoring()
        assert task.done()


class TestToolsCatchUpBeforeAnswering:
    def setup_method(self):
        reset_data_store()

    def teardown_method(self):
        reset_data_store()

    def _server(self, journal_dir):
        with patch("src.server.EliteConfig") as mock_config:
            config = Mock()
            config.journal_path = Path(journal_dir)
            config.file_check_interval = 1.0
            mock_config.return_value = config
            server = EliteDangerousServer()
        server.setup_basic_mcp_handlers()
        server.setup_core_mcp_handlers()
        return server

    async def test_every_tool_call_polls_the_journal_first(self, journal_dir):
        server = self._server(journal_dir)
        server.journal_monitor = Mock()
        server.journal_monitor.poll_once = AsyncMock(return_value=0)

        await server.app.call_tool("get_current_location", {})
        await server.app.call_tool("server_status", {})

        assert server.journal_monitor.poll_once.await_count == 2

    async def test_tool_call_works_when_monitoring_is_not_running(self, journal_dir):
        server = self._server(journal_dir)
        assert server.journal_monitor is None
        result = await server.app.call_tool("get_current_location", {})
        assert result is not None

    async def test_a_failing_poll_does_not_break_the_tool(self, journal_dir):
        server = self._server(journal_dir)
        server.journal_monitor = Mock()
        server.journal_monitor.poll_once = AsyncMock(side_effect=RuntimeError("disk gone"))
        result = await server.app.call_tool("get_current_location", {})
        assert result is not None

    async def test_tool_parameters_survive_the_wrapper(self, journal_dir):
        server = self._server(journal_dir)
        tools = {tool.name: tool for tool in await server.app.list_tools()}
        properties = tools["find_commodity_market"].inputSchema["properties"]
        assert {"commodity", "mode", "reference_system", "large_pad_only"} <= set(properties)
        assert tools["find_commodity_market"].description.strip().startswith("Find stations")

    async def test_end_to_end_location_follows_a_session_started_later(self, journal_dir):
        server = self._server(journal_dir)
        await server.start_journal_monitoring()
        try:
            new_file = journal_dir / "Journal.2026-10-01T151701.01.log"
            with open(new_file, "a", encoding="utf-8") as game:
                game.write(_line("LoadGame"))
                game.write(_line("FSDJump", StarSystem="Paemara", StarPos=[1.0, 2.0, 3.0]))
                game.write(_line("Docked", StationName="Rukavishnikov Terminal",
                                 StarSystem="Paemara"))
                game.flush()
                # No sleep and no file close: the tool call itself must catch up.
                location = await server.mcp_tools.get_current_location()
                assert location["current_system"] == "Hollatja"
                await server.app.call_tool("get_current_location", {})
                location = await server.mcp_tools.get_current_location()
            assert location["current_system"] == "Paemara"
            assert location["current_station"] == "Rukavishnikov Terminal"
        finally:
            await server.stop_journal_monitoring()


class TestSurvivesTheLoopHandOver:
    """The server starts monitoring on a setup loop, then the MCP framework
    runs its own loop and the setup loop never runs again. Background work
    left on the setup loop is dead; it has to move to the live loop."""

    def test_background_poll_moves_to_the_live_loop(self, journal_dir):
        collector = _Collector()
        monitor = JournalMonitor(journal_dir, collector.collect, poll_interval=0.05)
        setup_loop = asyncio.new_event_loop()
        try:
            assert setup_loop.run_until_complete(monitor.start_monitoring())
            stranded = monitor._poll_task
            collector.entries.clear()
            newest = journal_dir / "Journal.2026-10-01T060000.01.log"

            async def live():
                with open(newest, "a", encoding="utf-8") as game:
                    game.write(_line("FSDJump", StarSystem="Paemara"))
                    game.flush()
                    # Nothing arrives by itself: the poll task is on the dead loop.
                    await asyncio.sleep(0.3)
                    assert collector.entries == []

                    # The first tool call polls, and that re-homes the monitor.
                    await monitor.poll_once()
                    assert collector.names() == ["FSDJump"]
                    assert monitor._poll_task is not stranded
                    assert monitor._poll_task.get_loop() is asyncio.get_running_loop()
                    assert monitor.event_handler.event_loop is asyncio.get_running_loop()

                    # From here the background loop keeps up without being asked.
                    game.write(_line("Docked", StationName="Rukavishnikov Terminal"))
                    game.flush()
                    for _ in range(40):
                        if len(collector.entries) == 2:
                            break
                        await asyncio.sleep(0.05)
                    assert collector.names() == ["FSDJump", "Docked"]
                await monitor.stop_monitoring()

            asyncio.run(live())
        finally:
            # Let the abandoned loop process the cancellation of its stranded task.
            setup_loop.run_until_complete(asyncio.sleep(0.01))
            setup_loop.close()

    async def test_attach_is_a_no_op_on_the_loop_it_started_on(self, journal_dir):
        monitor = JournalMonitor(journal_dir, _Collector().collect, poll_interval=0.05)
        assert await monitor.start_monitoring()
        try:
            task = monitor._poll_task
            assert monitor.attach_to_running_loop() is False
            assert monitor._poll_task is task
        finally:
            await monitor.stop_monitoring()

    def test_stop_from_another_loop_does_not_raise(self, journal_dir):
        monitor = JournalMonitor(journal_dir, _Collector().collect, poll_interval=0.05)
        setup_loop = asyncio.new_event_loop()
        try:
            assert setup_loop.run_until_complete(monitor.start_monitoring())
            asyncio.run(monitor.stop_monitoring())
            assert monitor._poll_task is None
        finally:
            # Let the abandoned loop process the cancellation of its stranded task.
            setup_loop.run_until_complete(asyncio.sleep(0.01))
            setup_loop.close()
