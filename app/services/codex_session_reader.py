"""Read Codex CLI rollout JSONL data from ~/.codex/sessions/ for display.

Codex stores each session as ~/.codex/sessions/YYYY/MM/DD/rollout-{ISO}-{UUID}.jsonl
where UUID matches the session id (also in session_meta payload).

Rollout entries have shape `{"timestamp", "type", "payload"}` with `type` ∈
{session_meta, turn_context, event_msg, response_item}. For the chat-view
contract (`{role, text, streaming, ts}`) we read the high-level
`event_msg.user_message` / `event_msg.agent_message` events — they're the
clean, deduplicated user-visible turns. The raw `response_item.message` lines
include developer/system bootstrap (`<environment_context>`, permissions
instructions, etc.) that we don't want in the chat UI.
"""
from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from time import monotonic

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = Lock()
_CACHE_TTL = 30.0

_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"


def _iter_rollout_files() -> list[Path]:
    """Walk ~/.codex/sessions/YYYY/MM/DD/ for rollout files.

    Returns newest-first by mtime. Safe to call when the dir doesn't exist.
    """
    if not _SESSIONS_ROOT.is_dir():
        return []
    out: list[Path] = []
    for year_dir in _SESSIONS_ROOT.iterdir():
        if not year_dir.is_dir():
            continue
        for month_dir in year_dir.iterdir():
            if not month_dir.is_dir():
                continue
            for day_dir in month_dir.iterdir():
                if not day_dir.is_dir():
                    continue
                for f in day_dir.iterdir():
                    if f.is_file() and f.name.startswith("rollout-") and f.suffix == ".jsonl":
                        out.append(f)
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out


def _read_session_meta(rollout: Path) -> dict | None:
    """Read the first line (session_meta) and return its payload, or None."""
    try:
        with open(rollout, "r", encoding="utf-8") as f:
            first = f.readline()
    except OSError:
        return None
    try:
        d = json.loads(first)
    except json.JSONDecodeError:
        return None
    if d.get("type") != "session_meta":
        return None
    return d.get("payload") or None


def _session_id_from_rollout(rollout: Path) -> str | None:
    """Extract the UUID from a rollout file name (cheap, no read)."""
    # rollout-2026-05-11T12-10-32-019e153a-c047-79c0-9357-4f247f8ab6e7.jsonl
    name = rollout.stem  # drops .jsonl
    if not name.startswith("rollout-"):
        return None
    # UUID is the last 5 dash-joined groups.
    parts = name.split("-")
    if len(parts) < 5:
        return None
    return "-".join(parts[-5:])


def _find_rollout(session_id: str, cwd: str | None = None) -> Path | None:
    """Return the rollout file for a given session UUID, or None.

    cwd is informational only — we verify the UUID and (when cwd is given)
    confirm session_meta.cwd matches. Different cwd → return None.
    """
    for f in _iter_rollout_files():
        if _session_id_from_rollout(f) != session_id:
            continue
        if cwd is not None:
            meta = _read_session_meta(f)
            if meta and meta.get("cwd") != cwd:
                # session id collision shouldn't happen, but be defensive
                continue
        return f
    return None


def list_codex_sessions(cwd: str) -> list[dict]:
    """All Codex sessions whose session_meta.cwd matches `cwd`, newest first."""
    results: list[dict] = []
    for f in _iter_rollout_files():
        meta = _read_session_meta(f)
        if not meta or meta.get("cwd") != cwd:
            continue
        sid = meta.get("id") or _session_id_from_rollout(f)
        if not sid:
            continue
        results.append({
            "codex_session_id": sid,
            "mtime": f.stat().st_mtime,
            "title": _quick_title(f),
        })
    return results


def find_newest_codex_session_id(cwd: str) -> str | None:
    sessions = list_codex_sessions(cwd)
    return sessions[0]["codex_session_id"] if sessions else None


def _quick_title(rollout: Path) -> str | None:
    """First user_message (first ~80 chars) — used in list views."""
    try:
        with open(rollout, "r", encoding="utf-8") as f:
            for line in f:
                if '"user_message"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                p = d.get("payload") or {}
                if p.get("type") == "user_message":
                    msg = (p.get("message") or "").strip()
                    if msg:
                        return msg[:80]
    except OSError:
        return None
    return None


