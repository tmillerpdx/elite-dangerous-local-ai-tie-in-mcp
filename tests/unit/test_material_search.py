"""
Tests for the Spansh raw material search and the find_material_bodies tool.

All HTTP is mocked with httpx.MockTransport; no test touches the network.
"""

import json
from datetime import datetime, timezone
from unittest.mock import Mock

import httpx
import pytest

from src.elite_mcp.mcp_tools import MCPTools
from src.utils.data_store import DataStore, GameState
from src.utils.spansh_client import (
    RAW_MATERIALS,
    SpanshClient,
    build_material_request,
    normalize_material,
    parse_material_list,
    shape_material_body,
)


def _body(name="Hollatja 3 a", distance=0.0, selenium=4.42, cadmium=None,
          gravity=0.06, arrival=900.0, geo=0):
    materials = [
        {"name": "Carbon", "share": 23.75},
        {"name": "Iron", "share": 12.39},
    ]
    if selenium is not None:
        materials.append({"name": "Selenium", "share": selenium})
    if cadmium is not None:
        materials.append({"name": "Cadmium", "share": cadmium})
    signals = [{"name": "Geological", "count": geo}] if geo else []
    return {
        "name": name,
        "system_name": "Hollatja",
        "distance": distance,
        "distance_to_arrival": arrival,
        "subtype": "Icy body",
        "gravity": gravity,
        "volcanism_type": "Water Geysers" if geo else None,
        "is_landable": True,
        "signals": signals,
        "materials": materials,
    }


def _client(handler):
    return SpanshClient(transport=httpx.MockTransport(handler))


def _json_response(results):
    return httpx.Response(200, json={"count": len(results), "results": results})


class TestMaterialNames:
    def test_all_four_grades_are_present(self):
        assert sorted(set(RAW_MATERIALS.values())) == [1, 2, 3, 4]
        assert len(RAW_MATERIALS) == 28
        assert RAW_MATERIALS["Selenium"] == 4
        assert RAW_MATERIALS["Cadmium"] == 3

    def test_normalize_is_case_insensitive(self):
        assert normalize_material(" selenium ") == "Selenium"
        assert normalize_material("CADMIUM") == "Cadmium"

    def test_normalize_accepts_american_spelling(self):
        assert normalize_material("sulfur") == "Sulphur"

    def test_normalize_unknown(self):
        assert normalize_material("Unobtainium") is None
        assert normalize_material("") is None

    def test_parse_list(self):
        assert parse_material_list("selenium, cadmium") == {"materials": ["Selenium", "Cadmium"]}

    def test_parse_list_removes_duplicates(self):
        assert parse_material_list("Selenium,selenium") == {"materials": ["Selenium"]}

    def test_parse_list_unknown_returns_error_object(self):
        result = parse_material_list("Selenium,Unobtainium")
        assert "Unobtainium" in result["error"]
        assert "Selenium" in result["available_materials"]

    def test_parse_list_empty_returns_error_object(self):
        assert "error" in parse_material_list("  ")


class TestMaterialRequest:
    def test_threshold_is_sent_as_share(self):
        # Spansh ignores the threshold when it is sent as "value".
        req = build_material_request("Hollatja", ["Selenium"], 4.0, 50.0, False, 10)
        entry = req["filters"]["materials"][0]
        assert entry["name"] == "Selenium"
        assert entry["share"] == [4.0, 100.0]
        assert "value" not in entry
        assert req["filters"]["is_landable"] == {"value": True}
        assert req["sort"] == [{"distance": {"direction": "asc"}}]

    def test_min_percent_applies_to_primary_only(self):
        req = build_material_request("Hollatja", ["Selenium", "Cadmium"], 4.0, 50.0, False, 10)
        entries = req["filters"]["materials"]
        assert [e["name"] for e in entries] == ["Selenium", "Cadmium"]
        assert entries[0]["share"][0] == 4.0
        assert entries[1]["share"][0] == 0.0

    def test_sort_by_percent(self):
        req = build_material_request("Hollatja", ["Selenium"], 0.0, 50.0, True, 10)
        assert req["sort"] == [{"materials": [{"name": "Selenium", "direction": "desc"}]}]


