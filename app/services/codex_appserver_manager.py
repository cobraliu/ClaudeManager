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
import contextlib
import logging
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

from app.services.codex_appserver_client import (
    CodexAppServerClient,
    CodexAppServerError,
)

logger = logging.getLogger(__name__)


# Codex app-server listen-port pool. We bind on 127.0.0.1 only (codex itself
# enforces localhost), so the range only needs to be large enough to cover all
# concurrent app-server sessions on one host.
_PORT_RANGE_START = 54300
_PORT_RANGE_END = 54400

# Where to spool codex's stdout/stderr. Each session gets its own logfile so we
# can grep per-session crashes without losing other sessions' output.
_LOG_DIR = Path(os.environ.get("CM_CODEX_LOG_DIR", "logs/codex"))


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
    pid: int  # OS pid of the detached codex app-server process
    port: int  # the ws://127.0.0.1:PORT codex is listening on
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
    resume_thread_id: str | None = None,
) -> tuple[int, int]:
    """Spawn a detached codex app-server, connect via ws, start/resume a thread.

    Returns (pid, port). The caller (api/sessions.py) persists both into the
    session row so a backend restart can reconnect via `reconnect()`.

    Raises RuntimeError if a client already exists for this session.

    If `resume_thread_id` is provided, codex resumes that existing thread via
    `thread/resume`; otherwise a fresh thread is created via `thread/start`.
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

    port = _alloc_port()
    log_path = _ensure_log_path(session_id)
    pid = _spawn_detached(
        session_id=session_id,
        cwd=cwd,
        env=env,
        codex_bin=codex_bin,
        port=port,
        extra_args=extra_args,
        log_path=log_path,
    )

    client = CodexAppServerClient(ws_url=f"ws://127.0.0.1:{port}/")
    state = _SessionState(client=client, pid=pid, port=port)

    async def _setup() -> None:
        await _wait_ready(port, timeout=8.0)
        await client.connect(timeout=5.0)
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
            await client.close()
            raise
        try:
            if resume_thread_id:
                method = "thread/resume"
                ts_params: dict[str, Any] = {
                    "threadId": resume_thread_id,
                    **_default_thread_overrides(),
                }
            else:
                method = "thread/start"
                ts_params = {"cwd": cwd, **_default_thread_overrides()}
                if model:
                    ts_params["model"] = model
            try:
                resp = await client.send_request(method, ts_params, timeout=15.0)
            except CodexAppServerError as exc:
                # thread/resume requires an on-disk rollout JSONL. If the previous
                # spawn died before writing one (e.g. crashed at startup), codex
                # answers "no rollout found". Fall back to a fresh thread/start so
                # the user can keep using the ClaudeManager session; the caller
                # will pick up the new thread id via get_thread_id and update DB.
                if method == "thread/resume" and _is_no_rollout_error(exc):
                    logger.warning(
                        "codex app-server: thread/resume found no rollout for %s; "
                        "falling back to thread/start",
                        resume_thread_id,
                    )
                    method = "thread/start"
                    ts_params = {"cwd": cwd, **_default_thread_overrides()}
                    if model:
                        ts_params["model"] = model
                    resp = await client.send_request(method, ts_params, timeout=15.0)
                else:
                    raise
            tid = _extract_thread_id(resp)
            if tid is None:
                raise RuntimeError(
                    f"codex {method} response missing thread id: {resp!r}"
                )
            state.thread_id = tid
        except Exception:
            logger.exception(
                "codex app-server: %s failed for session %s", method, session_id
            )
            await client.close()
            raise

    try:
        _runner.submit(_setup(), timeout=25.0)
    except Exception:
        # Spawned process may still be alive; kill it to avoid orphans.
        _terminate_pid(pid)
        with _state_lock:
            _sessions.pop(session_id, None)
        raise
    with _state_lock:
        _sessions[session_id] = state
    return pid, port


def reconnect(
    session_id: str,
    *,
    pid: int,
    port: int,
    thread_id: str | None,
) -> None:
    """Reattach to a codex app-server process that survived a backend restart.

    Does NOT call thread/start or thread/resume — the codex process still has
    the thread in memory; subsequent turn/start calls use the cached thread_id.
    Raises if the process is dead or the ws handshake fails.
    """
    with _state_lock:
        if session_id in _sessions:
            raise RuntimeError(f"codex app-server already attached for {session_id}")
    if not _pid_alive(pid):
        raise RuntimeError(f"codex pid {pid} not alive")

    client = CodexAppServerClient(ws_url=f"ws://127.0.0.1:{port}/")
    state = _SessionState(client=client, pid=pid, port=port, thread_id=thread_id)

    async def _setup() -> None:
        await _wait_ready(port, timeout=4.0)
        await client.connect(timeout=5.0)
        _attach_handlers(session_id, state)
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

    try:
        _runner.submit(_setup(), timeout=15.0)
    except Exception:
        with _state_lock:
            _sessions.pop(session_id, None)
        raise
    with _state_lock:
        _sessions[session_id] = state


def stop(session_id: str, *, terminate_process: bool = True) -> None:
    """Close the ws client and (optionally) terminate the detached codex
    process. Idempotent.

    `terminate_process=False` lets the manager detach from a still-running
    codex (e.g. during a backend graceful shutdown that should leave the
    process alive for reconnect on next startup).
    """
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
    if terminate_process:
        _terminate_pid(state.pid)


def is_alive(session_id: str) -> bool:
    with _state_lock:
        state = _sessions.get(session_id)
    return state is not None and state.client.is_alive()


def get_pid(session_id: str) -> int | None:
    with _state_lock:
        state = _sessions.get(session_id)
    return state.pid if state is not None else None


def get_port(session_id: str) -> int | None:
    with _state_lock:
        state = _sessions.get(session_id)
    return state.port if state is not None else None


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
    # codex 0.130.0 turn/start expects `input` (list of content items); older
    # versions used `items`. threadId is required.
    params: dict = {"input": [{"type": "text", "text": text}]}
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


def resolve_auq(session_id: str, answers: dict[str, list[str]]) -> None:
    """Respond to a pending tool/requestUserInput ServerRequest.

    `answers` is keyed by `question.id` (from the original request's
    `params.questions[].id`). Each value is the list of selected/typed
    strings for that question.

    Codex's ToolRequestUserInputResponse shape is:
        {"answers": {<qid>: {"answers": [<str>, ...]}, ...}}
    — a dict of question ids to a wrapper object whose `answers` is a
    string array. Sending the wrong shape causes codex to parse `answers`
    as `{}` and report empty answers to the model.
    """
    with _state_lock:
        state = _sessions.get(session_id)
        pending = state.pending_auq if state is not None else None
        if state is not None:
            state.pending_auq = None
    if state is None or pending is None:
        raise KeyError(f"no pending AUQ for {session_id}")

    payload = {
        "answers": {
            qid: {"answers": [s for s in (vals or []) if isinstance(s, str)]}
            for qid, vals in (answers or {}).items()
            if isinstance(qid, str)
        }
    }

    async def _send_response() -> None:
        await state.client._write_line(  # noqa: SLF001
            {"jsonrpc": "2.0", "id": pending.request_id, "result": payload}
        )

    _runner.submit(_send_response(), timeout=5.0)


def list_sessions() -> list[str]:
    with _state_lock:
        return list(_sessions.keys())


def shutdown_all(*, terminate_process: bool = False) -> None:
    """Tear down every active client. Called on backend shutdown.

    By default leaves the detached codex processes running so the next
    backend startup can reconnect via `reconnect()`. Pass `terminate_process=
    True` only for tests / hard cleanup.
    """
    with _state_lock:
        ids = list(_sessions.keys())
    for sid in ids:
        try:
            stop(sid, terminate_process=terminate_process)
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


def _default_thread_overrides() -> dict[str, Any]:
    """Force `sandbox` + `approvalPolicy` on every thread/start and thread/resume.

    codex's compiled-in defaults pick `read-only` for any cwd that isn't in
    `~/.codex/config.toml`'s `projects.*.trust_level = "trusted"` list. With
    `read-only` we observe a 175s silent hang on tool-use turns (the model
    receives a degraded tool catalog and never emits a function_call). The
    ClaudeManager UI also has no way to surface a sandbox-escape prompt, so a
    silent denial is the worst possible UX.

    Behavior we want: same as `codex` TUI in a trusted directory — read+write
    inside cwd, approval prompt for anything that needs escalation. The user
    already opted into this directory by creating the ClaudeManager session
    against it, so we treat that as authorization.
    """
    return {
        "sandbox": "workspace-write",
        "approvalPolicy": "on-request",
    }


def _alloc_port() -> int:
    """Find a free port in the codex listen range, biased to skip ports already
    held by an active session (cheaper than relying on bind-collision retries).

    No registry, no lease — between this returning and codex binding the same
    port a third party could grab it. Single-user app, narrow window; codex
    will fail loudly if its rebind loses the race and the caller can retry.
    """
    with _state_lock:
        used = {s.port for s in _sessions.values()}
    for port in range(_PORT_RANGE_START, _PORT_RANGE_END):
        if port in used:
            continue
        try:
            with contextlib.closing(
                socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            ) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise RuntimeError(
        f"no free port in {_PORT_RANGE_START}-{_PORT_RANGE_END} for codex app-server"
    )


def _ensure_log_path(session_id: str) -> Path:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    return _LOG_DIR / f"{session_id}.log"


def _spawn_detached(
    *,
    session_id: str,
    cwd: str,
    env: dict[str, str] | None,
    codex_bin: str,
    port: int,
    extra_args: list[str],
    log_path: Path,
) -> int:
    """Spawn `codex app-server --listen ws://127.0.0.1:PORT` detached from us.

    `start_new_session=True` puts codex in its own session+process group, so:
      * SIGHUP from our terminal won't reach it
      * we can target the whole pgrp later via `os.killpg`
      * Python's atexit / asyncio teardown won't propagate to it
    stdout+stderr go to a per-session logfile so the wire stays uncluttered.
    """
    full_env = {**os.environ, **(env or {})}
    args = [
        codex_bin,
        "app-server",
        "--listen",
        f"ws://127.0.0.1:{port}",
        *extra_args,
    ]
    logger.info(
        "codex app-server: spawning detached session=%s port=%d cmd=%r",
        session_id,
        port,
        args,
    )
    with open(log_path, "ab") as logf:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            env=full_env,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=logf,
            start_new_session=True,
        )
    return proc.pid


async def _wait_ready(port: int, *, timeout: float) -> None:
    """Poll the /readyz endpoint until codex reports ready, or raise."""
    url = f"http://127.0.0.1:{port}/readyz"
    deadline = time.time() + timeout
    last_err: Exception | None = None
    async with aiohttp.ClientSession() as sess:
        while time.time() < deadline:
            try:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=1.0)) as resp:
                    if resp.status == 200:
                        return
                    last_err = RuntimeError(f"readyz returned {resp.status}")
            except Exception as exc:
                last_err = exc
            await asyncio.sleep(0.1)
    raise RuntimeError(
        f"codex app-server on port {port} not ready after {timeout}s: {last_err!r}"
    )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # process exists but we're not allowed to signal it
        return True


def _terminate_pid(pid: int) -> None:
    """SIGTERM the codex process group; SIGKILL if it lingers."""
    if not _pid_alive(pid):
        return
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, 15)  # SIGTERM
    except ProcessLookupError:
        return
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.killpg(pgid, 9)  # SIGKILL
    except ProcessLookupError:
        pass


def _extract_thread_id(resp: Any) -> str | None:
    """Pluck the codex thread id from a thread/start or thread/resume reply.

    codex 0.130 returns {thread: {id, sessionId, ...}, model, ...}; older
    shapes used a top-level `threadId`. Tolerate both.
    """
    if not isinstance(resp, dict):
        return None
    thread = resp.get("thread")
    if isinstance(thread, dict):
        cand = thread.get("id") or thread.get("sessionId")
        if isinstance(cand, str):
            return cand
    cand = resp.get("threadId") or resp.get("thread_id")
    return cand if isinstance(cand, str) else None


def _is_no_rollout_error(exc: CodexAppServerError) -> bool:
    """Detect the codex 'no rollout found for thread id <X>' error.

    codex uses generic -32600 (Invalid Request) for this, so we match on the
    message text instead of the code. The phrasing has been stable across
    0.130.x but we keep the check loose to survive small wording tweaks.
    """
    msg = (exc.message or "").lower()
    return "no rollout" in msg or "rollout not found" in msg


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
