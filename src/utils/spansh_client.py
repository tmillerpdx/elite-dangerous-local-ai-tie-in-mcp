"""Spansh API client

Read-only client for the public Spansh searches (https://spansh.co.uk), used
to find ring hotspots, exobiology candidates, materials and markets near a
reference system, and for its neutron-highway route plotters.

Route plotters are asynchronous jobs: POST returns a job id, then
GET /results/<job> answers "queued" until the route is ready.

Notes on the Spansh search API, verified against the live service:
- Signal thresholds must be sent as "count": [min, max]. If they are sent as
  "value" the name still matches but the threshold is silently ignored.
- Unknown filter keys are silently ignored, so every limit is re-applied
  client-side after the response arrives.
- An unknown reference system returns HTTP 400 {"error": "Invalid request"}.
"""

import asyncio
import difflib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

SPANSH_BASE_URL = "https://spansh.co.uk/api"
USER_AGENT = "elite-dangerous-local-ai-tie-in-mcp/0.1 (local MCP server; read-only)"
MAX_SIGNAL_COUNT = 99
MAX_RESULTS = 50

# Ring hotspot commodities known to Spansh, with how they are mined.
HOTSPOT_COMMODITIES: Dict[str, Dict[str, str]] = {
    "Platinum": {"method": "laser"},
    "Painite": {"method": "laser"},
    "Bromellite": {"method": "laser"},
    "Tritium": {"method": "laser"},
    "Low Temperature Diamonds": {"method": "laser or core"},
    "Alexandrite": {"method": "core"},
    "Benitoite": {"method": "core"},
    "Grandidierite": {"method": "core"},
    "Monazite": {"method": "core"},
    "Musgravite": {"method": "core"},
    "Rhodplumsite": {"method": "core"},
    "Serendibite": {"method": "core"},
    "Void Opal": {"method": "core"},
}

_COMMODITY_ALIASES: Dict[str, str] = {
    "ltd": "Low Temperature Diamonds",
    "ltds": "Low Temperature Diamonds",
    "low temp diamonds": "Low Temperature Diamonds",
    "diamonds": "Low Temperature Diamonds",
    "void opals": "Void Opal",
    "opals": "Void Opal",
    "opal": "Void Opal",
    "vo": "Void Opal",
}


def normalize_commodity(name: str) -> Optional[str]:
    """Map user input to the exact Spansh hotspot name, or None if unknown."""
    key = (name or "").strip().lower()
    if not key:
        return None
    if key in _COMMODITY_ALIASES:
        return _COMMODITY_ALIASES[key]
    for commodity in HOTSPOT_COMMODITIES:
        if commodity.lower() == key:
            return commodity
    return None


def _base_request(reference_system: str, max_distance_ly: float, size: int) -> Dict[str, Any]:
    return {
        "filters": {"distance": {"min": "0", "max": str(float(max_distance_ly))}},
        "sort": [{"distance": {"direction": "asc"}}],
        "size": max(1, min(int(size), MAX_RESULTS)),
        "page": 0,
        "reference_system": reference_system,
    }


def build_hotspot_request(
    reference_system: str,
    commodity: str,
    min_hotspots: int,
    max_distance_ly: float,
    pristine_only: bool,
    size: int,
) -> Dict[str, Any]:
    """Build the Spansh body search payload for ring hotspots."""
    request = _base_request(reference_system, max_distance_ly, size)
    request["filters"]["ring_signals"] = [
        {"name": commodity, "count": [int(min_hotspots), MAX_SIGNAL_COUNT], "comparison": "<=>"}
    ]
    if pristine_only:
        request["filters"]["reserve_level"] = {"value": ["Pristine"]}
    return request


def build_exobiology_request(
    reference_system: str,
    min_bio_signals: int,
    max_distance_ly: float,
    size: int,
) -> Dict[str, Any]:
    """Build the Spansh body search payload for landable bodies with life."""
    request = _base_request(reference_system, max_distance_ly, size)
    request["filters"]["is_landable"] = {"value": True}
    request["filters"]["signals"] = [
        {"name": "Biological", "count": [int(min_bio_signals), MAX_SIGNAL_COUNT], "comparison": "<=>"}
    ]
    return request


# Raw (surface) materials and their engineering grade, 1 = very common.
RAW_MATERIALS: Dict[str, int] = {
    "Carbon": 1, "Iron": 1, "Lead": 1, "Nickel": 1, "Phosphorus": 1, "Rhenium": 1, "Sulphur": 1,
    "Arsenic": 2, "Chromium": 2, "Germanium": 2, "Manganese": 2, "Vanadium": 2, "Zinc": 2,
    "Zirconium": 2,
    "Boron": 3, "Cadmium": 3, "Mercury": 3, "Molybdenum": 3, "Niobium": 3, "Tin": 3, "Tungsten": 3,
    "Antimony": 4, "Polonium": 4, "Ruthenium": 4, "Selenium": 4, "Technetium": 4, "Tellurium": 4,
    "Yttrium": 4,
}

