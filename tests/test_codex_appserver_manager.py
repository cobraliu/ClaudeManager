"""Tests for codex_appserver_manager.

Uses the same fake ws server as test_codex_appserver_client.py, with the
manager's spawn/health helpers stubbed so we never actually launch a
`codex` binary or bind a real port. The fake server speaks JSON-RPC over
websockets the same way `codex app-server --listen ws://` does; one
running server handles every per-session ws connection (each connection
gets its own FakeServer state, so sessions don't cross-talk).

What we actually verify:

  * start/stop lifecycle + is_alive + pid/port plumbing
  * thread/start + thread/resume sandbox/approval overrides
  * thread/resume falls back to thread/start on "no rollout found"
  * plan + compaction notification caches
  * ServerRequest (AUQ / approval) interception → pending state cached
  * resolve_approval / resolve_auq write a response back to the server
"""
from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest
import websockets

from app.services import codex_appserver_manager as mgr
from app.services.codex_appserver_client import CodexAppServerClient
from tests.fake_codex_appserver import FakeServer


# ── shared fake ws server (one per test, supports multiple connections) ──


class _FakeServerHandle:
    """A websockets.serve() running on its own thread+loop.

    Tests use this instead of `serve_fake()` because the manager runs from
    sync code and spawns multiple ws connections per test — we need the
    server alive across all of them.
    """

    def __init__(self) -> None:
        self.port: int = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopped = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="fake-codex-appserver", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("fake codex app-server failed to start")

    def stop(self) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        async def _handler(ws):
            await FakeServer(ws).run()

        async def _start_server():
            self._server = await websockets.serve(_handler, "127.0.0.1", 0)
            self.port = self._server.sockets[0].getsockname()[1]
            self._ready.set()

        loop.run_until_complete(_start_server())
        try:
            loop.run_forever()
        finally:
            if self._server is not None:
                self._server.close()
                loop.run_until_complete(self._server.wait_closed())
            loop.close()
            self._stopped.set()


@pytest.fixture(autouse=True)
def _patch_manager_for_fake(monkeypatch):
    """Stub the manager's process+port helpers so tests run against the fake.

    The fake replaces the actual `codex app-server --listen ws://...` binary.
    The manager still goes through its real `start()` codepath; only the
    side-effects that touch real OS resources (spawn, port bind, /readyz,
    pid kill) are swapped for harmless stand-ins.
    """
    handle = _FakeServerHandle()
    handle.start()

    # Manager will use the fake's port instead of trying to bind a real one.
    monkeypatch.setattr(mgr, "_alloc_port", lambda: handle.port)

    # No subprocess — pretend the spawn returned our own pid. Our patched
    # _terminate_pid will refuse to signal it.
    fake_pid = os.getpid()
    monkeypatch.setattr(
        mgr,
        "_spawn_detached",
        lambda **kw: fake_pid,
    )

    # The fake doesn't host /readyz; just succeed immediately.
    async def _no_wait(port, *, timeout):
        return None

    monkeypatch.setattr(mgr, "_wait_ready", _no_wait)

    # Belt-and-suspenders: never actually kill our own process.
    monkeypatch.setattr(mgr, "_terminate_pid", lambda pid: None)
    monkeypatch.setattr(mgr, "_pid_alive", lambda pid: True)

    # Short-circuit codex-specific RPCs the fake doesn't speak (initialize,
    # thread/start, thread/resume). Everything else (echo, emit_notification,
    # send_server_request, blackhole, ...) still hits the fake.
    real_send = CodexAppServerClient.send_request

    async def _patched_send(self, method, params=None, *, timeout=30.0):
        if method == "initialize":
            return {}
        if method == "thread/start":
            return {"thread": {"id": "fake-thread-id"}}
        if method == "thread/resume":
            tid = (params or {}).get("threadId", "fake-thread-id")
            return {"thread": {"id": tid}}
        return await real_send(self, method, params, timeout=timeout)

    monkeypatch.setattr(CodexAppServerClient, "send_request", _patched_send)

    try:
        yield
    finally:
        # Tear down any clients this test left behind. Default
        # terminate_process=False is harmless (our _terminate_pid is a no-op
        # anyway) but matches production graceful-shutdown semantics.
        mgr.shutdown_all(terminate_process=False)
        handle.stop()


