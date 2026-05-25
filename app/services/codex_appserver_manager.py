"""Per-session lifecycle + state cache for `codex app-server` clients.

The FastAPI REST handlers in api/sessions.py are synchronous (run on a
threadpool). The codex client (codex_appserver_client.py) is fully async.
This manager bridges them by owning a dedicated background asyncio loop:

  * sync callers (start/stop/query/send) submit coroutines via
    `run_coroutine_threadsafe` and block briefly for the result.
  * the background loop owns all CodexAppServerClient instances; everything
    that touches the subprocess runs there.

State cache. Codex routes interactive prompts via ServerNotifications and
ServerRequests rather than synchronous tool output. We subscribe to the
ones that map onto Claude-parity state slots and keep the latest payload
per session:

  * turn/plan/updated          → plan
  * thread/compacted           → compaction marker
  * item/tool/requestUserInput → pending AUQ          (ServerRequest)
  * item/commandExecution/requestApproval
    item/fileChange/requestApproval
    item/permissions/requestApproval
    execCommandApproval / applyPatchApproval (legacy) → pending approval

Pending ServerRequests are held with their request_id; the client cannot
reply until the user accepts or denies. resolve_approval(session_id,
allow=True/False) finishes the dance.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from app.services.codex_appserver_client import (
    CodexAppServerClient,
    CodexAppServerError,
)

logger = logging.getLogger(__name__)


# ── notification / request method buckets ────────────────────────────────

PLAN_NOTIFICATION = "turn/plan/updated"
COMPACTED_NOTIFICATION = "thread/compacted"

AUQ_SERVER_REQUEST_METHODS = {"item/tool/requestUserInput"}

APPROVAL_SERVER_REQUEST_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "execCommandApproval",
    "applyPatchApproval",
}


@dataclass
class PendingServerRequest:
    request_id: Any
    method: str
    params: dict
    arrived_at: float


@dataclass
class _SessionState:
    client: CodexAppServerClient
    pending_auq: PendingServerRequest | None = None
    pending_approval: PendingServerRequest | None = None
    plan: dict | None = None
    compacted_at: float | None = None
    thread_id: str | None = None
    last_event_at: float = field(default_factory=time.time)


# ── background loop owner ────────────────────────────────────────────────


class _LoopRunner:
    """One asyncio loop running in a daemon thread. Lazy-started on first use."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and self._loop.is_running():
                return self._loop
            self._loop = asyncio.new_event_loop()
            ready = threading.Event()

            def _runner() -> None:
                asyncio.set_event_loop(self._loop)
                ready.set()
                self._loop.run_forever()

            self._thread = threading.Thread(
                target=_runner,
                name="codex-appserver-loop",
                daemon=True,
            )
            self._thread.start()
            ready.wait()
            return self._loop

    def submit(self, coro, *, timeout: float | None = 30.0):
        loop = self.loop()
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=timeout)


_runner = _LoopRunner()
_sessions: dict[str, _SessionState] = {}
_state_lock = threading.Lock()


# ── public sync API (called from FastAPI handlers) ───────────────────────


def start(
    session_id: str,
    *,
    cwd: str,
    env: dict[str, str] | None = None,
    model: str | None = None,
    codex_bin: str = "codex",
) -> int:
    """Spawn the codex app-server, send `initialize`, and start a thread.

    Returns the subprocess PID. Raises RuntimeError if a client already
    exists for this session.
    """
    with _state_lock:
        if session_id in _sessions:
            raise RuntimeError(f"codex app-server already running for {session_id}")
    # Enable the `request_user_input` tool in Default collaboration mode.
    # Why: codex's default mode normally hides this tool ("request_user_input is
    # unavailable in Default mode") and only allows free-text questions. The
    # ClaudeManager UI surfaces structured AUQ via this RPC, so we opt into the
    # `default_mode_request_user_input` feature for every app-server session.
    extra_args = ["--enable", "default_mode_request_user_input"]
    client = CodexAppServerClient(
        cwd=cwd, env=env, codex_bin=codex_bin, extra_args=extra_args
    )
    state = _SessionState(client=client)

    async def _setup() -> int:
        pid = await client.spawn()
        # Wire handlers BEFORE issuing requests so we never miss the first
        # notification that may arrive interleaved with the initialize reply.
        _attach_handlers(session_id, state)
        try:
            await client.send_request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "ClaudeManager",
                        "title": "ClaudeManager",
                        "version": "0.1.0",
                    },
                },
                timeout=10.0,
            )
        except Exception:
            logger.exception(
                "codex app-server: initialize failed for session %s", session_id
            )
            # initialize failure is fatal — tear down so caller sees an error
            await client.close()
            raise
        try:
            ts_params: dict[str, Any] = {"cwd": cwd}
            if model:
                ts_params["model"] = model
            resp = await client.send_request("thread/start", ts_params, timeout=15.0)
            # codex 0.130.0 returns {thread: {id, sessionId, ...}, model, ...}.
            # Older shapes (top-level threadId) are still tolerated as fallbacks.
            tid: str | None = None
            if isinstance(resp, dict):
                thread = resp.get("thread")
                if isinstance(thread, dict):
                    cand = thread.get("id") or thread.get("sessionId")
                    if isinstance(cand, str):
                        tid = cand
                if tid is None:
                    cand = resp.get("threadId") or resp.get("thread_id")
                    if isinstance(cand, str):
                        tid = cand
            if tid is None:
                raise RuntimeError(
                    f"codex thread/start response missing thread id: {resp!r}"
                )
            state.thread_id = tid
        except Exception:
            logger.exception(
                "codex app-server: thread/start failed for session %s", session_id
            )
            await client.close()
            raise
        return pid

    try:
        pid = _runner.submit(_setup(), timeout=20.0)
    except Exception:
        # Ensure we don't leave a partial entry behind.
        with _state_lock:
            _sessions.pop(session_id, None)
        raise
    with _state_lock:
        _sessions[session_id] = state
    return pid