_MATERIAL_ALIASES: Dict[str, str] = {"sulfur": "Sulphur"}


def normalize_material(name: str) -> Optional[str]:
    """Map user input to the exact Spansh raw material name, or None if unknown."""
    key = (name or "").strip().lower()
    if not key:
        return None
    if key in _MATERIAL_ALIASES:
        return _MATERIAL_ALIASES[key]
    for material in RAW_MATERIALS:
        if material.lower() == key:
            return material
    return None


def parse_material_list(materials: str) -> Dict[str, Any]:
    """Split a comma-separated material list into canonical names.

    Returns {"materials": [...]} or a structured error object.
    """
    requested = [part.strip() for part in (materials or "").split(",") if part.strip()]
    if not requested:
        return {"error": "No material given", "available_materials": sorted(RAW_MATERIALS)}
    canonical: List[str] = []
    for name in requested:
        material = normalize_material(name)
        if material is None:
            return {
                "error": "Unknown raw material: '%s'" % name,
                "available_materials": sorted(RAW_MATERIALS),
            }
        if material not in canonical:
            canonical.append(material)
    return {"materials": canonical}


def build_material_request(
    reference_system: str,
    materials: List[str],
    min_percent: float,
    max_distance_ly: float,
    sort_by_percent: bool,
    size: int,
) -> Dict[str, Any]:
    """Build the Spansh body search payload for landable bodies with raw materials.

    The first material is the primary one: min_percent and percent sorting
    apply to it. Every listed material must be present on the body.
    """
    request = _base_request(reference_system, max_distance_ly, size)
    request["filters"]["is_landable"] = {"value": True}
    request["filters"]["materials"] = [
        {
            "name": material,
            # The threshold key is "share"; sent as "value" it is ignored.
            "share": [float(min_percent) if index == 0 else 0.0, 100.0],
            "comparison": "<=>",
        }
        for index, material in enumerate(materials)
    ]
    if sort_by_percent:
        request["sort"] = [{"materials": [{"name": materials[0], "direction": "desc"}]}]
    return request


def shape_material_body(body: Dict[str, Any], materials: List[str]) -> Optional[Dict[str, Any]]:
    """Trim a Spansh body to what matters for surface prospecting.

    Returns None when the body lacks any of the requested materials.
    """
    shares = {
        m.get("name"): float(m.get("share") or 0.0)
        for m in body.get("materials") or []
        if m.get("name")
    }
    if any(shares.get(material, 0.0) <= 0.0 for material in materials):
        return None
    arrival = body.get("distance_to_arrival")
    return {
        "system": body.get("system_name"),
        "body": body.get("name"),
        "distance_ly": _round(body.get("distance"), 1),
        "arrival_distance_ls": int(arrival) if arrival is not None else None,
        "body_type": body.get("subtype"),
        "gravity_g": _round(body.get("gravity"), 2),
        "volcanism": body.get("volcanism_type") or "None",
        "geological_signals": _signal_count(body.get("signals"), "Geological"),
        "biological_signals": _signal_count(body.get("signals"), "Biological"),
        "requested_percent": {material: round(shares[material], 2) for material in materials},
        "all_materials_percent": {
            name: round(share, 2) for name, share in sorted(shares.items(), key=lambda kv: -kv[1])
        },
    }


def _signal_count(signals: Optional[List[Dict[str, Any]]], name: str) -> int:
    for signal in signals or []:
        if signal.get("name") == name:
            return int(signal.get("count") or 0)
    return 0


def _round(value: Any, digits: int) -> Optional[float]:
    return round(float(value), digits) if value is not None else None


def _json_or_empty(response: httpx.Response) -> Dict[str, Any]:
    """Parse a response body as a JSON object; anything else becomes {}."""
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def shape_hotspot_body(
    body: Dict[str, Any], commodity: str, min_hotspots: int
) -> Optional[Dict[str, Any]]:
    """Trim a Spansh body to the rings that carry the commodity.

    Returns None when no ring meets the hotspot threshold.
    """
    rings = []
    for ring in body.get("rings") or []:
        count = _signal_count(ring.get("signals"), commodity)
        if count < min_hotspots:
            continue
        rings.append({
            "ring": ring.get("name"),
            "ring_type": ring.get("type"),
            "hotspots": count,
            "other_hotspots": {
                s.get("name"): int(s.get("count") or 0)
                for s in ring.get("signals") or []
                if s.get("name") != commodity
            },
            "signals_updated_at": ring.get("signals_updated_at"),
        })
    if not rings:
        return None
    arrival = body.get("distance_to_arrival")
    return {
        "system": body.get("system_name"),
        "body": body.get("name"),
        "distance_ly": _round(body.get("distance"), 1),
        "arrival_distance_ls": int(arrival) if arrival is not None else None,
        "reserve_level": body.get("reserve_level"),
        "best_hotspot_count": max(r["hotspots"] for r in rings),
        "rings": rings,
    }