def _wait_until(predicate, *, timeout: float = 2.0, interval: float = 0.02) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ── lifecycle ────────────────────────────────────────────────────────────


def test_start_returns_pid_and_port_and_marks_alive():
    pid, port = mgr.start("s1", cwd=os.getcwd())
    try:
        assert isinstance(pid, int) and pid > 0
        assert isinstance(port, int) and port > 0
        assert mgr.is_alive("s1") is True
        assert mgr.get_pid("s1") == pid
        assert mgr.get_port("s1") == port
    finally:
        mgr.stop("s1")


def test_stop_clears_state_and_is_idempotent():
    mgr.start("s1", cwd=os.getcwd())
    mgr.stop("s1")
    mgr.stop("s1")  # idempotent
    assert mgr.is_alive("s1") is False
    assert mgr.get_pid("s1") is None
    assert mgr.get_port("s1") is None


def test_start_twice_same_session_raises():
    mgr.start("s1", cwd=os.getcwd())
    try:
        with pytest.raises(RuntimeError):
            mgr.start("s1", cwd=os.getcwd())
    finally:
        mgr.stop("s1")


def test_resume_falls_back_to_start_when_no_rollout(monkeypatch):
    """If thread/resume reports 'no rollout found', start() should retry
    with thread/start and end up alive with the new thread id."""
    from app.services.codex_appserver_client import (
        CodexAppServerError,
    )

    real_send = CodexAppServerClient.send_request
    new_thread_id = "fresh-thread-after-fallback"

    async def _no_rollout_then_start(self, method, params=None, *, timeout=30.0):
        if method == "initialize":
            return {}
        if method == "thread/resume":
            raise CodexAppServerError(
                -32600,
                "no rollout found for thread id 019e5e49-d65a-7a53-8dad-a6dc820f33c0",
            )
        if method == "thread/start":
            return {"thread": {"id": new_thread_id}}
        return await real_send(self, method, params, timeout=timeout)

    monkeypatch.setattr(CodexAppServerClient, "send_request", _no_rollout_then_start)

    pid, port = mgr.start(
        "s1",
        cwd=os.getcwd(),
        resume_thread_id="019e5e49-d65a-7a53-8dad-a6dc820f33c0",
    )
    try:
        assert isinstance(pid, int) and pid > 0
        assert mgr.is_alive("s1") is True
        # Manager must surface the NEW thread id so the caller can update DB.
        assert mgr.get_thread_id("s1") == new_thread_id
    finally:
        mgr.stop("s1")


def test_thread_start_sends_sandbox_and_approval_overrides(monkeypatch):
    """start() must pin sandbox=workspace-write + approvalPolicy=on-request on
    fresh threads. Without this, codex's compiled-in default falls to read-only
    in any untrusted cwd, which silently hangs request_user_input turns."""
    real_send = CodexAppServerClient.send_request
    captured: dict[str, dict] = {}

    async def _capture_send(self, method, params=None, *, timeout=30.0):
        if method == "initialize":
            return {}
        if method == "thread/start":
            captured["thread/start"] = dict(params or {})
            return {"thread": {"id": "tid-fresh"}}
        return await real_send(self, method, params, timeout=timeout)

    monkeypatch.setattr(CodexAppServerClient, "send_request", _capture_send)

    mgr.start("s1", cwd=os.getcwd())
    try:
        params = captured["thread/start"]
        assert params["sandbox"] == "workspace-write"
        assert params["approvalPolicy"] == "on-request"
        assert params["cwd"] == os.getcwd()
    finally:
        mgr.stop("s1")


