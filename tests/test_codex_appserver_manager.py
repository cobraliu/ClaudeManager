"""Tests for codex_appserver_manager.

Uses the same fake server as test_codex_appserver_client.py, with a small
adapter that lets us spawn the manager-owned client pointed at the fake
instead of the real `codex` binary. Two slots of the manager are not
testable against the fake without extra protocol fixtures:

  * thread/start    — the real server returns {threadId: ...}; our fake
                       doesn't implement it. We monkeypatch the manager's
                       start() to skip thread/start.
  * initialize       — same; fake responds with an error. We monkeypatch to
                       skip it too.

What we actually verify:

  * start/stop lifecycle + is_alive
  * plan + compaction notification caches
  * ServerRequest (AUQ / approval) interception → pending state cached
  * resolve_approval / resolve_auq write a response back to the server
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from app.services import codex_appserver_manager as mgr
from app.services.codex_appserver_client import CodexAppServerClient


FAKE_SERVER = Path(__file__).parent / "fake_codex_appserver.py"


@pytest.fixture(autouse=True)
def _patch_client_for_fake(monkeypatch):
    """Wrap CodexAppServerClient inside the manager so it points at the fake
    and skips initialize/thread/start (the fake doesn't speak the real
    codex protocol). Restored after each test."""

    real_cls = CodexAppServerClient

    def _make(*, cwd, env=None, codex_bin="codex", extra_args=None):
        # extra_args is accepted (and ignored) so the manager can pass --enable
        # default_mode_request_user_input — the fake server doesn't care about
        # codex CLI feature flags.
        return real_cls(
            cwd=cwd,
            env=env,
            codex_bin=sys.executable,
            subcommand=[str(FAKE_SERVER)],
        )

    monkeypatch.setattr(mgr, "CodexAppServerClient", _make)

    # Bypass initialize / thread/start by overriding the inner setup coro.
    # We do this by intercepting the client.send_request to no-op for those
    # two methods.
    real_send = real_cls.send_request

    async def _patched_send(self, method, params=None, *, timeout=30.0):
        if method == "initialize":
            return {}  # fake doesn't implement initialize
        if method == "thread/start":
            # Manager reads {thread: {id|sessionId}} or top-level {threadId}.
            return {"thread": {"id": "fake-thread-id"}}
        if method == "thread/resume":
            return {"thread": {"id": params.get("threadId", "fake-thread-id") if params else "fake-thread-id"}}
        return await real_send(self, method, params, timeout=timeout)

    monkeypatch.setattr(CodexAppServerClient, "send_request", _patched_send)
    yield
    # Tear down anything left behind from this test
    mgr.shutdown_all()


def _wait_until(predicate, *, timeout: float = 2.0, interval: float = 0.02) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ── lifecycle ────────────────────────────────────────────────────────────


def test_start_returns_pid_and_marks_alive():
    pid = mgr.start("s1", cwd=os.getcwd())
    try:
        assert isinstance(pid, int) and pid > 0
        assert mgr.is_alive("s1") is True
        assert mgr.get_pid("s1") == pid
    finally:
        mgr.stop("s1")


def test_stop_clears_state_and_is_idempotent():
    mgr.start("s1", cwd=os.getcwd())
    mgr.stop("s1")
    mgr.stop("s1")  # idempotent
    assert mgr.is_alive("s1") is False
    assert mgr.get_pid("s1") is None


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
        CodexAppServerClient,
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

    pid = mgr.start(
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
    from app.services.codex_appserver_client import CodexAppServerClient

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
    from app.services.codex_appserver_client import CodexAppServerClient

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
        CodexAppServerClient,
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