class TestMaterialShaping:
    def test_shape(self):
        shaped = shape_material_body(_body(cadmium=1.24, geo=3), ["Selenium", "Cadmium"])
        assert shaped["body"] == "Hollatja 3 a"
        assert shaped["requested_percent"] == {"Selenium": 4.42, "Cadmium": 1.24}
        assert shaped["geological_signals"] == 3
        assert shaped["volcanism"] == "Water Geysers"
        assert shaped["gravity_g"] == 0.06
        assert list(shaped["all_materials_percent"])[0] == "Carbon"

    def test_missing_material_drops_the_body(self):
        assert shape_material_body(_body(), ["Selenium", "Cadmium"]) is None

    def test_no_volcanism_is_reported_as_text(self):
        assert shape_material_body(_body(), ["Selenium"])["volcanism"] == "None"


class TestFindMaterialBodies:
    async def test_posts_expected_request_and_shapes_result(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return _json_response([_body()])

        result = await _client(handler).find_material_bodies(
            "Hollatja", "selenium", 0.0, 50.0, 0.0, 0.0, "distance", 5)
        assert seen["body"]["filters"]["materials"][0]["name"] == "Selenium"
        assert result["materials"] == [{"name": "Selenium", "grade": 4}]
        assert result["result_count"] == 1
        assert result["results"][0]["requested_percent"] == {"Selenium": 4.42}

    async def test_limits_are_reapplied_client_side(self):
        bodies = [
            _body("Poor 1", 1.0, selenium=2.5),
            _body("Heavy 2", 2.0, gravity=1.8),
            _body("Distant 3", 3.0, arrival=300000.0),
            _body("Far 4", 90.0),
            _body("No Cadmium 5", 4.0),
            _body("Good 6", 5.0, cadmium=1.2),
        ]
        client = _client(lambda request: _json_response(bodies))
        result = await client.find_material_bodies(
            "Hollatja", "Selenium,Cadmium", 4.0, 50.0, 1.0, 10000.0, "distance", 5)
        assert [r["body"] for r in result["results"]] == ["Good 6"]

    async def test_unknown_material_returns_error_object(self):
        client = _client(lambda request: _json_response([]))
        result = await client.find_material_bodies(
            "Hollatja", "Unobtainium", 0.0, 50.0, 0.0, 0.0, "distance", 5)
        assert "error" in result
        assert "available_materials" in result

    async def test_bad_sort_returns_error_object(self):
        client = _client(lambda request: _json_response([]))
        result = await client.find_material_bodies(
            "Hollatja", "Selenium", 0.0, 50.0, 0.0, 0.0, "richest", 5)
        assert "sort_by" in result["error"]

    async def test_empty_sort_defaults_to_distance(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return _json_response([_body()])

        result = await _client(handler).find_material_bodies(
            "Hollatja", "Selenium", 0.0, 50.0, 0.0, 0.0, "", 5)
        assert seen["body"]["sort"] == [{"distance": {"direction": "asc"}}]
        assert result["search"]["sort_by"] == "distance"


class TestGatewayRetry:
    async def test_transient_gateway_error_is_retried_once(self):
        calls = []

        def handler(request):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(502, text="Bad Gateway")
            return _json_response([_body()])

        result = await _client(handler).find_material_bodies(
            "Hollatja", "Selenium", 0.0, 50.0, 0.0, 0.0, "distance", 5)
        assert len(calls) == 2
        assert result["result_count"] == 1

    async def test_persistent_gateway_error_gives_up_after_one_retry(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(502, text="Bad Gateway")

        result = await _client(handler).find_material_bodies(
            "Hollatja", "Selenium", 0.0, 50.0, 0.0, 0.0, "distance", 5)
        assert len(calls) == 2
        assert "502" in result["error"]


class TestFindMaterialBodiesTool:
    @pytest.fixture
    def store(self):
        store = Mock(spec=DataStore)
        store.get_game_state.return_value = GameState(
            current_system="Hollatja", last_updated=datetime.now(timezone.utc))
        return store

    async def test_defaults_to_current_system(self, store):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return _json_response([_body()])

        tools = MCPTools(store, spansh_client=_client(handler))
        result = await tools.find_material_bodies("Selenium")
        assert seen["body"]["reference_system"] == "Hollatja"
        assert result["reference_source"] == "current_location"

    async def test_unknown_location_returns_error_object(self, store):
        store.get_game_state.return_value = GameState(
            current_system=None, last_updated=datetime.now(timezone.utc))
        tools = MCPTools(store, spansh_client=_client(lambda request: _json_response([])))
        result = await tools.find_material_bodies("Selenium")
        assert "error" in result
