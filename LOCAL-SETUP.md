# Local setup notes (Trevor, 2026-09-30)

Cloned from https://github.com/GWLlosa/elite-dangerous-local-ai-tie-in-mcp (commit 1d78014, last upstream push 2025-11-10).

## Why there's a local pin
Upstream `requirements.txt` says `mcp>=1.0.0`. The MCP Python SDK 2.x renamed
`mcp.server.fastmcp.FastMCP` -> `MCPServer`, which breaks `src/server.py` on import.
The venv here is pinned to `mcp<2` (currently 1.30.0). If you ever rebuild the venv:

    C:\Python311\python.exe -m venv venv
    venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-local-pins.txt

## Where it's registered
- Claude Code (user scope): `claude mcp get elite-dangerous`
- Claude Desktop / Cowork: `%APPDATA%\Claude\claude_desktop_config.json` -> `mcpServers.elite-dangerous`
  (a timestamped `.bak-*` copy of the previous config sits next to it)

Both point at `venv\Scripts\python.exe src\server.py` with
`ELITE_JOURNAL_PATH=C:/Users/Trevor/Saved Games/Frontier Developments/Elite Dangerous`.

## EDCoPilot
EDCoPilot is installed at `C:/EDCoPilot`; `ELITE_EDCOPILOT_PATH=C:/EDCoPilot/User custom files` is set in both registrations (added 2026-09-30).
Chatter generation overwrites the `EDCoPilot.*Chatter.Custom.txt` files there (the server backs them up first).

## Local additions (branch feature/spansh-nearby-search, staged, not committed)
Two tools that query the public Spansh body search, defaulting to the current system:
- `find_mining_hotspots` - nearest ring hotspots for a commodity
- `find_exobiology_targets` - nearest landable bodies with biological signals
Code: `src/utils/spansh_client.py`; tests: `tests/unit/test_spansh_client.py`.
Spansh quirks found the hard way: signal thresholds must be sent as `count` (sent as
`value` they are silently ignored), unknown filters are silently ignored, and the
hotspot name is `Void Opal` (singular).

## Trade Dangerous (added 2026-10-01)
`plan_trade_route` runs Trade Dangerous (https://github.com/eyeonus/Trade-Dangerous, MPL-2.0)
as a separate program. It needs Python 3.12+, so it has its own environment and is not
installed in this repo's venv.

- Install: `C:\TradeDangerous\venv` (Python 3.14, `tradedangerous==13.2.0`), made with
  `uv venv --python 3.14 C:\TradeDangerous\venv` then
  `uv pip install --python C:\TradeDangerous\venv\Scripts\python.exe tradedangerous==13.2.0`
- Data: `C:\TradeDangerous\data` (price database), temp files in `C:\TradeDangerous\tmp`.
  Do NOT put it under `%LOCALAPPDATA%`: the Claude desktop app redirects that folder to
  its own private copy, so other programs would not find it.
- The server finds it through two environment variables, which must be set in both
  MCP registrations:
  `ELITE_TD_PYTHON=C:/TradeDangerous/venv/Scripts/python.exe` and
  `ELITE_TD_DATA=C:/TradeDangerous/data`
- First import and every refresh (about 1.1 GB download from elite.tromador.com; run
  from `C:\TradeDangerous` with `TD_DATA` and `TD_TMP` set to the folders above):
  `venv\Scripts\trade.exe import -P eddblink -O clean,skipvend` the first time, then
  `venv\Scripts\trade.exe import -P eddblink -O listings` to refresh prices.
- Bridge: `scripts/td_bridge.py` runs inside that environment and prints routes as JSON.
  It uses Trade Dangerous internals, so re-test it before upgrading past 13.2.
