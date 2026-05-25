"""
Cleanup for ~/.claude/cached_messages/ written by tools/anthropic_proxy/.

Three rules:
  1. JSONL-caught-up: delete snapshots whose ts_ns ≤ max(timestamp) in the
     matching Claude JSONL — once the CLI has flushed past them, they're
     redundant duplicates of what session_reader already serves.
  2. Session-gone: delete whole directories whose claude_session_id no longer
     appears in SessionStore.
  3. 7-day fallback: nuke anything older than 7 days that escaped rules 1/2
     (e.g. when the JSONL file went missing).
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from app.services.session_store import SessionStore

logger = logging.getLogger(__name__)

CACHE_ROOT = Path.home() / ".claude" / "cached_messages"
JSONL_ROOT = Path.home() / ".claude" / "projects"
_TAIL_BYTES = 8192
_FALLBACK_AGE_S = 7 * 24 * 3600


def _find_jsonl(claude_session_id: str) -> Path | None:
    if not JSONL_ROOT.is_dir():
        return None
    for p in JSONL_ROOT.glob(f"*/{claude_session_id}.jsonl"):
        return p
    return None


def _last_jsonl_ts_ns(path: Path) -> int:
    """Best-effort: parse the latest `timestamp` field from the last few KB."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            tail = f.read()
    except OSError:
        return 0
    lines = tail.splitlines()
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        ts = obj.get("timestamp")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1_000_000_000)
        except Exception:
            continue
    return 0


def _delete_caught_up(session_dir: Path, max_jsonl_ts_ns: int) -> int:
    """Delete snapshot files whose embedded ts_ns ≤ max_jsonl_ts_ns."""
    if max_jsonl_ts_ns <= 0:
        return 0
    removed = 0
    for entry in session_dir.iterdir():
        name = entry.name
        if not name.endswith(".json") or name.startswith("."):
            continue
        try:
            ts_ns = int(name[:-5])
        except ValueError:
            continue
        if ts_ns <= max_jsonl_ts_ns:
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def _sweep_old(now_ns: int) -> int:
    """7-day fallback: delete files whose filename ts is older than the cutoff."""
    cutoff = now_ns - _FALLBACK_AGE_S * 1_000_000_000
    removed = 0
    if not CACHE_ROOT.is_dir():
        return 0
    for sdir in CACHE_ROOT.iterdir():
        if not sdir.is_dir():
            continue
        for entry in sdir.iterdir():
            name = entry.name
            if not name.endswith(".json"):
                continue
            try:
                ts_ns = int(name[:-5])
            except ValueError:
                continue
            if ts_ns < cutoff:
                try:
                    entry.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed


def run_once(store: SessionStore) -> None:
    """One pass of all three cleanup rules."""
    if not CACHE_ROOT.is_dir():
        return

    live_csids = store.get_all_agent_session_ids() or set()
    total_caught_up = 0
    total_orphan_dirs = 0

    for sdir in list(CACHE_ROOT.iterdir()):
        if not sdir.is_dir():
            continue
        csid = sdir.name

        # Rule 2: orphan directory — session no longer tracked
        if csid not in live_csids:
            jsonl = _find_jsonl(csid)
            # Only delete if there's also no JSONL — otherwise this is a session
            # we may simply not yet have linked back to a SessionStore row.
            if jsonl is None:
                try:
                    shutil.rmtree(sdir, ignore_errors=True)
                    total_orphan_dirs += 1
                except OSError:
                    pass
                continue

        # Rule 1: JSONL caught up
        jsonl = _find_jsonl(csid)
        if jsonl is not None:
            max_ts = _last_jsonl_ts_ns(jsonl)
            total_caught_up += _delete_caught_up(sdir, max_ts)

    # Rule 3: time-based fallback
    fallback_removed = _sweep_old(time.time_ns())

    if total_caught_up or total_orphan_dirs or fallback_removed:
        logger.info(
            "proxy_tap cleanup: caught_up=%d orphan_dirs=%d fallback=%d",
            total_caught_up, total_orphan_dirs, fallback_removed,
        )
