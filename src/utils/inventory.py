"""Inventory and loadout summaries built from journal events.

Pure functions, no I/O. The journal writes a full `Materials` snapshot only at
game start, then one small event per change, so the current inventory is the
newest snapshot plus every change after it.
"""

from typing import Any, Dict, Iterable, List, Optional

MATERIAL_CATEGORIES = ("raw", "manufactured", "encoded")

# Journal events that change the engineering material inventory.
MATERIAL_CHANGE_EVENTS = frozenset({
    "MaterialCollected", "MaterialDiscarded", "MaterialTrade", "EngineerCraft",
    "Synthesis", "TechnologyBroker", "MissionCompleted", "ScientificResearch",
    "EngineerContribution",
})

# Landing pad needed, by journal ship id. Only ships whose size is certain.
SHIP_PAD_SIZES: Dict[str, str] = {
    "sidewinder": "small", "eagle": "small", "hauler": "small", "adder": "small",
    "viper": "small", "viper_mkiv": "small", "cobramkiii": "small", "cobramkiv": "small",
    "cobramkv": "small", "diamondback": "small", "diamondbackxl": "small", "vulture": "small",
    "dolphin": "small", "empire_eagle": "small", "empire_courier": "small",
    "asp": "medium", "asp_scout": "medium", "type6": "medium", "type8": "medium",
    "independant_trader": "medium", "python": "medium", "python_nx": "medium",
    "krait_mkii": "medium", "krait_light": "medium", "mandalay": "medium", "corsair": "medium",
    "ferdelance": "medium", "mamba": "medium", "typex": "medium", "typex_2": "medium",
    "typex_3": "medium", "federation_dropship": "medium", "federation_dropship_mkii": "medium",
    "federation_gunship": "medium",
    "type7": "large", "type9": "large", "type9_military": "large", "anaconda": "large",
    "cutter": "large", "federation_corvette": "large", "belugaliner": "large", "orca": "large",
    "empire_trader": "large", "panthermkii": "large",
}

# Capability name -> substrings of module item ids that provide it.
_CAPABILITY_MARKERS: Dict[str, tuple] = {
    "mining_laser": ("mininglaser",),
    "refinery": ("int_refinery",),
    "collector_limpets": ("dronecontrol_collection", "multidronecontrol_mining",
                          "multidronecontrol_universal", "multidronecontrol_operations"),
    "prospector_limpets": ("dronecontrol_prospector", "multidronecontrol_mining",
                           "multidronecontrol_universal"),
    "core_mining_tools": ("seismiccharge", "subsurfacedisplacement", "abrasionblaster"),
    "fuel_scoop": ("int_fuelscoop",),
    "srv_bay": ("int_buggybay",),
    "surface_scanner": ("detailedsurfacescanner",),
    "cargo_racks": ("int_cargorack",),
    "fsd_booster": ("guardianfsdbooster",),
    "shields": ("int_shieldgenerator",),
}


def latest_by_timestamp(events: Iterable[Any]) -> Optional[Any]:
    """Return the event with the newest timestamp, or None.

    Storage order is not time order: an on-demand historical query appends old
    events after new ones. Anything that means "current" must sort by time.
    """
    newest = None
    for event in events:
        # ">=" so that of two events in the same second, the later-stored wins.
        if newest is None or event.timestamp >= newest.timestamp:
            newest = event
    return newest


def _category_key(value: Any) -> Optional[str]:
    """Normalise a journal category such as "Raw" or "$MICRORESOURCE_CATEGORY_Encoded;"."""
    text = str(value or "").lower()
    for category in MATERIAL_CATEGORIES:
        if category in text:
            return category
    return None


def _find_category(inventory: Dict[str, Dict[str, int]], name: str) -> Optional[str]:
    for category in MATERIAL_CATEGORIES:
        if name in inventory[category]:
            return category
    return None


def _adjust(inventory: Dict[str, Dict[str, int]], names: Dict[str, str], name: Any,
            delta: int, category: Any = None, display: Any = None) -> None:
    """Add delta units of a material. Counts never go below zero."""
    key = str(name or "").strip().lower()
    if not key or not delta:
        return
    bucket = _category_key(category) or _find_category(inventory, key)
    if bucket is None:
        # A spend of something the snapshot never listed: nothing to subtract from.
        return
    current = inventory[bucket].get(key, 0)
    updated = max(0, current + int(delta))
    if updated == 0:
        inventory[bucket].pop(key, None)
    else:
        inventory[bucket][key] = updated
    if display and key not in names:
        names[key] = str(display)


