"""WebSocket endpoint for tmux-backed bash terminals.

Differs from ``terminal_ws.py`` (Claude TUI) in that there's no AUQ state to
track. Multi-attach is supported natively by tmux.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.models.session import WsClientMessage
from app.services.bash_term_service import TerminalManager
from app.services.tmux_service import PtyHandle, TmuxService

logger = logging.getLogger(__name__)

router = APIRouter()

_tmux: TmuxService | None = None
_term_mgr: TerminalManager | None = None


def configure(tmux: TmuxService, term_mgr: TerminalManager) -> None:
    global _tmux, _term_mgr
    _tmux = tmux
    _term_mgr = term_mgr


_MIN_COLS = 40
_MIN_ROWS = 10


@router.websocket("/ws/terminals/{term_id}")
async def terminal_ws(
    websocket: WebSocket,
    term_id: str,
    token: str = Query(...),
    cols: int = Query(default=120),
    rows: int = Query(default=30),
):
    assert _tmux is not None and _term_mgr is not None

    if cols < _MIN_COLS:
        cols = 120
    if rows < _MIN_ROWS:
        rows = 30

    # Validate one-shot token
    bound_term_id = _term_mgr.consume_token(token)
    if bound_term_id != term_id:
        await websocket.close(code=4001, reason="invalid token")
        return

    rec = _term_mgr.get(term_id)
    if rec is None:
        await websocket.close(code=4004, reason="terminal not found")
        return

    await websocket.accept()
    loop = asyncio.get_event_loop()

    pty_handle: PtyHandle | None = None
    try:
        pty_handle = await loop.run_in_executor(
            None, _tmux.attach_pty, rec.tmux_name, cols, rows
        )
    except Exception as exc:
        try:
            await websocket.send_json({
                "type": "state",
                "payload": {"status": "error", "message": str(exc)},
            })
        except Exception:
            pass
        await websocket.close(code=4003, reason="failed to attach")
        return

    _term_mgr.on_attach(term_id)

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
                            await websocket.send_json({
                                "type": "state",
                                "payload": {"status": "terminated"},
                            })
                        except Exception:
                            pass
                    break
                await websocket.send_bytes(data)
            except Exception:
                break

    pusher_task = asyncio.create_task(output_pusher())

    # Helpful: a small banner naming the terminal
    try:
        await websocket.send_json({
            "type": "state",
            "payload": {
                "status": "attached",
                "term_id": term_id,
                "name": rec.name,
                "is_named": rec.is_named,
            },
        })
    except Exception:
        pass

    # Per-connection tmux copy-mode state, used by the scroll WS protocol.
    in_copy_mode = False
    scroll_depth = 0  # net lines scrolled up; 0 = live screen

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
                # Mirror the size on the underlying tmux window so its grid
                # matches the PTY exactly; otherwise tmux can clip output.
                try:
                    await loop.run_in_executor(
                        None, _tmux._run,
                        "resize-window", "-t", rec.tmux_name,
                        "-x", str(msg.cols), "-y", str(msg.rows),
                    )
                except Exception:
                    pass
            elif msg.type == "scroll" and msg.delta is not None:
                delta = msg.delta  # negative = scroll up, positive = scroll down
                # Re-sync with tmux's actual mode: copy-mode can be cancelled by
                # user keypresses (Enter/Escape/q) without us knowing, which
                # would leave in_copy_mode=True stale and make subsequent
                # scroll-up commands no-ops.
                actually_in_mode = await loop.run_in_executor(
                    None, _tmux.is_pane_in_mode, rec.tmux_name,
                )
                if in_copy_mode and not actually_in_mode:
                    in_copy_mode = False
                    scroll_depth = 0
                    try:
                        await websocket.send_json({"type": "copy-mode-exited"})
                    except Exception:
                        pass
                try:
                    if delta < 0:
                        n = min(abs(delta), 50)
                        if not in_copy_mode:
                            await loop.run_in_executor(
                                None, _tmux._run, "copy-mode", "-t", rec.tmux_name,
                            )
                            in_copy_mode = True
                        await loop.run_in_executor(
                            None, _tmux._run,
                            "send-keys", "-t", rec.tmux_name,
                            "-N", str(n), "-X", "scroll-up",
                        )
                        scroll_depth += n
                    elif delta > 0 and in_copy_mode:
                        n = min(delta, 50)
                        scroll_depth = max(0, scroll_depth - n)
                        if scroll_depth <= 0:
                            await loop.run_in_executor(
                                None, _tmux._run,
                                "send-keys", "-t", rec.tmux_name, "-X", "cancel",
                            )
                            in_copy_mode = False
                            scroll_depth = 0
                        else:
                            await loop.run_in_executor(
                                None, _tmux._run,
                                "send-keys", "-t", rec.tmux_name,
                                "-N", str(n), "-X", "scroll-down",
                            )
                except Exception:
                    pass
            elif msg.type == "exit-copy-mode":
                try:
                    await loop.run_in_executor(
                        None, _tmux._run,
                        "send-keys", "-t", rec.tmux_name, "-X", "cancel",
                    )
                    await loop.run_in_executor(
                        None, _tmux._run, "refresh-client", "-t", rec.tmux_name,
                    )
                except Exception:
                    pass
                in_copy_mode = False
                scroll_depth = 0
            elif msg.type == "ping":
                await websocket.send_json({
                    "type": "pong",
                    "payload": {"client_ts": msg.ts, "server_ts": int(time.time() * 1000)},
                })
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("term_ws receive loop error id=%s: %s", term_id, exc)
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
        if pty_handle is not None:
            pty_handle.close()
        # Refcount-decrement; ephemeral with zero attachers gets killed.
        try:
            _term_mgr.on_detach(term_id)
        except Exception as exc:
            logger.warning("on_detach failed id=%s: %s", term_id, exc)
