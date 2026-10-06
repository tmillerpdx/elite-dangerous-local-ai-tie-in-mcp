# elite-dangerous MCP server: tool reference

Tool names are prefixed `mcp__elite-dangerous__`. Status reflects what has been checked against the commander's real journals. Update this file when a tool is added, fixed or found wrong.

Contents: search tools, route tool, trade planning tool, wing mining tools, state tools, event tools, summaries, known wrong, do not use, Spansh behaviour.

## Search tools (trusted)

All four query the public Spansh database and default to the commander's current system. All return `reference_system`, `reference_source`, `result_count`, `results`, and `notes`. `limit` caps at 50.

### find_commodity_market
Stations where a commodity can be bought or sold.

| Parameter | Default | Meaning |
|---|---|---|
| `commodity` | required | Any capitalisation. A misspelling returns `did_you_mean`. |
| `mode` | `buy` | `buy` = stations selling to the commander. `sell` = stations buying from them. |
| `reference_system` | current | |
| `min_quantity` | 1 | Stock when buying, demand when selling. |
| `max_distance_ly` | 50 | |
| `large_pad_only` | false | |
| `include_fleet_carriers` | false | Carrier data is often months or years old. |
| `max_data_age_days` | 30 | 0 = no limit. |
| `max_arrival_ls` | 0 | 0 = no limit. |
| `sort_by` | `distance` | `price` = cheapest when buying, highest when selling. |

Each result: `system`, `station`, `station_type`, `distance_ly`, `arrival_distance_ls`, `has_large_pad`, `is_planetary`, `is_fleet_carrier`, `price`, `quantity`, `data_age_days`.

### find_mining_hotspots
Nearest ring hotspots for a commodity.

Parameters: `commodity` (default Platinum), `reference_system`, `min_hotspots` (1), `max_distance_ly` (100), `pristine_only` (false), `limit`.

Laser mined: Platinum, Painite, Bromellite, Tritium. Laser or core: Low Temperature Diamonds. Core mined: Alexandrite, Benitoite, Grandidierite, Monazite, Musgravite, Rhodplumsite, Serendibite, Void Opal.

Each result: `system`, `body`, `distance_ly`, `arrival_distance_ls`, `reserve_level`, `best_hotspot_count`, `rings` (each with `ring`, `ring_type`, `hotspots`, `other_hotspots`). The response states `mining_method`.

Hotspot counts are per ring. Whether hotspots overlap is not recorded anywhere.

### find_exobiology_targets
Nearest landable bodies with biological signals.

Parameters: `reference_system`, `min_bio_signals` (2), `max_distance_ly` (50), `max_gravity_g` (0 = no limit), `max_arrival_ls` (0 = no limit), `limit`.

Each result: `system`, `body`, `distance_ly`, `arrival_distance_ls`, `body_type`, `atmosphere`, `gravity_g`, `surface_temperature_k`, `biological_signals`, `known_genera`, `known_species` (name and value), `known_species_value`, `unidentified_signals`.

`known_species_value` is the base value of species already reported. The first-logged bonus is not included. These bodies have all been scanned by someone, so none are undiscovered.

### find_material_bodies
Landable bodies carrying raw engineering materials.

Parameters: `materials` (required; one name or several comma separated, first is primary), `reference_system`, `min_percent` (0, applies to the primary), `max_distance_ly` (50), `max_gravity_g`, `max_arrival_ls`, `sort_by` (`distance` or `percent`), `limit`.

Raw materials by grade:
1. Carbon, Iron, Lead, Nickel, Phosphorus, Rhenium, Sulphur
2. Arsenic, Chromium, Germanium, Manganese, Vanadium, Zinc, Zirconium
3. Boron, Cadmium, Mercury, Molybdenum, Niobium, Tin, Tungsten
4. Antimony, Polonium, Ruthenium, Selenium, Technetium, Tellurium, Yttrium

Each result: `system`, `body`, `distance_ly`, `arrival_distance_ls`, `body_type`, `gravity_g`, `volcanism`, `geological_signals`, `biological_signals`, `requested_percent`, `all_materials_percent`.

Bodies with geological signals have sites that drop materials in larger amounts. Manufactured and encoded materials are not found this way; they come from signal sources, missions and scans.

## Route tool (new, lightly checked)

### plot_neutron_route
Neutron-highway route to a destination, from the Spansh route plotters. Checked against one live route; treat fuel figures as a plan, not a guarantee. Takes up to two minutes.

