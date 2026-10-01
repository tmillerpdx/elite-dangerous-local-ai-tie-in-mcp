"""
Elite Dangerous MCP Tools - Core functionality for AI assistant integration.

This module provides comprehensive MCP tools for querying and analyzing Elite Dangerous
journal data, including location tracking, activity summaries, and detailed analytics.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional, Set
from enum import Enum

try:
    from ..journal.events import EventCategory, ProcessedEvent
    from ..utils.data_store import EventFilter, QuerySortOrder, GameState
    from ..utils.spansh_client import SpanshClient, laden_jump_range, ship_from_loadout
    from ..utils.trade_dangerous import TradeDangerousClient
    from ..utils.wmm import (
        REPUTATION_EVENT_TYPES, STACK_EVENT_TYPES, WMM_SYSTEMS, build_wmm_stack,
        faction_reputation, read_recent_journal_events
    )
    from ..utils.inventory import (
        MATERIAL_CHANGE_EVENTS, compute_material_inventory, latest_by_timestamp, summarize_loadout
    )
except ImportError:
    from src.journal.events import EventCategory, ProcessedEvent
    from src.utils.data_store import EventFilter, QuerySortOrder, GameState
    from src.utils.spansh_client import SpanshClient, laden_jump_range, ship_from_loadout
    from src.utils.trade_dangerous import TradeDangerousClient
    from src.utils.wmm import (
        REPUTATION_EVENT_TYPES, STACK_EVENT_TYPES, WMM_SYSTEMS, build_wmm_stack,
        faction_reputation, read_recent_journal_events
    )
    from src.utils.inventory import (
        MATERIAL_CHANGE_EVENTS, compute_material_inventory, latest_by_timestamp, summarize_loadout
    )

logger = logging.getLogger(__name__)


class ActivityType(Enum):
    """Types of player activities for summary generation."""
    EXPLORATION = "exploration"
    TRADING = "trading"
    COMBAT = "combat"
    MINING = "mining"
    MISSIONS = "missions"
    ENGINEERING = "engineering"
    PASSENGER = "passenger"
    FLEET_CARRIER = "fleet_carrier"


class MCPTools:
    """
    Core MCP tools for Elite Dangerous data access and analysis.
    
    Provides comprehensive tools for:
    - Current game state queries
    - Event searching and filtering
    - Activity summaries and analytics
    - Journey tracking and navigation history
    - Performance metrics and statistics
    """
    
    def __init__(self, data_store, spansh_client=None, trade_client=None, journal_reader=None):
        """
        Initialize MCP tools with data store reference.

        Args:
            data_store: Reference to the global data store
            spansh_client: Optional SpanshClient, injectable for tests
            trade_client: Optional TradeDangerousClient, injectable for tests
            journal_reader: Optional callable (days, event_types) -> raw journal
                events in time order, injectable for tests
        """
        self.data_store = data_store
        self.spansh_client = spansh_client or SpanshClient()
        self.trade_client = trade_client or TradeDangerousClient()
        self.journal_reader = journal_reader or self._read_journal_files
        logger.info("MCP Tools initialized")

    # ==================== Nearby Search Tools (Spansh) ====================

    def _resolve_reference_system(self, reference_system: str) -> Dict[str, Any]:
        """
        Pick the system to search around.

        An empty string selects the commander's current system from the journal.
        Returns {"system", "source"} or a structured error object.
        """
        explicit = (reference_system or "").strip()
        if explicit:
            return {"system": explicit, "source": "argument"}
        current = self.data_store.get_game_state().current_system
        if not current or current == "Unknown":
            return {
                "error": "Current system is not known from the journal yet. "
                         "Pass reference_system explicitly."
            }
        return {"system": current, "source": "current_location"}

    async def find_mining_hotspots(
        self,
        commodity: str = "Platinum",
        reference_system: str = "",
        min_hotspots: int = 1,
        max_distance_ly: float = 100.0,
        pristine_only: bool = False,
        limit: int = 10
    ) -> Dict[str, Any]:
        """
        Find the nearest ring hotspots for a commodity using Spansh.

        Args:
            commodity: Hotspot commodity; empty string selects Platinum
            reference_system: System to search around; empty selects current system
            min_hotspots: Minimum hotspots of the commodity in a single ring
            max_distance_ly: Search radius in light years
            pristine_only: Only return rings with pristine reserves
            limit: Maximum bodies to return

        Returns:
            Dict with ranked ring candidates, or a structured error object
        """
        try:
            reference = self._resolve_reference_system(reference_system)
            if "error" in reference:
                return reference
            result = await self.spansh_client.find_ring_hotspots(
                reference["system"],
                (commodity or "").strip() or "Platinum",
                min_hotspots,
                float(max_distance_ly),
                bool(pristine_only),
                limit
            )
            if "error" not in result:
                result["reference_source"] = reference["source"]
            return result
        except Exception as e:
            logger.error(f"Error finding mining hotspots: {e}")
            return {"error": str(e)}

    async def find_exobiology_targets(
        self,
        reference_system: str = "",
        min_bio_signals: int = 2,
        max_distance_ly: float = 50.0,
        max_gravity_g: float = 0.0,
        max_arrival_ls: float = 0.0,
        limit: int = 10
    ) -> Dict[str, Any]:
        """
        Find the nearest landable bodies with biological signals using Spansh.

        Args:
            reference_system: System to search around; empty selects current system
            min_bio_signals: Minimum biological signal count on the body
            max_distance_ly: Search radius in light years
            max_gravity_g: Skip bodies above this gravity; 0 means no limit
            max_arrival_ls: Skip bodies further than this from arrival; 0 means no limit
            limit: Maximum bodies to return

        Returns:
            Dict with ranked body candidates, or a structured error object
        """
        try:
            reference = self._resolve_reference_system(reference_system)
            if "error" in reference:
                return reference
            result = await self.spansh_client.find_exobiology_bodies(
                reference["system"],
                min_bio_signals,
                float(max_distance_ly),
                float(max_gravity_g),
                float(max_arrival_ls),
                limit
            )
            if "error" not in result:
                result["reference_source"] = reference["source"]
            return result
        except Exception as e:
            logger.error(f"Error finding exobiology targets: {e}")
            return {"error": str(e)}

    async def find_material_bodies(
        self,
        materials: str,
        reference_system: str = "",
        min_percent: float = 0.0,
        max_distance_ly: float = 50.0,
        max_gravity_g: float = 0.0,
        max_arrival_ls: float = 0.0,
        sort_by: str = "distance",
        limit: int = 10
    ) -> Dict[str, Any]:
        """
        Find landable bodies carrying raw engineering materials using Spansh.

        Args:
            materials: Comma-separated raw materials; the first is the primary one
            reference_system: System to search around; empty selects current system
            min_percent: Minimum surface share of the primary material
            max_distance_ly: Search radius in light years
            max_gravity_g: Skip bodies above this gravity; 0 means no limit
            max_arrival_ls: Skip bodies further than this from arrival; 0 means no limit
            sort_by: "distance" or "percent"; empty string selects distance
            limit: Maximum bodies to return

        Returns:
            Dict with ranked body candidates, or a structured error object
        """
        try:
            reference = self._resolve_reference_system(reference_system)
            if "error" in reference:
                return reference
            result = await self.spansh_client.find_material_bodies(
                reference["system"],
                materials or "",
                float(min_percent),
                float(max_distance_ly),
                float(max_gravity_g),
                float(max_arrival_ls),
                sort_by or "",
                limit
            )
            if "error" not in result:
                result["reference_source"] = reference["source"]
            return result
        except Exception as e:
            logger.error(f"Error finding material bodies: {e}")
            return {"error": str(e)}

    async def find_commodity_market(
        self,
        commodity: str,
        mode: str = "buy",
        reference_system: str = "",
        min_quantity: int = 1,
        max_distance_ly: float = 50.0,
        large_pad_only: bool = False,
        include_fleet_carriers: bool = False,
        max_data_age_days: float = 30.0,
        max_arrival_ls: float = 0.0,
        sort_by: str = "distance",
        limit: int = 10
    ) -> Dict[str, Any]:
        """
        Find stations where a commodity can be bought or sold, using Spansh.

        Args:
            commodity: Commodity name, any capitalisation
            mode: "buy" or "sell"; empty string selects buy
            reference_system: System to search around; empty selects current system
            min_quantity: Minimum stock (buy) or demand (sell)
            max_distance_ly: Search radius in light years
            large_pad_only: Only stations with a large landing pad
            include_fleet_carriers: Include player fleet carriers
            max_data_age_days: Skip markets not updated within this many days; 0 means no limit
            max_arrival_ls: Skip stations further than this from arrival; 0 means no limit
            sort_by: "distance" or "price"; empty string selects distance
            limit: Maximum stations to return

        Returns:
            Dict with ranked stations, or a structured error object
        """
        try:
            reference = self._resolve_reference_system(reference_system)
            if "error" in reference:
                return reference
            result = await self.spansh_client.find_commodity_stations(
                reference["system"],
                commodity or "",
                mode or "",
                min_quantity,
                float(max_distance_ly),
                bool(large_pad_only),
                bool(include_fleet_carriers),
                float(max_data_age_days),
                float(max_arrival_ls),
                sort_by or "",
                limit
            )
            if "error" not in result:
                result["reference_source"] = reference["source"]
            return result
        except Exception as e:
            logger.error(f"Error finding commodity market: {e}")
            return {"error": str(e)}

    async def plot_neutron_route(
        self,
        destination: str,
        origin: str = "",
        jump_range_ly: float = 0.0,
        efficiency: int = 60,
        mode: str = ""
    ) -> Dict[str, Any]:
        """
        Plot a neutron-highway route using Spansh.

        Args:
            destination: System to travel to
            origin: System to start from; empty selects current system
            jump_range_ly: Jump range to plan with; 0 reads the ship from the journal
            efficiency: Neutron plotter efficiency percent (simple mode only)
            mode: "fuel", "simple", or empty to pick from what the journal offers

        Returns:
            Dict with the route waypoints, or a structured error object
        """
        try:
            target = (destination or "").strip()
            if not target:
                return {"error": "No destination given"}
            wanted = (mode or "").strip().lower()
            if wanted not in ("", "fuel", "simple"):
                return {"error": "mode must be 'fuel' or 'simple', got '%s'" % mode}
            start = self._resolve_reference_system(origin)
            if "error" in start:
                return start

            # No limit: storage order is not time order, so pick the newest by timestamp.
            latest_loadout = latest_by_timestamp(self.data_store.get_events_by_type("Loadout"))
            loadout = (latest_loadout.raw_event or {}) if latest_loadout is not None else {}
            jump_range = float(jump_range_ly or 0.0)
            ship = None
            ship_source = "argument"
            extra_notes: List[str] = []

            if wanted == "fuel" or (wanted == "" and jump_range <= 0):
                parsed = ship_from_loadout(loadout)
                if "error" not in parsed:
                    ship = parsed
                    ship_source = "journal_loadout"
                elif wanted == "fuel":
                    return {"error": "Fuel mode needs the ship from the journal: " + parsed["error"]}
                else:
                    extra_notes.append("Fuel was not modelled: " + parsed["error"] + ".")
            if ship is None and jump_range <= 0:
                jump_range = float(loadout.get("MaxJumpRange") or 0.0)
                ship_source = "journal_max_jump_range"
                if jump_range <= 0:
                    return {
                        "error": "The ship's jump range is not known from the journal yet. "
                                 "Pass jump_range_ly explicitly."
                    }
                extra_notes.append(
                    "Planned with the journal's maximum jump range, which assumes a "
                    "nearly empty tank. Pass a lower jump_range_ly for a safer plan."
                )

            result = await self.spansh_client.plot_neutron_route(
                start["system"], target, jump_range, efficiency, ship
            )
            if "error" not in result:
                result["origin_source"] = start["source"]
                result["ship_source"] = ship_source
                result["notes"].extend(extra_notes)
                if ship is not None and not ship["has_fuel_scoop"]:
                    result["notes"].append(
                        "The ship has no fuel scoop fitted; refuel stops cannot be used."
                    )
            return result
        except Exception as e:
            logger.error(f"Error plotting neutron route: {e}")
            return {"error": str(e)}

    # ==================== Trade Planning (Trade Dangerous) ====================

    async def plan_trade_route(
        self,
        origin: str = "",
        destination: str = "",
        credits: int = 0,
        cargo_capacity: int = 0,
        jump_range_ly: float = 0.0,
        hops: int = 2,
        max_jumps_per_hop: int = 0,
        max_data_age_days: float = 2.0,
        pad_size: str = "",
        routes: int = 1
    ) -> Dict[str, Any]:
        """
        Plan the most profitable multi-stop trade run using Trade Dangerous.

        Args:
            origin: "System/Station" or system to start from; empty selects the
                current station when docked, otherwise the current system
            destination: Optional place the run must end at
            credits: Credits to trade with; 0 reads the journal
            cargo_capacity: Cargo hold in tonnes; 0 reads the journal Loadout
            jump_range_ly: Jump range; 0 computes the laden range from the Loadout
            hops: Number of station-to-station trades
            max_jumps_per_hop: Jumps allowed between stations; 0 lets Trade Dangerous decide
            max_data_age_days: Ignore prices older than this; 0 means no limit
            pad_size: "S", "M" or "L"; empty reads the ship from the journal
            routes: Number of alternative routes to return

        Returns:
            Dict with planned routes, or a structured error object
        """
        try:
            state = self.data_store.get_game_state()
            start = (origin or "").strip()
            origin_source = "argument"
            if not start:
                system = state.current_system
                if not system or system == "Unknown":
                    return {
                        "error": "Current system is not known from the journal yet. "
                                 "Pass origin explicitly."
                    }
                docked_at = state.current_station if state.docked else None
                start = "%s/%s" % (system, docked_at) if docked_at else system
                origin_source = "current_location"

            # No limit: storage order is not time order, so pick the newest by timestamp.
            latest_loadout = latest_by_timestamp(self.data_store.get_events_by_type("Loadout"))
            loadout = (latest_loadout.raw_event or {}) if latest_loadout is not None else {}

            budget = int(credits or 0) or int(state.credits or 0)
            if budget <= 0:
                return {"error": "The commander's credits are not known from the journal yet. "
                                 "Pass credits explicitly."}
            capacity = int(cargo_capacity or 0) or int(loadout.get("CargoCapacity") or 0)
            if capacity <= 0:
                return {"error": "The ship has no cargo hold, or it is not known from the "
                                 "journal yet. Pass cargo_capacity explicitly."}

            jump_range = float(jump_range_ly or 0.0)
            range_source = "argument"
            if jump_range <= 0:
                ship = ship_from_loadout(loadout)
                if "error" not in ship:
                    jump_range = laden_jump_range(ship, capacity)
                    range_source = "journal_loadout_laden"
                else:
                    jump_range = float(loadout.get("MaxJumpRange") or 0.0)
                    range_source = "journal_max_jump_range"
                if jump_range <= 0:
                    return {"error": "The ship's jump range is not known from the journal "
                                     "yet. Pass jump_range_ly explicitly."}

            pad = (pad_size or "").strip().upper()[:1]
            if (pad_size or "").strip() and (pad not in ("S", "M", "L") or len(pad_size.strip()) > 6):
                return {"error": "pad_size must be 'S', 'M' or 'L', got '%s'" % pad_size}
            if not pad and loadout:
                known = summarize_loadout(loadout).get("landing_pad") or ""
                pad = known[:1].upper() if known in ("small", "medium", "large") else ""

            result = await self.trade_client.plan_trade_run(
                start, (destination or "").strip(), budget, capacity, jump_range,
                hops, int(max_jumps_per_hop or 0), float(max_data_age_days), pad, routes
            )
            fallback_note = None
            if (
                "error" in result and origin_source == "current_location"
                and start != state.current_system
                and "unknown station" in str(result["error"]).lower()
            ):
                # Fleet carriers and other unlisted stations: plan from the system.
                fallback_note = (
                    "The current station '%s' is not in the Trade Dangerous database, so "
                    "the run starts from any station in %s." % (
                        state.current_station, state.current_system)
                )
                start = state.current_system
                result = await self.trade_client.plan_trade_run(
                    start, (destination or "").strip(), budget, capacity, jump_range,
                    hops, int(max_jumps_per_hop or 0), float(max_data_age_days), pad, routes
                )
            if "error" not in result:
                if fallback_note:
                    result["notes"].append(fallback_note)
                result["inputs"] = {
                    "origin_source": origin_source,
                    "credits_source": "argument" if credits else "journal",
                    "cargo_capacity_source": "argument" if cargo_capacity else "journal_loadout",
                    "jump_range_source": range_source,
                }
                if range_source == "journal_max_jump_range":
                    result["notes"].append(
                        "Jump range is the journal's best-case figure, not the laden "
                        "range; hops may need more jumps than shown."
                    )
            return result
        except Exception as e:
            logger.error(f"Error planning trade route: {e}")
            return {"error": str(e)}

    # ==================== Wing Mining Missions and Reputation ====================

    # A mission lasts up to a week; two weeks of journals covers every active one.
    WMM_STACK_LOOKBACK_DAYS = 14
    REPUTATION_LOOKBACK_DAYS = 180

    def _read_journal_files(self, days: float, event_types: Set[str]) -> List[Dict[str, Any]]:
        """Read raw events of the given types from recent journal files."""
        return read_recent_journal_events(
            getattr(self.data_store, "journal_path", None), days, event_types,
            datetime.now(timezone.utc)
        )

    async def get_wmm_stack(self, cargo_capacity: int = 0) -> Dict[str, Any]:
        """
        Show the active wing mining mission stack, rebuilt from the journal.

        Args:
            cargo_capacity: Cargo hold in tonnes for the hauling plan; 0 reads
                the journal Loadout

        Returns:
            Dict with missions, per-commodity totals and expiry, or a structured error object
        """
        try:
            capacity = int(cargo_capacity or 0)
            if capacity <= 0:
                latest_loadout = latest_by_timestamp(self.data_store.get_events_by_type("Loadout"))
                if latest_loadout is not None:
                    capacity = int((latest_loadout.raw_event or {}).get("CargoCapacity") or 0)
            events = self.journal_reader(self.WMM_STACK_LOOKBACK_DAYS, STACK_EVENT_TYPES)
            result = build_wmm_stack(events, datetime.now(timezone.utc), capacity)
            result["notes"] = [
                "Rebuilt from the last %d days of journal files." % self.WMM_STACK_LOOKBACK_DAYS,
                "Flags: wrong_commodity (not Gold, Silver, Bertrandite or Indite), "
                "source_and_return, not_wing, unsupported_station (not Burkin Orbital, "
                "Darlton Port or Rukavishnikov Terminal), details_unknown (the mission is "
                "active but its acceptance is not in the journals read, for example one "
                "shared by a wingmate).",
                "Flagged missions are left out of the totals and the hauling plan.",
            ]
            return result
        except Exception as e:
            logger.error(f"Error building WMM stack: {e}")
            return {"error": str(e)}

    async def get_faction_reputation(self, systems: str = "") -> Dict[str, Any]:
        """
        Show reputation with each minor faction from the last visit to each system.

        Args:
            systems: Comma-separated system names; empty selects the PTN wing
                mining systems Mbutas and Paemara

        Returns:
            Dict with factions per system, or a structured error object
        """
        try:
            wanted = [name.strip() for name in (systems or "").split(",") if name.strip()]
            events = self.journal_reader(self.REPUTATION_LOOKBACK_DAYS, REPUTATION_EVENT_TYPES)
            return faction_reputation(events, wanted or list(WMM_SYSTEMS), datetime.now(timezone.utc))
        except Exception as e:
            logger.error(f"Error getting faction reputation: {e}")
            return {"error": str(e)}

    # ==================== Location and Status Tools ====================

    async def get_current_location(self) -> Dict[str, Any]:
        """
        Get comprehensive current location information.
        
        Returns:
            Dict containing current system, station, coordinates, and nearby info
        """
        try:
            game_state = self.data_store.get_game_state()
            
            # Get recent location events for additional context
            location_events = self.data_store.get_events_by_type("Location")
            recent_jumps = self.data_store.get_events_by_type("FSDJump", limit=5)
            
            response = {
                "current_system": game_state.current_system or "Unknown",
                "current_station": game_state.current_station,
                "current_body": game_state.current_body,
                "coordinates": game_state.coordinates,
                "docked": game_state.docked,
                "landed": game_state.landed,
                "in_supercruise": game_state.supercruise,
                "recent_systems": []
            }
            
            # Add recent system visits
            for jump in recent_jumps:
                system_info = {
                    "system": jump.key_data.get("system"),
                    "timestamp": jump.timestamp.isoformat(),
                    "distance": jump.key_data.get("distance")
                }
                response["recent_systems"].append(system_info)
            
            # location_timestamp is when the journal last placed the commander:
            # the newest of any event that does so, not only Location. Clients
            # use it to judge whether the server is stale.
            placing_events = list(location_events)
            for event_type in ("FSDJump", "CarrierJump", "Docked"):
                placing_events.extend(self.data_store.get_events_by_type(event_type))
            newest_placing = latest_by_timestamp(placing_events)
            if newest_placing is not None:
                response["location_timestamp"] = newest_placing.timestamp.isoformat()
                response["location_event"] = newest_placing.event_type

            # System details come from the newest event that describes a system.
            system_events = [e for e in placing_events if e.event_type != "Docked"]
            latest = latest_by_timestamp(system_events)
            if latest is not None:
                raw = latest.raw_event or {}
                response["population"] = raw.get("Population", 0)
                response["allegiance"] = raw.get("SystemAllegiance") or raw.get("Allegiance")
                response["economy"] = (raw.get("SystemEconomy_Localised")
                                       or raw.get("SystemEconomy") or raw.get("Economy"))
                response["government"] = (raw.get("SystemGovernment_Localised")
                                          or raw.get("SystemGovernment") or raw.get("Government"))
                response["security"] = (raw.get("SystemSecurity_Localised")
                                        or raw.get("SystemSecurity") or raw.get("Security"))

            return response
            
        except Exception as e:
            logger.error(f"Error getting current location: {e}")
            return {"error": str(e)}
    
    async def get_ship_status(self) -> Dict[str, Any]:
        """
        Get comprehensive ship status and configuration.
        
        Returns:
            Dict containing ship type, name, modules, and condition
        """
        try:
            game_state = self.data_store.get_game_state()
            
            # Get recent ship-related events
            # No limit: storage order is not time order, so pick the newest by timestamp.
            latest_loadout = latest_by_timestamp(self.data_store.get_events_by_type("Loadout"))
            repair_events = self.data_store.get_events_by_type("Repair", limit=5)
            refuel_events = self.data_store.get_events_by_type("RefuelAll", limit=1)
            
            response = {
                "ship_type": game_state.current_ship or "Unknown",
                "ship_name": game_state.ship_name,
                "ship_id": game_state.ship_id,
                "modules": [],
                "module_count": 0,
                "loadout_timestamp": None,
                "status": {
                    "docked": game_state.docked,
                    "landed": game_state.landed,
                    "in_srv": game_state.in_srv,
                    "in_fighter": game_state.in_fighter,
                    "low_fuel": game_state.low_fuel,
                    "overheating": game_state.overheating,
                    "in_danger": game_state.is_in_danger,
                    "being_interdicted": game_state.being_interdicted
                },
                "recent_maintenance": []
            }
            
            # Add loadout details if available. The Loadout event is the
            # authority on the ship: type, name, modules, values and ranges.
            if latest_loadout is not None:
                summary = summarize_loadout(latest_loadout.raw_event or {})
                for key in ("ship_type", "ship_name", "ship_id"):
                    if summary.get(key):
                        response[key] = summary[key]
                for key in ("landing_pad", "max_jump_range_ly", "cargo_capacity_t",
                            "fuel_capacity_t", "hull_value", "modules_value", "rebuy",
                            "capabilities", "can_laser_mine", "module_count", "modules"):
                    response[key] = summary[key]
                response["loadout_timestamp"] = latest_loadout.timestamp.isoformat()
            
            # Add recent maintenance
            for repair in repair_events:
                response["recent_maintenance"].append({
                    "type": "repair",
                    "timestamp": repair.timestamp.isoformat(),
                    "cost": repair.raw_event.get("Cost", 0)
                })
            
            for refuel in refuel_events:
                response["recent_maintenance"].append({
                    "type": "refuel",
                    "timestamp": refuel.timestamp.isoformat(),
                    "amount": refuel.raw_event.get("Amount", 0),
                    "cost": refuel.raw_event.get("Cost", 0)
                })
            
            return response
            
        except Exception as e:
            logger.error(f"Error getting ship status: {e}")
            return {"error": str(e)}
    
    # ==================== Event Search and Filter Tools ====================

    async def search_historical_events(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        event_types: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        system_name: Optional[str] = None,
        limit: Optional[int] = 1000,
        sort_order: str = "desc"
    ) -> Dict[str, Any]:
        """
        Search historical journal events with flexible date range support.

        This tool enables querying events using natural language date expressions
        and absolute dates, making it easy for LLMs to retrieve historical gameplay data.

        Args:
            start_date: Start of date range (inclusive). Supports:
                       - ISO format: "2025-01-15" or "2025-01-15T10:30:00Z"
                       - Natural language: "today", "yesterday", "last week", "last month"
                       - Relative: "3 days ago", "2 weeks ago", "30 days ago"
                       - If omitted, searches from beginning of available data
            end_date: End of date range (inclusive). Supports same formats as start_date.
                     If omitted, searches up to present time.
            event_types: Filter by specific event types (e.g., ["FSDJump", "Scan"])
            categories: Filter by event categories (e.g., ["exploration", "combat"])
            system_name: Filter by system name
            limit: Maximum number of events to return (default: 1000)
            sort_order: "asc" (oldest first) or "desc" (newest first, default)

        Returns:
            Dict containing:
                - events: List of matching events with full details
                - total_count: Total number of matching events
                - date_range: Parsed start and end dates
                - truncated: Whether results were limited
                - search_criteria: Echo of search parameters used

        Examples:
            >>> # Get all events from last month to two weeks ago
            >>> await search_historical_events(
            ...     start_date="last month",
            ...     end_date="two weeks ago"
            ... )

            >>> # Find all exploration scans in January 2025
            >>> await search_historical_events(
            ...     start_date="2025-01-01",
            ...     end_date="2025-01-31",
            ...     categories=["exploration"],
            ...     event_types=["Scan"]
            ... )

            >>> # What systems did I visit between Christmas and New Year?
            >>> await search_historical_events(
            ...     start_date="2024-12-25",
            ...     end_date="2025-01-01",
            ...     event_types=["FSDJump", "Location"]
            ... )
        """
        try:
            # Convert category strings to EventCategory enums
            category_enums = None
            if categories:
                category_enums = set()
                for cat_str in categories:
                    try:
                        category_enums.add(EventCategory(cat_str.lower()))
                    except ValueError:
                        logger.warning(f"Invalid category: {cat_str}")

            # Query historical events
            result = self.data_store.query_historical_events(
                start_date=start_date,
                end_date=end_date,
                event_types=set(event_types) if event_types else None,
                categories=category_enums,
                system_name=system_name,
                limit=limit,
                sort_order=sort_order
            )

            # Format events for response
            formatted_events = []
            for event in result["events"]:
                formatted_events.append({
                    "timestamp": event.timestamp.isoformat(),
                    "event_type": event.event_type,
                    "category": event.category.value,
                    "summary": event.summary,
                    "key_data": event.key_data,
                    "is_valid": event.is_valid
                })

            return {
                "events": formatted_events,
                "total_count": result["total_count"],
                "date_range": result["date_range"],
                "truncated": result["truncated"],
                "search_criteria": {
                    "start_date": start_date,
                    "end_date": end_date,
                    "event_types": event_types,
                    "categories": categories,
                    "system_name": system_name,
                    "limit": limit,
                    "sort_order": sort_order
                }
            }

        except Exception as e:
            logger.error(f"Error searching historical events: {e}")
            return {"error": str(e)}

    async def search_events(
        self,
        event_types: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        time_range_minutes: Optional[int] = None,
        system_names: Optional[List[str]] = None,
        contains_text: Optional[str] = None,
        max_results: int = 100
    ) -> Dict[str, Any]:
        """
        Search for events with flexible filtering criteria.
        
        Args:
            event_types: List of event types to filter
            categories: List of event categories to filter
            time_range_minutes: Time range in minutes from now
            system_names: Filter by system names
            contains_text: Text search in event data
            max_results: Maximum number of results
            
        Returns:
            Dict containing matching events and metadata
        """
        try:
            # Build filter criteria
            filter_criteria = EventFilter(max_results=max_results)
            
            if event_types:
                filter_criteria.event_types = set(event_types)
            
            if categories:
                # Convert string categories to EventCategory enums
                category_enums = []
                for cat_str in categories:
                    try:
                        category_enums.append(EventCategory(cat_str.lower()))
                    except ValueError:
                        logger.warning(f"Invalid category: {cat_str}")
                if category_enums:
                    filter_criteria.categories = set(category_enums)
            
            if time_range_minutes:
                filter_criteria.start_time = datetime.now(timezone.utc) - timedelta(minutes=time_range_minutes)
            
            if system_names:
                filter_criteria.system_names = set(system_names)
            
            if contains_text:
                filter_criteria.contains_text = contains_text
            
            # Execute query
            events = self.data_store.query_events(
                filter_criteria=filter_criteria,
                sort_order=QuerySortOrder.NEWEST_FIRST
            )
            
            # Format response
            response = {
                "total_found": len(events),
                "search_criteria": {
                    "event_types": event_types,
                    "categories": categories,
                    "time_range_minutes": time_range_minutes,
                    "system_names": system_names,
                    "contains_text": contains_text
                },
                "events": []
            }
            
            for event in events:
                response["events"].append({
                    "timestamp": event.timestamp.isoformat(),
                    "event_type": event.event_type,
                    "category": event.category.value,
                    "summary": event.summary,
                    "key_data": event.key_data,
                    "is_valid": event.is_valid
                })
            
            return response
            
        except Exception as e:
            logger.error(f"Error searching events: {e}")
            return {"error": str(e)}
    
    # ==================== Activity Summary Tools ====================
    
    async def get_activity_summary(
        self,
        activity_type: str,
        time_range_hours: int = 24
    ) -> Dict[str, Any]:
        """
        Get comprehensive summary of specific activity type.
        
        Args:
            activity_type: Type of activity (exploration, trading, combat, etc.)
            time_range_hours: Time range to analyze
            
        Returns:
            Dict containing activity statistics and highlights
        """
        try:
            # Validate activity type
            try:
                activity = ActivityType(activity_type.lower())
            except ValueError:
                return {"error": f"Invalid activity type: {activity_type}"}
            
            # Get events for time range
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=time_range_hours)
            
            if activity == ActivityType.EXPLORATION:
                return await self._get_exploration_summary(cutoff_time)
            elif activity == ActivityType.TRADING:
                return await self._get_trading_summary(cutoff_time)
            elif activity == ActivityType.COMBAT:
                return await self._get_combat_summary(cutoff_time)
            elif activity == ActivityType.MINING:
                return await self._get_mining_summary(cutoff_time)
            elif activity == ActivityType.MISSIONS:
                return await self._get_mission_summary(cutoff_time)
            elif activity == ActivityType.ENGINEERING:
                return await self._get_engineering_summary(cutoff_time)
            else:
                return {"error": f"Activity type {activity_type} not yet implemented"}
                
        except Exception as e:
            logger.error(f"Error getting activity summary: {e}")
            return {"error": str(e)}
    
    async def _get_exploration_summary(self, cutoff_time: datetime) -> Dict[str, Any]:
        """Generate exploration activity summary."""
        filter_criteria = EventFilter(
            categories={EventCategory.EXPLORATION},
            start_time=cutoff_time
        )
        
        events = self.data_store.query_events(filter_criteria)
        
        summary = {
            "activity_type": "exploration",
            "total_events": len(events),
            "bodies_scanned": 0,
            "valuable_bodies": [],
            "systems_discovered": set(),
            "exploration_value": 0,
            "first_discoveries": 0,
            "terraformable_found": 0,
            "detailed_scans": []
        }
        
        for event in events:
            if event.event_type == "Scan":
                summary["bodies_scanned"] += 1
                
                # Check for valuable bodies
                if event.key_data.get("terraformable"):
                    summary["terraformable_found"] += 1
                    summary["valuable_bodies"].append({
                        "name": event.key_data.get("body_name"),
                        "type": event.key_data.get("body_type"),
                        "terraformable": True,
                        "timestamp": event.timestamp.isoformat()
                    })
                
                # Track systems
                system = event.raw_event.get("StarSystem")
                if system:
                    summary["systems_discovered"].add(system)
                
                # Add scan details
                summary["detailed_scans"].append({
                    "body": event.key_data.get("body_name"),
                    "type": event.key_data.get("body_type"),
                    "distance": event.key_data.get("distance"),
                    "landable": event.key_data.get("landable"),
                    "timestamp": event.timestamp.isoformat()
                })
                
            elif event.event_type in ["SellExplorationData", "MultiSellExplorationData"]:
                summary["exploration_value"] += event.key_data.get("value", 0)
                summary["first_discoveries"] += event.key_data.get("discovered", 0)
        
        # Convert set to list for JSON serialization
        summary["systems_discovered"] = list(summary["systems_discovered"])
        
        # Limit detailed scans to most recent 20
        summary["detailed_scans"] = summary["detailed_scans"][:20]
        
        return summary
    
    async def _get_trading_summary(self, cutoff_time: datetime) -> Dict[str, Any]:
        """Generate trading activity summary."""
        filter_criteria = EventFilter(
            categories={EventCategory.TRADING},
            start_time=cutoff_time
        )
        
        events = self.data_store.query_events(filter_criteria)
        
        summary = {
            "activity_type": "trading",
            "total_events": len(events),
            "total_profit": 0,
            "total_loss": 0,
            "commodities_traded": {},
            "stations_visited": set(),
            "best_trades": [],
            "recent_transactions": []
        }
        
        for event in events:
            if event.event_type == "MarketBuy":
                commodity = event.key_data.get("commodity", "Unknown")
                cost = event.key_data.get("total", 0)
                
                if commodity not in summary["commodities_traded"]:
                    summary["commodities_traded"][commodity] = {
                        "bought": 0,
                        "sold": 0,
                        "spent": 0,
                        "earned": 0
                    }
                
                summary["commodities_traded"][commodity]["bought"] += event.key_data.get("count", 0)
                summary["commodities_traded"][commodity]["spent"] += cost
                summary["total_loss"] += cost
                
            elif event.event_type == "MarketSell":
                commodity = event.key_data.get("commodity", "Unknown")
                revenue = event.key_data.get("total", 0)
                
                if commodity not in summary["commodities_traded"]:
                    summary["commodities_traded"][commodity] = {
                        "bought": 0,
                        "sold": 0,
                        "spent": 0,
                        "earned": 0
                    }
                
                summary["commodities_traded"][commodity]["sold"] += event.key_data.get("count", 0)
                summary["commodities_traded"][commodity]["earned"] += revenue
                summary["total_profit"] += revenue
                
                # Track best trades
                summary["best_trades"].append({
                    "commodity": commodity,
                    "amount": event.key_data.get("count", 0),
                    "revenue": revenue,
                    "timestamp": event.timestamp.isoformat()
                })
            
            # Track recent transactions
            summary["recent_transactions"].append({
                "type": event.event_type,
                "commodity": event.key_data.get("commodity"),
                "amount": event.key_data.get("count"),
                "value": event.key_data.get("total"),
                "timestamp": event.timestamp.isoformat()
            })
            
            # Track stations
            station = event.raw_event.get("StationName")
            if station:
                summary["stations_visited"].add(station)
        
        # Calculate net profit
        summary["net_profit"] = summary["total_profit"] - summary["total_loss"]
        
        # Sort and limit best trades
        summary["best_trades"].sort(key=lambda x: x["revenue"], reverse=True)
        summary["best_trades"] = summary["best_trades"][:10]
        
        # Limit recent transactions
        summary["recent_transactions"] = summary["recent_transactions"][:20]
        
        # Convert set to list
        summary["stations_visited"] = list(summary["stations_visited"])
        
        return summary
    
    async def _get_combat_summary(self, cutoff_time: datetime) -> Dict[str, Any]:
        """Generate combat activity summary."""
        filter_criteria = EventFilter(
            categories={EventCategory.COMBAT},
            start_time=cutoff_time
        )
        
        events = self.data_store.query_events(filter_criteria)
        
        summary = {
            "activity_type": "combat",
            "total_events": len(events),
            "bounties_collected": 0,
            "total_bounty_value": 0,
            "combat_bonds": 0,
            "total_bond_value": 0,
            "kills": [],
            "deaths": 0,
            "interdictions_escaped": 0,
            "interdictions_won": 0,
            "combat_zones": set(),
            "factions_fought": set()
        }
        
        for event in events:
            if event.event_type == "Bounty":
                summary["bounties_collected"] += 1
                summary["total_bounty_value"] += event.key_data.get("reward", 0)
                
                # Track kills
                summary["kills"].append({
                    "target": event.key_data.get("target"),
                    "faction": event.key_data.get("faction"),
                    "reward": event.key_data.get("reward"),
                    "timestamp": event.timestamp.isoformat()
                })
                
                # Track factions
                faction = event.key_data.get("faction")
                if faction:
                    summary["factions_fought"].add(faction)
                    
            elif event.event_type == "FactionKillBond":
                summary["combat_bonds"] += 1
                summary["total_bond_value"] += event.raw_event.get("Reward", 0)
                
            elif event.event_type == "Died":
                summary["deaths"] += 1
                
            elif event.event_type == "EscapeInterdiction":
                summary["interdictions_escaped"] += 1
                
            elif event.event_type == "Interdiction":
                if event.raw_event.get("Success"):
                    summary["interdictions_won"] += 1
        
        # Convert sets to lists
        summary["combat_zones"] = list(summary["combat_zones"])
        summary["factions_fought"] = list(summary["factions_fought"])
        
        # Limit kills list
        summary["kills"] = summary["kills"][:20]
        
        return summary
    
    async def _get_mining_summary(self, cutoff_time: datetime) -> Dict[str, Any]:
        """Generate mining activity summary."""
        # Get mining events
        mining_filter = EventFilter(
            categories={EventCategory.MINING},
            start_time=cutoff_time
        )
        mining_events = self.data_store.query_events(mining_filter)

        # Also get material collection events that might be mining-related
        material_filter = EventFilter(
            event_types={"MaterialCollected"},
            start_time=cutoff_time
        )
        material_events = self.data_store.query_events(material_filter)

        # Combine events for comprehensive analysis
        events = mining_events + material_events
        
        summary = {
            "activity_type": "mining",
            "total_events": len(events),
            # Separate commodities (sellable cargo from refinery) from materials (engineering resources)
            "commodities_refined": {},  # From MiningRefined events - what users want to see
            "raw_materials_collected": {},  # From MaterialCollected events - for engineering
            "materials_mined": {},  # Legacy field, now populated from Mined events only
            "asteroids_cracked": 0,
            "asteroids_prospected": 0,
            "refineries_used": set(),
            "mining_locations": set(),
            "recent_mining": []
        }
        
        for event in events:
            if event.event_type == "Mined":
                # Handle actual Elite Dangerous mining events
                material = event.raw_event.get("Type", "Unknown")
                count = event.raw_event.get("Count", 1)
                if material not in summary["materials_mined"]:
                    summary["materials_mined"][material] = 0
                summary["materials_mined"][material] += count

                summary["recent_mining"].append({
                    "type": "mined",
                    "material": material,
                    "count": count,
                    "timestamp": event.timestamp.isoformat()
                })

            elif event.event_type == "AsteroidCracked":
                summary["asteroids_cracked"] += 1

                # Track cracked asteroids in recent mining
                summary["recent_mining"].append({
                    "type": "cracked",
                    "body": event.raw_event.get("Body"),
                    "timestamp": event.timestamp.isoformat()
                })

            elif event.event_type == "ProspectedAsteroid":
                summary["asteroids_prospected"] += 1

                summary["recent_mining"].append({
                    "type": "prospected",
                    "content": event.raw_event.get("Content"),
                    "remaining": event.raw_event.get("Remaining"),
                    "timestamp": event.timestamp.isoformat()
                })

            elif event.event_type == "RefineryOpen":
                # Track refinery usage
                refinery = event.raw_event.get("Name", "Unknown Refinery")
                summary["refineries_used"].add(refinery)

            elif event.event_type == "MiningRefined":
                # Handle refined commodities - sellable cargo from refinery (what users want to see)
                commodity = event.raw_event.get("Type", "Unknown")
                # MiningRefined events in Elite Dangerous don't have Count field, they represent 1 unit
                count = 1

                if commodity not in summary["commodities_refined"]:
                    summary["commodities_refined"][commodity] = 0
                summary["commodities_refined"][commodity] += count

                summary["recent_mining"].append({
                    "type": "refined",
                    "commodity": commodity,
                    "count": count,
                    "timestamp": event.timestamp.isoformat()
                })

            elif event.event_type == "MaterialCollected":
                # Track raw materials collected (for engineering, not sellable)
                material = event.raw_event.get("Name", "Unknown")
                count = event.raw_event.get("Count", 1)
                category = event.raw_event.get("Category", "")

                # Store raw materials separately from commodities
                if material not in summary["raw_materials_collected"]:
                    summary["raw_materials_collected"][material] = 0
                summary["raw_materials_collected"][material] += count

                summary["recent_mining"].append({
                    "type": "material_collected",
                    "material": material,
                    "count": count,
                    "category": category,
                    "timestamp": event.timestamp.isoformat()
                })
        
        # Convert sets to lists
        summary["refineries_used"] = list(summary["refineries_used"])
        summary["mining_locations"] = list(summary["mining_locations"])
        
        # Limit recent mining
        summary["recent_mining"] = summary["recent_mining"][:20]
        
        return summary
    
    async def _get_mission_summary(self, cutoff_time: datetime) -> Dict[str, Any]:
        """Generate mission activity summary."""
        filter_criteria = EventFilter(
            categories={EventCategory.MISSION},
            start_time=cutoff_time
        )
        
        events = self.data_store.query_events(filter_criteria)
        
        summary = {
            "activity_type": "missions",
            "total_events": len(events),
            "missions_accepted": 0,
            "missions_completed": 0,
            "missions_failed": 0,
            "missions_abandoned": 0,
            "total_rewards": 0,
            "total_donated": 0,
            "factions_worked_for": set(),
            "active_missions": [],
            "completed_missions": []
        }
        
        active_missions = {}

        # Walk oldest first. The query returns newest first, and in that order
        # a completion is seen before its acceptance, which left every
        # finished mission listed as still active.
        for event in sorted(events, key=lambda e: e.timestamp):
            if event.event_type == "MissionAccepted":
                summary["missions_accepted"] += 1
                mission_id = event.raw_event.get("MissionID")
                
                if mission_id:
                    active_missions[mission_id] = {
                        "name": event.key_data.get("name"),
                        "faction": event.key_data.get("faction"),
                        "reward": event.key_data.get("reward"),
                        "expiry": event.key_data.get("expiry"),
                        "accepted_at": event.timestamp.isoformat()
                    }
                
                faction = event.key_data.get("faction")
                if faction:
                    summary["factions_worked_for"].add(faction)
                    
            elif event.event_type == "MissionCompleted":
                summary["missions_completed"] += 1
                # Donation missions carry no Reward, and key_data may hold an
                # explicit None; either must count as zero, not crash the sum.
                reward = event.key_data.get("reward")
                if reward is None:
                    reward = event.raw_event.get("Reward")
                summary["total_rewards"] += int(reward or 0)
                summary["total_donated"] += int(event.raw_event.get("Donated") or 0)

                mission_id = event.raw_event.get("MissionID")
                if mission_id and mission_id in active_missions:
                    mission_info = active_missions.pop(mission_id)
                    mission_info["completed_at"] = event.timestamp.isoformat()
                    summary["completed_missions"].append(mission_info)
                else:
                    summary["completed_missions"].append({
                        "name": event.key_data.get("name"),
                        "faction": event.key_data.get("faction"),
                        "reward": event.key_data.get("reward"),
                        "completed_at": event.timestamp.isoformat()
                    })
                    
            elif event.event_type == "MissionFailed":
                summary["missions_failed"] += 1
                mission_id = event.raw_event.get("MissionID")
                if mission_id and mission_id in active_missions:
                    active_missions.pop(mission_id)
                    
            elif event.event_type == "MissionAbandoned":
                summary["missions_abandoned"] += 1
                mission_id = event.raw_event.get("MissionID")
                if mission_id and mission_id in active_missions:
                    active_missions.pop(mission_id)
        
        # Add remaining active missions
        summary["active_missions"] = list(active_missions.values())
        
        # Convert sets to lists
        summary["factions_worked_for"] = list(summary["factions_worked_for"])
        
        # Limit completed missions
        summary["completed_missions"] = summary["completed_missions"][:20]
        
        return summary
    
    async def _get_engineering_summary(self, cutoff_time: datetime) -> Dict[str, Any]:
        """Generate engineering activity summary."""
        filter_criteria = EventFilter(
            categories={EventCategory.ENGINEERING},
            start_time=cutoff_time
        )
        
        events = self.data_store.query_events(filter_criteria)
        
        summary = {
            "activity_type": "engineering",
            "total_events": len(events),
            "modifications_applied": 0,
            "engineers_visited": set(),
            "modules_modified": {},
            "materials_contributed": 0,
            "recent_modifications": []
        }
        
        for event in events:
            if event.event_type == "EngineerCraft":
                summary["modifications_applied"] += 1
                
                engineer = event.key_data.get("engineer")
                if engineer:
                    summary["engineers_visited"].add(engineer)
                
                module = event.key_data.get("module", "Unknown")
                if module not in summary["modules_modified"]:
                    summary["modules_modified"][module] = []
                
                summary["modules_modified"][module].append({
                    "blueprint": event.key_data.get("blueprint"),
                    "level": event.key_data.get("level"),
                    "engineer": engineer,
                    "timestamp": event.timestamp.isoformat()
                })
                
                summary["recent_modifications"].append({
                    "module": module,
                    "blueprint": event.key_data.get("blueprint"),
                    "level": event.key_data.get("level"),
                    "engineer": engineer,
                    "timestamp": event.timestamp.isoformat()
                })
                
            elif event.event_type == "EngineerContribution":
                summary["materials_contributed"] += 1
        
        # Convert sets to lists
        summary["engineers_visited"] = list(summary["engineers_visited"])
        
        # Limit recent modifications
        summary["recent_modifications"] = summary["recent_modifications"][:20]
        
        return summary
    
    # ==================== Journey and Navigation Tools ====================
    
    async def get_journey_summary(
        self,
        time_range_hours: int = 24
    ) -> Dict[str, Any]:
        """
        Get comprehensive journey and navigation summary.
        
        Args:
            time_range_hours: Time range to analyze
            
        Returns:
            Dict containing journey statistics and route information
        """
        try:
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=time_range_hours)
            
            filter_criteria = EventFilter(
                categories={EventCategory.NAVIGATION},
                start_time=cutoff_time
            )
            
            events = self.data_store.query_events(filter_criteria)
            
            summary = {
                "total_jumps": 0,
                "total_distance": 0,
                "fuel_used": 0,
                "systems_visited": [],
                "stations_docked": [],
                "bodies_landed": [],
                "route_map": [],
                "current_location": None
            }
            
            # Get current location
            game_state = self.data_store.get_game_state()
            summary["current_location"] = {
                "system": game_state.current_system,
                "station": game_state.current_station,
                "body": game_state.current_body,
                "docked": game_state.docked,
                "landed": game_state.landed
            }
            
            visited_systems = set()
            
            for event in events:
                if event.event_type == "FSDJump":
                    summary["total_jumps"] += 1
                    distance = event.key_data.get("distance", 0)
                    summary["total_distance"] += distance
                    summary["fuel_used"] += event.key_data.get("fuel_used", 0)
                    
                    system = event.key_data.get("system")
                    if system and system not in visited_systems:
                        visited_systems.add(system)
                        summary["systems_visited"].append({
                            "system": system,
                            "timestamp": event.timestamp.isoformat(),
                            "distance": distance
                        })
                    
                    # Add to route map
                    summary["route_map"].append({
                        "type": "jump",
                        "system": system,
                        "timestamp": event.timestamp.isoformat(),
                        "distance": distance
                    })
                    
                elif event.event_type == "Docked":
                    station = event.key_data.get("station")
                    system = event.key_data.get("system")
                    
                    summary["stations_docked"].append({
                        "station": station,
                        "system": system,
                        "station_type": event.key_data.get("station_type"),
                        "timestamp": event.timestamp.isoformat()
                    })
                    
                    summary["route_map"].append({
                        "type": "dock",
                        "station": station,
                        "system": system,
                        "timestamp": event.timestamp.isoformat()
                    })
                    
                elif event.event_type == "Touchdown":
                    body = event.raw_event.get("Body")
                    summary["bodies_landed"].append({
                        "body": body,
                        "timestamp": event.timestamp.isoformat()
                    })
                    
                    summary["route_map"].append({
                        "type": "landing",
                        "body": body,
                        "timestamp": event.timestamp.isoformat()
                    })
            
            # Calculate statistics
            summary["unique_systems"] = len(visited_systems)
            summary["average_jump_distance"] = (
                summary["total_distance"] / summary["total_jumps"] 
                if summary["total_jumps"] > 0 else 0
            )
            
            # Limit route map to last 50 events
            summary["route_map"] = summary["route_map"][-50:]
            
            return summary
            
        except Exception as e:
            logger.error(f"Error getting journey summary: {e}")
            return {"error": str(e)}
    
    # ==================== Performance and Statistics Tools ====================
    
    async def get_performance_metrics(
        self,
        time_range_hours: int = 24
    ) -> Dict[str, Any]:
        """
        Get comprehensive performance metrics across all activities.
        
        Args:
            time_range_hours: Time range to analyze
            
        Returns:
            Dict containing performance metrics and efficiency ratings
        """
        try:
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=time_range_hours)
            
            # Get all events in time range
            filter_criteria = EventFilter(start_time=cutoff_time)
            events = self.data_store.query_events(filter_criteria)
            
            # Initialize metrics
            metrics = {
                "time_range_hours": time_range_hours,
                "total_events": len(events),
                "credits_earned": 0,
                "credits_spent": 0,
                "net_profit": 0,
                "efficiency_metrics": {},
                "activity_breakdown": {},
                "peak_activity_times": [],
                "achievements": []
            }
            
            # Track activity by hour
            hourly_activity = {}
            
            # Process events for metrics
            for event in events:
                hour_key = event.timestamp.strftime("%Y-%m-%d %H:00")
                if hour_key not in hourly_activity:
                    hourly_activity[hour_key] = 0
                hourly_activity[hour_key] += 1
                
                # Track credits
                if event.event_type in ["MarketSell", "Bounty", "MissionCompleted", "SellExplorationData"]:
                    credits = (
                        event.key_data.get("total", 0) or 
                        event.key_data.get("reward", 0) or 
                        event.key_data.get("value", 0)
                    )
                    metrics["credits_earned"] += credits
                    
                elif event.event_type in ["MarketBuy", "Repair", "RefuelAll"]:
                    cost = event.key_data.get("total", 0) or event.raw_event.get("Cost", 0)
                    metrics["credits_spent"] += cost
                
                # Track activity breakdown
                category = event.category.value
                if category not in metrics["activity_breakdown"]:
                    metrics["activity_breakdown"][category] = 0
                metrics["activity_breakdown"][category] += 1
            
            # Calculate net profit
            metrics["net_profit"] = metrics["credits_earned"] - metrics["credits_spent"]
            
            # Calculate efficiency metrics
            if metrics["total_events"] > 0:
                metrics["efficiency_metrics"]["credits_per_event"] = (
                    metrics["net_profit"] / metrics["total_events"]
                )
                metrics["efficiency_metrics"]["events_per_hour"] = (
                    metrics["total_events"] / time_range_hours
                )
            
            # Find peak activity times
            sorted_hours = sorted(hourly_activity.items(), key=lambda x: x[1], reverse=True)
            metrics["peak_activity_times"] = [
                {"hour": hour, "events": count} 
                for hour, count in sorted_hours[:5]
            ]
            
            # Identify achievements
            if metrics["credits_earned"] > 1000000:
                metrics["achievements"].append("Millionaire - Earned over 1M credits")
            
            if metrics["activity_breakdown"].get("exploration", 0) > 50:
                metrics["achievements"].append("Explorer - 50+ exploration events")
            
            if metrics["activity_breakdown"].get("combat", 0) > 30:
                metrics["achievements"].append("Combat Veteran - 30+ combat events")
            
            return metrics
            
        except Exception as e:
            logger.error(f"Error getting performance metrics: {e}")
            return {"error": str(e)}
    
    # ==================== Specialized Query Tools ====================
    
    async def get_faction_standings(self) -> Dict[str, Any]:
        """
        Get current faction standings and reputation changes.
        
        Returns:
            Dict containing faction relationships and recent changes
        """
        try:
            # Get reputation events
            rep_events = self.data_store.get_events_by_type("Reputation", limit=1)
            faction_events = []
            
            # Get mission completions for faction tracking
            mission_filter = EventFilter(
                event_types={"MissionCompleted", "MissionFailed"},
                start_time=datetime.now(timezone.utc) - timedelta(days=7)
            )
            mission_events = self.data_store.query_events(mission_filter)
            
            summary = {
                "current_reputation": {},
                "faction_interactions": {},
                "recent_changes": []
            }
            
            # Process reputation status
            if rep_events:
                latest_rep = rep_events[-1]
                if "Reputation" in latest_rep.raw_event:
                    for faction in latest_rep.raw_event["Reputation"]:
                        summary["current_reputation"][faction["Faction"]] = {
                            "reputation": faction.get("Reputation", 0),
                            "trend": faction.get("Trend", "Stable")
                        }
            
            # Track faction interactions from missions
            for event in mission_events:
                faction = event.key_data.get("faction")
                if faction:
                    if faction not in summary["faction_interactions"]:
                        summary["faction_interactions"][faction] = {
                            "missions_completed": 0,
                            "missions_failed": 0,
                            "total_rewards": 0
                        }
                    
                    if event.event_type == "MissionCompleted":
                        summary["faction_interactions"][faction]["missions_completed"] += 1
                        summary["faction_interactions"][faction]["total_rewards"] += event.key_data.get("reward", 0)
                    else:
                        summary["faction_interactions"][faction]["missions_failed"] += 1
                    
                    summary["recent_changes"].append({
                        "faction": faction,
                        "type": event.event_type,
                        "timestamp": event.timestamp.isoformat()
                    })
            
            # Limit recent changes
            summary["recent_changes"] = summary["recent_changes"][:20]
            
            return summary
            
        except Exception as e:
            logger.error(f"Error getting faction standings: {e}")
            return {"error": str(e)}
    
    async def get_material_inventory(self) -> Dict[str, Any]:
        """
        Get current material and cargo inventory.
        
        Returns:
            Dict containing materials, cargo, and recent changes
        """
        try:
            # Get cargo and materials events
            # No limit: storage order is not time order, so pick the newest by timestamp.
            latest_cargo = latest_by_timestamp(self.data_store.get_events_by_type("Cargo"))
            latest_materials = latest_by_timestamp(self.data_store.get_events_by_type("Materials"))
            
            summary = {
                "cargo": {},
                "carrier_cargo": {},
                "materials": {
                    "raw": {},
                    "manufactured": {},
                    "encoded": {}
                },
                "material_names": {},
                "snapshot_timestamp": None,
                "changes_since_snapshot": 0,
                "recent_changes": []
            }

            # Process cargo inventory
            if latest_cargo is not None:
                if "Inventory" in latest_cargo.raw_event:
                    for item in latest_cargo.raw_event["Inventory"]:
                        summary["cargo"][item["Name"]] = {
                            "count": item["Count"],
                            "stolen": item.get("Stolen", 0)
                        }
            
            # Process materials inventory: the newest login snapshot plus every
            # pickup, trade and spend recorded after it.
            if latest_materials is not None:
                change_filter = EventFilter(
                    event_types=set(MATERIAL_CHANGE_EVENTS),
                    start_time=latest_materials.timestamp
                )
                later = [
                    e for e in self.data_store.query_events(change_filter)
                    if e.timestamp > latest_materials.timestamp
                ]
                computed = compute_material_inventory(latest_materials.raw_event, later)
                summary["materials"] = computed["materials"]
                summary["material_names"] = computed["names"]
                summary["changes_since_snapshot"] = computed["changes_applied"]
                summary["snapshot_timestamp"] = latest_materials.timestamp.isoformat()
            else:
                summary["warning"] = (
                    "No Materials snapshot is loaded, so the inventory is unknown, not empty. "
                    "The game writes one at each login."
                )

            # Get fleet carrier cargo from game state
            game_state = self.data_store.get_game_state()
            if hasattr(game_state, 'carrier_cargo') and game_state.carrier_cargo:
                summary["carrier_cargo"] = dict(game_state.carrier_cargo)

            # Track recent material collection
            material_filter = EventFilter(
                event_types={"MaterialCollected", "MaterialDiscarded", "MaterialTrade"},
                start_time=datetime.now(timezone.utc) - timedelta(hours=24)
            )
            material_events = self.data_store.query_events(material_filter)
            
            for event in material_events[:20]:
                summary["recent_changes"].append({
                    "type": event.event_type,
                    "material": event.raw_event.get("Name") or event.raw_event.get("Paid"),
                    "category": event.raw_event.get("Category"),
                    "count": event.raw_event.get("Count", 1),
                    "timestamp": event.timestamp.isoformat()
                })
            
            return summary
            
        except Exception as e:
            logger.error(f"Error getting material inventory: {e}")
            return {"error": str(e)}

    def generate_edcopilot_chatter(self, chatter_type: str = "all") -> Dict[str, Any]:
        """
        Generate EDCoPilot custom chatter files based on current game state.

        Args:
            chatter_type: Type of chatter to generate ("space", "crew", "deepspace", or "all")

        Returns:
            Status of file generation with details
        """
        try:
            try:
                from ..edcopilot.generator import EDCoPilotContentGenerator
                from ..utils.config import EliteConfig
            except ImportError:
                from src.edcopilot.generator import EDCoPilotContentGenerator
                from src.utils.config import EliteConfig

            config = EliteConfig()
            generator = EDCoPilotContentGenerator(self.data_store, config.edcopilot_path)

            if chatter_type == "all":
                written_files = generator.write_files(backup_existing=True)
                return {
                    "status": "success",
                    "files_generated": list(written_files.keys()),
                    "output_directory": str(config.edcopilot_path),
                    "message": f"Generated {len(written_files)} EDCoPilot chatter files"
                }
            else:
                # Generate specific chatter type
                files = generator.generate_contextual_chatter()

                # Filter to requested type
                target_files = {}
                if chatter_type.lower() in ["space", "spacechatter"]:
                    target_files = {k: v for k, v in files.items() if "SpaceChatter" in k}
                elif chatter_type.lower() in ["crew", "crewchatter"]:
                    target_files = {k: v for k, v in files.items() if "CrewChatter" in k}
                elif chatter_type.lower() in ["deepspace", "deepspacechatter"]:
                    target_files = {k: v for k, v in files.items() if "DeepSpaceChatter" in k}
                else:
                    return {"error": f"Unknown chatter type: {chatter_type}"}

                # Write filtered files
                written_files = {}
                for filename, content in target_files.items():
                    file_path = config.edcopilot_path / filename
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(content)
                    written_files[filename] = file_path

                return {
                    "status": "success",
                    "files_generated": list(written_files.keys()),
                    "output_directory": str(config.edcopilot_path),
                    "message": f"Generated {chatter_type} chatter file(s)"
                }

        except ImportError as e:
            return {
                "error": "EDCoPilot integration not available",
                "details": str(e)
            }
        except Exception as e:
            logger.error(f"Error generating EDCoPilot chatter: {e}")
            return {"error": str(e)}

    def get_edcopilot_status(self) -> Dict[str, Any]:
        """
        Get status of EDCoPilot integration and existing custom files.

        Returns:
            Status of EDCoPilot integration and file information
        """
        try:
            try:
                from ..edcopilot.generator import EDCoPilotFileManager
                from ..utils.config import EliteConfig
            except ImportError:
                from src.edcopilot.generator import EDCoPilotFileManager
                from src.utils.config import EliteConfig

            config = EliteConfig()
            file_manager = EDCoPilotFileManager(config.edcopilot_path)

            # Check if EDCoPilot directory exists
            if not config.edcopilot_path.exists():
                return {
                    "status": "not_configured",
                    "edcopilot_path": str(config.edcopilot_path),
                    "exists": False,
                    "message": "EDCoPilot directory not found. Set ELITE_EDCOPILOT_PATH environment variable."
                }

            # List existing custom files
            custom_files = file_manager.list_custom_files()
            file_info = {}

            for file_path in custom_files:
                file_info[file_path.name] = file_manager.get_file_info(file_path)

            # Get current game context for chatter generation
            try:
                from ..edcopilot.generator import EDCoPilotContextAnalyzer
            except ImportError:
                from src.edcopilot.generator import EDCoPilotContextAnalyzer
            context_analyzer = EDCoPilotContextAnalyzer(self.data_store)
            context = context_analyzer.analyze_current_context()

            return {
                "status": "available",
                "edcopilot_path": str(config.edcopilot_path),
                "exists": True,
                "custom_files": file_info,
                "total_files": len(custom_files),
                "current_context": {
                    "primary_activity": context["primary_activity"],
                    "current_system": context["current_system"],
                    "docked": context["docked"],
                    "fuel_low": context["fuel_low"],
                    "deep_space": context["is_deep_space"]
                },
                "message": f"EDCoPilot integration ready. Found {len(custom_files)} existing custom files."
            }

        except ImportError as e:
            return {
                "error": "EDCoPilot integration not available",
                "details": str(e)
            }
        except Exception as e:
            logger.error(f"Error getting EDCoPilot status: {e}")
            return {"error": str(e)}

    def backup_edcopilot_files(self) -> Dict[str, Any]:
        """
        Create backups of all existing EDCoPilot custom files.

        Returns:
            Status of backup operation
        """
        try:
            try:
                from ..edcopilot.generator import EDCoPilotFileManager
                from ..utils.config import EliteConfig
            except ImportError:
                from src.edcopilot.generator import EDCoPilotFileManager
                from src.utils.config import EliteConfig

            config = EliteConfig()
            file_manager = EDCoPilotFileManager(config.edcopilot_path)

            if not config.edcopilot_path.exists():
                return {
                    "status": "error",
                    "message": "EDCoPilot directory not found"
                }

            backup_files = file_manager.backup_files()

            return {
                "status": "success",
                "backups_created": len(backup_files),
                "backup_files": {name: str(path) for name, path in backup_files.items()},
                "message": f"Created {len(backup_files)} backup files"
            }

        except ImportError as e:
            return {
                "error": "EDCoPilot integration not available",
                "details": str(e)
            }
        except Exception as e:
            logger.error(f"Error backing up EDCoPilot files: {e}")
            return {"error": str(e)}

    def preview_edcopilot_chatter(self, chatter_type: str = "space") -> Dict[str, Any]:
        """
        Preview EDCoPilot chatter content without writing files.

        Args:
            chatter_type: Type of chatter to preview ("space", "crew", "deepspace")

        Returns:
            Preview of generated chatter content
        """
        try:
            try:
                from ..edcopilot.generator import EDCoPilotContentGenerator
                from ..utils.config import EliteConfig
            except ImportError:
                from src.edcopilot.generator import EDCoPilotContentGenerator
                from src.utils.config import EliteConfig

            config = EliteConfig()
            generator = EDCoPilotContentGenerator(self.data_store, config.edcopilot_path)

            # Generate content without writing files
            files = generator.generate_contextual_chatter()

            # Filter to requested type
            target_file = None
            if chatter_type.lower() in ["space", "spacechatter"]:
                target_file = next((k for k in files.keys() if "SpaceChatter" in k), None)
            elif chatter_type.lower() in ["crew", "crewchatter"]:
                target_file = next((k for k in files.keys() if "CrewChatter" in k), None)
            elif chatter_type.lower() in ["deepspace", "deepspacechatter"]:
                target_file = next((k for k in files.keys() if "DeepSpaceChatter" in k), None)
            else:
                return {"error": f"Unknown chatter type: {chatter_type}"}

            if not target_file:
                return {"error": f"Could not find {chatter_type} chatter template"}

            content = files[target_file]

            # Count entries (non-comment, non-empty lines)
            lines = content.split('\n')
            entry_lines = [line for line in lines if line.strip() and not line.strip().startswith('#')]

            return {
                "status": "success",
                "chatter_type": chatter_type,
                "filename": target_file,
                "content_preview": content[:1000] + "..." if len(content) > 1000 else content,
                "total_lines": len(lines),
                "entry_count": len(entry_lines),
                "sample_entries": entry_lines[:5],
                "message": f"Generated {len(entry_lines)} {chatter_type} chatter entries"
            }

        except ImportError as e:
            return {
                "error": "EDCoPilot integration not available",
                "details": str(e)
            }
        except Exception as e:
            logger.error(f"Error previewing EDCoPilot chatter: {e}")
            return {"error": str(e)}

    async def server_status(self) -> Dict[str, Any]:
        """
        Get server status information.

        Returns:
            Dict containing server status information
        """
        try:
            stats = self.data_store.get_statistics()
            game_state = self.data_store.get_game_state()

            return {
                "server_running": True,
                "journal_monitoring": True,  # Assume monitoring is active if we have stats
                "uptime_seconds": stats.get('uptime_seconds', 0),
                "total_events": stats.get('total_processed', 0),
                "memory_usage_events": stats.get('memory_usage_events', 0),
                "current_system": game_state.current_system,
                "current_station": game_state.current_station,
                "last_updated": game_state.last_updated.isoformat() if game_state.last_updated else None,
                "data_store_stats": {
                    "total_events": stats.get('total_processed', 0),
                    "events_by_type": stats.get('events_by_type', {}),
                    "events_by_category": stats.get('events_by_category', {}),
                    "uptime_seconds": stats.get('uptime_seconds', 0),
                    "memory_usage_events": stats.get('memory_usage_events', 0),
                    "max_events": stats.get('max_events', 0),
                    "storage_efficiency": stats.get('storage_efficiency', 0)
                }
            }
        except Exception as e:
            logger.error(f"Error getting server status: {e}")
            return {
                "server_running": False,
                "error": str(e)
            }

    def get_recent_events(self, minutes: int = 60) -> Dict[str, Any]:
        """
        Get recent events from specified time range.

        Args:
            minutes: Number of minutes to look back

        Returns:
            Dict containing recent events information
        """
        try:
            from datetime import datetime, timezone, timedelta

            cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=minutes)
            events = self.data_store.get_recent_events(minutes)

            return {
                "event_count": len(events),
                "time_range_minutes": minutes,
                "cutoff_time": cutoff_time.isoformat(),
                "events": [
                    {
                        "timestamp": event.timestamp.isoformat(),
                        "event_type": event.event_type,
                        "category": event.category.value,
                        "summary": event.summary
                    }
                    for event in events
                ]
            }
        except Exception as e:
            logger.error(f"Error getting recent events: {e}")
            return {
                "event_count": 0,
                "error": str(e)
            }