def stop(session_id: str) -> None:
    """Close the client and drop session state. Idempotent."""
    with _state_lock:
        state = _sessions.pop(session_id, None)
    if state is None:
        return

    async def _close() -> None:
        await state.client.close()

    try:
        _runner.submit(_close(), timeout=5.0)
    except Exception:
        logger.exception("codex app-server: error closing session %s", session_id)


def is_alive(session_id: str) -> bool:
    with _state_lock:
        state = _sessions.get(session_id)
    return state is not None and state.client.is_alive()


def get_pid(session_id: str) -> int | None:
    with _state_lock:
        state = _sessions.get(session_id)
    return state.client.pid if state is not None else None


def get_thread_id(session_id: str) -> str | None:
    with _state_lock:
        state = _sessions.get(session_id)
    return state.thread_id if state is not None else None


def get_pending_auq(session_id: str) -> dict | None:
    with _state_lock:
        state = _sessions.get(session_id)
    if state is None or state.pending_auq is None:
        return None
    p = state.pending_auq
    return {
        "request_id": p.request_id,
        "method": p.method,
        "params": p.params,
        "arrived_at": p.arrived_at,
    }


def get_pending_approval(session_id: str) -> dict | None:
    with _state_lock:
        state = _sessions.get(session_id)
    if state is None or state.pending_approval is None:
        return None
    p = state.pending_approval
    return {
        "request_id": p.request_id,
        "method": p.method,
        "params": p.params,
        "arrived_at": p.arrived_at,
    }


def get_plan(session_id: str) -> dict | None:
    with _state_lock:
        state = _sessions.get(session_id)
    return state.plan if state is not None else None


def is_compacting(session_id: str) -> bool:
    """Return True if a compaction notification arrived within the last 60s."""
    with _state_lock:
        state = _sessions.get(session_id)
    if state is None or state.compacted_at is None:
        return False
    return (time.time() - state.compacted_at) < 60.0


def send_user_message(
    session_id: str,
    text: str,
    *,
    timeout: float = 30.0,
) -> dict:
    """Send a user message via turn/start. Returns the server response."""
    with _state_lock:
        state = _sessions.get(session_id)
    if state is None:
        raise KeyError(session_id)
    params: dict = {"items": [{"type": "text", "text": text}]}
    if state.thread_id:
        params["threadId"] = state.thread_id
    return _runner.submit(
        state.client.send_request("turn/start", params, timeout=timeout),
        timeout=timeout + 5.0,
    )


def resolve_approval(
    session_id: str,
    *,
    allow: bool,
    feedback: str | None = None,
) -> None:
    """Respond to a pending approval ServerRequest.

    The exact response shape depends on the request method. We use a
    permissive default that covers the common approval shapes; if codex
    upstream tightens the schema, this is the place to specialise.
    """
    with _state_lock:
        state = _sessions.get(session_id)
        pending = state.pending_approval if state is not None else None
        if state is not None:
            state.pending_approval = None
    if state is None or pending is None:
        raise KeyError(f"no pending approval for {session_id}")

    response_payload = _build_approval_response(pending.method, allow=allow, feedback=feedback)

    async def _send_response() -> None:
        # Write the response directly. The client's _handle_server_request
        # normally does this, but here we already deferred answering and own
        # the response shape.
        await state.client._write_line(  # noqa: SLF001 — intentional
            {"jsonrpc": "2.0", "id": pending.request_id, "result": response_payload}
        )

    _runner.submit(_send_response(), timeout=5.0)


