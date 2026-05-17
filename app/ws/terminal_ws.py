from __future__ import annotations

import asyncio
import logging
import os
import time

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.models.session import SessionStatus, WsClientMessage
from app.services.claude_pid import format_tui_hint, get_pid_waiting_state
from app.services.claude_session_reader import get_latest_turn_info
from app.services.session_store import SessionStore
from app.services.tmux_service import PtyHandle, TmuxService

logger = logging.getLogger(__name__)

router = APIRouter()

_store: SessionStore | None = None
_tmux: TmuxService | None = None


def _pid_is_waiting_for_auq(pid: int | None) -> bool:
    """Return True iff Claude CLI's PID file says it's paused on an AskUserQuestion."""
    state = get_pid_waiting_state(pid)
    return state is not None and state[1] == "auq"


def configure(store: SessionStore, tmux: TmuxService) -> None:
    global _store, _tmux
    _store = store
    _tmux = tmux


def _get_store() -> SessionStore:
    assert _store is not None
    return _store


def _get_tmux() -> TmuxService:
    assert _tmux is not None
    return _tmux


_MIN_COLS = 40
_MIN_ROWS = 10


@router.websocket("/ws/sessions/{session_id}")
async def terminal_ws(
    websocket: WebSocket,
    session_id: str,
    token: str = Query(...),
    cols: int = Query(default=220),
    rows: int = Query(default=50),
):
    store = _get_store()
    tmux = _get_tmux()

    # Reject obviously-broken sizes from transient layout glitches in the client.
    # Without this, a 5-col-wide pane will permanently mangle Claude's TUI.
    if cols < _MIN_COLS:
        cols = 220
    if rows < _MIN_ROWS:
        rows = 50

    session = store.get(session_id)
    if session is None or session.ws_token != token:
        await websocket.close(code=4001, reason="unauthorized")
        return

    if session.status == SessionStatus.TERMINATED:
        await websocket.close(code=4002, reason="session terminated")
        return

    await websocket.accept()

    loop = asyncio.get_event_loop()

    # Attach to the tmux session via a PTY for full raw terminal I/O.
    # Run in executor: attach_pty calls subprocess (has_session) + os.fork(), both blocking.
    pty_handle: PtyHandle | None = None
    try:
        pty_handle = await loop.run_in_executor(
            None, tmux.attach_pty, session.tmux_session_name, cols, rows
        )
    except Exception as exc:
        await websocket.send_json({
            "type": "state",
            "payload": {"status": "error", "message": str(exc)},
        })
        await websocket.close(code=4003, reason="failed to attach")
        return

    closed = False
    fd = pty_handle.master_fd

    # Use asyncio native fd reader for zero-latency output
    output_queue: asyncio.Queue[bytes] = asyncio.Queue()

    def _on_pty_readable():
        try:
            data = os.read(fd, 65536)
            if data:
                output_queue.put_nowait(data)
            else:
                output_queue.put_nowait(b"")  # EOF
        except OSError:
            output_queue.put_nowait(b"")  # EOF

    loop.add_reader(fd, _on_pty_readable)

    # Fire-and-forget: sync offset baseline, resize window, refresh screen.
    # Done after add_reader so PTY data can flow immediately without blocking.
    # Syncing last_output_offset at connect lets the watchdog accurately detect
    # genuinely new output after the client disconnects (avoids stale-offset false positives).
    async def _refresh_client():
        # Sync output offset baseline (no last_activity_at change)
        try:
            hs = await loop.run_in_executor(None, tmux.get_pane_history_size, session.tmux_session_name)
            if hs is not None:
                store.sync_output_offset(session_id, hs)
        except Exception:
            pass
        # Resize to the client's exact dimensions
        try:
            await loop.run_in_executor(
                None, tmux._run,
                "resize-window", "-t", session.tmux_session_name, "-x", str(cols), "-y", str(rows),
            )
        except Exception:
            pass
        # Force tmux to resend the current screen
        try:
            await loop.run_in_executor(
                None, tmux._run, "refresh-client", "-t", session.tmux_session_name
            )
        except Exception:
            pass
    asyncio.create_task(_refresh_client())

    # copy-mode scroll state (per WebSocket connection)
    in_copy_mode = False
    scroll_depth = 0  # lines scrolled up; 0 = at live bottom

    # Initialize to now so the initial tmux screen-refresh burst (first ~1s after attach)
    # doesn't update last_activity_at and falsely trigger is_streaming.
    _last_activity_update = [time.time()]
    # Shared flag: True once PTY has gone idle for 2s (Claude finished the current turn).
    # Used by both output_pusher and the finally-block to decide whether to stamp activity.
    _pty_idle_synced = [False]

    async def output_pusher():
        nonlocal closed
        while not closed:
            try:
                try:
                    data = await asyncio.wait_for(output_queue.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    # PTY has been idle for 2s — sync last_turn_at immediately so
                    # is_streaming clears without waiting for the 5s watchdog.
                    if not _pty_idle_synced[0]:
                        _pty_idle_synced[0] = True
                        cur = store.get(session_id)
                        if cur and cur.claude_session_id:
                            try:
                                _sid, _cwd = cur.claude_session_id, cur.cwd
                                info = await loop.run_in_executor(
                                    None, get_latest_turn_info, _sid, _cwd, 0.0
                                )
                                if info["turn_ts"]:
                                    store.update_last_turn_at(session_id, info["turn_ts"])
                            except Exception:
                                pass
                        # PID file is authoritative: set/clear hint based on waiting state
                        try:
                            cur2 = store.get(session_id)
                            state = get_pid_waiting_state(cur2.claude_proc_pid if cur2 else None)
                            hint = format_tui_hint(state[0], state[1]) if state else None
                            store.set_tui_hint(session_id, hint)
                        except Exception:
                            pass
                    continue
                _pty_idle_synced[0] = False  # PTY became active again
                store.set_tui_hint(session_id, None)  # clear hint on new output
                if not data:
                    # EOF – claude process exited, tmux session ended
                    try:
                        cur = store.get(session_id)
                        if cur and cur.status != SessionStatus.TERMINATED:
                            store.force_status(session_id, SessionStatus.TERMINATED)
                    except Exception:
                        pass
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
                # Update last_activity_at (throttled to once per 2s).
                now = time.time()
                if now - _last_activity_update[0] > 2.0:
                    _last_activity_update[0] = now
                    try:
                        store.update_activity(session_id)
                    except Exception:
                        pass
            except Exception:
                break
        # If output_pusher exited unexpectedly (send error etc.) while WS appears
        # open, close the WS so the frontend knows to reconnect.
        if not closed:
            try:
                await websocket.close(code=1011, reason="output error")
            except Exception:
                pass

    pusher_task = asyncio.create_task(output_pusher())

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = WsClientMessage.model_validate_json(raw)
            except Exception:
                continue

            if msg.type == "input" and msg.data is not None:
                if in_copy_mode:
                    # Exit copy mode before forwarding keystrokes to the app
                    try:
                        await loop.run_in_executor(
                            None, tmux._run,
                            "send-keys", "-t", session.tmux_session_name, "-X", "cancel",
                        )
                    except Exception:
                        pass
                    in_copy_mode = False
                    scroll_depth = 0

                data = msg.data
                if msg.pane is not None:
                    # Direct-to-pane: route via send-keys to pane 0.0 so input always
                    # reaches the claude process regardless of tmux split focus.
                    pane_target = f"{session.tmux_session_name}:0.{msg.pane}"
                    text_part = data.rstrip("\r\n")
                    # Defense-in-depth: refuse to forward chat prompts when Claude
                    # is paused on an AUQ. The frontend gates the send button, but
                    # races (stale state, multi-tab) can still slip through and
                    # corrupt the Ink TUI. Bounce the text back so the user can edit.
                    fresh = store.get(session_id)
                    if fresh and _pid_is_waiting_for_auq(fresh.claude_proc_pid):
                        try:
                            await websocket.send_json({
                                "type": "prompt-rejected",
                                "reason": "auq_pending",
                                "text": text_part,
                            })
                        except Exception:
                            pass
                        continue
                    # Deliver via tmux paste-buffer + Enter key name. send-keys -l
                    # was unreliable: literal "\r" in -l mode is just a byte to the
                    # pane PTY and Ink occasionally fails to recognize it as Enter
                    # (text lands nowhere, user sees the bubble hang in "sending"
                    # for 120s). paste-buffer routes through tmux's paste path,
                    # and "Enter" by key name fires a real return keypress.
                    #
                    # For multi-line prompts we need BOTH:
                    #   -p: wrap the buffer in bracketed-paste markers so Ink
                    #       treats the whole thing as a single paste and inserts
                    #       it into the input box rather than acting on each
                    #       intermediate keypress.
                    #   -r: skip tmux's default LF→CR translation. Without -r,
                    #       every internal LF becomes a CR even inside the
                    #       bracketed-paste envelope, and Ink interprets each CR
                    #       as submit — so the prompt is fragmented into one
                    #       submission per line. With -r the LFs survive as
                    #       literal newlines in the input buffer; the explicit
                    #       send-keys Enter below then submits the full prompt
                    #       as one message.
                    needs_enter = data.endswith(("\r", "\n"))
                    if text_part or needs_enter:
                        buf_name = f"cm-{session_id[:8]}-{int(time.time() * 1000)}"
                        try:
                            if text_part:
                                await loop.run_in_executor(
                                    None, tmux._run,
                                    "set-buffer", "-b", buf_name, text_part,
                                )
                                await loop.run_in_executor(
                                    None, tmux._run,
                                    "paste-buffer", "-d", "-p", "-r", "-b", buf_name, "-t", pane_target,
                                )
                            if needs_enter:
                                await loop.run_in_executor(
                                    None, tmux._run,
                                    "send-keys", "-t", pane_target, "Enter",
                                )
                        except Exception as exc:
                            # Don't leave the client hanging on a silently dropped
                            # message. Bounce it back so the optimistic bubble
                            # disappears immediately and the text returns to the
                            # input box for retry.
                            logger.warning(
                                "Failed to deliver chat prompt to %s: %s",
                                pane_target, exc,
                            )
                            try:
                                await websocket.send_json({
                                    "type": "prompt-rejected",
                                    "reason": "send_failed",
                                    "text": text_part,
                                })
                            except Exception:
                                pass
                elif session.tool == "cursor" and data.endswith(("\r", "\n")):
                    # Cursor agent's Ink TUI does not accept \r/\n via PTY write as Enter.
                    # Instead, split text from the trailing newline and inject Enter via tmux send-keys.
                    text_part = data.rstrip("\r\n")
                    if text_part:
                        await loop.run_in_executor(None, pty_handle.write, text_part.encode("utf-8"))
                    try:
                        await loop.run_in_executor(
                            None, tmux._run,
                            "send-keys", "-t", session.tmux_session_name, "", "Enter",
                        )
                    except Exception:
                        pass
                else:
                    # Run in executor: write() may block on large pastes (PTY buffer ~4096 bytes)
                    await loop.run_in_executor(None, pty_handle.write, data.encode("utf-8"))
            elif msg.type == "scroll" and msg.delta is not None:
                delta = msg.delta  # negative = scroll up, positive = scroll down
                try:
                    if delta < 0:
                        n = min(abs(delta), 50)
                        if not in_copy_mode:
                            await loop.run_in_executor(
                                None, tmux._run,
                                "copy-mode", "-t", session.tmux_session_name,
                            )
                            in_copy_mode = True
                        await loop.run_in_executor(
                            None, tmux._run,
                            "send-keys", "-t", session.tmux_session_name,
                            "-N", str(n), "-X", "scroll-up",
                        )
                        scroll_depth += n
                    elif delta > 0 and in_copy_mode:
                        n = min(delta, 50)
                        scroll_depth = max(0, scroll_depth - n)
                        if scroll_depth <= 0:
                            await loop.run_in_executor(
                                None, tmux._run,
                                "send-keys", "-t", session.tmux_session_name, "-X", "cancel",
                            )
                            in_copy_mode = False
                            scroll_depth = 0
                        else:
                            await loop.run_in_executor(
                                None, tmux._run,
                                "send-keys", "-t", session.tmux_session_name,
                                "-N", str(n), "-X", "scroll-down",
                            )
                except Exception:
                    pass
            elif msg.type == "resize" and msg.cols and msg.rows:
                # Drop garbage sizes from transient client layout glitches —
                # a 5-col resize would corrupt Claude's TUI for the whole session.
                if msg.cols < _MIN_COLS or msg.rows < _MIN_ROWS:
                    continue
                pty_handle.resize(msg.cols, msg.rows)
                # Explicitly set tmux window to the client's exact dimensions.
                # -x/-y is reliable in both directions; -A only grows, never shrinks.
                try:
                    await loop.run_in_executor(
                        None,
                        tmux._run,
                        "resize-window", "-t", session.tmux_session_name,
                        "-x", str(msg.cols), "-y", str(msg.rows),
                    )
                except Exception:
                    pass
            elif msg.type == "exit-copy-mode":
                # Exit server-side copy mode (entered via the scroll WS message handler).
                # send-keys -X cancel is safe for server-side copy mode; no PTY writes needed.
                try:
                    await loop.run_in_executor(
                        None, tmux._run,
                        "send-keys", "-t", session.tmux_session_name, "-X", "cancel",
                    )
                except Exception:
                    pass
                # Force the client to re-render the live screen after exiting copy mode
                try:
                    await loop.run_in_executor(
                        None, tmux._run, "refresh-client", "-t", session.tmux_session_name
                    )
                except Exception:
                    pass
                in_copy_mode = False
                scroll_depth = 0
            elif msg.type == "refresh":
                try:
                    await loop.run_in_executor(
                        None, tmux._run, "refresh-client", "-t", session.tmux_session_name
                    )
                except Exception:
                    pass
            elif msg.type == "search-init" and msg.query:
                await loop.run_in_executor(
                    None, tmux.search_init_pty, pty_handle, msg.query
                )
            elif msg.type == "search-next":
                await loop.run_in_executor(
                    None, tmux.search_next_pty, pty_handle
                )
            elif msg.type == "ping":
                await websocket.send_json({
                    "type": "pong",
                    "payload": {"client_ts": msg.ts, "server_ts": int(time.time() * 1000)},
                })
    except WebSocketDisconnect:
        pass
    finally:
        # Mark session as viewed at disconnect time so that only output produced
        # AFTER this point triggers has_new_output on the next list poll.
        try:
            store.mark_viewed(session_id, session.owner_id)
        except Exception:
            pass
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
        store.update_attached_clients(session_id, -1)
        updated = store.get(session_id)
        if updated and updated.status == SessionStatus.RUNNING:
            # Check if tmux session is still alive — subprocess call, run off event loop.
            try:
                session_alive = await loop.run_in_executor(
                    None, tmux.has_session, session.tmux_session_name
                )
            except Exception:
                session_alive = False
            if session_alive:
                if updated.attached_clients <= 0:
                    store.transition(session_id, SessionStatus.DETACHED)
            else:
                store.force_status(session_id, SessionStatus.TERMINATED)
        # At disconnect: check if the tmux pane grew since we connected (sync_output_offset
        # set the baseline at connect time).  If history grew → Claude was streaming during
        # the watch → update last_activity_at so the watchdog has the full 5s budget.
        # If history is unchanged → Claude was idle → leave last_activity_at as-is so it
        # naturally expires and clears is_streaming without a false positive.
        try:
            hs = await loop.run_in_executor(None, tmux.get_pane_history_size, session.tmux_session_name)
            if hs is not None:
                store.update_activity_if_offset_changed(session_id, hs)
        except Exception:
            pass
        # Immediately sync last_turn_at so is_streaming clears promptly on disconnect.
        # Run in executor — reads JSONL from disk, must not block the event loop.
        if updated and updated.claude_session_id:
            try:
                _sid, _cwd = updated.claude_session_id, updated.cwd
                info = await loop.run_in_executor(
                    None, get_latest_turn_info, _sid, _cwd, 0.0
                )
                if info["turn_ts"]:
                    store.update_last_turn_at(session_id, info["turn_ts"])
            except Exception:
                pass
