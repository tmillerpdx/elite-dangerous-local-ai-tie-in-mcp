"""
Tests for material inventory and ship status.

Three defects are covered:
1. "Latest" was chosen by storage order. A historical query appends old events
   after new ones, so the tools then reported a weeks-old inventory and ship.
2. Inventory was the login snapshot only; pickups and spends made afterwards
   were ignored.
3. The ship's module list was always empty.
"""

from src.elite_mcp.mcp_tools import MCPTools
from src.journal.events import EventProcessor
from src.utils.data_store import DataStore
from src.utils.inventory import (
    SHIP_PAD_SIZES,
    compute_material_inventory,
    latest_by_timestamp,
    summarize_loadout,
)

PROCESSOR = EventProcessor()


def _event(timestamp, name, **fields):
    raw = {"timestamp": timestamp, "event": name}
    raw.update(fields)
    return PROCESSOR.process_event(raw)


def _materials(timestamp, raw=None, manufactured=None, encoded=None):
    def items(mapping):
        return [{"Name": k, "Count": v} for k, v in (mapping or {}).items()]

    return _event(timestamp, "Materials", Raw=items(raw), Manufactured=items(manufactured),
                  Encoded=items(encoded))


def _loadout(timestamp, ship="mandalay", modules=None, **extra):
    fields = {
        "Ship": ship, "ShipName": "Audrey jean", "ShipIdent": "ajb", "HullValue": 16114020,
        "ModulesValue": 41564054, "Rebuy": 2162928, "CargoCapacity": 32,
        "MaxJumpRange": 84.721603, "FuelCapacity": {"Main": 32.0, "Reserve": 0.5},
        "Modules": modules if modules is not None else [
            {"Slot": "TinyHardpoint1", "Item": "hpt_heatsinklauncher_turret_tiny"},
            {"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_overcharge_size5_class5",
             "Engineering": {"BlueprintName": "FSD_LongRange", "Level": 5,
                             "ExperimentalEffect_Localised": "Mass Manager"}},
            {"Slot": "Slot01_Size6", "Item": "int_fuelscoop_size6_class5"},
            {"Slot": "Slot04_Size3", "Item": "int_buggybay_size2_class2"},
            {"Slot": "Slot07_Size1", "Item": "int_detailedsurfacescanner_tiny"},
        ],
    }
    fields.update(extra)
    return _event(timestamp, "Loadout", **fields)


MINER_MODULES = [
    {"Slot": "MediumHardpoint1", "Item": "hpt_mininglaser_fixed_medium"},
    {"Slot": "Slot02_Size4", "Item": "int_refinery_size4_class5"},
    {"Slot": "Slot03_Size3", "Item": "int_dronecontrol_collection_size3_class5"},
    {"Slot": "Slot04_Size1", "Item": "int_dronecontrol_prospector_size1_class5"},
    {"Slot": "Slot01_Size8", "Item": "int_cargorack_size8_class1"},
]


class TestLatestByTimestamp:
    def test_picks_newest_regardless_of_order(self):
        new = _event("2026-10-01T06:47:56Z", "Materials")
        old = _event("2026-08-14T20:05:48Z", "Materials")
        assert latest_by_timestamp([new, old]) is new
        assert latest_by_timestamp([old, new]) is new

    def test_empty_gives_none(self):
        assert latest_by_timestamp([]) is None


