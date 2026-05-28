"""Public, unauthenticated read API for shared conversations.

These routes back the `/share/{full,limited}/{hash}.html` viewer. A share link
is a capability URL: anyone holding it can read the conversation, scoped to the
single session bound to the hash. Lookups go through the in-memory `share_cache`
(warmed at startup, kept in sync by the authed create/delete endpoints and the
cleanup loop), so a hit never touches the DB.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from app.agents import get_adapter
from app.api.sessions import _get_store
from app.services.claude_session_reader import _parse_ts, read_raw_messages_page
from app.services.share_cache import share_cache

public_router = APIRouter(prefix="/api/public/share", tags=["public-share"])


def _resolve_jsonl(session):
    adapter = get_adapter(session.tool)
    chat_sid = session.agent_session_id or adapter.find_newest_session_id(session.cwd)
    jsonl_path = adapter.get_jsonl_path(chat_sid, session.cwd) if chat_sid else None
    return adapter, chat_sid, jsonl_path


@public_router.get("/{hash}")
def get_share_meta(hash: str) -> dict:
    rec = share_cache.get(hash)
    if rec is None:
        raise HTTPException(status_code=404, detail="share not found or expired")
    store = _get_store()
    session = store.get(rec.session_id)
    return {
        "hash": rec.hash,
        "share_type": rec.share_type,
        "title": session.name if session else "Shared conversation",
        "created_at": rec.created_at,
        "expires_at": rec.expires_at,
        "cutoff_ts": rec.cutoff_ts,
        "default_theme": rec.default_theme,
    }


@public_router.get("/{hash}/messages")
def get_share_messages(
    hash: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=2000),
) -> dict:
    """Forward (oldest→newest) page of the shared conversation.

    The viewer reads top-to-bottom and grows the window as it scrolls, so this
    returns the ascending slice `[offset : offset+limit]`; `total` lets the
    client know when it has reached the end (and, for full shares, when new
    messages have appeared).
    """
    rec = share_cache.get(hash)
    if rec is None:
        raise HTTPException(status_code=404, detail="share not found or expired")

    store = _get_store()
    session = store.get(rec.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session no longer exists")

    cutoff = rec.cutoff_ts if rec.share_type == "limited" else None
    adapter, chat_sid, jsonl_path = _resolve_jsonl(session)

    def _resp(messages: list, total: int) -> dict:
        return {"messages": messages, "total": total, "title": session.name,
                "share_type": rec.share_type, "expires_at": rec.expires_at}

    # Codex stores its transcript outside the line-based JSONL the helper reads;
    # its reader already returns renderer-compatible messages.
    if session.tool == "codex" and chat_sid:
        from app.services.codex_session_reader import get_codex_raw_messages
        data = get_codex_raw_messages(chat_sid, session.cwd, tail=None)
        msgs = data.get("messages", [])
        if cutoff is not None:
            msgs = [m for m in msgs if _parse_ts(m) <= cutoff]
        return _resp(msgs[offset:offset + limit], len(msgs))

    if jsonl_path is None:
        return _resp([], 0)

    # Cursor stores {"role": ...} at top level; transform to Claude shape so the
    # shared renderer (same as Chat export) can display it. Transform the full
    # ordered list (stable cursor-{idx} ids) before slicing.
    if session.tool == "cursor":
        from app.services.cursor_session_reader import _strip_user_query_tags
        full = read_raw_messages_page(Path(jsonl_path), 0, cutoff_ts=cutoff)
        transformed = []
        for idx, d in enumerate(full["messages"]):
            role = d.get("role")
            if role not in ("user", "assistant"):
                continue
            raw_content = d.get("message", {}).get("content", [])
            clean_blocks = []
            for block in (raw_content if isinstance(raw_content, list) else []):
                if block.get("type") == "text":
                    text = _strip_user_query_tags(block.get("text", ""))
                    if text:
                        clean_blocks.append({"type": "text", "text": text})
                else:
                    clean_blocks.append(block)
            if not clean_blocks:
                continue
            transformed.append({
                "type": role,
                "uuid": f"cursor-{idx}",
                "parentUuid": f"cursor-{idx - 1}" if idx > 0 else None,
                "timestamp": "",
                "message": {"role": role, "content": clean_blocks},
            })
        return _resp(transformed[offset:offset + limit], len(transformed))

    page = read_raw_messages_page(Path(jsonl_path), limit, cutoff_ts=cutoff, offset=offset)
    return _resp(page["messages"], page["total"])