def resolve_auq(session_id: str, text: str) -> None:
    """Respond to a pending tool/requestUserInput ServerRequest with text."""
    with _state_lock:
        state = _sessions.get(session_id)
        pending = state.pending_auq if state is not None else None
        if state is not None:
            state.pending_auq = None
    if state is None or pending is None:
        raise KeyError(f"no pending AUQ for {session_id}")

    async def _send_response() -> None:
        await state.client._write_line(  # noqa: SLF001
            {"jsonrpc": "2.0", "id": pending.request_id, "result": {"text": text}}
        )

    _runner.submit(_send_response(), timeout=5.0)


def list_sessions() -> list[str]:
    with _state_lock:
        return list(_sessions.keys())


def shutdown_all() -> None:
    """Tear down every active client. Called on backend shutdown."""
    with _state_lock:
        ids = list(_sessions.keys())
    for sid in ids:
        try:
            stop(sid)
        except Exception:
            logger.exception("codex app-server: shutdown error for %s", sid)


# ── internal: handler wiring ─────────────────────────────────────────────


def _attach_handlers(session_id: str, state: _SessionState) -> None:
    client = state.client

    def _on_plan(params: dict) -> None:
        with _state_lock:
            state.plan = params
            state.last_event_at = time.time()

    def _on_compacted(params: dict) -> None:
        with _state_lock:
            state.compacted_at = time.time()
            state.last_event_at = state.compacted_at

    client.subscribe(PLAN_NOTIFICATION, _on_plan)
    client.subscribe(COMPACTED_NOTIFICATION, _on_compacted)
    # AUQ + approval ServerRequests must be DEFERRED (waiting on a human),
    # not auto-responded. The generic set_server_request_handler always
    # writes a reply after the handler returns, which would race the user.
    # Hook _dispatch directly so deferred kinds short-circuit the auto-reply.
    _intercept_server_requests(session_id, state)


def _intercept_server_requests(session_id: str, state: _SessionState) -> None:
    """Replace the client's _dispatch so we can stash deferred ServerRequests.

    The codex_appserver_client's default handler-based path always responds
    immediately when the handler returns. Approval and AUQ require deferring
    the response until the human decides. Cleanest way to support that
    without bloating the generic client is to wrap _dispatch here.
    """
    client = state.client
    original_dispatch = client._dispatch  # type: ignore[attr-defined]

    def _wrapped(msg: dict) -> None:
        msg_id = msg.get("id")
        method = msg.get("method")
        if msg_id is not None and method is not None:
            # ServerRequest (has both id and method)
            params = msg.get("params") or {}
            if method in AUQ_SERVER_REQUEST_METHODS:
                with _state_lock:
                    state.pending_auq = PendingServerRequest(
                        request_id=msg_id,
                        method=method,
                        params=params,
                        arrived_at=time.time(),
                    )
                    state.last_event_at = time.time()
                logger.info(
                    "codex[%s]: AUQ deferred (method=%s id=%s)",
                    session_id,
                    method,
                    msg_id,
                )
                return
            if method in APPROVAL_SERVER_REQUEST_METHODS:
                with _state_lock:
                    state.pending_approval = PendingServerRequest(
                        request_id=msg_id,
                        method=method,
                        params=params,
                        arrived_at=time.time(),
                    )
                    state.last_event_at = time.time()
                logger.info(
                    "codex[%s]: approval deferred (method=%s id=%s)",
                    session_id,
                    method,
                    msg_id,
                )
                return
        # Everything else (responses, notifications, unknown ServerRequests)
        # falls through to the client's normal dispatch.
        original_dispatch(msg)

    client._dispatch = _wrapped  # type: ignore[attr-defined]


def _build_approval_response(method: str, *, allow: bool, feedback: str | None) -> dict:
    """Construct the result payload for an approval ServerRequest.

    Codex's approval result schemas vary by method but all of them accept
    either {"decision": "approved"|"denied", "reason": ...} or a boolean
    "approved" field. We send both keys so the server can pick whichever
    it expects.
    """
    payload: dict = {
        "decision": "approved" if allow else "denied",
        "approved": allow,
    }
    if feedback:
        payload["feedback"] = feedback
        payload["reason"] = feedback
    return payload
