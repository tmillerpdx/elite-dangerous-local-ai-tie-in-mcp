"""
Tests for the Trade Dangerous bridge client and the plan_trade_route tool.

Trade Dangerous runs as a separate program; the subprocess is replaced with a
fake runner, so no test needs it installed.
"""

import json
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.elite_mcp.mcp_tools import MCPTools
from src.utils.data_store import DataStore, GameState
from src.utils.spansh_client import laden_jump_range, ship_from_loadout
from src.utils.trade_dangerous import (
    TradeDangerousClient,
    build_run_args,
    shape_trade_route,
)


def _station(name, system, ls=120, pad="L", age=0.4):
    return {
        "name": name, "system_name": system, "ls_from_star": ls,
        "max_pad_size": pad, "planetary": "N", "fleet_carrier": "N",
        "data_age_days": age,
    }


def _route():
    kummer = _station("Kummer City", "Andere")
    lorrah = _station("Lorrah Dock", "G 224-46", ls=2400, pad="M", age=1.26)
    return {
        "stations": [kummer, lorrah],
        "hops": [{
            "source_station": kummer,
            "destination_station": lorrah,
            "cargo": {
                "lines": [{
                    "item_name": "Titanium", "quantity": 6, "buy_price": 800,
                    "sell_price": 1300, "profit_per_unit": 500, "total_cost": 4800,
                    "total_profit": 3000, "source_supply_units": 900,
                    "destination_demand_units": 4000,
                }],
                "units_loaded": 6, "total_cost": 4800, "total_profit": 3000,
                "unused_capacity": 2, "unspent_capital": 200,
            },
            "raw_profit": 3000,
            "jump_path": {
                "systems": [{"name": "Andere"}, {"name": "LHS 1"}, {"name": "G 224-46"}],
                "distance_ly": 15.234, "jumps": 2,
            },
        }],
        "total_raw_profit": 3000,
        "starting_credits": 5000,
        "ending_credits": 8000,
    }


def _bridge_output(routes=None, warnings=None):
    return json.dumps({"ok": True, "routes": [_route()] if routes is None else routes,
                       "warnings": warnings or []})


def _runner(stdout, seen=None, returncode=0, stderr=""):
    async def run(argv, env, timeout):
        if seen is not None:
            seen["argv"] = argv
            seen["env"] = env
        return returncode, stdout, stderr
    return run


def _client(stdout=None, seen=None, **kwargs):
    kwargs.setdefault("python_path", "C:/td/python.exe")
    kwargs.setdefault("data_dir", "C:/td/data")
    return TradeDangerousClient(
        runner=_runner(_bridge_output() if stdout is None else stdout, seen), **kwargs)


class TestBuildRunArgs:
    def test_required_arguments(self):
        args = build_run_args("Andere/Kummer City", "", 5000, 8, 8.56, 2, 0, 2.0, "", 1)
        assert args == [
            "run", "--from", "Andere/Kummer City", "--credits", "5000", "--capacity", "8",
            "--ly-per", "8.56", "--hops", "2", "--age", "2.0", "--routes", "1",
        ]

    def test_optional_arguments(self):
        args = build_run_args("Andere", "Sol", 5000, 8, 8.56, 3, 4, 0.0, "L", 2)
        assert args[args.index("--to") + 1] == "Sol"
        assert args[args.index("--jumps-per") + 1] == "4"
        assert args[args.index("--pad-size") + 1] == "L"
        assert "--age" not in args


class TestShapeTradeRoute:
    def test_shape(self):
        shaped = shape_trade_route(_route())
        assert shaped["total_profit"] == 3000
        assert shaped["starting_credits"] == 5000
        assert shaped["ending_credits"] == 8000
        hop = shaped["hops"][0]
        assert hop["from"] == "Andere/Kummer City"
        assert hop["to"] == "G 224-46/Lorrah Dock"
        assert hop["to_arrival_distance_ls"] == 2400
        assert hop["to_pad_size"] == "M"
        assert hop["profit"] == 3000
        assert hop["jumps"] == 2
        assert hop["distance_ly"] == 15.2
        assert hop["via_systems"] == ["LHS 1"]
        assert "oldest_data_age_days" not in hop
        assert hop["cargo"] == [{
            "commodity": "Titanium", "units": 6, "buy_price": 800, "sell_price": 1300,
            "profit_per_unit": 500, "profit": 3000, "supply": 900, "demand": 4000,
        }]

    def test_hop_without_jump_path(self):
        route = _route()
        route["hops"][0]["jump_path"] = None
        hop = shape_trade_route(route)["hops"][0]
        assert hop["jumps"] is None
        assert hop["via_systems"] == []


