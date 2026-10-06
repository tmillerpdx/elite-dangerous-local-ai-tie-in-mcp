"""
Tests for raw journal access: the history search and the live file reader.

Both read files directly and never touch the in-memory event store, so a
history question cannot disturb the server's idea of where the commander is.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.elite_mcp.mcp_tools import MCPTools
from src.journal.events import EventProcessor
from src.utils.data_store import DataStore
from src.utils.raw_journal import (
    extract_field,
    list_live_files,
    read_live_file,
    search_journal_history,
)


def _write(directory, name, events):
    path = Path(directory) / name
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def _stats(timestamp, wealth):
    return {"timestamp": timestamp, "event": "Statistics",
            "Bank_Account": {"Current_Wealth": wealth, "Owned_Ship_Count": 3}}


@pytest.fixture
def journals(tmp_path):
    """Three sessions. Filenames are local time; event timestamps are UTC."""
    _write(tmp_path, "Journal.2026-08-14T130000.01.log", [
        {"timestamp": "2026-08-14T20:05:40Z", "event": "LoadGame", "Credits": 100},
        _stats("2026-08-14T20:05:48Z", 15164381),
        {"timestamp": "2026-08-14T20:30:00Z", "event": "FSDJump", "StarSystem": "Sol"},
    ])
    _write(tmp_path, "Journal.2026-09-01T100000.01.log", [
        _stats("2026-09-01T17:00:05Z", 1696774441),
        {"timestamp": "2026-09-01T17:20:00Z", "event": "MarketSell", "Type": "wine",
         "Count": 700, "TotalSale": 195000000},
        {"timestamp": "2026-09-01T17:40:00Z", "event": "FSDJump", "StarSystem": "Mizar"},
    ])
    _write(tmp_path, "Journal.2026-09-30T142736.01.log", [
        _stats("2026-09-30T21:29:46Z", 7026699865),
        {"timestamp": "2026-09-30T22:45:10Z", "event": "CarrierJump", "StarSystem": "Hollatja"},
    ])
    return tmp_path


class TestExtractField:
    def test_nested_path(self):
        event = {"Bank_Account": {"Current_Wealth": 7}, "Modules": [{"Item": "a"}, {"Item": "b"}]}
        assert extract_field(event, "Bank_Account.Current_Wealth") == 7
        assert extract_field(event, "Modules.1.Item") == "b"

    def test_missing_path_is_none(self):
        assert extract_field({"a": {"b": 1}}, "a.c") is None
        assert extract_field({"a": [1]}, "a.5") is None
        assert extract_field({"a": 1}, "a.b.c") is None


class TestSearchJournalHistory:
    def test_returns_the_full_original_event(self, journals):
        result = search_journal_history(journals, event_types=["Statistics"])
        assert result["matched_count"] == 3
        newest = result["events"][0]
        assert newest["timestamp"] == "2026-09-30T21:29:46Z"
        assert newest["Bank_Account"]["Current_Wealth"] == 7026699865
        assert newest["Bank_Account"]["Owned_Ship_Count"] == 3

    def test_fields_projection_keeps_responses_small(self, journals):
        result = search_journal_history(
            journals, event_types=["Statistics"], fields=["Bank_Account.Current_Wealth"],
            sort_order="asc")
        assert result["events"] == [
            {"timestamp": "2026-08-14T20:05:48Z", "event": "Statistics",
             "Bank_Account.Current_Wealth": 15164381},
            {"timestamp": "2026-09-01T17:00:05Z", "event": "Statistics",
             "Bank_Account.Current_Wealth": 1696774441},
            {"timestamp": "2026-09-30T21:29:46Z", "event": "Statistics",
             "Bank_Account.Current_Wealth": 7026699865},
        ]

    def test_date_range_filters_by_event_time(self, journals):
        result = search_journal_history(
            journals, start_date="2026-09-01", end_date="2026-09-02", sort_order="asc")
        assert [e["event"] for e in result["events"]] == ["Statistics", "MarketSell", "FSDJump"]
        # The August session could still have been running on 1 September,
        # so its file is read too; the one from 30 September is skipped.
        assert result["files_scanned"] == 2

    def test_event_type_match_is_case_insensitive(self, journals):
        result = search_journal_history(journals, event_types=["fsdjump", "CARRIERJUMP"])
        assert result["matched_count"] == 3

    def test_contains_text(self, journals):
        result = search_journal_history(journals, contains_text="mizar")
        assert [e["StarSystem"] for e in result["events"]] == ["Mizar"]

    def test_limit_and_truncation(self, journals):
        result = search_journal_history(journals, limit=2)
        assert result["returned_count"] == 2
        assert result["matched_count"] == 8
        assert result["truncated"] is True
        # Newest first by default.
        assert result["events"][0]["event"] == "CarrierJump"

    def test_oldest_first(self, journals):
        result = search_journal_history(journals, limit=1, sort_order="asc")
        assert result["events"][0]["event"] == "LoadGame"

    def test_size_cap_truncates_and_says_how_to_narrow(self, journals):
        result = search_journal_history(journals, max_chars=200)
        assert result["truncated"] is True
        assert 1 <= result["returned_count"] < 8
        assert "fields" in result["note"]

    def test_bad_date_returns_error_object(self, journals):
        result = search_journal_history(journals, start_date="not a date at all")
        assert "error" in result

    def test_bad_sort_returns_error_object(self, journals):
        assert "error" in search_journal_history(journals, sort_order="sideways")

    def test_missing_folder_returns_error_object(self, tmp_path):
        assert "error" in search_journal_history(tmp_path / "nope")

    def test_broken_lines_are_skipped(self, journals):
        with open(journals / "Journal.2026-09-30T142736.01.log", "a", encoding="utf-8") as f:
            f.write('{"timestamp":"2026-09-30T23:00:00Z","event":"Docked","Sta')
        result = search_journal_history(journals, event_types=["Docked"])
        assert result["matched_count"] == 0

    def test_search_does_not_touch_the_event_store(self, journals):
        store = DataStore(journal_path=journals)
        store.store_event(EventProcessor().process_event(
            {"timestamp": "2026-10-01T23:45:35Z", "event": "Docked",
             "StarSystem": "Hydrae Sector QI-T b3-3", "StationName": "Ronis Mining Exploration"}))
        before = store.get_statistics()["total_events"]
        search_journal_history(journals, start_date="2026-08-01", end_date="2026-08-31")
        assert store.get_statistics()["total_events"] == before
        assert store.get_game_state().current_system == "Hydrae Sector QI-T b3-3"


class TestLiveFiles:
    @pytest.fixture
    def folder(self, tmp_path):
        (tmp_path / "Cargo.json").write_text(json.dumps({
            "timestamp": "2026-10-01T23:45:00Z", "event": "Cargo", "Vessel": "Ship", "Count": 12,
            "Inventory": [{"Name": "drones", "Count": 12}]}), encoding="utf-8")
        (tmp_path / "Market.json").write_text(json.dumps({
            "timestamp": "2026-10-01T23:46:00Z", "event": "Market", "StationName": "Ronis",
            "Items": [
                {"Name_Localised": "Tritium", "BuyPrice": 50319, "Stock": 32386},
                {"Name_Localised": "Wine", "BuyPrice": 260, "Stock": 5000},
                {"Name_Localised": "Gold", "BuyPrice": 9401, "Stock": 12},
            ]}), encoding="utf-8")
        (tmp_path / "Journal.2026-10-01T151701.01.log").write_text("{}\n", encoding="utf-8")
        (tmp_path / "notes.txt").write_text("private", encoding="utf-8")
        return tmp_path

    def test_list_names_only_json_state_files(self, folder):
        listing = list_live_files(folder)
        assert [f["filename"] for f in listing["files"]] == ["Cargo.json", "Market.json"]
        assert all("modified_at" in f and "size_bytes" in f for f in listing["files"])

    def test_read_by_name_with_or_without_extension(self, folder):
        assert read_live_file(folder, "Cargo.json")["data"]["Count"] == 12
        assert read_live_file(folder, "cargo")["filename"] == "Cargo.json"

    def test_reports_age(self, folder):
        old = time.time() - 3600
        os.utime(folder / "Cargo.json", (old, old))
        result = read_live_file(folder, "Cargo")
        assert 3500 <= result["age_seconds"] <= 3700
        assert result["modified_at"].endswith("+00:00")

    def test_item_filter_narrows_the_main_list(self, folder):
        result = read_live_file(folder, "Market", item_filter="trit")
        assert [i["Name_Localised"] for i in result["data"]["Items"]] == ["Tritium"]
        assert result["items_matched"] == 1
        assert result["items_total"] == 3
        assert result["data"]["StationName"] == "Ronis"

    def test_unknown_file_lists_what_exists(self, folder):
        result = read_live_file(folder, "Shipyard")
        assert "error" in result
        assert result["available_files"] == ["Cargo.json", "Market.json"]

    def test_cannot_escape_the_journal_folder(self, folder, tmp_path):
        outside = tmp_path.parent / "secret.json"
        outside.write_text('{"k": 1}', encoding="utf-8")
        for name in ("../secret.json", "..\\secret.json", str(outside), "notes.txt",
                     "Journal.2026-10-01T151701.01.log"):
            assert "error" in read_live_file(folder, name), name

    def test_half_written_file_returns_error_object(self, folder):
        (folder / "Status.json").write_text('{"timestamp":"2026', encoding="utf-8")
        result = read_live_file(folder, "Status")
        assert "error" in result


class TestRawAccessTools:
    def _tools(self, folder):
        store = Mock(spec=DataStore)
        store.journal_path = folder
        return MCPTools(store)

    async def test_search_tool_passes_through(self, journals):
        result = await self._tools(journals).search_journal_history(
            event_types=["Statistics"], fields=["Bank_Account.Current_Wealth"], sort_order="asc")
        assert result["returned_count"] == 3
        assert result["events"][-1]["Bank_Account.Current_Wealth"] == 7026699865

    async def test_empty_strings_select_defaults(self, journals):
        result = await self._tools(journals).search_journal_history(
            start_date="", end_date="", contains_text="", sort_order="")
        assert result["matched_count"] == 8

    async def test_live_file_tool_lists_when_no_name_given(self, tmp_path):
        (tmp_path / "Cargo.json").write_text('{"Count": 0}', encoding="utf-8")
        result = await self._tools(tmp_path).get_live_file("")
        assert [f["filename"] for f in result["files"]] == ["Cargo.json"]

    async def test_live_file_tool_reads_a_file(self, tmp_path):
        (tmp_path / "Cargo.json").write_text('{"Count": 4}', encoding="utf-8")
        result = await self._tools(tmp_path).get_live_file("Cargo")
        assert result["data"] == {"Count": 4}

    async def test_no_journal_path_returns_error_object(self):
        store = Mock(spec=DataStore)
        store.journal_path = None
        tools = MCPTools(store)
        assert "error" in await tools.search_journal_history()
        assert "error" in await tools.get_live_file("Cargo")