def shape_exobiology_body(body: Dict[str, Any]) -> Dict[str, Any]:
    """Trim a Spansh body to what matters for planning an exobiology stop."""
    bio_signals = _signal_count(body.get("signals"), "Biological")
    genera = [g.get("name") for g in body.get("genuses") or [] if g.get("name")]

    species: List[Dict[str, Any]] = []
    seen = set()
    for landmark in body.get("landmarks") or []:
        name = landmark.get("subtype")
        value = int(landmark.get("value") or 0)
        if not name or value <= 0 or name in seen:
            continue
        seen.add(name)
        species.append({"species": name, "value": value})

    identified = max(len(species), len(genera))
    arrival = body.get("distance_to_arrival")
    return {
        "system": body.get("system_name"),
        "body": body.get("name"),
        "distance_ly": _round(body.get("distance"), 1),
        "arrival_distance_ls": int(arrival) if arrival is not None else None,
        "body_type": body.get("subtype"),
        "atmosphere": body.get("atmosphere"),
        "gravity_g": _round(body.get("gravity"), 2),
        "surface_temperature_k": _round(body.get("surface_temperature"), 0),
        "biological_signals": bio_signals,
        "known_genera": genera,
        "known_species": species,
        "known_species_value": sum(s["value"] for s in species),
        "unidentified_signals": max(0, bio_signals - identified),
        "signals_updated_at": body.get("signals_updated_at"),
    }


# Station types with a regular market. Fleet carriers are left out on purpose:
# their market data is often years old and their prices are set by players.
STATION_TYPES_WITHOUT_CARRIERS: List[str] = [
    "Asteroid base", "Coriolis Starport", "Dockable Planet Station", "Mega ship",
    "Ocellus Starport", "Orbis Starport", "Outpost", "Planetary Outpost",
    "Planetary Port", "Settlement", "Surface Settlement",
]
MAX_MARKET_QUANTITY = "999999999"


def is_fleet_carrier(station: Dict[str, Any]) -> bool:
    """Return True for player or squadron fleet carriers."""
    return "carrier" in (station.get("type") or "").lower()


def resolve_commodity_name(name: str, known_names: List[str]) -> Dict[str, Any]:
    """Match user input to an exact Spansh commodity name.

    Spansh matches commodity names case-sensitively and returns nothing for
    a wrong name. Returns {"commodity": name} or an error object with
    suggestions.
    """
    wanted = (name or "").strip()
    if not wanted:
        return {"error": "No commodity given"}
    if not known_names:
        # Name list unavailable: best effort, Spansh names are title case.
        return {"commodity": wanted.title(), "unverified_name": True}
    lowered = {known.lower(): known for known in known_names}
    if wanted.lower() in lowered:
        return {"commodity": lowered[wanted.lower()]}
    close = difflib.get_close_matches(wanted.lower(), list(lowered), n=5, cutoff=0.6)
    partial = [known for low, known in lowered.items() if wanted.lower() in low]
    suggestions = []
    for candidate in [lowered[c] for c in close] + sorted(partial):
        if candidate not in suggestions:
            suggestions.append(candidate)
    return {
        "error": "Unknown commodity: '%s'" % wanted,
        "did_you_mean": suggestions[:8],
    }


def build_commodity_request(
    reference_system: str,
    commodity: str,
    mode: str,
    min_quantity: int,
    max_distance_ly: float,
    large_pad_only: bool,
    include_fleet_carriers: bool,
    updated_since: Optional[str],
    sort_by_price: bool,
    size: int,
) -> Dict[str, Any]:
    """Build the Spansh station search payload for a commodity market.

    mode "buy" finds stations selling the commodity to the commander (supply);
    mode "sell" finds stations buying it from the commander (demand).
    updated_since is an ISO date; markets not seen since then are left out.
    """
    quantity_field = "supply" if mode == "buy" else "demand"
    request = _base_request(reference_system, max_distance_ly, size)
    request["filters"]["market"] = [{
        "name": commodity,
        quantity_field: {
            "value": [str(max(1, int(min_quantity))), MAX_MARKET_QUANTITY],
            "comparison": "<=>",
        },
    }]
    if large_pad_only:
        request["filters"]["has_large_pad"] = {"value": True}
    if not include_fleet_carriers:
        request["filters"]["type"] = {"value": list(STATION_TYPES_WITHOUT_CARRIERS)}
    if updated_since:
        request["filters"]["market_updated_at"] = {
            "value": [updated_since, "2999-12-31"],
            "comparison": "<=>",
        }
    if sort_by_price:
        if mode == "buy":
            request["sort"] = [{"market_buy_price": [{"name": commodity, "direction": "asc"}]}]
        else:
            request["sort"] = [{"market_sell_price": [{"name": commodity, "direction": "desc"}]}]
    return request


