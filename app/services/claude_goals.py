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


def read_goals(claude_session_id: str, cwd: str) -> dict:
    """Return {"active": Goal|None, "history": [Goal,...]}.

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

    goals: list[dict] = []   # closed goals, in chronological order
    current: dict | None = None

    def _close_current(replaced: bool = False) -> None:
        nonlocal current
        if current is not None:
            if replaced and not current["met"]:
                current["replaced"] = True
            goals.append(current)
            current = None

    try:
        with open(jsonl) as f:
            for line in f:
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
    except OSError:
        pass

    active = current
    return {"active": active, "history": goals}
