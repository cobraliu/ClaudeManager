"""Public, unauthenticated read API for shared conversations.

These routes back the `/share/{full,limited}/{hash}.html` viewer. A share link
is a capability URL: anyone holding it can read the conversation, scoped to the
single session bound to the hash. Lookups go through the in-memory `share_cache`
(warmed at startup, kept in sync by the authed create/delete endpoints and the
cleanup loop), so a hit never touches the DB.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.agents import get_adapter
from app.api.files import (
    MAX_DIR_ENTRIES,
    MAX_FILE_SIZE,
    SKIP_DIRS,
    SQLITE_EXTENSIONS,
    FileEntry,
    FilesResponse,
    _is_archive_name,
    _is_text,
    _resolve,
)
from app.api.sessions import _get_store
from app.models.session import FileAccessSpec
from app.services.claude_session_reader import _parse_ts, read_raw_messages_page
from app.services.share_cache import share_cache

public_router = APIRouter(prefix="/api/public/share", tags=["public-share"])

# Images we will serve as raw bytes for inline <img> rendering. SVG is text but
# is safe in an <img> context (scripts inside don't execute), so it's included.
SHARE_IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico", ".avif",
}
MAX_SHARE_RAW_SIZE = 25 * 1024 * 1024  # 25 MB cap for raw image serving


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
    has_files = bool(rec.file_access and (rec.file_access.full or rec.file_access.files))
    return {
        "hash": rec.hash,
        "share_type": rec.share_type,
        "title": session.name if session else "Shared conversation",
        "created_at": rec.created_at,
        "expires_at": rec.expires_at,
        "cutoff_ts": rec.cutoff_ts,
        "default_theme": rec.default_theme,
        "has_files": has_files,
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


# ── Public file viewing ──────────────────────────────────────────────────────
# A share may expose a set of project files for read-only browsing. The owner
# picks them at create time; the spec is `{full: [dirs], files: [files]}` (paths
# relative to the session cwd). `full` dirs grant their entire non-hidden,
# non-skipped subtree recursively; `files` are individual grants. A dir not in
# `full` but on the path to a granted entry is a navigation-only "partial"
# container. Every access goes through `_resolve` (path-traversal guard) AND the
# spec check below.


def _norm(rel: str) -> str:
    return rel.strip().strip("/")


def _segment_hidden_or_skipped(rel: str) -> bool:
    return any(p.startswith(".") or p in SKIP_DIRS for p in _norm(rel).split("/") if p)


def _is_under_full(spec: FileAccessSpec, rel: str) -> bool:
    """True if rel equals, or is nested under, any 'full' dir."""
    rel = _norm(rel)
    for d in spec.full:
        d = _norm(d)
        if d == "":
            return True  # full grant on the project root
        if rel == d or rel.startswith(d + "/"):
            return True
    return False


def _is_file_allowed(spec: FileAccessSpec, rel: str) -> bool:
    rel = _norm(rel)
    if rel in {_norm(f) for f in spec.files}:
        return True  # explicit grant — honored even if hidden
    return _is_under_full(spec, rel) and not _segment_hidden_or_skipped(rel)


def _has_allowed_descendant(spec: FileAccessSpec, rel_dir: str) -> bool:
    """True if rel_dir is an ancestor of some granted file/full dir (→ a
    partial navigation container worth showing in the tree)."""
    prefix = _norm(rel_dir)
    needle = prefix + "/"
    for p in list(spec.files) + list(spec.full):
        p = _norm(p)
        if prefix == "" or p == prefix or p.startswith(needle):
            return True
    return False


def _share_spec(hash: str):
    """Resolve (rec, session, spec) for a file-enabled share, or 404."""
    rec = share_cache.get(hash)
    if rec is None:
        raise HTTPException(status_code=404, detail="share not found or expired")
    spec = rec.file_access
    if spec is None or (not spec.full and not spec.files):
        raise HTTPException(status_code=404, detail="no files shared")
    store = _get_store()
    session = store.get(rec.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session no longer exists")
    return rec, session, spec


def _visible_children(spec: FileAccessSpec, session_cwd: str, rel_dir: str) -> list[FileEntry]:
    base = Path(session_cwd).resolve()
    target = _resolve(session_cwd, rel_dir)
    full_here = _is_under_full(spec, rel_dir)

    try:
        raw = [e for e in target.iterdir() if not e.name.startswith(".")]
    except (OSError, PermissionError):
        raw = []

    def _sort_key(e: Path) -> tuple:
        try:
            return (e.is_file(), e.name.lower())
        except OSError:
            return (True, e.name.lower())

    raw.sort(key=_sort_key)

    entries: list[FileEntry] = []
    for entry in raw[:MAX_DIR_ENTRIES]:
        try:
            is_dir = entry.is_dir()
            if is_dir and entry.name in SKIP_DIRS:
                continue
            rel = str(entry.relative_to(base))
        except OSError:
            continue

        if full_here:
            visible = True
        elif is_dir:
            visible = _is_under_full(spec, rel) or _has_allowed_descendant(spec, rel)
        else:
            visible = _norm(rel) in {_norm(f) for f in spec.files}
        if not visible:
            continue

        is_sq = not is_dir and entry.suffix.lower() in SQLITE_EXTENSIONS
        is_arc = not is_dir and _is_archive_name(entry.name)
        try:
            size = entry.stat().st_size if not is_dir else None
        except OSError:
            size = None
        entries.append(FileEntry(
            name=entry.name,
            path=rel,
            type="dir" if is_dir else "file",
            size=size,
            is_text=_is_text(entry) if (not is_dir and not is_sq and not is_arc) else False,
            is_sqlite=is_sq,
            is_archive=is_arc,
        ))
    return entries


@public_router.get("/{hash}/files")
def get_share_files(hash: str, path: str = Query(default="", max_length=500)) -> FilesResponse:
    _rec, session, spec = _share_spec(hash)
    target = _resolve(session.cwd, path)
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail="path not found")
    rel = _norm(path)
    # The requested dir must itself be visible: either under a full grant, or a
    # partial container on the path to a granted entry (root is always allowed).
    if rel and not (_is_under_full(spec, rel) or _has_allowed_descendant(spec, rel)):
        raise HTTPException(status_code=404, detail="not found")
    base = Path(session.cwd).resolve()
    rel_current = str(target.relative_to(base)) if target != base else ""
    return FilesResponse(entries=_visible_children(spec, session.cwd, path), path=rel_current)


@public_router.get("/{hash}/file")
def get_share_file(hash: str, path: str = Query(..., max_length=500)) -> dict:
    _rec, session, spec = _share_spec(hash)
    rel = _norm(path)
    if not _is_file_allowed(spec, rel):
        raise HTTPException(status_code=404, detail="not found")
    target = _resolve(session.cwd, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    # Non-text / oversized files are reported as flags (not errors) so the
    # viewer can render a placeholder instead of treating it as a failure.
    try:
        too_large = target.stat().st_size > MAX_FILE_SIZE
    except OSError:
        too_large = False
    if too_large or not _is_text(target):
        return {"path": rel, "content": "", "is_text": False, "too_large": too_large}
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"path": rel, "content": content, "is_text": True, "too_large": False}


@public_router.get("/{hash}/raw")
def get_share_raw(hash: str, path: str = Query(..., max_length=500)) -> StreamingResponse:
    """Serve a granted image file as raw bytes for inline <img> rendering."""
    _rec, session, spec = _share_spec(hash)
    rel = _norm(path)
    if not _is_file_allowed(spec, rel):
        raise HTTPException(status_code=404, detail="not found")
    target = _resolve(session.cwd, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    if target.suffix.lower() not in SHARE_IMAGE_EXTS:
        raise HTTPException(status_code=415, detail="only images can be served raw")
    if target.stat().st_size > MAX_SHARE_RAW_SIZE:
        raise HTTPException(status_code=413, detail="file too large")

    mime, _ = mimetypes.guess_type(str(target))

    def _iter_file(p: Path):
        with p.open("rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(_iter_file(target), media_type=mime or "application/octet-stream")