def _parse_spansh_time(value: Any) -> Optional[datetime]:
    """Parse a Spansh timestamp into an aware UTC datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def shape_commodity_station(
    station: Dict[str, Any], commodity: str, mode: str, now: datetime
) -> Optional[Dict[str, Any]]:
    """Trim a Spansh station to what matters for one commodity trade.

    Returns None when the station does not list the commodity.
    """
    entry = None
    for item in station.get("market") or []:
        if item.get("commodity") == commodity:
            entry = item
            break
    if entry is None:
        return None
    updated = _parse_spansh_time(station.get("market_updated_at"))
    age_days = round((now - updated).total_seconds() / 86400.0, 1) if updated else None
    arrival = station.get("distance_to_arrival")
    if mode == "buy":
        price = entry.get("buy_price")
        quantity = entry.get("supply")
    else:
        price = entry.get("sell_price")
        quantity = entry.get("demand")
    return {
        "system": station.get("system_name"),
        "station": station.get("name"),
        "station_type": station.get("type"),
        "distance_ly": _round(station.get("distance"), 1),
        "arrival_distance_ls": int(arrival) if arrival is not None else None,
        "has_large_pad": bool(station.get("has_large_pad")),
        "is_planetary": bool(station.get("is_planetary")),
        "is_fleet_carrier": is_fleet_carrier(station),
        "price": int(price) if price is not None else None,
        "quantity": int(quantity) if quantity is not None else None,
        "market_updated_at": station.get("market_updated_at"),
        "data_age_days": age_days,
    }


# Frame shift drive stats by module size, one value per class 1-5 (E to A).
# These are game outfitting values, as published by EDCD coriolis-data.
# Size 8 and special drives are not listed; callers fall back to a plain range.
_FSD_ITEM = re.compile(r"^int_hyperdrive(_overcharge)?_size(\d)_class(\d)$")
_FSD_FUEL_POWER: Dict[int, float] = {2: 2.0, 3: 2.15, 4: 2.3, 5: 2.45, 6: 2.6, 7: 2.75}
_FSD_FUEL_MULTIPLIER: List[float] = [0.011, 0.010, 0.008, 0.010, 0.012]
_SCO_FUEL_MULTIPLIER: List[float] = [0.008, 0.012, 0.012, 0.012, 0.013]
_FSD_OPTIMAL_MASS: Dict[int, List[float]] = {
    2: [48, 54, 60, 75, 90],
    3: [80, 90, 100, 125, 150],
    4: [280, 315, 350, 437.5, 525],
    5: [560, 630, 700, 875, 1050],
    6: [960, 1080, 1200, 1500, 1800],
    7: [1440, 1620, 1800, 2250, 2700],
}
_FSD_MAX_FUEL: Dict[int, List[float]] = {
    2: [0.6, 0.6, 0.6, 0.8, 0.9],
    3: [1.2, 1.2, 1.2, 1.5, 1.8],
    4: [2.0, 2.0, 2.0, 2.5, 3.0],
    5: [3.3, 3.3, 3.3, 4.1, 5.0],
    6: [5.3, 5.3, 5.3, 6.6, 8.0],
    7: [8.5, 8.5, 8.5, 10.6, 12.8],
}
_SCO_OPTIMAL_MASS: Dict[int, List[float]] = {
    2: [60, 90, 90, 90, 100],
    3: [100, 150, 150, 150, 167],
    4: [350, 525, 525, 525, 585],
    5: [700, 1050, 1050, 1050, 1175],
    6: [1200, 1800, 1800, 1800, 2000],
    7: [1800, 2700, 2700, 2700, 3000],
}
_SCO_MAX_FUEL: Dict[int, List[float]] = {
    2: [0.6, 0.9, 0.9, 0.9, 1.0],
    3: [1.2, 1.8, 1.8, 1.8, 1.9],
    4: [2.0, 3.0, 3.0, 3.0, 3.2],
    5: [3.3, 5.0, 5.0, 5.0, 5.2],
    6: [5.3, 8.0, 8.0, 8.0, 8.3],
    7: [8.5, 12.8, 12.8, 12.8, 13.1],
}
_GUARDIAN_BOOST_LY: Dict[str, float] = {
    "int_guardianfsdbooster_size1": 4.0,
    "int_guardianfsdbooster_size2": 6.0,
    "int_guardianfsdbooster_size3": 7.75,
    "int_guardianfsdbooster_size4": 9.25,
    "int_guardianfsdbooster_size5": 10.5,
}


def ship_from_loadout(loadout: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Read the jump and fuel parameters Spansh needs from a journal Loadout event.

    Returns the parameters, or a structured error object when the Loadout is
    missing or carries a drive this module has no stats for.
    """
    modules = (loadout or {}).get("Modules")
    if not isinstance(modules, list):
        return {"error": "No ship Loadout with modules is available from the journal"}
    drive = None
    range_boost = 0.0
    has_scoop = False
    for module in modules:
        item = str(module.get("Item") or "").lower()
        if str(module.get("Slot") or "").lower() == "frameshiftdrive":
            drive = module
        elif item in _GUARDIAN_BOOST_LY:
            range_boost = _GUARDIAN_BOOST_LY[item]
        elif "fuelscoop" in item:
            has_scoop = True
    if drive is None:
        return {"error": "The Loadout does not list a frame shift drive"}
    item = str(drive.get("Item") or "").lower()
    match = _FSD_ITEM.match(item)
    size = int(match.group(2)) if match else 0
    rating = int(match.group(3)) - 1 if match else -1
    if size not in _FSD_FUEL_POWER or not 0 <= rating <= 4:
        return {"error": "No fuel data for frame shift drive '%s'" % item}
    sco = bool(match.group(1))
    optimal_mass = (_SCO_OPTIMAL_MASS if sco else _FSD_OPTIMAL_MASS)[size][rating]
    max_fuel = (_SCO_MAX_FUEL if sco else _FSD_MAX_FUEL)[size][rating]
    for modifier in (drive.get("Engineering") or {}).get("Modifiers") or []:
        if modifier.get("Label") == "FSDOptimalMass":
            optimal_mass = modifier.get("Value")
        elif modifier.get("Label") == "MaxFuelPerJump":
            max_fuel = modifier.get("Value")
    capacity = loadout.get("FuelCapacity") or {}
    try:
        return {
            "fuel_power": _FSD_FUEL_POWER[size],
            "fuel_multiplier": (_SCO_FUEL_MULTIPLIER if sco else _FSD_FUEL_MULTIPLIER)[rating],
            "optimal_mass": float(optimal_mass),
            "max_fuel_per_jump": float(max_fuel),
            "base_mass": float(loadout["UnladenMass"]),
            "tank_size": float(capacity["Main"]),
            "internal_tank_size": float(capacity.get("Reserve") or 0.0),
            "range_boost": range_boost,
            "has_fuel_scoop": has_scoop,
        }
    except (KeyError, TypeError, ValueError):
        return {"error": "The Loadout is missing mass or fuel capacity values"}


