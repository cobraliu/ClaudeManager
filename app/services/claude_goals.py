"""Read Claude CLI /goal history from a session JSONL.

Goals are persisted as `type: "attachment"` entries with
`attachment.type == "goal_status"`. The first event for a new goal carries
`sentinel: true, met: false` (created by /goal <condition>). Subsequent
entries are per-turn Stop-hook evaluations: `{met: bool, condition, reason}`,
and when `met: true` the goal is achieved and auto-clears.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.services.claude_session_reader import _find_session_jsonl


def _parse_ts(d: dict) -> float:
    ts = d.get("timestamp", "")
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


# Incremental scan cache: jsonl path -> (consumed_offset, closed_goals, open_goal).
# The session JSONL is append-only; a poll seeks to the cached offset and parses
# only newly-appended goal_status entries instead of re-reading the whole file.
# Copy-on-read/write so concurrent callers racing on the same delta stay correct.
_goals_scan_cache: dict[str, tuple[int, list, dict | None]] = {}


def read_goals(claude_session_id: str, cwd: str, window_bytes: int | None = None) -> dict:
    """Return {"active": Goal|None, "history": [Goal,...]}.

    `window_bytes` (bounded tail mode, status-bar hot path): read only the last
    `window_bytes` and forward-parse, never the whole file — O(window)
    regardless of size, restart-safe, no cache. If the window starts after the
    active goal's sentinel, the first orphan evaluation reconstructs the open
    goal (set_at/checks then reflect only the in-window evals; condition/met/
    reason stay exact), so "active" matches the full scan as long as the window
    holds the goal's recent evals — which it does, since an active goal is
    evaluated every turn at the tail. Default (None) = full incremental scan
    with cache, used by the on-demand dock history endpoint.

    A Goal record:
      {
        "condition": str,
        "set_at": float,           # timestamp of sentinel event
        "met": bool,
        "met_at": float | None,    # timestamp the goal was achieved
        "last_reason": str | None, # most recent classifier reason
        "checks": int,             # # of evaluations (excludes sentinel)
        "replaced": bool,          # superseded by another /goal before being met
      }
    """
    jsonl = _find_session_jsonl(claude_session_id, cwd)
    if jsonl is None:
        return {"active": None, "history": []}

    try:
        size = jsonl.stat().st_size
    except OSError:
        return {"active": None, "history": []}
    _pk = str(jsonl)
    if window_bytes is not None:
        _start = max(0, size - window_bytes)
        goals: list[dict] = []
        current: dict | None = None
    else:
        _cached = _goals_scan_cache.get(_pk)
        if _cached is not None and size >= _cached[0]:
            _start = _cached[0]
            goals = list(_cached[1])   # closed goals, in chronological order
            current = dict(_cached[2]) if _cached[2] is not None else None
        else:
            _start = 0
            goals = []
            current = None
    _consumed = _start
    _windowed = window_bytes is not None and _start > 0

    def _close_current(replaced: bool = False) -> None:
        nonlocal current
        if current is not None:
            if replaced and not current["met"]:
                current["replaced"] = True
            goals.append(current)
            current = None

    try:
        # Read only the appended region [consumed_offset, size); goal_status is
        # the discriminating literal, so the byte pre-filter skips json.loads on
        # every non-goal line (the vast majority).
        with open(jsonl, "rb") as f:
            f.seek(_start)
            _data = f.read(max(0, size - _start))
            if _windowed:
                # Started mid-file: drop the partial first line.
                _fnl = _data.find(b"\n")
                _data = _data[_fnl + 1:] if _fnl != -1 else b""
            _last_nl = _data.rfind(b"\n")
            if _last_nl != -1:
                _consumed = _start + _last_nl + 1
            _lines = _data[:_last_nl + 1].split(b"\n") if _last_nl != -1 else []
            for line in _lines:
                if not line or b"goal_status" not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "attachment":
                    continue
                att = d.get("attachment") or {}
                if att.get("type") != "goal_status":
                    continue

                condition = att.get("condition") or ""
                met = bool(att.get("met"))
                reason = att.get("reason")
                sentinel = bool(att.get("sentinel"))
                ts = _parse_ts(d)

                if sentinel:
                    # New goal set — close any prior open goal as replaced.
                    _close_current(replaced=True)
                    current = {
                        "condition": condition,
                        "set_at": ts,
                        "met": False,
                        "met_at": None,
                        "last_reason": None,
                        "checks": 0,
                        "replaced": False,
                    }
                    continue

                # Per-turn evaluation: must belong to the currently active goal.
                if current is None or current["condition"] != condition:
                    if _windowed and current is None:
                        # Bounded tail mode: the window started after this goal's
                        # sentinel, so the eval looks orphaned. Reconstruct the
                        # open goal from it — set_at/checks then count only the
                        # in-window evals, but condition/met/reason stay exact.
                        current = {
                            "condition": condition,
                            "set_at": ts,
                            "met": met,
                            "met_at": ts if met else None,
                            "last_reason": reason,
                            "checks": 1,
                            "replaced": False,
                        }
                        if met:
                            _close_current()
                        continue
                    # Defensive: orphan eval (shouldn't happen). Treat as a
                    # standalone closed goal so we don't drop it.
                    g = {
                        "condition": condition,
                        "set_at": ts,
                        "met": met,
                        "met_at": ts if met else None,
                        "last_reason": reason,
                        "checks": 1,
                        "replaced": False,
                    }
                    goals.append(g)
                    continue

                current["checks"] += 1
                if reason:
                    current["last_reason"] = reason
                if met:
                    current["met"] = True
                    current["met_at"] = ts
                    _close_current()
            if window_bytes is None:
                _goals_scan_cache[_pk] = (
                    _consumed,
                    list(goals),
                    dict(current) if current is not None else None,
                )
    except OSError:
        pass

    active = current
    return {"active": active, "history": goals}


# Bounded tail windows for the active-goal hot path, smallest first. An active
# goal evaluated every turn is caught by the small window via its tail evals. A
# goal that was just SET (sentinel only, checks=0) leaves nothing at the tail, so
# the window must actually reach back over the sentinel line itself — hence the
# larger cap. 32MB is a hard ceiling: a goal whose sentinel is farther back AND
# has had zero evals since is the only gap, and it self-heals on the next turn's
# eval. The cap keeps the hot path O(window), never O(file).
_GOAL_TAIL_WINDOWS = (1 * 1024 * 1024, 32 * 1024 * 1024)


def get_active_goal(claude_session_id: str, cwd: str) -> dict | None:
    """Active goal for the status-bar hot path WITHOUT reading the full file.

    Reads a bounded tail window and runs the exact read_goals logic on it. An
    active goal's per-turn evals live at the tail, and the windowed orphan-eval
    reconstruction recovers the open goal even when its sentinel falls before the
    window. An empty window is ambiguous (no goal vs. a far-back just-set goal),
    so we widen to the cap unless we already found an open goal or covered the
    whole file. Returns read_goals()'s "active" (a Goal dict) or None.
    """
    jsonl = _find_session_jsonl(claude_session_id, cwd)
    if jsonl is None:
        return None
    try:
        size = jsonl.stat().st_size
    except OSError:
        return None
    res: dict = {"active": None, "history": []}
    for wb in _GOAL_TAIL_WINDOWS:
        res = read_goals(claude_session_id, cwd, window_bytes=wb)
        if size <= wb or res["active"] is not None:
            break
    return res["active"]