def _apply_change(inventory: Dict[str, Dict[str, int]], names: Dict[str, str],
                  event_type: str, raw: Dict[str, Any]) -> bool:
    """Apply one journal event to the inventory. Returns True if it changed anything."""
    before = {category: dict(inventory[category]) for category in MATERIAL_CATEGORIES}
    if event_type == "MaterialCollected":
        _adjust(inventory, names, raw.get("Name"), raw.get("Count", 1), raw.get("Category"),
                raw.get("Name_Localised"))
    elif event_type == "MaterialDiscarded":
        _adjust(inventory, names, raw.get("Name"), -int(raw.get("Count", 1)), raw.get("Category"))
    elif event_type == "MaterialTrade":
        paid = raw.get("Paid") or {}
        received = raw.get("Received") or {}
        _adjust(inventory, names, paid.get("Material"), -int(paid.get("Quantity", 0)),
                paid.get("Category"))
        _adjust(inventory, names, received.get("Material"), int(received.get("Quantity", 0)),
                received.get("Category"), received.get("Material_Localised"))
    elif event_type in ("EngineerCraft",):
        for item in raw.get("Ingredients") or []:
            _adjust(inventory, names, item.get("Name"), -int(item.get("Count", 0)))
    elif event_type in ("Synthesis", "TechnologyBroker"):
        for item in raw.get("Materials") or []:
            _adjust(inventory, names, item.get("Name"), -int(item.get("Count", 0)),
                    item.get("Category"))
    elif event_type == "MissionCompleted":
        for item in raw.get("MaterialsReward") or []:
            _adjust(inventory, names, item.get("Name"), int(item.get("Count", 0)),
                    item.get("Category"), item.get("Name_Localised"))
    elif event_type == "ScientificResearch":
        _adjust(inventory, names, raw.get("Name"), -int(raw.get("Count", 0)), raw.get("Category"))
    elif event_type == "EngineerContribution":
        if raw.get("Type") == "Materials":
            _adjust(inventory, names, raw.get("Material"), -int(raw.get("Quantity", 0)))
    return any(inventory[category] != before[category] for category in MATERIAL_CATEGORIES)


def compute_material_inventory(snapshot_raw: Optional[Dict[str, Any]],
                               later_events: Iterable[Any]) -> Dict[str, Any]:
    """Build the current material inventory.

    Args:
        snapshot_raw: raw `Materials` journal event, or None if none is loaded.
        later_events: ProcessedEvents after the snapshot, any order.

    Returns:
        {"materials": {raw, manufactured, encoded}, "names": {id: display name},
         "changes_applied": int}
    """
    inventory: Dict[str, Dict[str, int]] = {category: {} for category in MATERIAL_CATEGORIES}
    names: Dict[str, str] = {}
    for label in ("Raw", "Manufactured", "Encoded"):
        for item in (snapshot_raw or {}).get(label) or []:
            key = str(item.get("Name") or "").strip().lower()
            if not key:
                continue
            inventory[label.lower()][key] = int(item.get("Count", 0))
            if item.get("Name_Localised"):
                names[key] = str(item["Name_Localised"])

    applied = 0
    for event in sorted(later_events, key=lambda e: e.timestamp):
        if event.event_type in MATERIAL_CHANGE_EVENTS and _apply_change(
                inventory, names, event.event_type, event.raw_event or {}):
            applied += 1
    return {"materials": inventory, "names": names, "changes_applied": applied}


def summarize_loadout(loadout_raw: Dict[str, Any]) -> Dict[str, Any]:
    """Summarise a raw `Loadout` journal event for answering fit questions."""
    modules: List[Dict[str, Any]] = []
    items: List[str] = []
    for module in loadout_raw.get("Modules") or []:
        item = str(module.get("Item") or "")
        items.append(item.lower())
        entry: Dict[str, Any] = {"slot": module.get("Slot"), "item": item}
        engineering = module.get("Engineering")
        if engineering:
            entry["engineering"] = {
                "blueprint": engineering.get("BlueprintName"),
                "level": engineering.get("Level"),
                "experimental": engineering.get("ExperimentalEffect_Localised")
                or engineering.get("ExperimentalEffect"),
            }
        modules.append(entry)

    capabilities = {
        name: any(marker in item for item in items for marker in markers)
        for name, markers in _CAPABILITY_MARKERS.items()
    }
    ship = str(loadout_raw.get("Ship") or "").lower()
    fuel = loadout_raw.get("FuelCapacity") or {}
    jump = loadout_raw.get("MaxJumpRange")
    return {
        "ship_type": ship or None,
        "ship_name": loadout_raw.get("ShipName") or None,
        "ship_id": loadout_raw.get("ShipIdent") or None,
        "landing_pad": SHIP_PAD_SIZES.get(ship, "unknown"),
        "max_jump_range_ly": round(float(jump), 2) if jump is not None else None,
        "cargo_capacity_t": loadout_raw.get("CargoCapacity"),
        "fuel_capacity_t": fuel.get("Main") if isinstance(fuel, dict) else None,
        "hull_value": loadout_raw.get("HullValue", 0),
        "modules_value": loadout_raw.get("ModulesValue", 0),
        "rebuy": loadout_raw.get("Rebuy", 0),
        "capabilities": capabilities,
        "can_laser_mine": capabilities["mining_laser"] and capabilities["refinery"],
        "module_count": len(modules),
        "modules": modules,
    }
