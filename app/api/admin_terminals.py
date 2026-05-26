"""REST endpoints for admin-scoped bash terminals (tmux-backed).

These reuse the same TerminalManager as session-scoped terminals — same
lifecycle (idle / standby / kept), same heartbeat protocol, same active-child
detection. The only differences are:

  * mounted at ``/api/admin/terminals/...`` (no session_id in the path)
  * require admin auth instead of session ownership
  * grouped under a sentinel session_id ``__admin__`` so the sweeper and
    list_for queries behave just like for a real session

The frontend can attach an ``EmbeddedTerminalPanel`` to these by swapping the
API adapter — the WebSocket path and heartbeat semantics are identical.
"""
from __future__ import annotations

import pathlib
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.security import AdminUser
from app.services.bash_term_service import TerminalManager

router = APIRouter(prefix="/api/admin/terminals", tags=["admin-terminals"])

ADMIN_SESSION_ID = "__admin__"

_term_mgr: TerminalManager | None = None


def configure(term_mgr: TerminalManager) -> None:
    global _term_mgr
    _term_mgr = term_mgr


class TerminalInfo(BaseModel):
    term_id: str
    session_id: str
    name: Optional[str] = None
    cwd: str
    is_named: bool
    attach_count: int
    created_at: float
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
    cwd: str


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


def _require_admin_term(term_id: str):
    assert _term_mgr is not None
    rec = _term_mgr.get(term_id)
    if rec is None or rec.session_id != ADMIN_SESSION_ID:
        raise HTTPException(status_code=404, detail="terminal not found")
    return rec


@router.get("", response_model=TerminalListResponse)
def list_terminals(admin: AdminUser) -> TerminalListResponse:
    assert _term_mgr is not None
    recs = _term_mgr.list_for(ADMIN_SESSION_ID, admin, is_admin=True)
    return TerminalListResponse(items=[TerminalInfo(**r.public()) for r in recs])


@router.post("", response_model=CreateTerminalResponse)
def create_terminal(body: CreateTerminalRequest, admin: AdminUser) -> CreateTerminalResponse:
    assert _term_mgr is not None
    cwd = body.cwd
    if not pathlib.Path(cwd).is_dir():
        raise HTTPException(status_code=400, detail=f"Directory not found: {cwd}")
    try:
        rec = _term_mgr.create(ADMIN_SESSION_ID, admin, cwd, name=body.name)
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


@router.post("/{term_id}/token", response_model=IssueTokenResponse)
def issue_token(term_id: str, _admin: AdminUser) -> IssueTokenResponse:
    assert _term_mgr is not None
    rec = _require_admin_term(term_id)
    token = _term_mgr.issue_token(term_id)
    return IssueTokenResponse(
        term_id=term_id,
        ws_token=token,
        ws_url=f"/ws/terminals/{term_id}?token={token}",
        name=rec.name,
        is_named=rec.is_named,
        kept=rec.kept,
    )


@router.post("/{term_id}/rename", response_model=TerminalInfo)
def rename_terminal(term_id: str, body: RenameTerminalRequest, _admin: AdminUser) -> TerminalInfo:
    assert _term_mgr is not None
    _require_admin_term(term_id)
    try:
        rec = _term_mgr.rename(term_id, body.name)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return TerminalInfo(**rec.public())


@router.post("/{term_id}/heartbeat", response_model=HeartbeatResponse)
def heartbeat_terminal(term_id: str, _admin: AdminUser) -> HeartbeatResponse:
    assert _term_mgr is not None
    rec = _term_mgr.get(term_id)
    if rec is None:
        raise HTTPException(status_code=410, detail="terminal gone")
    if rec.session_id != ADMIN_SESSION_ID:
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


@router.delete("/{term_id}")
def delete_terminal(term_id: str, _admin: AdminUser) -> dict:
    assert _term_mgr is not None
    _require_admin_term(term_id)
    _term_mgr.delete(term_id)
    return {"ok": True}
