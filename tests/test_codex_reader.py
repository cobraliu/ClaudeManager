"""Tests for the Codex rollout JSONL reader and adapter.

Uses tmp_path to construct synthetic ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
trees so tests don't depend on the real ~/.codex dir.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.agents.codex import CodexAdapter
from app.services import codex_session_reader as csr


SAMPLE_UUID_A = "019e152e-a0bb-7712-ac1f-db067f6a4984"
SAMPLE_UUID_B = "019e153a-c047-79c0-9357-4f247f8ab6e7"


def _rollout_path(root: Path, when: str, uuid: str) -> Path:
    """root/YYYY/MM/DD/rollout-{when}-{uuid}.jsonl. `when` is the ISO-ish prefix."""
    y, m, d = when[:4], when[5:7], when[8:10]
    p = root / y / m / d
    p.mkdir(parents=True, exist_ok=True)
    return p / f"rollout-{when}-{uuid}.jsonl"


def _write_rollout(
    path: Path,
    *,
    uuid: str,
    cwd: str,
    user_msgs: list[str],
    agent_msgs: list[str],
    extra_lines: list[dict[str, Any]] | None = None,
) -> None:
    """Write a minimal rollout file: session_meta + interleaved user/agent events."""
    lines: list[dict[str, Any]] = [{
        "timestamp": "2026-05-11T00:00:00.000Z",
        "type": "session_meta",
        "payload": {
            "id": uuid,
            "cwd": cwd,
            "originator": "codex-tui",
            "cli_version": "0.130.0",
        },
    }]
    # Interleave user/agent messages
    for i, msg in enumerate(user_msgs):
        lines.append({
            "timestamp": f"2026-05-11T00:0{i}:00.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": msg, "images": [], "local_images": []},
        })
        if i < len(agent_msgs):
            lines.append({
                "timestamp": f"2026-05-11T00:0{i}:30.000Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": agent_msgs[i], "phase": "final_answer"},
            })
    if extra_lines:
        lines.extend(extra_lines)
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")


@pytest.fixture
def codex_root(tmp_path, monkeypatch):
    """Override _SESSIONS_ROOT to a temp dir, clearing the enrich cache."""
    root = tmp_path / "codex_sessions"
    monkeypatch.setattr(csr, "_SESSIONS_ROOT", root)
    csr._cache.clear()
    yield root


def test_session_id_from_rollout_filename():
    name = Path(f"rollout-2026-05-11T12-10-32-{SAMPLE_UUID_B}.jsonl")
    assert csr._session_id_from_rollout(name) == SAMPLE_UUID_B


def test_iter_rollout_files_when_root_missing(codex_root):
    # _SESSIONS_ROOT doesn't exist yet → no crash, empty result
    assert csr._iter_rollout_files() == []
    assert csr.list_codex_sessions("/tmp") == []
    assert csr.find_newest_codex_session_id("/tmp") is None


def test_list_codex_sessions_filters_by_cwd(codex_root):
    p_a = _rollout_path(codex_root, "2026-05-11T10-00-00", SAMPLE_UUID_A)
    p_b = _rollout_path(codex_root, "2026-05-11T11-00-00", SAMPLE_UUID_B)
    _write_rollout(p_a, uuid=SAMPLE_UUID_A, cwd="/proj/alpha", user_msgs=["hi alpha"], agent_msgs=["hello"])
    _write_rollout(p_b, uuid=SAMPLE_UUID_B, cwd="/proj/beta", user_msgs=["hi beta"], agent_msgs=["sup"])

    alpha = csr.list_codex_sessions("/proj/alpha")
    beta = csr.list_codex_sessions("/proj/beta")
    none = csr.list_codex_sessions("/proj/missing")

    assert [s["codex_session_id"] for s in alpha] == [SAMPLE_UUID_A]
    assert [s["codex_session_id"] for s in beta] == [SAMPLE_UUID_B]
    assert none == []
    assert alpha[0]["title"] == "hi alpha"


def test_find_newest_codex_session_id_orders_by_mtime(codex_root):
    older = _rollout_path(codex_root, "2026-05-11T09-00-00", SAMPLE_UUID_A)
    newer = _rollout_path(codex_root, "2026-05-11T10-00-00", SAMPLE_UUID_B)
    _write_rollout(older, uuid=SAMPLE_UUID_A, cwd="/proj/x", user_msgs=["old"], agent_msgs=["a"])
    _write_rollout(newer, uuid=SAMPLE_UUID_B, cwd="/proj/x", user_msgs=["new"], agent_msgs=["b"])
    # Force newer's mtime to be more recent
    import os, time
    os.utime(older, (time.time() - 100, time.time() - 100))

    assert csr.find_newest_codex_session_id("/proj/x") == SAMPLE_UUID_B


def test_enrich_returns_title_and_prompts(codex_root):
    p = _rollout_path(codex_root, "2026-05-11T10-00-00", SAMPLE_UUID_A)
    _write_rollout(
        p, uuid=SAMPLE_UUID_A, cwd="/proj/x",
        user_msgs=["build feature X", "now refactor", "write tests"],
        agent_msgs=["ok", "done", "tests pass"],
    )

    data = csr.enrich_codex_session(SAMPLE_UUID_A, "/proj/x")
    assert data["title"] == "build feature X"
    # first + penultimate + last
    assert data["prompts"] == ["build feature X", "now refactor", "write tests"]


def test_enrich_empty_session_returns_empty_dict(codex_root):
    p = _rollout_path(codex_root, "2026-05-11T10-00-00", SAMPLE_UUID_A)
    _write_rollout(p, uuid=SAMPLE_UUID_A, cwd="/proj/x", user_msgs=[], agent_msgs=[])
    assert csr.enrich_codex_session(SAMPLE_UUID_A, "/proj/x") == {}


def test_enrich_two_prompts_includes_both(codex_root):
    p = _rollout_path(codex_root, "2026-05-11T10-00-00", SAMPLE_UUID_A)
    _write_rollout(p, uuid=SAMPLE_UUID_A, cwd="/proj/x", user_msgs=["a", "b"], agent_msgs=["x", "y"])
    data = csr.enrich_codex_session(SAMPLE_UUID_A, "/proj/x")
    assert data["prompts"] == ["a", "b"]


def test_get_conversation_yields_event_msg_turns(codex_root):
    p = _rollout_path(codex_root, "2026-05-11T10-00-00", SAMPLE_UUID_A)
    _write_rollout(
        p, uuid=SAMPLE_UUID_A, cwd="/proj/x",
        user_msgs=["hi", "more"], agent_msgs=["hello", "ok"],
        # response_item.message with role=developer should NOT show up
        extra_lines=[{
            "timestamp": "2026-05-11T00:00:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "<environment_context>...</environment_context>"}],
            },
        }],
    )

    turns = csr.get_codex_conversation(SAMPLE_UUID_A, "/proj/x")
    # 2 user + 2 agent = 4 turns, ts 1..4
    assert [t["role"] for t in turns] == ["user", "assistant", "user", "assistant"]
    assert [t["text"] for t in turns] == ["hi", "hello", "more", "ok"]
    assert [t["ts"] for t in turns] == [1.0, 2.0, 3.0, 4.0]
    # Developer bootstrap is NOT in the result
    assert not any("environment_context" in t["text"] for t in turns)


def test_get_conversation_from_ts_filter(codex_root):
    p = _rollout_path(codex_root, "2026-05-11T10-00-00", SAMPLE_UUID_A)
    _write_rollout(p, uuid=SAMPLE_UUID_A, cwd="/proj/x", user_msgs=["a", "b"], agent_msgs=["x", "y"])
    turns = csr.get_codex_conversation(SAMPLE_UUID_A, "/proj/x", from_ts=2.0)
    # Only ts > 2: turns 3 and 4
    assert [t["ts"] for t in turns] == [3.0, 4.0]


def test_search_codex_conversation_is_case_insensitive(codex_root):
    p = _rollout_path(codex_root, "2026-05-11T10-00-00", SAMPLE_UUID_A)
    _write_rollout(p, uuid=SAMPLE_UUID_A, cwd="/proj/x",
                   user_msgs=["Refactor the AUTH module"], agent_msgs=["ok"])
    assert csr.search_codex_conversation(SAMPLE_UUID_A, "/proj/x", "refactor")
    assert csr.search_codex_conversation(SAMPLE_UUID_A, "/proj/x", "AUTH")
    assert not csr.search_codex_conversation(SAMPLE_UUID_A, "/proj/x", "nope")


def test_list_all_global_groups_by_cwd_and_skips_excluded(codex_root):
    p_a = _rollout_path(codex_root, "2026-05-11T10-00-00", SAMPLE_UUID_A)
    p_b = _rollout_path(codex_root, "2026-05-11T11-00-00", SAMPLE_UUID_B)
    _write_rollout(p_a, uuid=SAMPLE_UUID_A, cwd="/proj/alpha", user_msgs=["hi"], agent_msgs=["ok"])
    _write_rollout(p_b, uuid=SAMPLE_UUID_B, cwd="/proj/alpha", user_msgs=["yo"], agent_msgs=["sup"])

    # No exclusion → both appear, grouped by cwd
    groups = csr.list_all_codex_sessions_global(set())
    assert len(groups) == 1
    assert groups[0]["dir"] == "/proj/alpha"
    assert {s["claude_session_id"] for s in groups[0]["sessions"]} == {SAMPLE_UUID_A, SAMPLE_UUID_B}

    # Exclude one → only the other appears
    groups = csr.list_all_codex_sessions_global({SAMPLE_UUID_A})
    assert {s["claude_session_id"] for s in groups[0]["sessions"]} == {SAMPLE_UUID_B}


# ── CodexAdapter wiring ─────────────────────────────────────────────────────

def test_codex_adapter_kind():
    assert CodexAdapter().kind == "codex"


def test_codex_adapter_does_not_need_proxy_tap():
    assert CodexAdapter().needs_proxy_tap() is False


def test_codex_adapter_waiting_state_is_none_in_phase2():
    state = CodexAdapter().get_waiting_state(
        agent_session_id=SAMPLE_UUID_A, agent_pid=12345, cwd="/proj/x",
    )
    assert state is None


def test_codex_adapter_build_command_fresh():
    cmd = CodexAdapter().build_command(
        cwd="/tmp", env={}, model=None, resume_session_id=None,
        inner_id=None, claude_bin="claude", cursor_bin="agent", claude_shell="",
    )
    # Bare 'codex' (or absolute path to it)
    assert cmd.endswith("codex") or "/codex" in cmd


def test_codex_adapter_build_command_resume():
    cmd = CodexAdapter().build_command(
        cwd="/tmp", env={}, model=None, resume_session_id=SAMPLE_UUID_A,
        inner_id=None, claude_bin="claude", cursor_bin="agent", claude_shell="",
    )
    assert "resume" in cmd
    assert SAMPLE_UUID_A in cmd


def test_codex_adapter_build_command_with_model():
    cmd = CodexAdapter().build_command(
        cwd="/tmp", env={}, model="gpt-5", resume_session_id=None,
        inner_id=None, claude_bin="claude", cursor_bin="agent", claude_shell="",
    )
    # Should pass `-c model="gpt-5"`
    assert "-c" in cmd
    assert 'model="gpt-5"' in cmd


def test_codex_adapter_uses_reader_for_enrich(codex_root):
    p = _rollout_path(codex_root, "2026-05-11T10-00-00", SAMPLE_UUID_A)
    _write_rollout(p, uuid=SAMPLE_UUID_A, cwd="/proj/x", user_msgs=["build X"], agent_msgs=["done"])
    data = CodexAdapter().enrich(SAMPLE_UUID_A, "/proj/x")
    assert data["title"] == "build X"


def test_codex_adapter_get_conversation_via_reader(codex_root):
    p = _rollout_path(codex_root, "2026-05-11T10-00-00", SAMPLE_UUID_A)
    _write_rollout(p, uuid=SAMPLE_UUID_A, cwd="/proj/x", user_msgs=["hi"], agent_msgs=["yo"])
    turns = CodexAdapter().get_conversation(SAMPLE_UUID_A, "/proj/x")
    assert len(turns) == 2
    assert turns[0] == {"role": "user", "text": "hi", "streaming": False, "ts": 1.0}
