"""
Expose cached Anthropic-API SSE snapshots written by tools/anthropic_proxy/.

The local tap proxy writes one JSON file per snapshot under
~/.claude/cached_messages/{claude_session_id}/{ts_ns}.json. This API lets the
frontend poll for snapshots whose ts_ns > since_ns, so it can render in-flight
text/tool_use blocks before the Claude CLI flushes its JSONL.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from app.security import CurrentUser
from app.services.session_store import SessionStore

router = APIRouter(prefix="/api/sessions", tags=["proxy-tap"])

_store: SessionStore | None = None
_CACHE_ROOT = Path.home() / ".claude" / "cached_messages"
_UUID_RE = re.compile(r"^[0-9a-f-]{8,64}$", re.IGNORECASE)


def configure(store: SessionStore) -> None:
    global _store
    _store = store


def _get_store() -> SessionStore:
    assert _store is not None
    return _store


@router.get("/{session_id}/proxy-tap")
def get_proxy_tap(
    session_id: str,
    _user: CurrentUser,
    since_ns: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=2000),
):
    """Return snapshot files whose ts_ns > since_ns for this session."""
    s = _get_store().get(session_id)
    if not s:
        raise HTTPException(404, "session not found")
    csid = s.claude_session_id
    if not csid or not _UUID_RE.match(csid):
        return {"snapshots": [], "claude_session_id": None}

    sdir = _CACHE_ROOT / csid
    if not sdir.is_dir():
        return {"snapshots": [], "claude_session_id": csid}

    rows: list[dict] = []
    for entry in sdir.iterdir():
        name = entry.name
        if not name.endswith(".json") or name.startswith("."):
            continue
        try:
            ts_ns = int(name[:-5])
        except ValueError:
            continue
        if ts_ns <= since_ns:
            continue
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows.append({
            "ts_ns": data.get("ts_ns", ts_ns),
            "kind": data.get("kind"),
            "request_id": data.get("request_id"),
            "content": data.get("content", []),
        })

    rows.sort(key=lambda r: r["ts_ns"])
    return {
        "snapshots": rows[:limit],
        "claude_session_id": csid,
        "truncated": len(rows) > limit,
    }