def laden_jump_range(ship: Dict[str, Any], cargo_t: float) -> float:
    """Jump range in light years with a full fuel tank and cargo_t tonnes of cargo.

    ship is the result of ship_from_loadout.
    """
    base = (ship["max_fuel_per_jump"] / ship["fuel_multiplier"]) ** (1.0 / ship["fuel_power"])
    mass = ship["base_mass"] + ship["tank_size"] + max(0.0, float(cargo_t))
    return base * ship["optimal_mass"] / mass + ship["range_boost"]


def shape_simple_route(result: Dict[str, Any]) -> Dict[str, Any]:
    """Trim a Spansh neutron plotter result to waypoints worth plotting to."""
    waypoints = [
        {
            "system": row.get("system"),
            "neutron_star": bool(row.get("neutron_star")),
            "jumps": int(row.get("jumps") or 0),
            "distance_ly": _round(row.get("distance_jumped") or 0.0, 1),
            "remaining_ly": _round(row.get("distance_left") or 0.0, 1),
        }
        for row in result.get("system_jumps") or []
    ]
    return {
        "mode": "simple",
        "origin": result.get("source_system"),
        "destination": result.get("destination_system"),
        "distance_ly": _round(result.get("distance") or 0.0, 1),
        "total_jumps": int(result.get("total_jumps") or 0),
        "neutron_boosts": sum(1 for w in waypoints if w["neutron_star"]),
        "search": {"jump_range_ly": result.get("range"), "efficiency": result.get("efficiency")},
        "waypoints": waypoints,
        "notes": [
            "Each waypoint is a system to plot to in the galaxy map; 'jumps' is the "
            "number of jumps the in-game plotter needs to reach it from the previous one.",
            "Supercharge at every waypoint marked neutron_star before jumping on.",
            "This mode does not model fuel. Scoop on the ordinary jumps between waypoints.",
        ],
        "source": "spansh.co.uk",
    }


def shape_fuel_route(result: Dict[str, Any], origin: str, destination: str) -> Dict[str, Any]:
    """Trim a Spansh galaxy plotter result; every row is one jump."""
    waypoints = [
        {
            "system": row.get("name"),
            "distance_ly": _round(row.get("distance") or 0.0, 1),
            "remaining_ly": _round(row.get("distance_to_destination") or 0.0, 1),
            "fuel_in_tank_t": _round(row.get("fuel_in_tank") or 0.0, 2),
            "fuel_used_t": _round(row.get("fuel_used") or 0.0, 2),
            "neutron_star": bool(row.get("has_neutron")),
            "scoopable": bool(row.get("is_scoopable")),
            "must_refuel": bool(row.get("must_refuel")),
        }
        for row in result.get("jumps") or []
    ]
    return {
        "mode": "fuel",
        "origin": origin,
        "destination": destination,
        "distance_ly": _round(sum(w["distance_ly"] for w in waypoints), 1),
        "total_jumps": max(0, len(waypoints) - 1),
        "neutron_boosts": sum(1 for w in waypoints if w["neutron_star"]),
        "refuel_stops": sum(1 for w in waypoints if w["must_refuel"]),
        "waypoints": waypoints,
        "notes": [
            "Every waypoint is a single jump. fuel_in_tank_t is the fuel at that "
            "system, after scooping where must_refuel is true.",
            "Supercharge at every waypoint marked neutron_star before jumping on.",
            "Scoop to full wherever must_refuel is true; the next jumps count on it.",
            "Fuel is modelled for an empty cargo hold.",
        ],
        "source": "spansh.co.uk",
    }


