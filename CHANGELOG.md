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
