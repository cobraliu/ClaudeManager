"""REST endpoints for managing bash terminals (tmux-backed)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.security import CurrentUserInfo
from app.services.bash_term_service import TerminalManager
from app.services.session_store import SessionStore

router = APIRouter(prefix="/api/sessions", tags=["terminals"])

_store: SessionStore | None = None
_term_mgr: TerminalManager | None = None


def configure(store: SessionStore, term_mgr: TerminalManager) -> None:
    global _store, _term_mgr
    _store = store
    _term_mgr = term_mgr


def _check_session_access(session_id: str, user_info: CurrentUserInfo):
    assert _store is not None
    session = _store.get(session_id)
    if session is None or (not user_info.is_admin and session.owner_id != user_info.username):
        raise HTTPException(status_code=404, detail="session not found")
    return session


class TerminalInfo(BaseModel):
    term_id: str
    session_id: str
    name: Optional[str] = None
    cwd: str
    is_named: bool
    attach_count: int
    created_at: float
    # True once promoted from standby — terminal is no longer subject to
    # auto-close, so the client can stop heartbeating if it wants.
    kept: bool = False


class HeartbeatResponse(BaseModel):
    term_id: str
    is_named: bool
    kept: bool
    attach_count: int


class TerminalListResponse(BaseModel):
    items: list[TerminalInfo]


class CreateTerminalRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=64)
    cwd: Optional[str] = None


class CreateTerminalResponse(BaseModel):
    term_id: str
    name: Optional[str] = None
    is_named: bool
    ws_token: str
    ws_url: str


class RenameTerminalRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=64)


class IssueTokenResponse(BaseModel):
    term_id: str
    ws_token: str
    ws_url: str
    name: Optional[str] = None
    is_named: bool = False
    kept: bool = False


@router.get("/{session_id}/terminals", response_model=TerminalListResponse)
def list_terminals(session_id: str, user_info: CurrentUserInfo) -> TerminalListResponse:
    _check_session_access(session_id, user_info)
    assert _term_mgr is not None
    recs = _term_mgr.list_for(session_id, user_info.username, is_admin=user_info.is_admin)
    return TerminalListResponse(items=[TerminalInfo(**r.public()) for r in recs])


@router.post("/{session_id}/terminals", response_model=CreateTerminalResponse)
def create_terminal(
    session_id: str,
    body: CreateTerminalRequest,
    user_info: CurrentUserInfo,
) -> CreateTerminalResponse:
    session = _check_session_access(session_id, user_info)
    assert _term_mgr is not None
    cwd = body.cwd or session.cwd
    import pathlib
    if not pathlib.Path(cwd).is_dir():
        raise HTTPException(status_code=400, detail=f"Directory not found: {cwd}")
    try:
        rec = _term_mgr.create(session_id, user_info.username, cwd, name=body.name)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    token = _term_mgr.issue_token(rec.term_id)
    return CreateTerminalResponse(
        term_id=rec.term_id,
        name=rec.name,
        is_named=rec.is_named,
        ws_token=token,
        ws_url=f"/ws/terminals/{rec.term_id}?token={token}",
    )


@router.post("/{session_id}/terminals/{term_id}/token", response_model=IssueTokenResponse)
def issue_token(session_id: str, term_id: str, user_info: CurrentUserInfo) -> IssueTokenResponse:
    _check_session_access(session_id, user_info)
    assert _term_mgr is not None
    rec = _term_mgr.get(term_id)
    if rec is None or rec.session_id != session_id:
        raise HTTPException(status_code=404, detail="terminal not found")
    if not user_info.is_admin and rec.user_id != user_info.username:
        raise HTTPException(status_code=404, detail="terminal not found")
    token = _term_mgr.issue_token(term_id)
    return IssueTokenResponse(
        term_id=term_id,
        ws_token=token,
        ws_url=f"/ws/terminals/{term_id}?token={token}",
        name=rec.name,
        is_named=rec.is_named,
        kept=rec.kept,
    )


@router.post("/{session_id}/terminals/{term_id}/rename", response_model=TerminalInfo)
def rename_terminal(
    session_id: str,
    term_id: str,
    body: RenameTerminalRequest,
    user_info: CurrentUserInfo,
) -> TerminalInfo:
    _check_session_access(session_id, user_info)
    assert _term_mgr is not None
    rec = _term_mgr.get(term_id)
    if rec is None or rec.session_id != session_id:
        raise HTTPException(status_code=404, detail="terminal not found")
    if not user_info.is_admin and rec.user_id != user_info.username:
        raise HTTPException(status_code=404, detail="terminal not found")
    try:
        rec = _term_mgr.rename(term_id, body.name)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return TerminalInfo(**rec.public())


@router.post("/{session_id}/terminals/{term_id}/heartbeat", response_model=HeartbeatResponse)
def heartbeat_terminal(session_id: str, term_id: str, user_info: CurrentUserInfo) -> HeartbeatResponse:
    """Refresh "last holder" timestamp; revives a standby ephemeral.

    Returns 410 (gone) if the terminal has already been swept. The frontend
    treats 410 as "your cached term_id is stale; create a fresh terminal."
    """
    _check_session_access(session_id, user_info)
    assert _term_mgr is not None
    rec = _term_mgr.get(term_id)
    if rec is None:
        raise HTTPException(status_code=410, detail="terminal gone")
    if rec.session_id != session_id:
        raise HTTPException(status_code=404, detail="terminal not found")
    if not user_info.is_admin and rec.user_id != user_info.username:
        raise HTTPException(status_code=404, detail="terminal not found")
    updated = _term_mgr.heartbeat(term_id)
    if updated is None:
        raise HTTPException(status_code=410, detail="terminal gone")
    return HeartbeatResponse(
        term_id=updated.term_id,
        is_named=updated.is_named,
        kept=updated.kept,
        attach_count=updated.attach_count,
    )


@router.delete("/{session_id}/terminals/{term_id}")
def delete_terminal(session_id: str, term_id: str, user_info: CurrentUserInfo) -> dict:
    _check_session_access(session_id, user_info)
    assert _term_mgr is not None
    rec = _term_mgr.get(term_id)
    if rec is None or rec.session_id != session_id:
        raise HTTPException(status_code=404, detail="terminal not found")
    if not user_info.is_admin and rec.user_id != user_info.username:
        raise HTTPException(status_code=404, detail="terminal not found")
    _term_mgr.delete(term_id)
    return {"ok": True}
