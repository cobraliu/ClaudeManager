"""Smoke tests for the AgentAdapter abstraction.

These don't touch the network or real CLI binaries; they verify:
  - registry returns the right adapter for each tool name,
  - each adapter conforms to the Protocol surface,
  - build_command yields the same shell command shapes the legacy code did.
"""
from __future__ import annotations

import shlex

import pytest

from app.agents import get_adapter, list_supported_tools
from app.agents.base import AgentAdapter
from app.agents.claude import ClaudeAdapter
from app.agents.cursor import CursorAdapter


def test_registry_returns_correct_kinds():
    assert get_adapter("claude").kind == "claude"
    assert get_adapter("cursor").kind == "cursor"
    # Unknown tool falls back to Claude (matches legacy default)
    assert get_adapter("nope").kind == "claude"


def test_registry_lists_supported_tools():
    assert set(list_supported_tools()) == {"claude", "cursor", "codex"}


def test_registry_routes_codex():
    assert get_adapter("codex").kind == "codex"


def test_claude_adapter_protocol_conformance():
    adapter = ClaudeAdapter()
    assert isinstance(adapter, AgentAdapter)


def test_cursor_adapter_protocol_conformance():
    adapter = CursorAdapter()
    assert isinstance(adapter, AgentAdapter)


@pytest.mark.parametrize(
    "resume,model,inner_id,expected_substrings",
    [
        # Fresh session, no model
        (None, None, None, ["--dangerously-skip-permissions"]),
        # Fresh session, with model
        (None, "claude-opus-4-7", None, ["--dangerously-skip-permissions", "--model", "claude-opus-4-7"]),
        # Fresh session with inner_id (PID file wrapper)
        (None, None, "abc123", ["echo $$ > /tmp/claude-inner-abc123.pid", "exec "]),
        # Resume
        ("sess-uuid-1", None, None, ["--resume", "sess-uuid-1"]),
        # Resume + inner_id (resume gets PID wrapper too)
        ("sess-uuid-2", None, "xyz", ["echo $$ > /tmp/claude-inner-xyz.pid", "--resume"]),
    ],
)
def test_claude_build_command_shapes(resume, model, inner_id, expected_substrings):
    cmd = ClaudeAdapter().build_command(
        cwd="/tmp",
        env={},
        model=model,
        resume_session_id=resume,
        inner_id=inner_id,
        claude_bin="claude",
        cursor_bin="agent",
        claude_shell="",
    )
    for s in expected_substrings:
        assert s in cmd, f"expected {s!r} in {cmd!r}"


def test_claude_build_command_with_login_shell():
    cmd = ClaudeAdapter().build_command(
        cwd="/tmp",
        env={},
        model=None,
        resume_session_id=None,
        inner_id=None,
        claude_bin="claude",
        cursor_bin="agent",
        claude_shell="bash -l",
    )
    # claude_shell wraps the whole thing in `bash -l -c '...'`
    assert cmd.startswith("bash -l -c ")
    # the inner -c arg should round-trip through shlex
    quoted_part = cmd[len("bash -l -c "):]
    inner = shlex.split(quoted_part)[0]
    assert "--dangerously-skip-permissions" in inner


def test_claude_quotes_special_chars_in_resume_id():
    # Defensive: resume_session_id should be shell-quoted
    cmd = ClaudeAdapter().build_command(
        cwd="/tmp",
        env={},
        model=None,
        resume_session_id="a b;c",  # not a real UUID but tests quoting
        inner_id=None,
        claude_bin="claude",
        cursor_bin="agent",
        claude_shell="",
    )
    # The dangerous string must not appear unquoted
    assert "a b;c" not in cmd or "'a b;c'" in cmd


def test_cursor_build_command_shapes():
    a = CursorAdapter()
    fresh = a.build_command(
        cwd="/tmp", env={}, model=None, resume_session_id=None,
        inner_id=None, claude_bin="claude", cursor_bin="agent", claude_shell="",
    )
    assert fresh == "agent --yolo"

    resumed = a.build_command(
        cwd="/tmp", env={}, model=None, resume_session_id="chat-xyz",
        inner_id=None, claude_bin="claude", cursor_bin="agent", claude_shell="",
    )
    assert resumed == "agent --yolo --resume chat-xyz"


def test_cursor_ignores_inner_id_and_model():
    # Cursor doesn't support --model and doesn't need PID wrapping.
    cmd = CursorAdapter().build_command(
        cwd="/tmp", env={}, model="gpt-5", resume_session_id=None,
        inner_id="should-be-ignored", claude_bin="claude", cursor_bin="agent",
        claude_shell="bash -l",  # also shouldn't wrap
    )
    assert cmd == "agent --yolo"


def test_proxy_tap_flags():
    assert ClaudeAdapter().needs_proxy_tap() is True
    assert CursorAdapter().needs_proxy_tap() is False


def test_cursor_never_has_waiting_state():
    state = CursorAdapter().get_waiting_state(
        agent_session_id="anything", agent_pid=12345, cwd="/tmp",
    )
    assert state is None


def test_claude_waiting_state_returns_none_without_pid():
    # No PID → no waiting state, regardless of session id
    state = ClaudeAdapter().get_waiting_state(
        agent_session_id="anything", agent_pid=None, cwd="/tmp",
    )
    assert state is None


def test_claude_models_have_known_ids():
    models = ClaudeAdapter().list_models()
    ids = {m["id"] for m in models}
    assert "claude-opus-4-7" in ids
    assert "claude-sonnet-4-6" in ids


def test_all_adapters_expose_get_jsonl_path_and_list_local_sessions():
    """Regression: api/sessions.py endpoints dispatch via these methods.
    Missing them on any adapter would break /conversation, /jsonl, or
    /available-claude-sessions for that tool.
    """
    from app.agents.codex import CodexAdapter
    for adapter in (ClaudeAdapter(), CursorAdapter(), CodexAdapter()):
        assert callable(getattr(adapter, "get_jsonl_path", None)), (
            f"{adapter.kind} missing get_jsonl_path"
        )
        assert callable(getattr(adapter, "list_local_sessions", None)), (
            f"{adapter.kind} missing list_local_sessions"
        )


def test_get_conversation_accepts_from_ts():
    """The migrated /conversation endpoint passes from_ts. All three adapters
    must accept it (Protocol-widened in app/agents/base.py)."""
    from app.agents.codex import CodexAdapter
    import inspect
    for adapter in (ClaudeAdapter(), CursorAdapter(), CodexAdapter()):
        sig = inspect.signature(adapter.get_conversation)
        assert "from_ts" in sig.parameters, f"{adapter.kind}.get_conversation missing from_ts"


def test_codex_get_jsonl_path_returns_none_for_unknown_id():
    """Smoke: get_jsonl_path tolerates unknown session ids without raising."""
    from app.agents.codex import CodexAdapter
    assert CodexAdapter().get_jsonl_path("not-a-real-uuid", "/tmp") is None


def test_codex_list_local_sessions_returns_normalized_shape():
    """The /available-claude-sessions endpoint expects each entry to have
    claude_session_id / title / mtime keys (frontend renders these directly)."""
    from app.agents.codex import CodexAdapter
    rows = CodexAdapter().list_local_sessions("/nonexistent-cwd-xyz")
    # Either empty or has the expected keys
    for r in rows:
        assert "claude_session_id" in r
        assert "title" in r
        assert "mtime" in r
