"""Raw journal access

Read-only helpers that return the game's own data untouched:

- search_journal_history: every event the commander has ever logged, as the
  original JSON, filtered by type, date and text.
- list_live_files / read_live_file: the state files the game rewrites in place
  (Cargo.json, Market.json, NavRoute.json and so on).

Nothing here goes through the in-memory event store. A history question reads
the files and returns; it cannot change the server's current game state or push
recent events out of memory.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from ..journal.parser import JournalParser
    from .date_parser import DateParseError, parse_date_range
except ImportError:
    from src.journal.parser import JournalParser
    from src.utils.date_parser import DateParseError, parse_date_range

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 200
MAX_LIMIT = 5000
# Roughly what an AI client can take in one tool result without drowning.
DEFAULT_MAX_CHARS = 300000

_EVENT_NAME = re.compile(r'"event"\s*:\s*"([^"]+)"')


def extract_field(event: Any, path: str) -> Any:
    """Follow a dotted path such as "Bank_Account.Current_Wealth" or "Modules.0.Item".

    Returns None when any step is missing.
    """
    current = event
    for step in path.split("."):
        if isinstance(current, dict):
            if step not in current:
                return None
            current = current[step]
        elif isinstance(current, list):
            if not step.isdigit() or int(step) >= len(current):
                return None
            current = current[int(step)]
        else:
            return None
    return current


def _stamp(moment: Optional[datetime]) -> Optional[str]:
    """Format a datetime the way the journal writes timestamps, for string comparison."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _files_for_range(parser: JournalParser, start: Optional[datetime],
                     end: Optional[datetime]) -> List[Path]:
    """Journal files that can hold events in the range, oldest first.

    A file holds events from its own start until the next file starts.
    """
    oldest_first = list(reversed(parser.find_journal_files(include_backups=False)))
    starts = [parser._extract_timestamp_from_filename(path) for path in oldest_first]
    chosen = []
    for index, path in enumerate(oldest_first):
        begins = starts[index]
        ends = starts[index + 1] if index + 1 < len(starts) else None
        if end is not None and begins > end:
            continue
        if start is not None and ends is not None and ends < start:
            continue
        chosen.append(path)
    return chosen