def enrich_codex_session(session_id: str, cwd: str) -> dict:
    """Title + first/penultimate/last user prompts (matches Claude/Cursor enrich shape)."""
    cache_key = f"{session_id}:{cwd}"
    now = monotonic()
    with _cache_lock:
        if cache_key in _cache:
            ts, data = _cache[cache_key]
            if now - ts < _CACHE_TTL:
                return data

    rollout = _find_rollout(session_id, cwd)
    if rollout is None:
        return {}

    user_msgs: list[str] = []
    user_timestamps: list[str] = []
    try:
        with open(rollout, "r", encoding="utf-8") as f:
            for line in f:
                if '"user_message"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                p = d.get("payload") or {}
                if p.get("type") != "user_message":
                    continue
                msg = (p.get("message") or "").strip()
                if msg:
                    user_msgs.append(msg)
                    ts = d.get("timestamp")
                    if isinstance(ts, str) and ts:
                        user_timestamps.append(ts)
    except OSError:
        return {}

    if not user_msgs:
        return {}

    title = user_msgs[0][:80]
    if len(user_msgs) == 1:
        prompts = [user_msgs[0]]
    elif len(user_msgs) == 2:
        prompts = user_msgs[:2]
    else:
        prompts = [user_msgs[0], user_msgs[-2], user_msgs[-1]]
    prompts = [p[:120] for p in prompts]

    result = {
        "title": title,
        "prompts": prompts,
        "last_user_input_at": user_timestamps[-1] if user_timestamps else None,
    }
    with _cache_lock:
        _cache[cache_key] = (now, result)
    return result


def get_codex_conversation(session_id: str, cwd: str, from_ts: float = 0.0) -> list[dict]:
    """Return chat turns matching the {role, text, streaming, ts} contract.

    Uses event_msg.user_message / event_msg.agent_message (high-level, clean)
    rather than response_item.message (raw API trace with developer bootstrap).
    ts is the 1-based turn index, stable across appends.
    """
    rollout = _find_rollout(session_id, cwd)
    if rollout is None:
        return []

    try:
        with open(rollout, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []

    turns: list[dict] = []
    turn_index = 0
    for line in lines:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "event_msg":
            continue
        p = d.get("payload") or {}
        sub = p.get("type")
        if sub == "user_message":
            role = "user"
        elif sub == "agent_message":
            role = "assistant"
        else:
            continue
        text = (p.get("message") or "").strip()
        if not text:
            continue
        turn_index += 1
        if turn_index > from_ts:
            turns.append({"role": role, "text": text, "streaming": False, "ts": float(turn_index)})

    return turns


def search_codex_conversation(session_id: str, cwd: str, query: str) -> bool:
    """Case-insensitive substring search across the conversation."""
    ql = query.lower()
    for turn in get_codex_conversation(session_id, cwd):
        if ql in turn["text"].lower():
            return True
    return False


def list_all_codex_sessions_global(excluded_ids: set[str]) -> list[dict]:
    """All Codex sessions grouped by cwd. Matches the Cursor/Claude global shape."""
    by_cwd: dict[str, list[dict]] = {}
    for f in _iter_rollout_files():
        meta = _read_session_meta(f)
        if not meta:
            continue
        sid = meta.get("id") or _session_id_from_rollout(f)
        if not sid or sid in excluded_ids:
            continue
        cwd = meta.get("cwd") or ""
        if not cwd:
            continue
        data = enrich_codex_session(sid, cwd)
        if not data.get("title") and not data.get("prompts"):
            continue
        by_cwd.setdefault(cwd, []).append({
            "claude_session_id": sid,  # field name reused over the wire (matches Cursor)
            "mtime": f.stat().st_mtime,
            "title": data.get("title"),
            "prompts": data.get("prompts", []),
            "cwd": cwd,
        })

    result = []
    for cwd, sessions in by_cwd.items():
        sessions.sort(key=lambda x: x["mtime"], reverse=True)
        result.append({
            "dir": cwd,
            "dir_exists": Path(cwd).is_dir(),
            "sessions": sessions,
            "latest_mtime": sessions[0]["mtime"] if sessions else 0.0,
        })
    result.sort(key=lambda x: x["latest_mtime"], reverse=True)
    return result
