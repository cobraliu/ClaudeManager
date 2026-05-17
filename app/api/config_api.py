from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import (
    get_claude_bin,
    get_cursor_bin,
    get_default_workspace,
    get_proxy,
    get_ssh_config,
    get_terminal_font,
    set_claude_bin,
    set_cursor_bin,
    set_default_workspace,
    set_proxy,
    set_ssh_config,
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
    terminal_font: str


class WorkspaceUpdateRequest(BaseModel):
    workspace: str


class ClaudeBinUpdateRequest(BaseModel):
    claude_bin: str


class CursorBinUpdateRequest(BaseModel):
    cursor_bin: str


class ProxyUpdateRequest(BaseModel):
    proxy: str


class SshConfigRequest(BaseModel):
    host: str = ""
    port: int = 22
    user: str = ""


class TerminalFontRequest(BaseModel):
    font: str


def _full_config() -> ConfigView:
    return ConfigView(
        workspace=get_default_workspace(),
        claude_bin=get_claude_bin(),
        cursor_bin=get_cursor_bin(),
        proxy=get_proxy(),
        terminal_font=get_terminal_font(),
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
    set_proxy(body.proxy.strip())
    return _full_config()


@router.get("/ssh")
def get_ssh(_user: CurrentUser):
    return get_ssh_config()


@router.put("/ssh")
def update_ssh(body: SshConfigRequest, _admin: AdminUser):
    set_ssh_config(body.host.strip(), body.port, body.user.strip())
    return get_ssh_config()


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
