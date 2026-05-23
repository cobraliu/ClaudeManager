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


def get_codex_raw_messages(session_id: str, cwd: str, tail: int | None = None) -> dict:
    """Synthesize per-event messages from a Codex rollout for the chat UI.

    Codex's native rollout schema doesn't match Claude's {type:user/assistant,
    message:{role,content}} shape, so we walk the file and emit one entry per
    interesting event. The chat UI has dedicated renderers for each codex_*
    type (see ConversationPane.tsx).

    Emitted types:
      user / assistant            — chat text (event_msg.user_message/agent_message)
      codex_reasoning             — response_item.reasoning (often encrypted)
      codex_tool_call             — function_call / custom_tool_call
      codex_tool_result           — function_call_output / custom_tool_call_output
      codex_patch_apply           — event_msg.patch_apply_end (richer than _output)
      codex_lifecycle             — task_started / task_complete / turn_aborted
      codex_token_count           — event_msg.token_count

    Returns {"messages": [...], "total": <total messages, pre-tail>}.
    """
    rollout = _find_rollout(session_id, cwd)
    if rollout is None:
        return {"messages": [], "total": 0}

    # ── Pass 1: find tool-output call_ids that also have a patch_apply_end.
    # When both exist we prefer the patch_apply_end (richer fields). The plain
    # *_output for the same call_id is skipped to avoid duplicate rendering.
    patch_call_ids: set[str] = set()
    try:
        with open(rollout, "r", encoding="utf-8") as f:
            for line in f:
                if '"patch_apply_end"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                p = d.get("payload") or {}
                if d.get("type") == "event_msg" and p.get("type") == "patch_apply_end":
                    cid = p.get("call_id")
                    if isinstance(cid, str) and cid:
                        patch_call_ids.add(cid)
    except OSError:
        return {"messages": [], "total": 0}

    messages: list[dict] = []
    try:
        with open(rollout, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                top = d.get("type")
                p = d.get("payload") or {}
                sub = p.get("type")
                ts = d.get("timestamp") if isinstance(d.get("timestamp"), str) else None
                base_uuid = f"codex-{session_id}-{idx}"

                # 1. user/assistant text
                if top == "event_msg" and sub in ("user_message", "agent_message"):
                    role = "user" if sub == "user_message" else "assistant"
                    text = (p.get("message") or "").strip()
                    if not text:
                        continue
                    messages.append({
                        "type": role,
                        "uuid": base_uuid,
                        "timestamp": ts,
                        "message": {"role": role, "content": [{"type": "text", "text": text}]},
                    })

                # 2. reasoning (usually encrypted — emit a marker)
                elif top == "response_item" and sub == "reasoning":
                    summary = p.get("summary") or []
                    content = p.get("content")
                    visible_text = ""
                    if isinstance(summary, list) and summary:
                        bits = []
                        for s in summary:
                            if isinstance(s, dict) and s.get("text"):
                                bits.append(str(s.get("text")))
                            elif isinstance(s, str):
                                bits.append(s)
                        visible_text = "\n".join(bits).strip()
                    if not visible_text and isinstance(content, list):
                        bits = []
                        for c in content:
                            if isinstance(c, dict) and c.get("text"):
                                bits.append(str(c.get("text")))
                        visible_text = "\n".join(bits).strip()
                    messages.append({
                        "type": "codex_reasoning",
                        "uuid": base_uuid,
                        "timestamp": ts,
                        "text": visible_text,
                        "encrypted": bool(p.get("encrypted_content")) and not visible_text,
                    })

                # 3. tool call (function_call or custom_tool_call)
                elif top == "response_item" and sub in ("function_call", "custom_tool_call"):
                    name = p.get("name") or ""
                    call_id = p.get("call_id") or ""
                    # function_call: arguments is a JSON string. custom_tool_call: input is raw text.
                    raw_args = p.get("arguments") if sub == "function_call" else p.get("input")
                    parsed: object
                    if sub == "function_call" and isinstance(raw_args, str):
                        try:
                            parsed = json.loads(raw_args)
                        except json.JSONDecodeError:
                            parsed = raw_args
                    else:
                        parsed = raw_args
                    messages.append({
                        "type": "codex_tool_call",
                        "uuid": base_uuid,
                        "timestamp": ts,
                        "call_id": call_id,
                        "name": name,
                        "input": parsed,
                        "status": p.get("status"),
                    })

                # 4. tool output (skip if a patch_apply_end will replace it)
                elif top == "response_item" and sub in ("function_call_output", "custom_tool_call_output"):
                    call_id = p.get("call_id") or ""
                    if call_id in patch_call_ids:
                        continue
                    raw_output = p.get("output")
                    # custom_tool_call_output sometimes wraps in {output, metadata}
                    output_text: str
                    if isinstance(raw_output, str):
                        # Try to unwrap {"output": "..."} JSON when present
                        try:
                            parsed = json.loads(raw_output)
                            if isinstance(parsed, dict) and "output" in parsed:
                                output_text = str(parsed.get("output") or "")
                            else:
                                output_text = raw_output
                        except json.JSONDecodeError:
                            output_text = raw_output
                    else:
                        output_text = json.dumps(raw_output, ensure_ascii=False)
                    messages.append({
                        "type": "codex_tool_result",
                        "uuid": base_uuid,
                        "timestamp": ts,
                        "call_id": call_id,
                        "output": output_text,
                    })

                # 5. patch_apply_end (richer apply_patch result)
                elif top == "event_msg" and sub == "patch_apply_end":
                    messages.append({
                        "type": "codex_patch_apply",
                        "uuid": base_uuid,
                        "timestamp": ts,
                        "call_id": p.get("call_id") or "",
                        "stdout": p.get("stdout") or "",
                        "stderr": p.get("stderr") or "",
                        "success": bool(p.get("success")),
                        "changes": p.get("changes"),
                        "status": p.get("status"),
                    })

                # 6. lifecycle events
                elif top == "event_msg" and sub in ("task_started", "task_complete", "turn_aborted"):
                    messages.append({
                        "type": "codex_lifecycle",
                        "uuid": base_uuid,
                        "timestamp": ts,
                        "subtype": sub,
                        "turn_id": p.get("turn_id"),
                        "duration_ms": p.get("duration_ms"),
                        "reason": p.get("reason"),
                        "model_context_window": p.get("model_context_window"),
                    })

                # 7. token usage
                elif top == "event_msg" and sub == "token_count":
                    info = p.get("info")
                    if info is None:
                        # All-null token_count events are noisy heartbeats — skip
                        continue
                    messages.append({
                        "type": "codex_token_count",
                        "uuid": base_uuid,
                        "timestamp": ts,
                        "info": info,
                    })
    except OSError:
        return {"messages": [], "total": 0}

    total = len(messages)
    if tail is not None and len(messages) > tail:
        messages = messages[-tail:]
    return {"messages": messages, "total": total}


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