class TestComputeMaterialInventory:
    def test_snapshot_only(self):
        snap = _materials("2026-10-01T06:47:56Z", raw={"iron": 99, "carbon": 70},
                          manufactured={"hybridcapacitors": 12}, encoded={"symmetrickeys": 3})
        result = compute_material_inventory(snap.raw_event, [])
        assert result["materials"]["raw"] == {"iron": 99, "carbon": 70}
        assert result["materials"]["manufactured"] == {"hybridcapacitors": 12}
        assert result["materials"]["encoded"] == {"symmetrickeys": 3}
        assert result["changes_applied"] == 0

    def test_no_snapshot_gives_empty_inventory(self):
        result = compute_material_inventory(None, [])
        assert result["materials"] == {"raw": {}, "manufactured": {}, "encoded": {}}

    def test_collected_adds_including_a_new_material(self):
        snap = _materials("2026-10-01T06:00:00Z", raw={"iron": 10})
        later = [
            _event("2026-10-01T06:10:00Z", "MaterialCollected", Category="Raw", Name="iron", Count=3),
            _event("2026-10-01T06:11:00Z", "MaterialCollected", Category="Raw", Name="selenium",
                   Count=3),
        ]
        result = compute_material_inventory(snap.raw_event, later)
        assert result["materials"]["raw"] == {"iron": 13, "selenium": 3}
        assert result["changes_applied"] == 2

    def test_trade_moves_both_sides(self):
        snap = _materials("2026-10-01T06:00:00Z",
                          manufactured={"configurablecomponents": 5, "mechanicalcomponents": 1})
        trade = _event("2026-10-01T06:20:00Z", "MaterialTrade",
                       Paid={"Material": "configurablecomponents", "Category": "Manufactured",
                             "Quantity": 2},
                       Received={"Material": "mechanicalcomponents", "Category": "Manufactured",
                                 "Quantity": 6})
        result = compute_material_inventory(snap.raw_event, [trade])
        assert result["materials"]["manufactured"] == {
            "configurablecomponents": 3, "mechanicalcomponents": 7}

    def test_engineering_synthesis_and_broker_spend(self):
        snap = _materials("2026-10-01T06:00:00Z", raw={"iron": 5, "carbon": 4, "germanium": 30},
                          manufactured={"hybridcapacitors": 2, "mechanicalscrap": 30})
        later = [
            _event("2026-10-01T06:30:00Z", "EngineerCraft",
                   Ingredients=[{"Name": "iron", "Count": 1}, {"Name": "hybridcapacitors", "Count": 1}]),
            _event("2026-10-01T06:31:00Z", "Synthesis",
                   Materials=[{"Name": "carbon", "Count": 1}]),
            _event("2026-10-01T06:32:00Z", "TechnologyBroker",
                   Materials=[{"Name": "mechanicalscrap", "Count": 26, "Category": "Manufactured"},
                              {"Name": "germanium", "Count": 22, "Category": "Raw"}]),
        ]
        result = compute_material_inventory(snap.raw_event, later)
        assert result["materials"]["raw"] == {"iron": 4, "carbon": 3, "germanium": 8}
        assert result["materials"]["manufactured"] == {"hybridcapacitors": 1, "mechanicalscrap": 4}

    def test_mission_reward_with_microresource_category(self):
        snap = _materials("2026-10-01T06:00:00Z", encoded={"archivedemissiondata": 1})
        reward = _event("2026-10-01T06:40:00Z", "MissionCompleted", MaterialsReward=[
            {"Name": "ArchivedEmissionData", "Name_Localised": "Irregular Emission Data",
             "Category": "$MICRORESOURCE_CATEGORY_Encoded;", "Count": 14}])
        result = compute_material_inventory(snap.raw_event, [reward])
        assert result["materials"]["encoded"] == {"archivedemissiondata": 15}

    def test_counts_never_go_negative_and_zero_is_removed(self):
        snap = _materials("2026-10-01T06:00:00Z", raw={"iron": 1})
        spend = _event("2026-10-01T06:30:00Z", "EngineerCraft",
                       Ingredients=[{"Name": "iron", "Count": 5}, {"Name": "unknownthing", "Count": 2}])
        result = compute_material_inventory(snap.raw_event, [spend])
        assert result["materials"]["raw"] == {}

    def test_changes_are_applied_in_time_order(self):
        snap = _materials("2026-10-01T06:00:00Z", raw={"iron": 1})
        spend = _event("2026-10-01T06:30:00Z", "EngineerCraft", Ingredients=[{"Name": "iron", "Count": 3}])
        collect = _event("2026-10-01T06:10:00Z", "MaterialCollected", Category="Raw", Name="iron",
                         Count=3)
        result = compute_material_inventory(snap.raw_event, [spend, collect])
        assert result["materials"]["raw"] == {"iron": 1}

    def test_display_names_are_collected(self):
        snap = PROCESSOR.process_event({
            "timestamp": "2026-10-01T06:00:00Z", "event": "Materials", "Raw": [],
            "Manufactured": [{"Name": "hybridcapacitors", "Name_Localised": "Hybrid Capacitors",
                              "Count": 2}], "Encoded": []})
        result = compute_material_inventory(snap.raw_event, [])
        assert result["names"] == {"hybridcapacitors": "Hybrid Capacitors"}


