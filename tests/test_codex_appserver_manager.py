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

    def _make(*, cwd, env=None, codex_bin="codex"):
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
        if method in ("initialize", "thread/start"):
            return {}  # fake doesn't implement these
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
        fut = _ask_fake_to_send_server_request_async(
            "s1", "item/tool/requestUserInput", {"prompt": "your name?"}
        )
        # Cache should populate quickly
        assert _wait_until(lambda: mgr.get_pending_auq("s1") is not None)
        auq = mgr.get_pending_auq("s1")
        assert auq is not None
        assert auq["method"] == "item/tool/requestUserInput"
        assert auq["params"] == {"prompt": "your name?"}
        # The fake's send_server_request future should NOT be done yet —
        # we never replied.
        assert not fut.done()
        # Resolve and verify the fake receives our reply
        mgr.resolve_auq("s1", "Codex")
        roundtrip = fut.result(timeout=3.0)
        assert roundtrip["client_reply"] == {"text": "Codex"}
        # After resolve, pending cleared
        assert mgr.get_pending_auq("s1") is None
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
