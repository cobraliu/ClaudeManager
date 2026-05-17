"""Ephemeral bash shell WebSocket — spawns bash directly in a PTY (no tmux).

Bypassing tmux gives xterm.js full native scrollback: every byte written by bash
flows through the PTY master into xterm's buffer, so the user can scroll all the
way back through the session history without needing tmux copy-mode.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import platform
import pty
import shutil
import signal
import struct
import tempfile
import termios
import time
import threading

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.models.session import WsClientMessage
from app.services.tmux_service import PtyHandle, TmuxService

router = APIRouter()

_tmux: TmuxService | None = None
_shell_tokens: dict | None = None


def configure(tmux: TmuxService, shell_tokens: dict) -> None:
    global _tmux, _shell_tokens
    _tmux = tmux
    _shell_tokens = shell_tokens


_BLOCKED_CMDS = (
    "sudo", "su", "pkexec", "passwd", "chsh", "chfn",
    "newgrp", "visudo", "doas", "runuser",
)

def _write_init_file(project_root: str) -> str:
    """Write a bash init file that restricts cd and blocks privileged commands."""
    blocked = "\n".join(
        f'{cmd}() {{ echo "[shell] {cmd}: not allowed" >&2; return 1; }}'
        for cmd in _BLOCKED_CMDS
    )
    content = f"""# ClaudeManager restricted shell init
PROJECT_ROOT="{project_root}"

# Restrict cd to project directory tree
cd() {{
    local target="${{1:-$PROJECT_ROOT}}"
    local abs
    abs=$(realpath -m "$target" 2>/dev/null || echo "$target")
    case "$abs" in
        "$PROJECT_ROOT"|"$PROJECT_ROOT/"*)
            builtin cd "$@" ;;
        *)
            echo "[shell] cd: access denied (outside project directory)" >&2
            return 1 ;;
    esac
}}

# Block privileged commands
{blocked}

export PS1='(project) \\w\\$ '
builtin cd "$PROJECT_ROOT" 2>/dev/null
"""
    fd, path = tempfile.mkstemp(prefix=".cm_shell_", suffix=".sh")
    os.write(fd, content.encode())
    os.close(fd)
    return path


def _use_firejail() -> bool:
    """True on Linux when firejail is available."""
    return platform.system() == "Linux" and shutil.which("firejail") is not None


def _write_admin_init_file(project_root: str) -> str:
    """Minimal init file for admin shells: cd to project dir, no restrictions."""
    content = f"""# ClaudeManager admin shell init
[ -f ~/.bashrc ] && source ~/.bashrc
builtin cd "{project_root}" 2>/dev/null
export PS1='(admin) \\w\\$ '
"""
    fd, path = tempfile.mkstemp(prefix=".cm_shell_admin_", suffix=".sh")
    os.write(fd, content.encode())
    os.close(fd)
    return path


def _spawn_bash(cwd: str, cols: int = 80, rows: int = 24, unrestricted: bool = False) -> PtyHandle:
    """Fork a bash process attached to a new PTY.

    unrestricted=True (admin): full system access, sources ~/.bashrc, no cd/command blocks.
    unrestricted=False (normal): restricted to cwd, privileged commands blocked.

    On Linux with firejail available (restricted only):
      - firejail drops all Linux capabilities → sudo/pkexec can't escalate
    """
    init_file = _write_admin_init_file(cwd) if unrestricted else _write_init_file(cwd)

    master_fd, slave_fd = pty.openpty()

    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

    pid = os.fork()
    if pid == 0:
        # ── child ──
        os.close(master_fd)
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)
        try:
            os.chdir(cwd)
        except OSError:
            pass
        if not unrestricted and _use_firejail():
            os.execvp("firejail", [
                "firejail", "--quiet", "--noprofile",
                "--caps.drop=all",   # drop all Linux capabilities (blocks sudo/pkexec at OS level)
                "--nonewprivs",      # prevent gaining new privileges via setuid binaries
                "--noroot",          # deny root inside sandbox
                "bash", "--init-file", init_file,
            ])
        else:
            os.execvp("bash", ["bash", "--init-file", init_file])
        os._exit(1)
    else:
        # ── parent ── delete init file after bash has had time to read it
        os.close(slave_fd)
        def _cleanup():
            time.sleep(3)
            try:
                os.unlink(init_file)
            except OSError:
                pass
        threading.Thread(target=_cleanup, daemon=True).start()
        return PtyHandle(master_fd=master_fd, child_pid=pid)


@router.websocket("/ws/shell")
async def shell_ws(websocket: WebSocket, token: str = Query(...)):
    assert _shell_tokens is not None

    info = _shell_tokens.pop(token, None)
    if info is None:
        await websocket.close(code=4001, reason="invalid token")
        return

    cwd = info["cwd"]
    unrestricted = info.get("unrestricted", False)

    pty_handle: PtyHandle | None = None
    try:
        pty_handle = _spawn_bash(cwd, unrestricted=unrestricted)
    except Exception as exc:
        await websocket.close(code=4003, reason=str(exc))
        return

    await websocket.accept()

    loop = asyncio.get_event_loop()
    closed = False
    fd = pty_handle.master_fd
    output_queue: asyncio.Queue[bytes] = asyncio.Queue()

    def _on_pty_readable():
        try:
            data = os.read(fd, 65536)
            output_queue.put_nowait(data if data else b"")
        except OSError:
            output_queue.put_nowait(b"")

    loop.add_reader(fd, _on_pty_readable)

    async def output_pusher():
        nonlocal closed
        while not closed:
            try:
                data = await output_queue.get()
                if not data:
                    if not closed:
                        try:
                            await websocket.send_json({"type": "state", "payload": {"status": "terminated"}})
                        except Exception:
                            pass
                    break
                await websocket.send_bytes(data)
            except Exception:
                break

    pusher_task = asyncio.create_task(output_pusher())

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = WsClientMessage.model_validate_json(raw)
            except Exception:
                continue

            if msg.type == "input" and msg.data is not None:
                await loop.run_in_executor(None, pty_handle.write, msg.data.encode("utf-8"))
            elif msg.type == "resize" and msg.cols and msg.rows:
                pty_handle.resize(msg.cols, msg.rows)
            elif msg.type == "ping":
                await websocket.send_json({"type": "pong", "payload": {"client_ts": msg.ts, "server_ts": int(time.time() * 1000)}})
    except WebSocketDisconnect:
        pass
    finally:
        closed = True
        try:
            loop.remove_reader(fd)
        except Exception:
            pass
        pusher_task.cancel()
        try:
            await pusher_task
        except asyncio.CancelledError:
            pass
        if pty_handle:
            pty_handle.close()
        # Reap the child process
        try:
            os.kill(pty_handle.child_pid, signal.SIGHUP)
        except OSError:
            pass
        try:
            os.waitpid(pty_handle.child_pid, os.WNOHANG)
        except (OSError, ChildProcessError):
            pass