def test_thread_resume_sends_sandbox_and_approval_overrides(monkeypatch):
    """thread/resume must also pin sandbox/approvalPolicy — codex applies the
    same trust-based default at resume time, so a stale read-only would still
    bite when the user returns to an existing session."""
    real_send = CodexAppServerClient.send_request
    captured: dict[str, dict] = {}

    async def _capture_send(self, method, params=None, *, timeout=30.0):
        if method == "initialize":
            return {}
        if method == "thread/resume":
            captured["thread/resume"] = dict(params or {})
            return {"thread": {"id": "tid-resumed"}}
        return await real_send(self, method, params, timeout=timeout)

    monkeypatch.setattr(CodexAppServerClient, "send_request", _capture_send)

    mgr.start("s1", cwd=os.getcwd(), resume_thread_id="prior-tid")
    try:
        params = captured["thread/resume"]
        assert params["threadId"] == "prior-tid"
        assert params["sandbox"] == "workspace-write"
        assert params["approvalPolicy"] == "on-request"
    finally:
        mgr.stop("s1")


def test_resume_propagates_other_errors(monkeypatch):
    """Errors other than 'no rollout' must NOT trigger the fallback."""
    from app.services.codex_appserver_client import (
        CodexAppServerError,
    )

    real_send = CodexAppServerClient.send_request

    async def _other_error(self, method, params=None, *, timeout=30.0):
        if method == "initialize":
            return {}
        if method == "thread/resume":
            raise CodexAppServerError(-32603, "internal server error")
        if method == "thread/start":
            pytest.fail("fallback should not run for non-rollout errors")
        return await real_send(self, method, params, timeout=timeout)

    monkeypatch.setattr(CodexAppServerClient, "send_request", _other_error)

    with pytest.raises(CodexAppServerError):
        mgr.start("s1", cwd=os.getcwd(), resume_thread_id="some-id")
    assert mgr.is_alive("s1") is False


def test_list_sessions_reflects_state():
    assert "x1" not in mgr.list_sessions()
    mgr.start("x1", cwd=os.getcwd())
    try:
        assert "x1" in mgr.list_sessions()
    finally:
        mgr.stop("x1")
        assert "x1" not in mgr.list_sessions()


# ── reconnect ────────────────────────────────────────────────────────────


def test_reconnect_attaches_without_thread_start():
    """reconnect() must NOT send thread/start or thread/resume — the codex
    process still owns the in-memory thread. Only initialize + ws handshake."""
    # First start so we have a port + pid + thread id on file.
    pid, port = mgr.start("s1", cwd=os.getcwd())
    thread_id = mgr.get_thread_id("s1")
    # Detach without killing the (fake) process.
    mgr.stop("s1", terminate_process=False)
    assert mgr.is_alive("s1") is False

    mgr.reconnect("s1", pid=pid, port=port, thread_id=thread_id)
    try:
        assert mgr.is_alive("s1") is True
        assert mgr.get_pid("s1") == pid
        assert mgr.get_port("s1") == port
        assert mgr.get_thread_id("s1") == thread_id
    finally:
        mgr.stop("s1")


# ── notification cache ───────────────────────────────────────────────────


def _ask_fake_to_emit_notification(session_id: str, method: str, params: dict) -> None:
    """Drive the fake via the manager's running client."""
    with mgr._state_lock:  # noqa: SLF001
        state = mgr._sessions[session_id]  # noqa: SLF001
    client = state.client

    async def _go():
        await client.send_request(
            "emit_notification",
            {"method": method, "params": params},
            timeout=5.0,
        )

    mgr._runner.submit(_go(), timeout=5.0)  # noqa: SLF001


def test_plan_notification_caches_payload():
    mgr.start("s1", cwd=os.getcwd())
    try:
        _ask_fake_to_emit_notification("s1", "turn/plan/updated", {"plan": "x"})
        assert _wait_until(lambda: mgr.get_plan("s1") is not None)
        assert mgr.get_plan("s1") == {"plan": "x"}
    finally:
        mgr.stop("s1")


def test_compacted_notification_sets_flag_briefly():
    mgr.start("s1", cwd=os.getcwd())
    try:
        assert mgr.is_compacting("s1") is False
        _ask_fake_to_emit_notification("s1", "thread/compacted", {})
        assert _wait_until(lambda: mgr.is_compacting("s1") is True)
    finally:
        mgr.stop("s1")


# ── ServerRequest interception ───────────────────────────────────────────