| Parameter | Default | Meaning |
|---|---|---|
| `destination` | required | System name. |
| `origin` | current | |
| `jump_range_ly` | 0 | 0 = read the ship from the journal. A value selects simple mode with that range. |
| `efficiency` | 60 | Simple mode only, 1-100. |
| `mode` | empty | `fuel`, `simple`, or empty to use fuel when the journal ship allows it. |

The response states `mode`, `origin`, `destination`, `distance_ly`, `total_jumps`, `neutron_boosts`, `origin_source`, `ship_source` and `notes`.

- **Fuel mode** (`ship_source` = `journal_loadout`): every waypoint is one jump, with `system`, `distance_ly`, `remaining_ly`, `fuel_in_tank_t`, `fuel_used_t`, `neutron_star`, `scoopable`, `must_refuel`. Also returns `refuel_stops`. Fuel assumes an empty cargo hold.
- **Simple mode**: each waypoint is a system to plot to, with `system`, `neutron_star`, `jumps`, `distance_ly`, `remaining_ly`. No fuel modelling. When the range comes from the journal (`journal_max_jump_range`) it is the best-case range, so the plan is optimistic.

Drives it has no fuel data for (size 8, special drives) fall back to simple mode and say so in `notes`. Spansh only routes through neutron stars players have reported. Long routes return hundreds of waypoints: summarise the totals and the next few stops instead of listing them all.

## Trade planning tool (new, lightly checked)

### plan_trade_route
Most profitable multi-stop trade run, planned by Trade Dangerous running locally against its own price database. Makes no network request. Needs Trade Dangerous installed and its database imported; if not, it returns an error naming `ELITE_TD_PYTHON`. Can take a minute or more.

| Parameter | Default | Meaning |
|---|---|---|
| `origin` | current | `System/Station` or a system. Current station when docked, otherwise current system. |
| `destination` | none | Where the run must end. |
| `credits` | journal | Credits to trade with. |
| `cargo_capacity` | journal | Tonnes. |
| `jump_range_ly` | journal | Laden range computed from the Loadout (full tank, full hold). |
| `hops` | 2 | Station-to-station trades, max 6. |
| `max_jumps_per_hop` | 0 | 0 = Trade Dangerous decides. |
| `max_data_age_days` | 2 | 0 = no limit. |
| `pad_size` | journal ship | `S`, `M` or `L`. |
| `routes` | 1 | Alternatives to return, max 5. |

The response has `origin`, `search`, `inputs` (where each default came from), `database_updated_at`, `database_age_days`, `route_count`, `routes` and `notes`. Each route: `total_profit`, `starting_credits`, `ending_credits`, `hops`. Each hop: `from`, `to`, `to_arrival_distance_ls`, `to_pad_size`, `profit`, `jumps`, `distance_ly`, `via_systems`, and `cargo` lines (`commodity`, `units`, `buy_price`, `sell_price`, `profit_per_unit`, `profit`, `supply`, `demand`).

The database is only as fresh as its last import, which the commander runs by hand. Always state `database_age_days`; per-price ages are not available. If the commander is docked on a fleet carrier the run starts from the system instead, and a note says so. A remote system can return "no profitable trade"; suggest a larger `max_jumps_per_hop` or a different origin. For a single commodity, `find_commodity_market` is live and quicker; use this tool when the question is what to haul, or a loop of several stops.

## Wing mining mission (WMM) tools (new, checked against real journals with no active stack)

Both read the journal files directly on every call, so they see more than the server's one-day event window. Local files only.