class TestTradeDangerousClient:
    async def test_runs_bridge_and_shapes_routes(self):
        seen = {}
        result = await _client(seen=seen).plan_trade_run(
            "Andere/Kummer City", "", 5000, 8, 8.56, 2, 0, 2.0, "", 1)
        assert seen["argv"][0] == "C:/td/python.exe"
        assert seen["argv"][1].endswith("td_bridge.py")
        assert seen["argv"][2:5] == ["run", "--from", "Andere/Kummer City"]
        assert seen["env"]["TD_DATA"] == "C:/td/data"
        assert Path(seen["env"]["TD_TMP"]) == Path("C:/td/tmp")
        assert result["origin"] == "Andere/Kummer City"
        assert result["route_count"] == 1
        assert result["routes"][0]["total_profit"] == 3000
        assert result["source"] == "Trade Dangerous (local price database)"

    async def test_reports_age_of_the_price_database(self, tmp_path):
        (tmp_path / "TradeDangerous.db").write_bytes(b"x")
        result = await _client(data_dir=str(tmp_path)).plan_trade_run(
            "Andere", "", 5000, 8, 8.56, 2, 0, 2.0, "", 1)
        assert result["database_age_days"] == 0.0
        assert result["database_updated_at"].endswith("+00:00")

    async def test_missing_database_file_gives_no_age(self):
        result = await _client().plan_trade_run("Andere", "", 5000, 8, 8.56, 2, 0, 2.0, "", 1)
        assert result["database_age_days"] is None

    async def test_not_configured_returns_error_object(self, monkeypatch):
        monkeypatch.delenv("ELITE_TD_PYTHON", raising=False)
        client = TradeDangerousClient(runner=_runner(_bridge_output()))
        result = await client.plan_trade_run("Andere", "", 5000, 8, 8.56, 2, 0, 2.0, "", 1)
        assert "ELITE_TD_PYTHON" in result["error"]

    async def test_reads_configuration_from_environment(self, monkeypatch):
        monkeypatch.setenv("ELITE_TD_PYTHON", "D:/venv/python.exe")
        monkeypatch.setenv("ELITE_TD_DATA", "D:/tddata")
        seen = {}
        client = TradeDangerousClient(runner=_runner(_bridge_output(), seen))
        await client.plan_trade_run("Andere", "", 5000, 8, 8.56, 2, 0, 2.0, "", 1)
        assert seen["argv"][0] == "D:/venv/python.exe"
        assert seen["env"]["TD_DATA"] == "D:/tddata"

    async def test_bridge_error_is_passed_through(self):
        client = _client(stdout=json.dumps({"ok": False, "error": "Unknown place: Andre"}))
        result = await client.plan_trade_run("Andre", "", 5000, 8, 8.56, 2, 0, 2.0, "", 1)
        assert result["error"] == "Trade Dangerous: Unknown place: Andre"

    async def test_unreadable_output_returns_error_object(self):
        client = TradeDangerousClient(
            python_path="C:/td/python.exe",
            runner=_runner("Traceback...", returncode=1, stderr="boom\nlast line"))
        result = await client.plan_trade_run("Andere", "", 5000, 8, 8.56, 2, 0, 2.0, "", 1)
        assert "last line" in result["error"]

    async def test_runner_failure_returns_error_object(self):
        async def run(argv, env, timeout):
            raise OSError("python.exe not found")

        client = TradeDangerousClient(python_path="C:/td/python.exe", runner=run)
        result = await client.plan_trade_run("Andere", "", 5000, 8, 8.56, 2, 0, 2.0, "", 1)
        assert "python.exe not found" in result["error"]

    async def test_partial_route_warning_becomes_a_note(self):
        warnings = [{"completed_hops": 1, "requested_hops": 3,
                     "phase": "expansion", "reason": "no_viable_continuation"}]
        client = _client(stdout=_bridge_output(warnings=warnings))
        result = await client.plan_trade_run("Andere", "", 5000, 8, 8.56, 3, 0, 2.0, "", 1)
        assert any("1 of 3" in note for note in result["notes"])


class TestLadenJumpRange:
    def test_full_cargo_shortens_the_range(self):
        ship = ship_from_loadout({
            "UnladenMass": 400.0, "FuelCapacity": {"Main": 32.0, "Reserve": 1.0},
            "Modules": [{"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size6_class5"}],
        })
        empty = laden_jump_range(ship, 0)
        laden = laden_jump_range(ship, 400)
        # (8 / 0.012) ** (1 / 2.6) * 1800 / (400 + 32) = 50.81
        assert empty == pytest.approx(50.81, abs=0.01)
        assert laden == pytest.approx(empty * 432 / 832, abs=0.01)


def _loadout():
    return {
        "event": "Loadout", "Ship": "anaconda", "UnladenMass": 400.0, "MaxJumpRange": 55.0,
        "CargoCapacity": 400, "FuelCapacity": {"Main": 32.0, "Reserve": 1.0},
        "Modules": [{"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size6_class5"}],
    }


