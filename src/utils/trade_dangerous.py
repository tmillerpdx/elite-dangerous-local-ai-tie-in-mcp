"""Trade Dangerous bridge client

Runs the Trade Dangerous trade-run optimizer (https://github.com/eyeonus/Trade-Dangerous)
as a separate program. Trade Dangerous needs Python 3.12 or newer and its own
local price database, so it lives in its own environment and is never
imported here. scripts/td_bridge.py runs inside that environment and prints
the planned routes as JSON.

Configuration, by environment variable:
- ELITE_TD_PYTHON: the Python executable of the environment Trade Dangerous
  is installed in.
- ELITE_TD_DATA: the Trade Dangerous data directory (passed on as TD_DATA).
"""

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

BRIDGE_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "td_bridge.py"
MAX_ROUTES = 5
MAX_HOPS = 6

Runner = Callable[[List[str], Dict[str, str], float], Awaitable[Tuple[int, str, str]]]

_WARNING_REASONS: Dict[str, str] = {
    "no_viable_continuation": "no profitable trade continued the route",
    "no_reachable_route": "no further station was in reach",
    "no_viable_trade": "no profitable trade was found for the last hop",
}


def build_run_args(
    origin: str,
    destination: str,
    credits: int,
    capacity: int,
    ly_per: float,
    hops: int,
    jumps_per: int,
    max_age_days: float,
    pad_size: str,
    routes: int,
) -> List[str]:
    """Build the Trade Dangerous 'run' command line.

    An empty destination, a jumps_per of 0, a max_age_days of 0 and an empty
    pad_size each leave that option out.
    """
    args = [
        "run", "--from", origin, "--credits", str(int(credits)), "--capacity", str(int(capacity)),
        "--ly-per", str(round(float(ly_per), 2)), "--hops", str(int(hops)),
    ]
    if destination:
        args += ["--to", destination]
    if jumps_per > 0:
        args += ["--jumps-per", str(int(jumps_per))]
    if max_age_days > 0:
        args += ["--age", str(float(max_age_days))]
    if pad_size:
        args += ["--pad-size", pad_size]
    args += ["--routes", str(int(routes))]
    return args


def _place(station: Dict[str, Any]) -> str:
    return "%s/%s" % (station.get("system_name"), station.get("name"))


def shape_trade_route(route: Dict[str, Any]) -> Dict[str, Any]:
    """Trim one planned Trade Dangerous route to what a commander acts on."""
    hops = []
    for hop in route.get("hops") or []:
        source = hop.get("source_station") or {}
        target = hop.get("destination_station") or {}
        path = hop.get("jump_path") or {}
        systems = [s.get("name") for s in path.get("systems") or []]
        hops.append({
            "from": _place(source),
            "to": _place(target),
            "to_arrival_distance_ls": target.get("ls_from_star"),
            "to_pad_size": target.get("max_pad_size"),
            "profit": (hop.get("cargo") or {}).get("total_profit"),
            "jumps": path.get("jumps"),
            "distance_ly": round(float(path["distance_ly"]), 1) if path.get("distance_ly") is not None else None,
            "via_systems": systems[1:-1],
            "cargo": [
                {
                    "commodity": line.get("item_name"),
                    "units": line.get("quantity"),
                    "buy_price": line.get("buy_price"),
                    "sell_price": line.get("sell_price"),
                    "profit_per_unit": line.get("profit_per_unit"),
                    "profit": line.get("total_profit"),
                    "supply": line.get("source_supply_units"),
                    "demand": line.get("destination_demand_units"),
                }
                for line in (hop.get("cargo") or {}).get("lines") or []
            ],
        })
    return {
        "total_profit": route.get("total_raw_profit"),
        "starting_credits": route.get("starting_credits"),
        "ending_credits": route.get("ending_credits"),
        "hop_count": len(hops),
        "hops": hops,
    }


def database_updated_at(data_dir: str) -> Optional[datetime]:
    """When the Trade Dangerous price database was last written, in UTC, or None."""
    if not data_dir:
        return None
    try:
        modified = (Path(data_dir) / "TradeDangerous.db").stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(modified, tz=timezone.utc)


async def _run_subprocess(argv: List[str], env: Dict[str, str], timeout: float) -> Tuple[int, str, str]:
    """Run the bridge in a worker thread; works on every event loop type."""
    def run() -> Tuple[int, str, str]:
        done = subprocess.run(
            argv, env=env, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
        )
        return done.returncode, done.stdout, done.stderr
    return await asyncio.to_thread(run)