def _ask_fake_to_send_server_request_async(
    session_id: str, method: str, params: dict
):
    """Ask the fake to send a ServerRequest. Returns a Future that resolves
    when the fake has received the client's response. Useful to assert that
    resolve_*() actually drove the round-trip."""
    with mgr._state_lock:  # noqa: SLF001
        state = mgr._sessions[session_id]  # noqa: SLF001
    client = state.client
    loop = mgr._runner.loop()  # noqa: SLF001

    coro = client.send_request(
        "send_server_request",
        {"method": method, "params": params},
        timeout=10.0,
    )
    return asyncio.run_coroutine_threadsafe(coro, loop)


def test_auq_server_request_is_cached_without_responding():
    mgr.start("s1", cwd=os.getcwd())
    try:
        params = {
            "questions": [
                {"id": "name", "header": "Identity", "question": "Your name?"},
            ],
        }
        fut = _ask_fake_to_send_server_request_async(
            "s1", "item/tool/requestUserInput", params
        )
        # Cache should populate quickly
        assert _wait_until(lambda: mgr.get_pending_auq("s1") is not None)
        auq = mgr.get_pending_auq("s1")
        assert auq is not None
        assert auq["method"] == "item/tool/requestUserInput"
        assert auq["params"] == params
        # The fake's send_server_request future should NOT be done yet —
        # we never replied.
        assert not fut.done()
        # Resolve and verify the fake receives our reply in codex's expected
        # shape: {answers: {<qid>: {answers: [<str>...]}}}.
        mgr.resolve_auq("s1", {"name": ["Codex"]})
        roundtrip = fut.result(timeout=3.0)
        assert roundtrip["client_reply"] == {
            "answers": {"name": {"answers": ["Codex"]}}
        }
        # After resolve, pending cleared
        assert mgr.get_pending_auq("s1") is None
    finally:
        mgr.stop("s1")


def test_resolve_auq_filters_non_string_entries():
    """Defensive: only string answers per question survive; bogus keys/values
    are dropped so we never send malformed JSON to codex."""
    mgr.start("s1", cwd=os.getcwd())
    try:
        fut = _ask_fake_to_send_server_request_async(
            "s1",
            "item/tool/requestUserInput",
            {"questions": [{"id": "q1", "header": "h", "question": "q?"}]},
        )
        assert _wait_until(lambda: mgr.get_pending_auq("s1") is not None)
        mgr.resolve_auq(
            "s1",
            {
                "q1": ["yes", 42, None, "also"],  # type: ignore[list-item]
                123: ["nope"],  # type: ignore[dict-item]
            },
        )
        roundtrip = fut.result(timeout=3.0)
        assert roundtrip["client_reply"] == {
            "answers": {"q1": {"answers": ["yes", "also"]}}
        }
    finally:
        mgr.stop("s1")


def test_approval_server_request_cached_and_resolvable():
    mgr.start("s1", cwd=os.getcwd())
    try:
        fut = _ask_fake_to_send_server_request_async(
            "s1",
            "item/commandExecution/requestApproval",
            {"command": "rm -rf /"},
        )
        assert _wait_until(lambda: mgr.get_pending_approval("s1") is not None)
        appr = mgr.get_pending_approval("s1")
        assert appr is not None
        assert appr["params"] == {"command": "rm -rf /"}
        assert not fut.done()
        mgr.resolve_approval("s1", allow=False, feedback="too risky")
        roundtrip = fut.result(timeout=3.0)
        reply = roundtrip["client_reply"]
        assert reply["decision"] == "denied"
        assert reply["approved"] is False
        assert reply["feedback"] == "too risky"
        assert mgr.get_pending_approval("s1") is None
    finally:
        mgr.stop("s1")


def test_resolve_without_pending_raises():
    mgr.start("s1", cwd=os.getcwd())
    try:
        with pytest.raises(KeyError):
            mgr.resolve_auq("s1", "hello")
        with pytest.raises(KeyError):
            mgr.resolve_approval("s1", allow=True)
    finally:
        mgr.stop("s1")


def test_shutdown_all_closes_every_session():
    mgr.start("a", cwd=os.getcwd())
    mgr.start("b", cwd=os.getcwd())
    assert set(mgr.list_sessions()) >= {"a", "b"}
    mgr.shutdown_all()
    assert mgr.list_sessions() == []