class TestPlanTradeRouteTool:
    @pytest.fixture
    def store(self):
        store = Mock(spec=DataStore)
        store.get_game_state.return_value = GameState(
            current_system="Andere", current_station="Kummer City", docked=True,
            credits=2500000, last_updated=datetime.now(timezone.utc))
        store.get_events_by_type.return_value = [
            SimpleNamespace(raw_event=_loadout(), timestamp=datetime.now(timezone.utc))
        ]
        return store

    def _tools(self, store, seen):
        return MCPTools(store, trade_client=_client(seen=seen))

    async def test_defaults_come_from_the_journal(self, store):
        seen = {}
        result = await self._tools(store, seen).plan_trade_route()
        argv = seen["argv"]
        assert argv[argv.index("--from") + 1] == "Andere/Kummer City"
        assert argv[argv.index("--credits") + 1] == "2500000"
        assert argv[argv.index("--capacity") + 1] == "400"
        assert argv[argv.index("--ly-per") + 1] == "26.38"
        assert argv[argv.index("--pad-size") + 1] == "L"
        assert result["inputs"]["origin_source"] == "current_location"
        assert result["inputs"]["jump_range_source"] == "journal_loadout_laden"

    async def test_not_docked_starts_from_the_system(self, store):
        store.get_game_state.return_value = GameState(
            current_system="Andere", current_station=None, docked=False,
            credits=2500000, last_updated=datetime.now(timezone.utc))
        seen = {}
        await self._tools(store, seen).plan_trade_route()
        assert seen["argv"][seen["argv"].index("--from") + 1] == "Andere"

    async def test_explicit_arguments_win(self, store):
        seen = {}
        await self._tools(store, seen).plan_trade_route(
            origin="Sol/Abraham Lincoln", credits=1000, cargo_capacity=16,
            jump_range_ly=12.5, pad_size="m")
        argv = seen["argv"]
        assert argv[argv.index("--from") + 1] == "Sol/Abraham Lincoln"
        assert argv[argv.index("--credits") + 1] == "1000"
        assert argv[argv.index("--capacity") + 1] == "16"
        assert argv[argv.index("--ly-per") + 1] == "12.5"
        assert argv[argv.index("--pad-size") + 1] == "M"

    async def test_unknown_credits_returns_error_object(self, store):
        store.get_game_state.return_value = GameState(
            current_system="Andere", credits=0, last_updated=datetime.now(timezone.utc))
        result = await self._tools(store, {}).plan_trade_route()
        assert "credits" in result["error"]

    async def test_no_cargo_hold_returns_error_object(self, store):
        loadout = _loadout()
        loadout["CargoCapacity"] = 0
        store.get_events_by_type.return_value = [
            SimpleNamespace(raw_event=loadout, timestamp=datetime.now(timezone.utc))]
        result = await self._tools(store, {}).plan_trade_route()
        assert "cargo_capacity" in result["error"]

    async def test_bad_pad_size_returns_error_object(self, store):
        result = await self._tools(store, {}).plan_trade_route(pad_size="huge")
        assert "pad_size" in result["error"]

    async def test_unknown_location_returns_error_object(self, store):
        store.get_game_state.return_value = GameState(
            current_system=None, credits=5000, last_updated=datetime.now(timezone.utc))
        result = await self._tools(store, {}).plan_trade_route()
        assert "error" in result

    async def test_unknown_current_station_falls_back_to_the_system(self, store):
        # A fleet carrier the commander is docked on is not in the price database.
        calls = []

        async def run(argv, env, timeout):
            calls.append(argv[argv.index("--from") + 1])
            if len(calls) == 1:
                return 1, json.dumps({
                    "ok": False,
                    "error": "ERROR: --from: unknown station: 'Andere/Kummer City'"}), ""
            return 0, _bridge_output(), ""

        client = TradeDangerousClient(python_path="C:/td/python.exe", runner=run)
        result = await MCPTools(store, trade_client=client).plan_trade_route()
        assert calls == ["Andere/Kummer City", "Andere"]
        assert result["origin"] == "Andere"
        assert any("Kummer City" in note for note in result["notes"])

    async def test_unknown_explicit_origin_is_not_retried(self, store):
        calls = []

        async def run(argv, env, timeout):
            calls.append(1)
            return 1, json.dumps({"ok": False, "error": "ERROR: --from: unknown station: 'X/Y'"}), ""

        client = TradeDangerousClient(python_path="C:/td/python.exe", runner=run)
        result = await MCPTools(store, trade_client=client).plan_trade_route(origin="X/Y")
        assert len(calls) == 1
        assert "error" in result