class TestSummarizeLoadout:
    def test_explorer_fit(self):
        summary = summarize_loadout(_loadout("2026-10-01T06:48:54Z").raw_event)
        assert summary["ship_type"] == "mandalay"
        assert summary["ship_name"] == "Audrey jean"
        assert summary["landing_pad"] == "medium"
        assert summary["max_jump_range_ly"] == 84.72
        assert summary["cargo_capacity_t"] == 32
        assert summary["fuel_capacity_t"] == 32.0
        assert summary["rebuy"] == 2162928
        assert summary["module_count"] == 5
        caps = summary["capabilities"]
        assert caps["fuel_scoop"] and caps["srv_bay"] and caps["surface_scanner"]
        assert not caps["mining_laser"] and not caps["refinery"]
        assert summary["can_laser_mine"] is False

    def test_engineering_is_reported_per_module(self):
        summary = summarize_loadout(_loadout("2026-10-01T06:48:54Z").raw_event)
        fsd = [m for m in summary["modules"] if m["slot"] == "FrameShiftDrive"][0]
        assert fsd["engineering"] == {"blueprint": "FSD_LongRange", "level": 5,
                                      "experimental": "Mass Manager"}
        assert "engineering" not in summary["modules"][0]

    def test_mining_fit(self):
        raw = _loadout("2026-10-01T06:48:54Z", ship="type9", modules=MINER_MODULES).raw_event
        summary = summarize_loadout(raw)
        assert summary["landing_pad"] == "large"
        assert summary["can_laser_mine"] is True
        assert summary["capabilities"]["collector_limpets"]
        assert summary["capabilities"]["prospector_limpets"]
        assert summary["capabilities"]["cargo_racks"]

    def test_unknown_ship_has_unknown_pad(self):
        summary = summarize_loadout(_loadout("2026-10-01T06:48:54Z", ship="brandnewship").raw_event)
        assert summary["landing_pad"] == "unknown"

    def test_pad_table_uses_known_sizes_only(self):
        assert set(SHIP_PAD_SIZES.values()) == {"small", "medium", "large"}
        assert SHIP_PAD_SIZES["type8"] == "medium"
        assert SHIP_PAD_SIZES["type9"] == "large"


class TestToolsUseNewestEvents:
    """End to end through MCPTools with a real DataStore."""

    def _tools(self, *events):
        store = DataStore()
        for event, update_state in events:
            store.store_event(event, update_state=update_state)
        return MCPTools(store)

    async def test_inventory_ignores_older_snapshot_appended_later(self):
        current = _materials("2026-10-01T06:47:56Z", raw={"iron": 99},
                             manufactured={"hybridcapacitors": 12})
        stale = _materials("2026-08-14T20:05:48Z", raw={"iron": 3, "selenium": 3})
        tools = self._tools((current, True), (stale, False))
        result = await tools.get_material_inventory()
        assert result["materials"]["raw"] == {"iron": 99}
        assert result["materials"]["manufactured"] == {"hybridcapacitors": 12}
        assert result["snapshot_timestamp"].startswith("2026-10-01T06:47:56")

    async def test_inventory_applies_changes_after_the_snapshot(self):
        snap = _materials("2026-10-01T06:47:56Z", raw={"iron": 99})
        before = _event("2026-10-01T06:00:00Z", "MaterialCollected", Category="Raw", Name="iron",
                        Count=50)
        after = _event("2026-10-01T06:50:00Z", "MaterialCollected", Category="Raw",
                       Name="selenium", Count=3)
        tools = self._tools((before, True), (snap, True), (after, True))
        result = await tools.get_material_inventory()
        assert result["materials"]["raw"] == {"iron": 99, "selenium": 3}
        assert result["changes_since_snapshot"] == 1

    async def test_inventory_without_snapshot_says_so(self):
        result = await self._tools().get_material_inventory()
        assert result["snapshot_timestamp"] is None
        assert "warning" in result

    async def test_ship_status_uses_newest_loadout_and_lists_modules(self):
        current = _loadout("2026-10-01T06:48:54Z")
        stale = _loadout("2026-08-14T20:06:00Z", ship="sidewinder", modules=[],
                         HullValue=0, ModulesValue=9121, Rebuy=342, ShipName="", ShipIdent="PA-15M")
        tools = self._tools((current, True), (stale, False))
        result = await tools.get_ship_status()
        assert result["ship_type"] == "mandalay"
        assert result["ship_name"] == "Audrey jean"
        assert result["rebuy"] == 2162928
        assert result["hull_value"] == 16114020
        assert result["landing_pad"] == "medium"
        assert result["module_count"] == 5
        assert len(result["modules"]) == 5
        assert result["can_laser_mine"] is False
        assert result["loadout_timestamp"].startswith("2026-10-01T06:48:54")

    async def test_ship_status_without_loadout_still_answers(self):
        result = await self._tools().get_ship_status()
        assert result["ship_type"] == "Unknown"
        assert result["modules"] == []
        assert result["loadout_timestamp"] is None
