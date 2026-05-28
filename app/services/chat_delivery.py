"""Single source of truth for delivering a chat-mode prompt into a live session.

Two callers share this:
  - the authenticated terminal WebSocket (`app/ws/terminal_ws.py`), and
  - the public "chat" share endpoint (`app/api/shares.py`).

`deliver_to_pane` holds the fragile tmux paste sequence so the two paths can't
drift. `deliver_chat_prompt` is the high-level entry the public endpoint uses:
it validates liveness, dispatches per tool, applies the AUQ guard, records
prompt history, and serializes concurrent senders per session.
"""

from __future__ import annotations

import logging
import threading
import time

from app.models.session import SessionMetadata, SessionStatus
from app.services.claude_pid import get_pid_waiting_state
from app.services.session_store import SessionStore
from app.services.tmux_service import TmuxService

logger = logging.getLogger(__name__)


class AuqPendingError(Exception):
    """Claude is paused on an AskUserQuestion; sending now would corrupt the TUI."""


class SessionOfflineError(Exception):
    """The session is not in a state that can receive input."""


class PromptDeliveryError(Exception):
    """The underlying send (tmux / codex app-server) failed."""


# Per-session locks: the public endpoint has no connection-level serialization,
# so two readers pasting at once could interleave and corrupt the Ink input box.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(session_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _locks[session_id] = lock
        return lock


def deliver_to_pane(
    tmux: TmuxService, pane_target: str, text: str, send_enter: bool = True
) -> None:
    """Paste `text` into a tmux pane and (optionally) submit it.

    set-buffer + paste-buffer (-p bracketed paste, -r to skip LF→CR translation)
    keeps a multi-line prompt as one input, then an explicit `send-keys Enter`
    submits it. send-keys -l for the text was unreliable: Ink sometimes failed to
    treat a literal "\\r" as Enter. A prompt containing "@" (image/file refs)
    needs a second Enter — empirically a single Enter leaves the cursor parked
    after the @path without submitting.
    """
    buf_name = f"cm-{int(time.time() * 1000)}"
    if text:
        tmux._run("set-buffer", "-b", buf_name, "--", text)
        tmux._run("paste-buffer", "-d", "-p", "-r", "-b", buf_name, "-t", pane_target)
    if send_enter:
        needs_double_enter = "@" in text
        if text:
            time.sleep(0.1)
        tmux._run("send-keys", "-t", pane_target, "Enter")
        if needs_double_enter:
            time.sleep(0.1)
            tmux._run("send-keys", "-t", pane_target, "Enter")


def _pid_is_waiting_for_auq(pid: int | None) -> bool:
    state = get_pid_waiting_state(pid)
    return state is not None and state[1] == "auq"


def deliver_chat_prompt(
    session: SessionMetadata, text: str, *, tmux: TmuxService, store: SessionStore
) -> None:
    """Deliver a chat prompt to a live session, dispatching by tool.

    Raises SessionOfflineError / AuqPendingError / PromptDeliveryError on the
    respective failure so callers can map them to HTTP statuses.
    """
    if session.status not in (SessionStatus.RUNNING, SessionStatus.DETACHED):
        raise SessionOfflineError(session.status.value)

    # codex app-server has no tmux pane; it takes structured messages over RPC.
    if session.tool == "codex" and session.codex_transport == "app_server":
        from app.services import codex_appserver_manager as csm
        try:
            csm.send_user_message(session.id, text)
        except KeyError as exc:
            raise SessionOfflineError("codex app-server not running") from exc
        except Exception as exc:
            raise PromptDeliveryError(str(exc)) from exc
        return

    # tmux-backed tools (claude / codex TUI / cursor): paste into pane 0.0.
    pane_target = f"{session.tmux_session_name}:0.0"
    lock = _lock_for(session.id)
    with lock:
        # AUQ guard is claude-specific (PID file). For other tools
        # claude_proc_pid is None, so this is a cheap no-op.
        if _pid_is_waiting_for_auq(session.claude_proc_pid):
            raise AuqPendingError()
        try:
            store.append_prompt_history(session.id, text, time.time(), "0")
        except Exception:
            logger.exception("append_prompt_history failed")
        try:
            deliver_to_pane(tmux, pane_target, text, True)
        except Exception as exc:
            raise PromptDeliveryError(str(exc)) from exc
