"""Read Cursor agent transcript data from ~/.cursor/projects/ for display purposes."""
from __future__ import annotations

import json
import re
from pathlib import Path
from threading import Lock
from time import monotonic

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = Lock()
_CACHE_TTL = 30.0


def _cwd_to_slug(cwd: str) -> str:
    """Convert workspace path to Cursor project directory name.

    e.g. /home/sgf/Projs/foo  →  home-sgf-Projs-foo
    """
    return cwd.strip("/").replace("/", "-")


def _transcripts_dir(cwd: str) -> Path | None:
    slug = _cwd_to_slug(cwd)
    base = Path.home() / ".cursor" / "projects" / slug / "agent-transcripts"
    return base if base.is_dir() else None


def _strip_user_query_tags(text: str) -> str:
    """Remove <user_query>...</user_query> wrapper and other system tags."""
    text = re.sub(r"<user_query>\s*", "", text)
    text = re.sub(r"\s*</user_query>", "", text)
    text = re.sub(r"<user_info>.*?</user_info>", "", text, flags=re.DOTALL)
    text = re.sub(r"<system_reminder>.*?</system_reminder>", "", text, flags=re.DOTALL)
    return text.strip()


def _extract_text_from_content(content: list[dict]) -> str:
    """Extract plain text from a Cursor message content array."""
    parts = []
    for block in content:
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n\n".join(p for p in parts if p).strip()


def _find_jsonl(chat_id: str, cwd: str) -> Path | None:
    td = _transcripts_dir(cwd)
    if td is None:
        return None
    p = td / chat_id / f"{chat_id}.jsonl"
    return p if p.is_file() else None


def list_all_cursor_sessions_global(excluded_ids: set[str]) -> list[dict]:
    """Scan all ~/.cursor/projects dirs and return sessions grouped by cwd."""
    projects_dir = Path.home() / ".cursor" / "projects"
    if not projects_dir.is_dir():
        return []

    by_cwd: dict[str, list[dict]] = {}

    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        transcripts = project_dir / "agent-transcripts"
        if not transcripts.is_dir():
            continue
        # Reverse slug to cwd: "home-sgf-Projs-foo" → "/home/sgf/Projs/foo"
        cwd = "/" + project_dir.name.replace("-", "/")

        for chat_dir in transcripts.iterdir():
            if not chat_dir.is_dir():
                continue
            chat_id = chat_dir.name
            if chat_id in excluded_ids:
                continue
            jsonl = chat_dir / f"{chat_id}.jsonl"
            if not jsonl.is_file():
                continue
            mtime = jsonl.stat().st_mtime
            data = enrich_cursor_session(chat_id, cwd)
            if not data.get("title") and not data.get("prompts"):
                continue
            entry = {
                "claude_session_id": chat_id,
                "mtime": mtime,
                "title": data.get("title"),
                "prompts": data.get("prompts", []),
                "cwd": cwd,
            }
            by_cwd.setdefault(cwd, []).append(entry)

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


def list_cursor_sessions(cwd: str) -> list[dict]:
    """Return all cursor agent sessions for cwd, sorted newest first."""
    td = _transcripts_dir(cwd)
    if td is None:
        return []
    results = []
    for chat_dir in td.iterdir():
        if not chat_dir.is_dir():
            continue
        chat_id = chat_dir.name
        jsonl = chat_dir / f"{chat_id}.jsonl"
        if not jsonl.is_file():
            continue
        mtime = jsonl.stat().st_mtime
        data = enrich_cursor_session(chat_id, cwd)
        results.append({
            "cursor_session_id": chat_id,
            "mtime": mtime,
            "title": data.get("title"),
        })
    results.sort(key=lambda x: x["mtime"], reverse=True)
    return results


def find_newest_cursor_session_id(cwd: str) -> str | None:
    sessions = list_cursor_sessions(cwd)
    return sessions[0]["cursor_session_id"] if sessions else None


def enrich_cursor_session(chat_id: str, cwd: str) -> dict:
    cache_key = f"{chat_id}:{cwd}"
    now = monotonic()
    with _cache_lock:
        if cache_key in _cache:
            ts, data = _cache[cache_key]
            if now - ts < _CACHE_TTL:
                return data

    jsonl = _find_jsonl(chat_id, cwd)
    if jsonl is None:
        return {}

    user_msgs: list[str] = []
    try:
        with open(jsonl) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("role") != "user":
                    continue
                content = d.get("message", {}).get("content", [])
                text = _strip_user_query_tags(_extract_text_from_content(content))
                if text:
                    user_msgs.append(text)
    except OSError:
        return {}

    if not user_msgs:
        return {}

    title = user_msgs[0][:80]
    # Show first, penultimate, and last prompts (like Claude reader)
    if len(user_msgs) == 1:
        prompts = [user_msgs[0]]
    elif len(user_msgs) == 2:
        prompts = user_msgs[:2]
    else:
        prompts = [user_msgs[0], user_msgs[-2], user_msgs[-1]]
    prompts = [p[:120] for p in prompts]

    result = {"title": title, "prompts": prompts}
    with _cache_lock:
        _cache[cache_key] = (now, result)
    return result


def get_cursor_conversation(chat_id: str, cwd: str, from_ts: float = 0.0) -> list[dict]:
    """Return conversation turns as [{"role", "text", "streaming", "ts"}].

    ts is the 1-based line index of the turn in the JSONL file, which is stable
    across file updates (new lines are appended, so old indices never change).
    Use from_ts to fetch only turns with ts > from_ts for incremental polling.
    """
    jsonl = _find_jsonl(chat_id, cwd)
    if jsonl is None:
        return []

    try:
        lines = jsonl.read_text().splitlines()
    except OSError:
        return []

    turns: list[dict] = []
    turn_index = 0  # counts only user/assistant turns with non-empty text

    for line in lines:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue

        role = d.get("role")
        if role not in ("user", "assistant"):
            continue

        content = d.get("message", {}).get("content", [])
        if role == "user":
            text = _strip_user_query_tags(_extract_text_from_content(content))
        else:
            text_parts = []
            for block in content:
                if block.get("type") == "text":
                    t = block.get("text", "").strip()
                    if t:
                        text_parts.append(t)
            text = "\n\n".join(text_parts)

        if not text:
            continue

        turn_index += 1
        if turn_index > from_ts:
            turns.append({"role": role, "text": text, "streaming": False, "ts": float(turn_index)})

    return turns
