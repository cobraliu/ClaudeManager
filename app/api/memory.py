"""Per-project memory file browser.

Memory files live at `~/.claude/projects/<encoded-cwd>/memory/`, beside the
session JSONLs that the Claude CLI writes. Each session's `cwd` maps to one
project dir via the same encoding the CLI uses (`/` → `-`, plus an `_` → `-`
variant for fallback). Dot-prefixed files are filtered out — they're
auto-generated indices the user doesn't normally browse manually.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.security import CurrentUserInfo
from app.services.session_store import SessionStore

router = APIRouter(prefix="/api/sessions", tags=["memory"])

_store: SessionStore | None = None


def configure(store: SessionStore) -> None:
    global _store
    _store = store


def _check_session_access(session_id: str, user_info: CurrentUserInfo):
    assert _store is not None
    session = _store.get(session_id)
    if session is None or (not user_info.is_admin and session.owner_id != user_info.username):
        raise HTTPException(status_code=404, detail="session not found")
    return session


def _resolve_memory_dir(cwd: str) -> Optional[Path]:
    if not cwd:
        return None
    projects = Path.home() / ".claude" / "projects"
    if not projects.is_dir():
        return None
    cwd = cwd.rstrip("/")
    for encoded in (cwd.replace("/", "-").replace("_", "-"), cwd.replace("/", "-")):
        candidate = projects / encoded / "memory"
        if candidate.is_dir():
            return candidate
    return None


class MemoryFileEntry(BaseModel):
    name: str
    size: int
    mtime: float


class MemoryListResponse(BaseModel):
    dir: Optional[str]
    files: list[MemoryFileEntry]


class MemoryReadResponse(BaseModel):
    name: str
    content: str
    size: int
    mtime: float


@router.get("/{session_id}/memory/list", response_model=MemoryListResponse)
def list_memory(session_id: str, user_info: CurrentUserInfo):
    session = _check_session_access(session_id, user_info)
    mem_dir = _resolve_memory_dir(session.cwd)
    if mem_dir is None:
        return MemoryListResponse(dir=None, files=[])
    entries: list[MemoryFileEntry] = []
    for f in mem_dir.iterdir():
        if not f.is_file():
            continue
        # Filter dot-prefixed hidden files (e.g. .DS_Store, .index).
        if f.name.startswith("."):
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        entries.append(MemoryFileEntry(name=f.name, size=st.st_size, mtime=st.st_mtime))
    entries.sort(key=lambda e: e.name.lower())
    return MemoryListResponse(dir=str(mem_dir), files=entries)


@router.get("/{session_id}/memory/read", response_model=MemoryReadResponse)
def read_memory(
    session_id: str,
    user_info: CurrentUserInfo,
    name: str = Query(..., min_length=1, max_length=255),
):
    session = _check_session_access(session_id, user_info)
    # Reject anything that could navigate out of the memory dir. The
    # subsequent relative_to() check is the actual guard, but failing fast on
    # obviously-bad names gives a clearer error.
    if "/" in name or "\\" in name or name.startswith(".") or name in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid file name")
    mem_dir = _resolve_memory_dir(session.cwd)
    if mem_dir is None:
        raise HTTPException(status_code=404, detail="memory dir not found")
    target = (mem_dir / name).resolve()
    try:
        target.relative_to(mem_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="path traversal")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))
    st = target.stat()
    return MemoryReadResponse(name=name, content=content, size=st.st_size, mtime=st.st_mtime)
