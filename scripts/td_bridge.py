"""Run a Trade Dangerous command and print its result as one line of JSON.

This script runs inside the Trade Dangerous environment (Python 3.12+), not
the MCP server's. src/utils/trade_dangerous.py calls it as:

    <td python> scripts/td_bridge.py run --from "Sol/Abraham Lincoln" ...

Only the "run" command is supported. Output is always a single JSON object on
the last line of stdout: {"ok": true, "routes": [...], "warnings": [...]} or
{"ok": false, "error": "..."}. Written against tradedangerous 13.2.
"""

import contextlib
import dataclasses
import io
import json
import sys


def _plan(argv):
    from tradedangerous import commands
    from tradedangerous.tradeorm import TradeORM

    cmdenv = commands.CommandIndex().parse(["trade"] + argv)
    preflight = getattr(cmdenv, "preflight", None)
    if callable(preflight):
        preflight()
    tdb = TradeORM(tdenv=cmdenv, require_db=True)
    try:
        results = cmdenv.run(tdb)
        data = getattr(results, "data", None)
        if data is None or not hasattr(data, "routes"):
            return {"ok": False, "error": "Trade Dangerous returned no route"}
        return {
            "ok": True,
            "routes": [dataclasses.asdict(route) for route in data.routes],
            "warnings": [dataclasses.asdict(warning) for warning in data.warnings],
        }
    finally:
        tdb.close(final=True)


def main(argv):
    if not argv or argv[0] != "run":
        payload = {"ok": False, "error": "Only the 'run' command is supported"}
    else:
        # Trade Dangerous prints notes and progress; keep stdout for the JSON.
        captured = io.StringIO()
        try:
            with contextlib.redirect_stdout(captured):
                payload = _plan(argv)
        except SystemExit as exc:
            text = captured.getvalue().strip().splitlines()
            payload = {"ok": False, "error": text[-1] if text else "exited with %s" % exc.code}
        except Exception as exc:  # every failure must still reach the caller as JSON
            payload = {"ok": False, "error": str(exc).strip() or type(exc).__name__}
    sys.stdout.write(json.dumps(payload, default=str) + "\n")
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
