# Changelog

All notable changes to the Elite Dangerous Local AI Tie-In MCP will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Nearby search tools backed by the public Spansh body search:
  `find_mining_hotspots` (ring hotspots for a commodity) and
  `find_exobiology_targets` (landable bodies with biological signals).
  Both default to the commander's current system. New module
  `src/utils/spansh_client.py`; new dependency `httpx`.
- `find_material_bodies`: nearest or richest landable bodies carrying one or
  more raw engineering materials, via Spansh. Reports surface share,
  gravity, volcanism and geological signal count.
- `find_commodity_market`: nearest or best-priced stations to buy or sell a
  commodity, via the Spansh station search. Filters on stock or demand, pad
  size, data age and arrival distance; leaves out fleet carriers by default;
  corrects capitalisation and suggests names for misspellings.
- `plot_neutron_route`: neutron-highway route to a destination, via the
  Spansh route plotters. Starts from the current system and reads the ship
  from the journal `Loadout`, so fuel is modelled for every jump and refuel
  stops are marked. Falls back to the plain neutron plotter with a jump
  range when the drive is not recognised or `jump_range_ly` is given.
- `plan_trade_route`: most profitable multi-stop trade run, planned by a
  separately installed Trade Dangerous (https://github.com/eyeonus/Trade-Dangerous).
  Starts from the current station and reads credits, cargo capacity, laden
  jump range and pad size from the journal. New module
  `src/utils/trade_dangerous.py` and bridge script `scripts/td_bridge.py`;
  configured with `ELITE_TD_PYTHON` and `ELITE_TD_DATA`. Trade Dangerous is
  not a dependency of the server and is never imported by it.
- `get_wmm_stack`: the active wing mining mission stack, rebuilt from the
  journal files: tons required, delivered and remaining per mission, totals
  per commodity, count toward the 20-mission limit, earliest expiry with a
  48 hour alert, a hauling plan and the next board refresh. Missions that
  break the PTN rules are flagged. New module `src/utils/wmm.py`.
- `get_faction_reputation`: reputation with each minor faction from the last
  visit to a system, read from `Factions[].MyReputation`. Defaults to Mbutas
  and Paemara.
- `get_ship_status` now reports landing pad size, jump range, cargo and fuel
  capacity, every module with its engineering, and capability flags such as
  `can_laser_mine`. New module `src/utils/inventory.py`.
- Companion skill for AI clients in `skills/elite-dangerous-companion/`.
- `search_journal_history`: search every journal file and get the original
  events back, filtered by type, date and text, with an optional `fields`
  projection for building series. Reads the files directly and never touches
  the in-memory event store. New module `src/utils/raw_journal.py`.
- `get_live_file`: list or read the state files the game rewrites in place
  (Cargo, Market, NavRoute, Outfitting, Shipyard and others), with an item
  filter for the large ones.
- Initial project structure and configuration
- Basic MCP server framework setup
- Journal monitoring system foundation
- EDCoPilot integration framework
- Comprehensive testing infrastructure
- Documentation and setup guides

### Fixed
- Current location was wrong after riding a fleet carrier, and after any restart:
  journal filename timestamps were timezone-naive, which aborted the startup
  history load; history files were replayed newest-first; and `CarrierJump` had
  no game-state handler. Startup now also looks back past the 24 hour window
  until it finds a journal that places the commander in a system.
- Game state from the Status file: flags are now read from the event itself
  (they were always zero, so every Status update cleared docked, landed and
  supercruise), bit positions above bit 4 now match the journal manual, and an
  empty Status file no longer overwrites journal state.
- Coordinates are now read from `StarPos`; they were always empty.
- `Docked` now updates the current system.
- The Spansh client retries once on a 502, 503 or 504.
- A historical search no longer changes the current game state. It used to
  replay old events through live state and move the commander back in time.
- `get_material_inventory` and `get_ship_status` now pick the newest snapshot
  by timestamp, not by load order, so a historical search cannot make them
  report a weeks-old inventory or ship.
- `get_material_inventory` now applies pickups, trades, engineering, synthesis,
  broker purchases and mission rewards made after the login snapshot.
- `get_ship_status` module list was always empty.
- Live journal tailing never worked on Windows. The game keeps its journal
  open and only flushes, which produces no file-change notifications until the
  file is closed, so a session started after the server was invisible. The
  monitor now polls the newest journals every second and before every tool
  call, and reads only complete lines.
- Background monitoring was started on a setup event loop that never ran
  again once the MCP framework took over, so the poll task and everything the
  file-watcher scheduled were stranded. The monitor now moves itself onto the
  live loop at the first tool call.
- A new journal file could be delivered twice when both the watcher and a
  poll saw it.
- `get_mission_summary` crashed on missions with no reward, such as donations,
  and listed completed missions as still active. It now also reports
  `total_donated`.
- `location_timestamp` now reflects the newest jump, carrier jump or docking,
  not only the last login, and `location_event` names it. System allegiance,
  economy, government and security were always empty.

### Changed
- N/A

### Deprecated
- N/A

### Removed
- N/A

### Fixed
- N/A

### Security
- N/A

## [0.1.0] - 2024-09-06

### Added
- Initial project repository setup
- Core dependencies and build configuration
- Python project structure with modular design
- Basic documentation and setup instructions
- Development workflow and testing framework
- Git configuration and ignore patterns

### Notes
- This is the initial release with basic project structure
- MCP server functionality to be implemented in subsequent releases
- Elite Dangerous journal monitoring to be added in future versions
- EDCoPilot integration planned for upcoming milestones

---

**Legend:**
- `Added` for new features
- `Changed` for changes in existing functionality
- `Deprecated` for soon-to-be removed features
- `Removed` for now removed features
- `Fixed` for any bug fixes
- `Security` in case of vulnerabilities
