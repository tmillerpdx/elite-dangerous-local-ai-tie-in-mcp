"""Spansh API client

Read-only client for the public Spansh body search (https://spansh.co.uk),
used to find ring hotspots and exobiology candidates near a reference system.

Notes on the Spansh search API, verified against the live service:
- Signal thresholds must be sent as "count": [min, max]. If they are sent as
  "value" the name still matches but the threshold is silently ignored.
- Unknown filter keys are silently ignored, so every limit is re-applied
  client-side after the response arrives.
- An unknown reference system returns HTTP 400 {"error": "Invalid request"}.
"""

import logging
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


def _signal_count(signals: Optional[List[Dict[str, Any]]], name: str) -> int:
    for signal in signals or []:
        if signal.get("name") == name:
            return int(signal.get("count") or 0)
    return 0


def _round(value: Any, digits: int) -> Optional[float]:
    return round(float(value), digits) if value is not None else None


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


class SpanshClient:
    """Minimal async client for the Spansh body search."""

    def __init__(self, transport: Optional[httpx.AsyncBaseTransport] = None, timeout: float = 30.0):
        """
        Args:
            transport: Optional httpx transport, used by tests to mock HTTP.
            timeout: Request timeout in seconds.
        """
        self._transport = transport
        self._timeout = timeout

    async def _search_bodies(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST a body search. Returns the parsed response or an error object."""
        url = SPANSH_BASE_URL + "/bodies/search"
        reference = payload.get("reference_system")
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout,
                headers={"User-Agent": USER_AGENT},
            ) as client:
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