class SpanshClient:
    """Minimal async client for the Spansh searches and route plotters."""

    def __init__(
        self,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        timeout: float = 30.0,
        poll_interval: float = 2.0,
        route_timeout: float = 120.0,
    ):
        """
        Args:
            transport: Optional httpx transport, used by tests to mock HTTP.
            timeout: Request timeout in seconds.
            poll_interval: Seconds between checks on a queued route job.
            route_timeout: Seconds to wait for a route job before giving up.
        """
        self._transport = transport
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._route_timeout = route_timeout
        self._commodity_names: Optional[List[str]] = None

    async def _run_route_job(self, path: str, form: Dict[str, Any]) -> Dict[str, Any]:
        """Submit a route job and wait for it. Returns the result or an error object."""
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                response = await client.post(SPANSH_BASE_URL + path, data=form)
                submitted = _json_or_empty(response)
                job = submitted.get("job")
                if response.status_code >= 400 or not job:
                    return {"error": "Spansh rejected the route: %s" % (
                        submitted.get("error") or "HTTP %d" % response.status_code)}
                deadline = time.monotonic() + self._route_timeout
                while True:
                    response = await client.get("%s/results/%s" % (SPANSH_BASE_URL, job))
                    data = _json_or_empty(response)
                    if data.get("error"):
                        return {"error": "Spansh could not plot the route: %s" % data["error"]}
                    if data.get("status") == "ok" and isinstance(data.get("result"), dict):
                        return data["result"]
                    if response.status_code >= 400 and response.status_code not in (502, 503, 504):
                        return {"error": "Spansh returned HTTP %d" % response.status_code}
                    if time.monotonic() >= deadline:
                        return {"error": "Spansh did not finish the route within %d seconds. "
                                         "Try again shortly." % int(self._route_timeout)}
                    await asyncio.sleep(self._poll_interval)
        except httpx.HTTPError as exc:
            logger.error("Spansh route request failed: %s", exc)
            return {"error": "Could not reach Spansh: %s" % (str(exc) or type(exc).__name__)}

    async def plot_neutron_route(
        self,
        origin: str,
        destination: str,
        jump_range_ly: float,
        efficiency: int,
        ship: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Plot a neutron-highway route between two systems.

        With ship (from ship_from_loadout) the galaxy plotter models fuel for
        every jump. Without it the neutron plotter is used with jump_range_ly.
        """
        if ship is not None:
            form: Dict[str, Any] = {
                key: value for key, value in ship.items() if key != "has_fuel_scoop"
            }
            form.update({
                "source": origin,
                "destination": destination,
                "is_supercharged": 0,
                "use_supercharge": 1,
                "use_injections": 0,
                "exclude_secondary": 1,
                "refuel_every_scoopable": 1,
                "cargo": 0,
                "algorithm": "optimistic",
            })
            result = await self._run_route_job("/generic/route", form)
            if "error" in result:
                return result
            return shape_fuel_route(result, origin, destination)
        efficiency = max(1, min(int(efficiency), 100))
        form = {
            "from": origin,
            "to": destination,
            "range": float(jump_range_ly),
            "efficiency": efficiency,
        }
        result = await self._run_route_job("/route", form)
        if "error" in result:
            return result
        return shape_simple_route(result)

    async def _search_bodies(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST a body search. Returns the parsed response or an error object."""
        return await self._post_search("/bodies/search", payload)

    async def _post_search(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST a Spansh search. Returns the parsed response or an error object."""
        url = SPANSH_BASE_URL + path
        reference = payload.get("reference_system")
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                response = await client.post(url, json=payload)
                # Spansh sits behind a gateway that occasionally answers
                # 502/503/504 for a moment. One retry clears most of them.
                if response.status_code in (502, 503, 504):
                    response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            logger.error("Spansh request failed: %s", exc)
            return {"error": "Could not reach Spansh: %s" % (str(exc) or type(exc).__name__)}

        if response.status_code == 400:
            return {
                "error": "Spansh rejected the search. The reference system '%s' is "
                         "probably not known to Spansh." % reference
            }
        if response.status_code != 200:
            return {"error": "Spansh returned HTTP %d" % response.status_code}
        try:
            data = response.json()
        except ValueError:
            return {"error": "Spansh returned a response that is not JSON"}
        if not isinstance(data, dict) or "results" not in data:
            return {"error": "Spansh returned an unexpected response shape"}
        return data

    async def find_ring_hotspots(
        self,
        reference_system: str,
        commodity: str,
        min_hotspots: int,
        max_distance_ly: float,
        pristine_only: bool,
        limit: int,
    ) -> Dict[str, Any]:
        """Find the nearest rings with hotspots for a commodity."""
        canonical = normalize_commodity(commodity)
        if canonical is None:
            return {
                "error": "Unknown hotspot commodity: '%s'" % commodity,
                "available_commodities": sorted(HOTSPOT_COMMODITIES),
            }
        min_hotspots = max(1, int(min_hotspots))
        limit = max(1, min(int(limit), MAX_RESULTS))
        payload = build_hotspot_request(
            reference_system, canonical, min_hotspots, max_distance_ly, pristine_only, limit
        )
        data = await self._search_bodies(payload)
        if "error" in data:
            return data

        results = []
        for body in data["results"]:
            if float(body.get("distance") or 0.0) > max_distance_ly:
                continue
            if pristine_only and body.get("reserve_level") != "Pristine":
                continue
            shaped = shape_hotspot_body(body, canonical, min_hotspots)
            if shaped is not None:
                results.append(shaped)
        results = results[:limit]
        return {
            "reference_system": reference_system,
            "commodity": canonical,
            "mining_method": HOTSPOT_COMMODITIES[canonical]["method"],
            "search": {
                "min_hotspots": min_hotspots,
                "max_distance_ly": max_distance_ly,
                "pristine_only": pristine_only,
            },
            "result_count": len(results),
            "results": results,
            "notes": [
                "Hotspot counts are per ring. Spansh does not record whether hotspots overlap.",
                "Data is community-sourced via EDDN and may be out of date.",
            ],
            "source": "spansh.co.uk",
        }

    async def find_exobiology_bodies(
        self,
        reference_system: str,
        min_bio_signals: int,
        max_distance_ly: float,
        max_gravity_g: float,
        max_arrival_ls: float,
        limit: int,
    ) -> Dict[str, Any]:
        """Find the nearest landable bodies with biological signals.

        A max_gravity_g or max_arrival_ls of 0 means no limit.
        """
        min_bio_signals = max(1, int(min_bio_signals))
        limit = max(1, min(int(limit), MAX_RESULTS))
        # Gravity and arrival distance are filtered client-side, so over-fetch.
        has_local_filter = max_gravity_g > 0 or max_arrival_ls > 0
        size = MAX_RESULTS if has_local_filter else limit
        payload = build_exobiology_request(reference_system, min_bio_signals, max_distance_ly, size)
        data = await self._search_bodies(payload)
        if "error" in data:
            return data

        results = []
        for body in data["results"]:
            if float(body.get("distance") or 0.0) > max_distance_ly:
                continue
            shaped = shape_exobiology_body(body)
            if shaped["biological_signals"] < min_bio_signals:
                continue
            if max_gravity_g > 0 and (shaped["gravity_g"] or 0.0) > max_gravity_g:
                continue
            if max_arrival_ls > 0 and (shaped["arrival_distance_ls"] or 0) > max_arrival_ls:
                continue
            results.append(shaped)
        results = results[:limit]
        return {
            "reference_system": reference_system,
            "search": {
                "min_bio_signals": min_bio_signals,
                "max_distance_ly": max_distance_ly,
                "max_gravity_g": max_gravity_g,
                "max_arrival_ls": max_arrival_ls,
            },
            "result_count": len(results),
            "results": results,
            "notes": [
                "known_species_value sums base scan values of species already reported. "
                "The first-logged bonus is not included.",
                "Spansh only lists bodies someone has already scanned, so these are not "
                "undiscovered. First footfall status is not recorded.",
            ],
            "source": "spansh.co.uk",
        }

    async def find_material_bodies(
        self,
        reference_system: str,
        materials: str,
        min_percent: float,
        max_distance_ly: float,
        max_gravity_g: float,
        max_arrival_ls: float,
        sort_by: str,
        limit: int,
    ) -> Dict[str, Any]:
        """Find landable bodies carrying one or more raw materials.

        materials is a comma-separated list; the first is the primary one.
        sort_by is "distance" or "percent" (of the primary material).
        A max_gravity_g or max_arrival_ls of 0 means no limit.
        """
        parsed = parse_material_list(materials)
        if "error" in parsed:
            return parsed
        wanted = parsed["materials"]
        sort_key = (sort_by or "").strip().lower() or "distance"
        if sort_key not in ("distance", "percent"):
            return {"error": "sort_by must be 'distance' or 'percent', got '%s'" % sort_by}
        min_percent = max(0.0, float(min_percent))
        limit = max(1, min(int(limit), MAX_RESULTS))
        has_local_filter = max_gravity_g > 0 or max_arrival_ls > 0
        size = MAX_RESULTS if has_local_filter else limit
        payload = build_material_request(
            reference_system, wanted, min_percent, max_distance_ly, sort_key == "percent", size
        )
        data = await self._search_bodies(payload)
        if "error" in data:
            return data

        primary = wanted[0]
        results = []
        for body in data["results"]:
            if float(body.get("distance") or 0.0) > max_distance_ly:
                continue
            shaped = shape_material_body(body, wanted)
            if shaped is None or shaped["requested_percent"][primary] < min_percent:
                continue
            if max_gravity_g > 0 and (shaped["gravity_g"] or 0.0) > max_gravity_g:
                continue
            if max_arrival_ls > 0 and (shaped["arrival_distance_ls"] or 0) > max_arrival_ls:
                continue
            results.append(shaped)
        results = results[:limit]
        return {
            "reference_system": reference_system,
            "materials": [{"name": m, "grade": RAW_MATERIALS[m]} for m in wanted],
            "search": {
                "min_percent_of_primary": min_percent,
                "max_distance_ly": max_distance_ly,
                "max_gravity_g": max_gravity_g,
                "max_arrival_ls": max_arrival_ls,
                "sort_by": sort_key,
            },
            "result_count": len(results),
            "results": results,
            "notes": [
                "Percentages are the share of the body's surface composition. Higher "
                "share means more frequent drops from surface rocks.",
                "Bodies with geological signals have sites that drop materials in "
                "larger quantities.",
            ],
            "source": "spansh.co.uk",
        }

    async def get_commodity_names(self) -> List[str]:
        """Return every commodity name Spansh knows. Cached; empty on failure."""
        if self._commodity_names is not None:
            return self._commodity_names
        url = SPANSH_BASE_URL + "/stations/field_values/market"
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                response = await client.get(url)
            names = sorted((response.json().get("min_max") or {}).keys())
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            logger.warning("Could not load Spansh commodity names: %s", exc)
            return []
        if response.status_code != 200 or not names:
            return []
        self._commodity_names = names
        return names

    async def find_commodity_stations(
        self,
        reference_system: str,
        commodity: str,
        mode: str,
        min_quantity: int,
        max_distance_ly: float,
        large_pad_only: bool,
        include_fleet_carriers: bool,
        max_data_age_days: float,
        max_arrival_ls: float,
        sort_by: str,
        limit: int,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Find stations where a commodity can be bought or sold.

        mode is "buy" or "sell". sort_by is "distance" or "price" (cheapest
        when buying, highest when selling). A max_data_age_days or
        max_arrival_ls of 0 means no limit.
        """
        trade = (mode or "").strip().lower() or "buy"
        if trade not in ("buy", "sell"):
            return {"error": "mode must be 'buy' or 'sell', got '%s'" % mode}
        sort_key = (sort_by or "").strip().lower() or "distance"
        if sort_key not in ("distance", "price"):
            return {"error": "sort_by must be 'distance' or 'price', got '%s'" % sort_by}

        resolved = resolve_commodity_name(commodity, await self.get_commodity_names())
        if "error" in resolved:
            return resolved
        name = resolved["commodity"]

        now = now or datetime.now(timezone.utc)
        min_quantity = max(1, int(min_quantity))
        limit = max(1, min(int(limit), MAX_RESULTS))
        updated_since = None
        if max_data_age_days > 0:
            updated_since = (now - timedelta(days=max_data_age_days)).strftime("%Y-%m-%d")
        size = MAX_RESULTS if max_arrival_ls > 0 else limit
        payload = build_commodity_request(
            reference_system, name, trade, min_quantity, max_distance_ly, large_pad_only,
            include_fleet_carriers, updated_since, sort_key == "price", size,
        )
        data = await self._post_search("/stations/search", payload)
        if "error" in data:
            return data

        results = []
        for station in data["results"]:
            if float(station.get("distance") or 0.0) > max_distance_ly:
                continue
            shaped = shape_commodity_station(station, name, trade, now)
            if shaped is None or (shaped["quantity"] or 0) < min_quantity:
                continue
            if large_pad_only and not shaped["has_large_pad"]:
                continue
            if not include_fleet_carriers and shaped["is_fleet_carrier"]:
                continue
            if max_data_age_days > 0 and (
                shaped["data_age_days"] is None or shaped["data_age_days"] > max_data_age_days + 1
            ):
                continue
            if max_arrival_ls > 0 and (shaped["arrival_distance_ls"] or 0) > max_arrival_ls:
                continue
            results.append(shaped)
        results = results[:limit]
        response = {
            "reference_system": reference_system,
            "commodity": name,
            "mode": trade,
            "price_meaning": (
                "credits per unit the commander pays" if trade == "buy"
                else "credits per unit the commander receives"
            ),
            "quantity_meaning": "units in stock" if trade == "buy" else "units of demand",
            "search": {
                "min_quantity": min_quantity,
                "max_distance_ly": max_distance_ly,
                "large_pad_only": large_pad_only,
                "include_fleet_carriers": include_fleet_carriers,
                "max_data_age_days": max_data_age_days,
                "max_arrival_ls": max_arrival_ls,
                "sort_by": sort_key,
            },
            "result_count": len(results),
            "results": results,
            "notes": [
                "Prices and stock are the last values a player uploaded. Check "
                "data_age_days; markets move.",
            ],
            "source": "spansh.co.uk",
        }
        if resolved.get("unverified_name"):
            response["notes"].append(
                "The commodity name could not be checked against Spansh. An empty "
                "result may mean the name is wrong."
            )
        return response
