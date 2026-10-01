"""
Tests for the Spansh commodity market search and the find_commodity_market tool.

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
    STATION_TYPES_WITHOUT_CARRIERS,
    SpanshClient,
    build_commodity_request,
    is_fleet_carrier,
    resolve_commodity_name,
    shape_commodity_station,
)

NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
NAMES = ["Low Temperature Diamonds", "Palladium", "Tritium", "Void Opal", "Wine"]


def _station(name="Old Sharlayan", system="Thuleppa", distance=7.9, station_type="Coriolis Starport",
             large_pad=True, arrival=409.0, supply=32386, demand=1, buy=50319, sell=49684,
             updated="2026-09-28T10:00:00Z", commodity="Tritium"):
    return {
        "name": name,
        "system_name": system,
        "distance": distance,
        "distance_to_arrival": arrival,
        "type": station_type,
        "has_large_pad": large_pad,
        "is_planetary": False,
        "market_updated_at": updated,
        "market": [
            {"commodity": "Gold", "supply": 5, "demand": 0, "buy_price": 9000, "sell_price": 8900},
            {"commodity": commodity, "supply": supply, "demand": demand,
             "buy_price": buy, "sell_price": sell},
        ],
    }


def _client(stations, seen=None, names=NAMES, names_status=200):
    def handler(request):
        if request.method == "GET":
            if seen is not None:
                seen["name_calls"] = seen.get("name_calls", 0) + 1
            return httpx.Response(names_status, json={"min_max": {n: {} for n in names}})
        if seen is not None:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"count": len(stations), "results": stations})

    return SpanshClient(transport=httpx.MockTransport(handler))


async def _find(client, commodity="Tritium", mode="buy", min_quantity=1, max_distance=50.0,
                large_pad=False, carriers=False, max_age=30.0, max_arrival=0.0,
                sort_by="distance", limit=10):
    return await client.find_commodity_stations(
        "Hollatja", commodity, mode, min_quantity, max_distance, large_pad, carriers,
        max_age, max_arrival, sort_by, limit, now=NOW)


class TestCommodityNames:
    def test_case_insensitive_match(self):
        assert resolve_commodity_name("tritium", NAMES) == {"commodity": "Tritium"}
        assert resolve_commodity_name(" VOID OPAL ", NAMES) == {"commodity": "Void Opal"}

    def test_misspelling_returns_suggestions(self):
        result = resolve_commodity_name("Tritum", NAMES)
        assert "error" in result
        assert "Tritium" in result["did_you_mean"]

    def test_partial_name_returns_suggestions(self):
        result = resolve_commodity_name("diamonds", NAMES)
        assert "Low Temperature Diamonds" in result["did_you_mean"]

    def test_empty_name_returns_error_object(self):
        assert "error" in resolve_commodity_name("  ", NAMES)

    def test_without_name_list_falls_back_to_title_case(self):
        result = resolve_commodity_name("void opal", [])
        assert result == {"commodity": "Void Opal", "unverified_name": True}

    def test_fleet_carrier_detection(self):
        assert is_fleet_carrier({"type": "Drake-Class Carrier"}) is True
        assert is_fleet_carrier({"type": "Coriolis Starport"}) is False
        assert not any("carrier" in t.lower() for t in STATION_TYPES_WITHOUT_CARRIERS)


class TestCommodityRequest:
    def test_buy_filters_on_supply(self):
        req = build_commodity_request("Hollatja", "Tritium", "buy", 500, 60.0, True, False,
                                      "2026-09-01", False, 10)
        market = req["filters"]["market"][0]
        assert market["name"] == "Tritium"
        assert market["supply"]["value"][0] == "500"
        assert "demand" not in market
        assert req["filters"]["has_large_pad"] == {"value": True}
        assert req["filters"]["type"] == {"value": STATION_TYPES_WITHOUT_CARRIERS}
        assert req["filters"]["market_updated_at"]["value"][0] == "2026-09-01"
        assert req["sort"] == [{"distance": {"direction": "asc"}}]

    def test_sell_filters_on_demand(self):
        req = build_commodity_request("Hollatja", "Wine", "sell", 100, 60.0, False, True,
                                      None, False, 10)
        market = req["filters"]["market"][0]
        assert market["demand"]["value"][0] == "100"
        assert "supply" not in market
        assert "type" not in req["filters"]
        assert "has_large_pad" not in req["filters"]
        assert "market_updated_at" not in req["filters"]

    def test_price_sort_is_cheapest_when_buying(self):
        req = build_commodity_request("Hollatja", "Tritium", "buy", 1, 60.0, False, False,
                                      None, True, 10)
        assert req["sort"] == [{"market_buy_price": [{"name": "Tritium", "direction": "asc"}]}]

    def test_price_sort_is_highest_when_selling(self):
        req = build_commodity_request("Hollatja", "Wine", "sell", 1, 60.0, False, False,
                                      None, True, 10)
        assert req["sort"] == [{"market_sell_price": [{"name": "Wine", "direction": "desc"}]}]


class TestCommodityShaping:
    def test_buy_uses_buy_price_and_supply(self):
        shaped = shape_commodity_station(_station(), "Tritium", "buy", NOW)
        assert shaped["station"] == "Old Sharlayan"
        assert shaped["price"] == 50319
        assert shaped["quantity"] == 32386
        assert shaped["has_large_pad"] is True
        assert shaped["is_fleet_carrier"] is False
        assert shaped["data_age_days"] == 3.1

    def test_sell_uses_sell_price_and_demand(self):
        shaped = shape_commodity_station(_station(demand=900, sell=49000), "Tritium", "sell", NOW)
        assert shaped["price"] == 49000
        assert shaped["quantity"] == 900

    def test_station_without_the_commodity_is_dropped(self):
        assert shape_commodity_station(_station(), "Wine", "buy", NOW) is None

    def test_missing_update_time_gives_unknown_age(self):
        shaped = shape_commodity_station(_station(updated=None), "Tritium", "buy", NOW)
        assert shaped["data_age_days"] is None


class TestFindCommodityStations:
    async def test_posts_to_station_search_with_resolved_name(self):
        seen = {}
        result = await _find(_client([_station()], seen), commodity="tritium")
        assert seen["url"] == "https://spansh.co.uk/api/stations/search"
        assert seen["body"]["filters"]["market"][0]["name"] == "Tritium"
        assert seen["body"]["filters"]["market_updated_at"]["value"][0] == "2026-09-01"
        assert result["commodity"] == "Tritium"
        assert result["mode"] == "buy"
        assert result["result_count"] == 1

    async def test_limits_are_reapplied_client_side(self):
        stations = [
            _station("Carrier X", station_type="Drake-Class Carrier", distance=0.0),
            _station("Stale", distance=1.0, updated="2025-02-05T00:00:00Z"),
            _station("Thin", distance=2.0, supply=28),
            _station("Small Pads", distance=3.0, large_pad=False),
            _station("Too Far Out", distance=4.0, arrival=250000.0),
            _station("Beyond Radius", distance=90.0),
            _station("Good", distance=5.0),
        ]
        result = await _find(_client(stations), min_quantity=500, large_pad=True,
                             max_arrival=20000.0)
        assert [r["station"] for r in result["results"]] == ["Good"]

    async def test_fleet_carriers_can_be_included(self):
        stations = [_station("Carrier X", station_type="Drake-Class Carrier", distance=0.0)]
        seen = {}
        result = await _find(_client(stations, seen), carriers=True)
        assert "type" not in seen["body"]["filters"]
        assert result["results"][0]["is_fleet_carrier"] is True

    async def test_no_age_limit(self):
        stations = [_station("Stale", updated="2023-03-19T00:00:00Z")]
        seen = {}
        result = await _find(_client(stations, seen), max_age=0.0)
        assert "market_updated_at" not in seen["body"]["filters"]
        assert result["result_count"] == 1

    async def test_sell_mode(self):
        stations = [_station(commodity="Wine", demand=20000, sell=7291, supply=0, buy=0)]
        result = await _find(_client(stations), commodity="wine", mode="sell", sort_by="price")
        assert result["mode"] == "sell"
        assert result["results"][0]["price"] == 7291
        assert result["results"][0]["quantity"] == 20000

    async def test_unknown_commodity_returns_suggestions_without_searching(self):
        seen = {}
        result = await _find(_client([_station()], seen), commodity="Tritum")
        assert "Tritium" in result["did_you_mean"]
        assert "body" not in seen

    async def test_bad_mode_and_sort_return_error_objects(self):
        client = _client([_station()])
        assert "mode" in (await _find(client, mode="trade"))["error"]
        assert "sort_by" in (await _find(client, sort_by="cheapest"))["error"]

    async def test_empty_mode_and_sort_select_defaults(self):
        result = await _find(_client([_station()]), mode="", sort_by="")
        assert result["mode"] == "buy"
        assert result["search"]["sort_by"] == "distance"

    async def test_name_list_is_fetched_once(self):
        seen = {}
        client = _client([_station()], seen)
        await _find(client)
        await _find(client)
        assert seen["name_calls"] == 1

    async def test_name_list_failure_falls_back_and_says_so(self):
        client = _client([_station()], names_status=503)
        result = await _find(client, commodity="tritium")
        assert result["commodity"] == "Tritium"
        assert any("could not be checked" in note for note in result["notes"])


class TestFindCommodityMarketTool:
    @pytest.fixture
    def store(self):
        store = Mock(spec=DataStore)
        store.get_game_state.return_value = GameState(
            current_system="Hollatja", last_updated=datetime.now(timezone.utc))
        return store

    async def test_defaults_to_current_system(self, store):
        seen = {}
        tools = MCPTools(store, spansh_client=_client([_station(updated=None)], seen))
        result = await tools.find_commodity_market("Tritium", max_data_age_days=0)
        assert seen["body"]["reference_system"] == "Hollatja"
        assert result["reference_source"] == "current_location"

    async def test_unknown_location_returns_error_object(self, store):
        store.get_game_state.return_value = GameState(
            current_system=None, last_updated=datetime.now(timezone.utc))
        tools = MCPTools(store, spansh_client=_client([]))
        assert "error" in await tools.find_commodity_market("Tritium")