class TradeDangerousClient:
    """Plans trade runs with a separately installed Trade Dangerous."""

    def __init__(
        self,
        python_path: Optional[str] = None,
        data_dir: Optional[str] = None,
        runner: Optional[Runner] = None,
        timeout: float = 300.0,
    ):
        """
        Args:
            python_path: Python executable of the Trade Dangerous environment.
                None reads ELITE_TD_PYTHON when a plan is requested.
            data_dir: Trade Dangerous data directory. None reads ELITE_TD_DATA.
            runner: Optional replacement for the subprocess call, used by tests.
            timeout: Seconds to let a plan run before giving up.
        """
        self._python_path = python_path
        self._data_dir = data_dir
        self._runner = runner or _run_subprocess
        self._timeout = timeout

    async def plan_trade_run(
        self,
        origin: str,
        destination: str,
        credits: int,
        capacity: int,
        ly_per: float,
        hops: int,
        jumps_per: int,
        max_age_days: float,
        pad_size: str,
        routes: int,
    ) -> Dict[str, Any]:
        """Plan the most profitable multi-stop trade run from origin.

        Returns the shaped routes or a structured error object.
        """
        python_path = self._python_path or os.environ.get("ELITE_TD_PYTHON", "").strip()
        data_dir = self._data_dir or os.environ.get("ELITE_TD_DATA", "").strip()
        if not python_path:
            return {
                "error": "Trade Dangerous is not set up. Set ELITE_TD_PYTHON to the Python "
                         "executable of the environment it is installed in, and ELITE_TD_DATA "
                         "to its data directory. See LOCAL-SETUP.md."
            }
        hops = max(1, min(int(hops), MAX_HOPS))
        routes = max(1, min(int(routes), MAX_ROUTES))
        args = build_run_args(
            origin, destination, credits, capacity, ly_per, hops, jumps_per,
            max_age_days, pad_size, routes,
        )
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        if data_dir:
            env["TD_DATA"] = data_dir
            # Trade Dangerous defaults its temp folder to the working directory.
            env.setdefault("TD_TMP", str(Path(data_dir).parent / "tmp"))
        try:
            returncode, stdout, stderr = await self._runner(
                [python_path, str(BRIDGE_SCRIPT)] + args, env, self._timeout
            )
        except subprocess.TimeoutExpired:
            return {"error": "Trade Dangerous did not finish within %d seconds. Try fewer "
                             "hops or a smaller jumps_per_hop." % int(self._timeout)}
        except OSError as exc:
            logger.error("Could not run Trade Dangerous: %s", exc)
            return {"error": "Could not run Trade Dangerous: %s" % exc}

        try:
            data = json.loads(stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            data = None
        if not isinstance(data, dict):
            tail = (stderr or stdout or "").strip().splitlines()
            return {"error": "Trade Dangerous failed (exit %d): %s" % (
                returncode, tail[-1] if tail else "no output")}
        if not data.get("ok"):
            return {"error": "Trade Dangerous: %s" % data.get("error", "unknown error")}

        shaped = [shape_trade_route(route) for route in data.get("routes") or []]
        updated = database_updated_at(data_dir)
        age_days = None
        if updated is not None:
            age_days = round((datetime.now(timezone.utc) - updated).total_seconds() / 86400.0, 1)
        notes = [
            "Prices and stock come from the local Trade Dangerous database, which is only "
            "as fresh as its last import (database_age_days). Trade Dangerous does not "
            "report the age of each price.",
            "Profit assumes the listed supply and demand still hold on arrival.",
        ]
        for warning in data.get("warnings") or []:
            notes.append("Only %s of %s hops could be planned: %s." % (
                warning.get("completed_hops"), warning.get("requested_hops"),
                _WARNING_REASONS.get(warning.get("reason"), warning.get("reason")),
            ))
        return {
            "origin": origin,
            "destination": destination or None,
            "search": {
                "credits": int(credits),
                "cargo_capacity_t": int(capacity),
                "jump_range_ly": round(float(ly_per), 2),
                "hops": hops,
                "max_jumps_per_hop": jumps_per or None,
                "max_data_age_days": max_age_days,
                "pad_size": pad_size or None,
            },
            "database_updated_at": updated.isoformat() if updated is not None else None,
            "database_age_days": age_days,
            "route_count": len(shaped),
            "routes": shaped,
            "notes": notes,
            "source": "Trade Dangerous (local price database)",
        }