def search_journal_history(
    journal_path: Any,
    event_types: Optional[Iterable[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    contains_text: Optional[str] = None,
    fields: Optional[Iterable[str]] = None,
    limit: int = DEFAULT_LIMIT,
    sort_order: str = "desc",
    max_chars: int = DEFAULT_MAX_CHARS,
) -> Dict[str, Any]:
    """Search every journal file and return the original events.

    Args:
        journal_path: Folder holding the Journal.*.log files
        event_types: Event names to keep, any capitalisation. None keeps all.
        start_date, end_date: ISO or natural-language dates, inclusive
        contains_text: Keep events whose JSON contains this text, any case
        fields: Dotted paths to return instead of the whole event
        limit: Most events to return
        sort_order: "desc" for newest first, "asc" for oldest first
        max_chars: Stop adding events once the result is about this large

    Returns:
        Dict with events, returned_count, matched_count, truncated,
        files_scanned and date_range, or a structured error object
    """
    order = (sort_order or "").strip().lower() or "desc"
    if order not in ("asc", "desc"):
        return {"error": "sort_order must be 'asc' or 'desc', got '%s'" % sort_order}

    folder = Path(journal_path) if journal_path else None
    if folder is None or not folder.is_dir():
        return {"error": "Journal folder not found: %s" % journal_path}

    try:
        start_dt, end_dt = parse_date_range(start_date or None, end_date or None)
    except (DateParseError, ValueError) as exc:
        return {"error": "Could not read the date range: %s" % exc}

    wanted = {str(name).strip().lower() for name in (event_types or []) if str(name).strip()}
    needle = (contains_text or "").strip().lower()
    paths = [str(path).strip() for path in (fields or []) if str(path).strip()]
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    start_stamp, end_stamp = _stamp(start_dt), _stamp(end_dt)

    files = _files_for_range(JournalParser(folder), start_dt, end_dt)
    matches: List[Dict[str, Any]] = []
    for file_path in files:
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    # Cheap text checks first; most lines are rejected unparsed.
                    if wanted:
                        name = _EVENT_NAME.search(line)
                        if not name or name.group(1).lower() not in wanted:
                            continue
                    if needle and needle not in line.lower():
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    stamp = str(event.get("timestamp") or "")
                    if start_stamp and stamp < start_stamp:
                        continue
                    if end_stamp and stamp > end_stamp:
                        continue
                    matches.append(event)
        except OSError as exc:
            logger.warning("Could not read %s: %s", file_path.name, exc)

    matches.sort(key=lambda e: str(e.get("timestamp") or ""), reverse=(order == "desc"))

    events: List[Dict[str, Any]] = []
    used = 0
    hit_size_cap = False
    for event in matches[:limit]:
        if paths:
            shaped: Dict[str, Any] = {"timestamp": event.get("timestamp"), "event": event.get("event")}
            for path in paths:
                shaped[path] = extract_field(event, path)
        else:
            shaped = event
        size = len(json.dumps(shaped, separators=(",", ":")))
        if events and used + size > max_chars:
            hit_size_cap = True
            break
        events.append(shaped)
        used += size

    result: Dict[str, Any] = {
        "events": events,
        "returned_count": len(events),
        "matched_count": len(matches),
        "truncated": len(events) < len(matches),
        "files_scanned": len(files),
        "date_range": {"start": start_stamp, "end": end_stamp},
        "sort_order": order,
    }
    if hit_size_cap:
        result["note"] = (
            "Stopped early to keep the result readable. Narrow the date range or "
            "event_types, or pass fields to return only the values needed."
        )
    elif result["truncated"]:
        result["note"] = "More events matched than limit allows. Raise limit or narrow the search."
    return result


def _live_file_paths(folder: Path) -> List[Path]:
    """State files the game rewrites in place: top-level *.json in the journal folder."""
    try:
        return sorted(
            (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".json"),
            key=lambda p: p.name.lower(),
        )
    except OSError:
        return []


def _modified(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)


def list_live_files(journal_path: Any) -> Dict[str, Any]:
    """List the game's live state files with their size and last update time."""
    folder = Path(journal_path) if journal_path else None
    if folder is None or not folder.is_dir():
        return {"error": "Journal folder not found: %s" % journal_path}
    files = []
    for path in _live_file_paths(folder):
        try:
            files.append({
                "filename": path.name,
                "size_bytes": path.stat().st_size,
                "modified_at": _modified(path).isoformat(),
            })
        except OSError:
            continue
    return {"files": files}


def read_live_file(journal_path: Any, filename: str, item_filter: str = "") -> Dict[str, Any]:
    """Read one live state file.

    Args:
        journal_path: Folder holding the game's files
        filename: Name with or without ".json", any capitalisation
        item_filter: Keep only entries of the file's main list whose JSON
            contains this text. Useful for Market.json and Outfitting.json.

    Returns:
        Dict with filename, modified_at, age_seconds and data, or a structured
        error object. Only files inside the journal folder can be read.
    """
    folder = Path(journal_path) if journal_path else None
    if folder is None or not folder.is_dir():
        return {"error": "Journal folder not found: %s" % journal_path}

    available = _live_file_paths(folder)
    names = [p.name for p in available]
    requested = (filename or "").strip()
    wanted = requested.lower()
    if wanted and not wanted.endswith(".json"):
        wanted += ".json"
    # Match against the listing only. A name is never joined into a path, so
    # nothing outside the journal folder can be reached.
    match = next((p for p in available if p.name.lower() == wanted), None)
    if match is None:
        return {"error": "No live file named '%s'" % requested, "available_files": names}

    try:
        with open(match, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        return {
            "error": "Could not read %s; the game may be writing it. Try again. (%s)"
                     % (match.name, exc)
        }

    modified = _modified(match)
    result: Dict[str, Any] = {
        "filename": match.name,
        "modified_at": modified.isoformat(),
        "age_seconds": int((datetime.now(timezone.utc) - modified).total_seconds()),
        "data": data,
    }

    needle = (item_filter or "").strip().lower()
    if needle and isinstance(data, dict):
        lists = [(key, value) for key, value in data.items() if isinstance(value, list)]
        if lists:
            key, items = max(lists, key=lambda pair: len(pair[1]))
            kept = [item for item in items if needle in json.dumps(item).lower()]
            result["data"] = dict(data)
            result["data"][key] = kept
            result["items_total"] = len(items)
            result["items_matched"] = len(kept)
    return result
