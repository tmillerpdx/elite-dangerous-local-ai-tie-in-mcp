"""
Tests for the Spansh route plotters and the plot_neutron_route tool.

All HTTP is mocked with httpx.MockTransport; no test touches the network.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs

import httpx
import pytest

from src.elite_mcp.mcp_tools import MCPTools
from src.utils.data_store import DataStore, GameState
from src.utils.spansh_client import SpanshClient, ship_from_loadout


def _loadout(**overrides):
    loadout = {
        "event": "Loadout",
        "Ship": "anaconda",
        "UnladenMass": 400.0,
        "MaxJumpRange": 60.5,
        "FuelCapacity": {"Main": 32.0, "Reserve": 1.07},
        "Modules": [
            {
                "Slot": "FrameShiftDrive",
                "Item": "int_hyperdrive_size6_class5",
                "On": True,
                "Engineering": {"Modifiers": [{"Label": "FSDOptimalMass", "Value": 2902.0}]},
            },
            {"Slot": "Slot05_Size5", "Item": "int_guardianfsdbooster_size5", "On": True},
            {"Slot": "Slot04_Size6", "Item": "int_fuelscoop_size6_class5", "On": True},
        ],
    }
    loadout.update(overrides)
    return loadout


SIMPLE_RESULT = {
    "status": "ok",
    "result": {
        "source_system": "Sol",
        "destination_system": "Colonia",
        "distance": 22000.474,
        "efficiency": 60,
        "range": 50,
        "total_jumps": 17,
        "system_jumps": [
            {"system": "Sol", "jumps": 0, "neutron_star": False,
             "distance_jumped": 0, "distance_left": 22000.474},
            {"system": "PSR J1752-2806", "jumps": 10, "neutron_star": True,
             "distance_jumped": 407.486, "distance_left": 21629.388},
            {"system": "Colonia", "jumps": 7, "neutron_star": False,
             "distance_jumped": 113.17, "distance_left": 0},
        ],
    },
}

FUEL_RESULT = {
    "status": "ok",
    "result": {
        "refuel_every_scoopable": True,
        "jumps": [
            {"name": "Sol", "distance": 0, "distance_to_destination": 300.0, "fuel_in_tank": 32,
             "fuel_used": 0, "has_neutron": False, "is_scoopable": True, "must_refuel": False},
            {"name": "Dalia", "distance": 60.59, "distance_to_destination": 240.0,
             "fuel_in_tank": 24.26, "fuel_used": 7.74, "has_neutron": False,
             "is_scoopable": True, "must_refuel": True},
            {"name": "PSR J1752-2806", "distance": 55.0, "distance_to_destination": 190.0,
             "fuel_in_tank": 26.0, "fuel_used": 6.0, "has_neutron": True,
             "is_scoopable": False, "must_refuel": False},
            {"name": "Colonia", "distance": 190.0, "distance_to_destination": 0,
             "fuel_in_tank": 18.5, "fuel_used": 7.5, "has_neutron": False,
             "is_scoopable": False, "must_refuel": False},
        ],
    },
}


def _route_handler(result, seen=None, queued_polls=0):
    """Mock Spansh: accept the job, answer 'queued' a few times, then the result."""
    seen = seen if seen is not None else {}
    seen.setdefault("polls", 0)

    def handler(request):
        if request.method == "POST":
            seen["path"] = request.url.path
            seen["form"] = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            return httpx.Response(202, json={"job": "JOB-1", "status": "queued"})
        seen["poll_path"] = request.url.path
        seen["polls"] += 1
        if seen["polls"] <= queued_polls:
            return httpx.Response(202, json={"job": "JOB-1", "status": "queued"})
        return httpx.Response(200, json=result)

    return handler


def _client(handler, **kwargs):
    kwargs.setdefault("poll_interval", 0.0)
    return SpanshClient(transport=httpx.MockTransport(handler), **kwargs)


class TestShipFromLoadout:
    def test_reads_fsd_tank_and_booster(self):
        ship = ship_from_loadout(_loadout())
        assert ship == {
            "fuel_power": 2.6,
            "fuel_multiplier": 0.012,
            "optimal_mass": 2902.0,
            "max_fuel_per_jump": 8.0,
            "base_mass": 400.0,
            "tank_size": 32.0,
            "internal_tank_size": 1.07,
            "range_boost": 10.5,
            "has_fuel_scoop": True,
        }

    def test_stock_drive_uses_base_values(self):
        loadout = _loadout(Modules=[
            {"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size5_class3", "On": True},
        ])
        ship = ship_from_loadout(loadout)
        assert ship["optimal_mass"] == 700.0
        assert ship["max_fuel_per_jump"] == 3.3
        assert ship["fuel_multiplier"] == 0.008
        assert ship["fuel_power"] == 2.45
        assert ship["range_boost"] == 0.0
        assert ship["has_fuel_scoop"] is False

    def test_sco_drive(self):
        loadout = _loadout(Modules=[
            {"Slot": "FrameShiftDrive", "Item": "Int_Hyperdrive_Overcharge_Size5_Class5", "On": True},
        ])
        ship = ship_from_loadout(loadout)
        assert ship["optimal_mass"] == 1175.0
        assert ship["max_fuel_per_jump"] == 5.2
        assert ship["fuel_multiplier"] == 0.013

    def test_unknown_drive_returns_error_object(self):
        loadout = _loadout(Modules=[
            {"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size9_class9", "On": True},
        ])
        assert "int_hyperdrive_size9_class9" in ship_from_loadout(loadout)["error"]

    def test_missing_modules_returns_error_object(self):
        assert "error" in ship_from_loadout({"event": "Loadout"})
        assert "error" in ship_from_loadout(None)


class TestSimpleRoute:
    async def test_posts_range_and_shapes_waypoints(self):
        seen = {}
        client = _client(_route_handler(SIMPLE_RESULT, seen, queued_polls=2))
        result = await client.plot_neutron_route("Sol", "Colonia", 50.0, 60, None)
        assert seen["path"] == "/api/route"
        assert seen["form"] == {"from": "Sol", "to": "Colonia", "range": "50.0", "efficiency": "60"}
        assert seen["poll_path"] == "/api/results/JOB-1"
        assert seen["polls"] == 3
        assert result["mode"] == "simple"
        assert result["origin"] == "Sol"
        assert result["destination"] == "Colonia"
        assert result["total_jumps"] == 17
        assert result["distance_ly"] == 22000.5
        assert result["neutron_boosts"] == 1
        assert result["waypoints"][1] == {
            "system": "PSR J1752-2806",
            "neutron_star": True,
            "jumps": 10,
            "distance_ly": 407.5,
            "remaining_ly": 21629.4,
        }
        assert result["source"] == "spansh.co.uk"

    async def test_rejected_job_returns_spansh_message(self):
        def handler(request):
            return httpx.Response(400, json={"error": "Could not find starting system"})

        result = await _client(handler).plot_neutron_route("Nowhere", "Colonia", 50.0, 60, None)
        assert "Could not find starting system" in result["error"]

    async def test_gives_up_when_job_never_finishes(self):
        client = _client(_route_handler(SIMPLE_RESULT, queued_polls=10 ** 6), route_timeout=0.0)
        result = await client.plot_neutron_route("Sol", "Colonia", 50.0, 60, None)
        assert "did not finish" in result["error"]

    async def test_network_failure_returns_error_object(self):
        def handler(request):
            raise httpx.ConnectError("boom")

        result = await _client(handler).plot_neutron_route("Sol", "Colonia", 50.0, 60, None)
        assert "Could not reach Spansh" in result["error"]


class TestFuelRoute:
    async def test_posts_ship_and_shapes_jumps(self):
        seen = {}
        ship = ship_from_loadout(_loadout())
        client = _client(_route_handler(FUEL_RESULT, seen))
        result = await client.plot_neutron_route("Sol", "Colonia", 0.0, 60, ship)
        assert seen["path"] == "/api/generic/route"
        form = seen["form"]
        assert form["source"] == "Sol"
        assert form["destination"] == "Colonia"
        assert form["optimal_mass"] == "2902.0"
        assert form["tank_size"] == "32.0"
        assert form["range_boost"] == "10.5"
        assert form["use_supercharge"] == "1"
        assert form["use_injections"] == "0"
        assert "has_fuel_scoop" not in form
        assert result["mode"] == "fuel"
        assert result["total_jumps"] == 3
        assert result["distance_ly"] == 305.6
        assert result["neutron_boosts"] == 1
        assert result["refuel_stops"] == 1
        assert result["waypoints"][1] == {
            "system": "Dalia",
            "distance_ly": 60.6,
            "remaining_ly": 240.0,
            "fuel_in_tank_t": 24.26,
            "fuel_used_t": 7.74,
            "neutron_star": False,
            "scoopable": True,
            "must_refuel": True,
        }


class TestPlotNeutronRouteTool:
    @pytest.fixture
    def store(self):
        store = Mock(spec=DataStore)
        store.get_game_state.return_value = GameState(
            current_system="Sol", last_updated=datetime.now(timezone.utc))
        store.get_events_by_type.return_value = [
            SimpleNamespace(raw_event=_loadout(), timestamp=datetime.now(timezone.utc))
        ]
        return store

    async def test_defaults_to_current_system_and_journal_ship(self, store):
        seen = {}
        tools = MCPTools(store, spansh_client=_client(_route_handler(FUEL_RESULT, seen)))
        result = await tools.plot_neutron_route("Colonia")
        assert seen["path"] == "/api/generic/route"
        assert seen["form"]["source"] == "Sol"
        assert result["origin_source"] == "current_location"
        assert result["ship_source"] == "journal_loadout"

    async def test_explicit_range_uses_simple_plotter(self, store):
        seen = {}
        tools = MCPTools(store, spansh_client=_client(_route_handler(SIMPLE_RESULT, seen)))
        result = await tools.plot_neutron_route("Colonia", jump_range_ly=50.0)
        assert seen["path"] == "/api/route"
        assert seen["form"]["range"] == "50.0"
        assert result["ship_source"] == "argument"

    async def test_unsupported_drive_falls_back_to_journal_range(self, store):
        store.get_events_by_type.return_value = [SimpleNamespace(
            raw_event=_loadout(Modules=[
                {"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size9_class9", "On": True}]),
            timestamp=datetime.now(timezone.utc))]
        seen = {}
        tools = MCPTools(store, spansh_client=_client(_route_handler(SIMPLE_RESULT, seen)))
        result = await tools.plot_neutron_route("Colonia")
        assert seen["path"] == "/api/route"
        assert seen["form"]["range"] == "60.5"
        assert result["ship_source"] == "journal_max_jump_range"
        assert any("int_hyperdrive_size9_class9" in note for note in result["notes"])

    async def test_simple_mode_ignores_ship(self, store):
        seen = {}
        tools = MCPTools(store, spansh_client=_client(_route_handler(SIMPLE_RESULT, seen)))
        await tools.plot_neutron_route("Colonia", mode="simple")
        assert seen["path"] == "/api/route"
        assert seen["form"]["range"] == "60.5"

    async def test_fuel_mode_without_usable_ship_returns_error_object(self, store):
        store.get_events_by_type.return_value = []
        tools = MCPTools(store, spansh_client=_client(_route_handler(FUEL_RESULT)))
        result = await tools.plot_neutron_route("Colonia", mode="fuel")
        assert "error" in result

    async def test_no_loadout_and_no_range_returns_error_object(self, store):
        store.get_events_by_type.return_value = []
        tools = MCPTools(store, spansh_client=_client(_route_handler(SIMPLE_RESULT)))
        result = await tools.plot_neutron_route("Colonia")
        assert "jump_range_ly" in result["error"]

    async def test_empty_destination_returns_error_object(self, store):
        tools = MCPTools(store, spansh_client=_client(_route_handler(SIMPLE_RESULT)))
        result = await tools.plot_neutron_route("  ")
        assert "destination" in result["error"]

    async def test_bad_mode_returns_error_object(self, store):
        tools = MCPTools(store, spansh_client=_client(_route_handler(SIMPLE_RESULT)))
        result = await tools.plot_neutron_route("Colonia", mode="fastest")
        assert "mode" in result["error"]

    async def test_unknown_location_returns_error_object(self, store):
        store.get_game_state.return_value = GameState(
            current_system=None, last_updated=datetime.now(timezone.utc))
        tools = MCPTools(store, spansh_client=_client(_route_handler(SIMPLE_RESULT)))
        result = await tools.plot_neutron_route("Colonia")
        assert "error" in result
