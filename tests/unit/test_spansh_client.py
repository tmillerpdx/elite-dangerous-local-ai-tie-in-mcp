"""
Tests for the Spansh client and the nearby-search MCP tools.

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
    HOTSPOT_COMMODITIES,
    SpanshClient,
    build_exobiology_request,
    build_hotspot_request,
    normalize_commodity,
    shape_exobiology_body,
    shape_hotspot_body,
)


def _ring_body(name="Alcor A 5", distance=1.1, platinum=3, reserve="Pristine"):
    return {
        "name": name,
        "system_name": name.rsplit(" ", 2)[0],
        "distance": distance,
        "distance_to_arrival": 1200.4,
        "reserve_level": reserve,
        "subtype": "Class I gas giant",
        "rings": [
            {
                "name": name + " A Ring",
                "type": "Metal Rich",
                "signals_updated_at": "2026-09-01T00:00:00Z",
                "signals": [
                    {"name": "Platinum", "count": platinum},
                    {"name": "Painite", "count": 4},
                ],
            },
            {
                "name": name + " B Ring",
                "type": "Rocky",
                "signals": [{"name": "Musgravite", "count": 2}],
            },
        ],
    }


def _bio_body(name="He Bo 10 e", distance=4.0, bio=3, gravity=0.16, arrival=5145.0):
    return {
        "name": name,
        "system_name": "He Bo",
        "distance": distance,
        "distance_to_arrival": arrival,
        "subtype": "Rocky body",
        "atmosphere": "Thin Ammonia",
        "gravity": gravity,
        "surface_temperature": 171.9,
        "is_landable": True,
        "signals_updated_at": "2026-09-26T17:14:12Z",
        "signals": [
            {"name": "Planetary Mining Location", "count": 17},
            {"name": "Biological", "count": bio},
        ],
        "genuses": [{"name": "Cactoids"}, {"name": "Osseus"}],
        "landmarks": [
            {"type": "Surface Station", "subtype": "Crater Outpost", "value": 0},
            {"type": "Cactoida", "subtype": "Cactoida Lapis", "value": 2483600},
            {"type": "Cactoida", "subtype": "Cactoida Lapis", "value": 2483600},
            {"type": "Osseus", "subtype": "Osseus Spiralis", "value": 2404700},
        ],
    }


def _client(handler):
    return SpanshClient(transport=httpx.MockTransport(handler))


def _json_response(results, status=200):
    return httpx.Response(status, json={"count": len(results), "results": results})


class TestCommodityNormalization:
    def test_case_insensitive(self):
        assert normalize_commodity("platinum") == "Platinum"
        assert normalize_commodity("  PAINITE ") == "Painite"

    def test_aliases(self):
        assert normalize_commodity("ltd") == "Low Temperature Diamonds"
        assert normalize_commodity("void opals") == "Void Opal"
        assert normalize_commodity("Void Opal") == "Void Opal"

    def test_unknown_returns_none(self):
        assert normalize_commodity("Unobtainium") is None

    def test_every_commodity_has_a_method(self):
        for info in HOTSPOT_COMMODITIES.values():
            assert info["method"] in ("laser", "core", "laser or core")


class TestRequestBuilders:
    def test_hotspot_request_uses_count_not_value(self):
        # Spansh silently ignores the threshold when it is sent as "value".
        req = build_hotspot_request("Mizar", "Platinum", 2, 50.0, False, 10)
        sig = req["filters"]["ring_signals"][0]
        assert sig["name"] == "Platinum"
        assert sig["count"][0] == 2
        assert "value" not in sig
        assert req["reference_system"] == "Mizar"
        assert req["sort"] == [{"distance": {"direction": "asc"}}]
        assert req["filters"]["distance"] == {"min": "0", "max": "50.0"}
        assert "reserve_level" not in req["filters"]

    def test_hotspot_request_pristine(self):
        req = build_hotspot_request("Mizar", "Platinum", 1, 50.0, True, 10)
        assert req["filters"]["reserve_level"] == {"value": ["Pristine"]}

    def test_exobiology_request(self):
        req = build_exobiology_request("Mizar", 3, 25.0, 10)
        sig = req["filters"]["signals"][0]
        assert sig["name"] == "Biological"
        assert sig["count"][0] == 3
        assert "value" not in sig
        assert req["filters"]["is_landable"] == {"value": True}
        assert req["filters"]["distance"] == {"min": "0", "max": "25.0"}


class TestShaping:
    def test_shape_hotspot_body(self):
        shaped = shape_hotspot_body(_ring_body(), "Platinum", 1)
        assert shaped["body"] == "Alcor A 5"
        assert shaped["distance_ly"] == 1.1
        assert shaped["arrival_distance_ls"] == 1200
        assert shaped["reserve_level"] == "Pristine"
        assert len(shaped["rings"]) == 1
        ring = shaped["rings"][0]
        assert ring["ring"] == "Alcor A 5 A Ring"
        assert ring["hotspots"] == 3
        assert ring["other_hotspots"] == {"Painite": 4}
        assert shaped["best_hotspot_count"] == 3

    def test_shape_hotspot_body_below_threshold_is_dropped(self):
        assert shape_hotspot_body(_ring_body(platinum=1), "Platinum", 2) is None

    def test_shape_exobiology_body(self):
        shaped = shape_exobiology_body(_bio_body())
        assert shaped["body"] == "He Bo 10 e"
        assert shaped["biological_signals"] == 3
        assert shaped["gravity_g"] == 0.16
        assert shaped["known_genera"] == ["Cactoids", "Osseus"]
        # Species are de-duplicated and non-biological landmarks are ignored.
        assert shaped["known_species"] == [
            {"species": "Cactoida Lapis", "value": 2483600},
            {"species": "Osseus Spiralis", "value": 2404700},
        ]
        assert shaped["known_species_value"] == 2483600 + 2404700
        assert shaped["unidentified_signals"] == 1

    def test_shape_exobiology_body_without_optional_fields(self):
        body = _bio_body()
        del body["genuses"]
        del body["landmarks"]
        shaped = shape_exobiology_body(body)
        assert shaped["known_genera"] == []
        assert shaped["known_species"] == []
        assert shaped["known_species_value"] == 0
        assert shaped["unidentified_signals"] == 3


class TestSpanshClient:
    async def test_find_ring_hotspots_posts_expected_request(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            seen["ua"] = request.headers.get("user-agent", "")
            return _json_response([_ring_body()])

        result = await _client(handler).find_ring_hotspots("Mizar", "platinum", 2, 50.0, False, 5)
        assert seen["url"] == "https://spansh.co.uk/api/bodies/search"
        assert seen["body"]["filters"]["ring_signals"][0]["name"] == "Platinum"
        assert "Mozilla" not in seen["ua"]
        assert result["commodity"] == "Platinum"
        assert result["mining_method"] == "laser"
        assert result["reference_system"] == "Mizar"
        assert result["result_count"] == 1
        assert result["results"][0]["body"] == "Alcor A 5"

    async def test_find_ring_hotspots_filters_client_side(self):
        # Even if Spansh ignores a filter, results must honour the limits.
        bodies = [
            _ring_body("Near A 1", 5.0, platinum=1),
            _ring_body("Far B 2", 80.0, platinum=3),
            _ring_body("Good C 3", 9.0, platinum=3),
        ]
        client = _client(lambda request: _json_response(bodies))
        result = await client.find_ring_hotspots("Mizar", "Platinum", 2, 50.0, False, 5)
        assert [r["body"] for r in result["results"]] == ["Good C 3"]

    async def test_unknown_commodity_returns_error_object(self):
        client = _client(lambda request: _json_response([]))
        result = await client.find_ring_hotspots("Mizar", "Unobtainium", 1, 50.0, False, 5)
        assert "error" in result
        assert "Platinum" in result["available_commodities"]

    async def test_http_error_returns_error_object(self):
        client = _client(lambda request: httpx.Response(503, text="down"))
        result = await client.find_ring_hotspots("Mizar", "Platinum", 1, 50.0, False, 5)
        assert "error" in result
        assert "503" in result["error"]

    async def test_unknown_system_returns_error_object(self):
        client = _client(lambda request: httpx.Response(400, json={"error": "Invalid request"}))
        result = await client.find_exobiology_bodies("Not A System", 2, 50.0, 0.0, 0.0, 5)
        assert "error" in result
        assert "Not A System" in result["error"]

    async def test_network_failure_returns_error_object(self):
        def handler(request):
            raise httpx.ConnectError("no route")

        result = await _client(handler).find_exobiology_bodies("Mizar", 2, 50.0, 0.0, 0.0, 5)
        assert "error" in result

    async def test_find_exobiology_bodies_applies_gravity_and_arrival_limits(self):
        bodies = [
            _bio_body("Heavy 1", 2.0, gravity=1.4),
            _bio_body("Distant 2", 3.0, arrival=250000.0),
            _bio_body("Fine 3", 4.0, bio=4),
        ]
        client = _client(lambda request: _json_response(bodies))
        result = await client.find_exobiology_bodies("Mizar", 2, 50.0, 1.0, 10000.0, 5)
        assert [r["body"] for r in result["results"]] == ["Fine 3"]
        assert result["results"][0]["biological_signals"] == 4

    async def test_limit_is_respected(self):
        bodies = [_bio_body("Body %d" % i, float(i)) for i in range(1, 9)]
        client = _client(lambda request: _json_response(bodies))
        result = await client.find_exobiology_bodies("Mizar", 2, 50.0, 0.0, 0.0, 3)
        assert result["result_count"] == 3


class TestNearbySearchTools:
    @pytest.fixture
    def store(self):
        store = Mock(spec=DataStore)
        state = GameState(current_system="Mizar", last_updated=datetime.now(timezone.utc))
        store.get_game_state.return_value = state
        return store

    async def test_empty_reference_defaults_to_current_system(self, store):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return _json_response([_ring_body()])

        tools = MCPTools(store, spansh_client=_client(handler))
        result = await tools.find_mining_hotspots(commodity="Platinum", reference_system="")
        assert seen["body"]["reference_system"] == "Mizar"
        assert result["reference_system"] == "Mizar"
        assert result["reference_source"] == "current_location"

    async def test_explicit_reference_system_wins(self, store):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return _json_response([_bio_body()])

        tools = MCPTools(store, spansh_client=_client(handler))
        result = await tools.find_exobiology_targets(reference_system="Sol")
        assert seen["body"]["reference_system"] == "Sol"
        assert result["reference_source"] == "argument"

    async def test_unknown_location_returns_error_object(self, store):
        store.get_game_state.return_value = GameState(
            current_system=None, last_updated=datetime.now(timezone.utc)
        )
        tools = MCPTools(store, spansh_client=_client(lambda request: _json_response([])))
        result = await tools.find_mining_hotspots()
        assert "error" in result
        assert "reference_system" in result["error"]

    async def test_empty_commodity_defaults_to_platinum(self, store):
        tools = MCPTools(store, spansh_client=_client(lambda request: _json_response([_ring_body()])))
        result = await tools.find_mining_hotspots(commodity="")
        assert result["commodity"] == "Platinum"