### get_wmm_stack
The active mission stack. Parameter: `cargo_capacity` (0 = the journal ship's hold) for the hauling plan.

Returns `wmm_count` (good missions), `flagged_count`, `active_mission_count` and `mission_slots_left` (the game limit is 20 missions of any kind), `total_reward`, `earliest_expiry` (`mission_id`, `expiry`, `hours_left`, `alert` when under 48 hours), `totals_by_commodity` (`missions`, `tons_required`, `tons_delivered`, `tons_remaining`, `reward`), `hauling_plan` (`loads_by_commodity`, `total_tons_remaining`, `total_loads`), `missions` and `board_refresh` (next ten-minute boundary).

Each mission: `mission_id`, `title`, `faction`, `station`, `system`, `commodity`, `tons_required`, `tons_delivered`, `tons_remaining`, `reward`, `expiry`, `hours_left`, `wing`, `flags`.

Flags: `wrong_commodity` (anything but Gold, Silver, Bertrandite, Indite; this catches Bromellite and Indium), `source_and_return`, `not_wing`, `unsupported_station` (not Burkin Orbital or Darlton Port in Mbutas, or Rukavishnikov Terminal in Paemara), `details_unknown` (active, but its acceptance is not in the last 14 days of journals; a mission shared by a wingmate looks like this). Flagged missions are left out of the totals and the hauling plan. Tell the commander about every flagged mission.

Not yet seen with a real stack: the commander had no wing mining missions when this was built, so the source-and-return check (mission name contains "Collect") and how missions received from wingmates appear are untested against real data.

### get_faction_reputation
Reputation per minor faction. Parameter: `systems`, comma separated; empty = Mbutas and Paemara.

Per system: `as_of` and `age_days` (when the game last wrote it), `factions` (`faction`, `reputation` from -100 to 100, `standing`, `allied`, `state`, `influence_percent`, `offers_wmm`), `not_allied`, `not_at_full_reputation`. A system not visited in the last 180 days of journals comes back with a note instead.

The value only updates when the commander enters or logs in to the system, so always state `age_days`. Allied is taken as 90 or more; that threshold is documented for superpowers and assumed for minor factions. Paemara Gold Posse offers no wing mining missions and is left out of the not-allied lists.

## State tools

| Tool | Status | Notes |
|---|---|---|
| `get_current_location` | Trusted | System, station, body, docked, coordinates, recent jumps. Correct across fleet carrier jumps and server restarts. |
| `server_status` | Trusted | Event counts and whether journal monitoring is running. |
| `get_ship_status` | Trusted | Current ship only. `ship_type`, `ship_name`, `landing_pad` (small, medium, large or unknown), `max_jump_range_ly`, `cargo_capacity_t`, `fuel_capacity_t`, `hull_value`, `modules_value`, `rebuy`, `module_count`, `modules` (slot, item, engineering blueprint, level, experimental), `capabilities`, `can_laser_mine`, `loadout_timestamp`. |
| `get_material_inventory` | Trusted | `materials` by `raw`, `manufactured`, `encoded`, keyed by lower-case journal id; `material_names` for display names; `snapshot_timestamp`; `changes_since_snapshot`; `cargo`; `carrier_cargo`. Absent material = zero held. Null `snapshot_timestamp` = inventory unknown. |

`capabilities` flags: `mining_laser`, `refinery`, `collector_limpets`, `prospector_limpets`, `core_mining_tools`, `fuel_scoop`, `srv_bay`, `surface_scanner`, `cargo_racks`, `fsd_booster`, `shields`.

Both tools were fixed on 1 October 2026. Before that they chose "latest" by load order, so a historical search made them report a weeks-old ship and inventory. A server still running older code shows it by returning no modules.

## Event tools

| Tool | Status | Notes |
|---|---|---|
| `get_recent_events` | Trusted | Events from the last N minutes. |
| `search_events` | Trusted | Filter by type, category, time, system, text. Covers what the server has loaded: roughly the last day, plus the last session if older. |
| `search_historical_events` | Use with care | Searches all history. Common events such as `MarketSell` come back with usable `key_data`; others, such as `Statistics` and `Materials`, come back empty, so check before relying on it for figures. On server builds from before 1 October 2026 a historical search also overwrote the live location with the old events' location. If the location looks stale after one, the server needs a restart. |

## Raw journal access (trusted)

Both read files directly and never change the server's state.

### search_journal_history
Every event from every journal file, as the original JSON.

| Parameter | Default | Meaning |
|---|---|---|
| `event_types` | all | List of journal event names, any capitalisation. |
| `start_date`, `end_date` | none | Inclusive. ISO date or natural language ("yesterday", "30 days ago"). |
| `contains_text` | none | Keep events whose JSON contains this text. |
| `fields` | whole event | Dotted paths, e.g. `Bank_Account.Current_Wealth`, `Modules.0.Item`. Results then hold `timestamp`, `event` and those values. |
| `limit` | 200 | Max 5000. |
| `sort_order` | `desc` | `asc` for oldest first. |

Returns `events`, `returned_count`, `matched_count`, `truncated`, `files_scanned`, `date_range`, and a `note` when the result was cut short. A result stops growing at about 300,000 characters.

Useful recipes:
- Net worth series: `event_types=["Statistics"]`, `fields=["Bank_Account.Current_Wealth"]`, `sort_order="asc"`, `limit=5000`.
- Sales of one commodity: `event_types=["MarketSell"]`, `contains_text="wine"`, `fields=["Count", "TotalSale"]`.
- Systems visited in a period: `event_types=["FSDJump", "CarrierJump"]`, `fields=["StarSystem", "JumpDist"]`.
- A past ship fit: `event_types=["Loadout"]`, `end_date=<date>`, `limit=1`.
- Exobiology income: `event_types=["SellOrganicData"]`.

### get_live_file
The state files the game rewrites in place.

Parameters: `filename` (with or without `.json`; empty lists the files), `item_filter` (keep entries of the file's main list containing this text).

Returns `filename`, `modified_at`, `age_seconds`, `data`, and `items_matched` / `items_total` when filtered.

| File | Holds | Updated when |
|---|---|---|
| `Status` | Flags, fuel, pips, cargo mass, balance | Every second or so in game |
| `Cargo` | Hold contents | Cargo changes |
| `NavRoute` | Plotted route, each stop with coordinates and star class | A route is plotted or cleared |
| `Market` | Every commodity at one station with prices, stock, demand | The commander opens the market screen |
| `Outfitting`, `Shipyard` | What one station sells | The commander opens that screen |
| `ModulesInfo` | Fitted modules with power use | Loadout changes |
| `ShipLocker`, `Backpack` | On-foot items | They change |
| `FCMaterials` | Fleet carrier bartender stock | The commander opens it |

`Market`, `Outfitting` and `Shipyard` can be a long way out of date and for a different station than the one the commander is at. Read the `StationName` inside.

## Summaries (unverified)

`get_activity_summary`, `get_exploration_summary`, `get_trading_summary`, `get_combat_summary`, `get_mining_summary`, `get_mission_summary`, `get_engineering_summary`, `get_journey_summary`, `get_performance_metrics`, `get_faction_standings` (always returns empty reputation: it reads the superpower `Reputation` event in the wrong shape and never reads minor factions; use `get_faction_reputation`).

All take `time_range_hours` except faction standings. They cover only what the server has loaded, so a 30 day range does not return 30 days. Verify any figure before stating it.

## Do not use for answering questions

- **Prompt generators:** `list_available_prompts`, `generate_analysis_prompt`, `generate_exploration_prompt`, `generate_trading_prompt`, `generate_combat_prompt`, `generate_mining_prompt`, `generate_mission_prompt`, `generate_engineering_prompt`, `generate_journey_prompt`, `generate_performance_prompt`, `generate_strategic_prompt`, `generate_custom_prompt`. They return prompt text, not data.
- **Resource plumbing:** `list_available_resources`, `get_resource_data`, `refresh_resource_cache`.
- **EDCoPilot:** `generate_edcopilot_chatter`, `preview_edcopilot_chatter`, `get_edcopilot_status`, `backup_edcopilot_files`, `set_edcopilot_theme`, `get_theme_status`, `generate_themed_templates_prompt`, `apply_generated_templates`, `configure_ship_crew`, `set_crew_member_theme`, `generate_crew_setup_prompt`, `preview_themed_content`, `reset_theme`, `backup_current_themes`. Only when the commander asks for EDCoPilot chatter. Preview first; generating overwrites files in the EDCoPilot folder.
- **`clear_data_store`:** wipes the server's loaded events. Never call it unless the commander asks.

## Spansh behaviour worth knowing

- It only knows systems that players running an upload tool have visited. Coverage is dense in inhabited space and thin far out.
- Market, hotspot and signal data are as fresh as the last player upload. Always surface the age.
- An unknown system name returns an error, not an empty list.
- It occasionally returns a gateway error for a moment. The server retries once. If it still fails, try again shortly.

## Journal files, when you can read them

`%USERPROFILE%\Saved Games\Frontier Developments\Elite Dangerous\`

- `Journal.<local date>T<time>.01.log` - one JSON event per line, one file per game session.
- `Status.json`, `Cargo.json`, `NavRoute.json`, `Market.json`, `ShipLocker.json`, `ModulesInfo.json` - current state, rewritten live.

Useful events for ground truth:
- `Materials` - full engineering material inventory, written at each game start.
- `Loadout` - current ship, modules, hull value, rebuy.
- `Statistics` - `Bank_Account.Current_Wealth` is net worth, written at each game start.
- `LoadGame` - credits and current ship.
- `MarketSell`, `MarketBuy`, `SellOrganicData`, `MultiSellExplorationData`, `MissionCompleted`, `RedeemVoucher` - income.
- `Location`, `FSDJump`, `CarrierJump`, `Docked` - where the commander is.
