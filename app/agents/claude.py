"""Claude Code adapter.

Wraps existing `claude_session_reader` / `claude_pid` / `tmux_service` logic
in the `AgentAdapter` Protocol. No behavior changes vs the previous inline
branches in api/sessions.py and tmux_service.py — this is purely an extraction.
"""
from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Optional

from app.agents.base import AgentAdapter, AgentKind, EnrichResult, WaitingState
from app.services import claude_pid, claude_session_reader


CLAUDE_MODELS = [
    {"id": "claude-opus-4-7", "name": "Claude Opus 4.7"},
    {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
    {"id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5"},
]


class ClaudeAdapter:
    kind: AgentKind = "claude"

    def build_command(
        self,
        *,
        cwd: str,
        env: dict[str, str],
        model: Optional[str],
        resume_session_id: Optional[str],
        inner_id: Optional[str],
        claude_bin: str,
        cursor_bin: str,
        claude_shell: str,
    ) -> str:
        bin_q = shlex.quote(claude_bin)
        if resume_session_id:
            cmd = f"{bin_q} --dangerously-skip-permissions --resume {shlex.quote(resume_session_id)}"
        elif model:
            cmd = f"{bin_q} --dangerously-skip-permissions --model {shlex.quote(model)}"
        else:
            cmd = f"{bin_q} --dangerously-skip-permissions"

        if inner_id:
            pid_file = f"/tmp/claude-inner-{inner_id}.pid"
            cmd = f"sh -c 'echo $$ > {pid_file} && exec {cmd}'"

        if claude_shell:
            cmd = f"{claude_shell} -c {shlex.quote(cmd)}"
        return cmd

    def needs_proxy_tap(self) -> bool:
        return True

    def find_newest_session_id(self, cwd: str) -> Optional[str]:
        return claude_session_reader.find_newest_claude_session_id(cwd)

    def enrich(self, agent_session_id: str, cwd: str) -> EnrichResult:
        data = claude_session_reader.enrich_session(agent_session_id, cwd)
        return EnrichResult(
            title=data.get("title"),
            prompts=data.get("prompts", []),
            last_user_input_at=data.get("last_user_input_at"),
            search_text=data.get("search_text"),
        )

    def get_conversation(
        self, agent_session_id: str, cwd: str, from_ts: float = 0.0
    ) -> list[dict]:
        return claude_session_reader.get_conversation(agent_session_id, cwd, from_ts=from_ts)

    def search_conversation(self, agent_session_id: str, cwd: str, query: str) -> bool:
        return claude_session_reader.search_conversation(agent_session_id, cwd, query)

    def list_external_sessions(self, cwd_filter: Optional[str] = None) -> list[dict]:
        if cwd_filter:
            return claude_session_reader.list_project_session_ids(cwd_filter)
        return claude_session_reader.list_all_claude_sessions_global()

    def get_jsonl_path(self, agent_session_id: str, cwd: str) -> Optional[Path]:
        return claude_session_reader._find_session_jsonl(agent_session_id, cwd)

    def list_local_sessions(self, cwd: str) -> list[dict]:
        return claude_session_reader.list_project_session_ids(cwd)

    def get_waiting_state(
        self,
        *,
        agent_session_id: Optional[str],
        agent_pid: Optional[int],
        cwd: str,
        session_id: Optional[str] = None,
    ) -> Optional[WaitingState]:
        result = claude_pid.get_pid_waiting_state(agent_pid)
        if not result:
            return None
        waiting_for, hint_type = result
        hint = claude_pid.format_tui_hint(waiting_for, hint_type)
        return WaitingState(kind=hint_type, hint=hint, raw={"waitingFor": waiting_for})  # type: ignore[typeddict-item]

    def list_models(self) -> list[dict[str, Any]]:
        return CLAUDE_MODELS


_INSTANCE: ClaudeAdapter | None = None


def get() -> ClaudeAdapter:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = ClaudeAdapter()
    return _INSTANCE
