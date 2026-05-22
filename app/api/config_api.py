from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.config import (
    PROXY_MODE_REAL,
    PROXY_MODE_TAP_UPSTREAM,
    get_claude_bin,
    get_cursor_bin,
    get_default_workspace,
    get_proxy,
    get_proxy_mode,
    get_term_idle_grace_seconds,
    get_term_standby_grace_seconds,
    get_terminal_font,
    set_claude_bin,
    set_cursor_bin,
    set_default_workspace,
    set_proxy,
    set_proxy_mode,
    set_term_idle_grace_seconds,
    set_term_standby_grace_seconds,
    set_terminal_font,
)
from app.security import AdminUser, CurrentUser

# Project root: app/api/config_api.py → ../../ → project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

router = APIRouter(prefix="/api/config", tags=["config"])


class ConfigView(BaseModel):
    workspace: str
    claude_bin: str
    cursor_bin: str
    proxy: str
    proxy_mode: str  # "tap_upstream" | "real"
    terminal_font: str
    term_idle_grace_seconds: int = 600
    term_standby_grace_seconds: int = 30


class WorkspaceUpdateRequest(BaseModel):
    workspace: str


class ClaudeBinUpdateRequest(BaseModel):
    claude_bin: str


class CursorBinUpdateRequest(BaseModel):
    cursor_bin: str


class ProxyUpdateRequest(BaseModel):
    proxy: str
    proxy_mode: str = PROXY_MODE_TAP_UPSTREAM


class TerminalFontRequest(BaseModel):
    font: str


class TermLifecycleRequest(BaseModel):
    """Idle = how long an ephemeral tmux terminal sits with no holder before
    standby; standby = grace period after standby starts before tmux kill."""
    idle_grace_seconds: int = Field(ge=10, le=86400)
    standby_grace_seconds: int = Field(ge=5, le=3600)


def _full_config() -> ConfigView:
    return ConfigView(
        workspace=get_default_workspace(),
        claude_bin=get_claude_bin(),
        cursor_bin=get_cursor_bin(),
        proxy=get_proxy(),
        proxy_mode=get_proxy_mode(),
        terminal_font=get_terminal_font(),
        term_idle_grace_seconds=get_term_idle_grace_seconds(),
        term_standby_grace_seconds=get_term_standby_grace_seconds(),
    )


@router.get("")
def get_config(_user: CurrentUser) -> ConfigView:
    return _full_config()


@router.put("/workspace")
def update_workspace(body: WorkspaceUpdateRequest, _admin: AdminUser) -> ConfigView:
    set_default_workspace(body.workspace.rstrip("/"))
    return _full_config()


@router.put("/claude-bin")
def update_claude_bin(body: ClaudeBinUpdateRequest, _admin: AdminUser) -> ConfigView:
    set_claude_bin(body.claude_bin.strip())
    return _full_config()


@router.put("/cursor-bin")
def update_cursor_bin(body: CursorBinUpdateRequest, _admin: AdminUser) -> ConfigView:
    set_cursor_bin(body.cursor_bin.strip())
    return _full_config()


@router.put("/proxy")
def update_proxy(body: ProxyUpdateRequest, _admin: AdminUser) -> ConfigView:
    mode = body.proxy_mode.strip() or PROXY_MODE_TAP_UPSTREAM
    if mode not in (PROXY_MODE_TAP_UPSTREAM, PROXY_MODE_REAL):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"invalid proxy_mode: {mode}")
    set_proxy(body.proxy.strip())
    set_proxy_mode(mode)
    return _full_config()


@router.get("/fonts")
def list_system_fonts(_user: CurrentUser) -> list[dict]:
    """Return monospace fonts installed on the system, recommended ones first."""
    _RECOMMENDED = [
        "Ubuntu Sans Mono", "Ubuntu Mono",
        "WenQuanYi Micro Hei Mono", "WenQuanYi Zen Hei Mono",
        "Noto Sans Mono CJK SC", "Noto Sans Mono",
        "JetBrains Mono", "Fira Code", "Cascadia Code",
        "Source Code Pro", "Hack", "Inconsolata",
        "DejaVu Sans Mono", "Liberation Mono", "Courier New",
    ]
    _RECOMMENDED_LOWER = {f.lower(): i for i, f in enumerate(_RECOMMENDED)}

    fonts: list[str] = []
    try:
        out = subprocess.run(
            ["fc-list", ":spacing=mono", "family"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            for part in line.split(","):
                name = part.strip().strip('"').rstrip(":")
                if name and not name.startswith("."):
                    fonts.append(name)
    except Exception:
        pass

    # Deduplicate preserving first occurrence
    seen: set[str] = set()
    unique: list[str] = []
    for f in fonts:
        key = f.lower()
        if key not in seen:
            seen.add(key)
            unique.append(f)

    def _sort_key(name: str):
        idx = _RECOMMENDED_LOWER.get(name.lower(), len(_RECOMMENDED))
        return (idx, name.lower())

    unique.sort(key=_sort_key)
    return [{"family": f, "recommended": f.lower() in _RECOMMENDED_LOWER} for f in unique]


@router.put("/terminal-font")
def update_terminal_font(body: TerminalFontRequest, _admin: AdminUser) -> ConfigView:
    set_terminal_font(body.font.strip())
    return _full_config()


@router.put("/term-lifecycle")
def update_term_lifecycle(body: TermLifecycleRequest, _admin: AdminUser) -> ConfigView:
    set_term_idle_grace_seconds(body.idle_grace_seconds)
    set_term_standby_grace_seconds(body.standby_grace_seconds)
    return _full_config()


@router.get("/available-tools")
def get_available_tools(_user: CurrentUser) -> dict:
    """Return which vibe-coding tools are installed on the server."""
    claude_bin = get_claude_bin()
    cursor_bin = get_cursor_bin()
    return {
        "claude": bool(shutil.which(claude_bin)),
        "cursor": bool(shutil.which(cursor_bin)),
    }


@router.post("/restart", status_code=204)
def restart_server(_admin: AdminUser) -> None:
    """Restart the server by running restart.sh in the project root (admin only)."""
    subprocess.Popen(
        ["nohup", "bash", "restart.sh"],
        cwd=str(_PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
