"""Codex CLI adapter.

Phase 2 scope: spawn the interactive `codex` TUI in tmux (like Claude) and
surface session history from rollout JSONL (like Claude/Cursor readers). The
app-server JSON-RPC integration (live AUQ/plan/approval state) is Phase 3.
"""
from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Optional

from app.agents.base import AgentAdapter, AgentKind, EnrichResult, WaitingState
from app.services import codex_session_reader


# Codex CLI 0.130 supports these via `-c model=...`. The CLI doesn't ship a
# discoverable `--list-models`, and the supported set depends on the configured
# `model_provider`. Keep this list short and stable; users can override via
# ~/.codex/config.toml.
CODEX_MODELS = [
    {"id": "gpt-5", "name": "GPT-5"},
    {"id": "gpt-5-codex", "name": "GPT-5 Codex"},
    {"id": "o3", "name": "OpenAI o3"},
    {"id": "o4-mini", "name": "OpenAI o4-mini"},
]


class CodexAdapter:
    kind: AgentKind = "codex"

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
        # Codex binary is configured separately; for Phase 2 we look it up from
        # PATH inside build_command (TmuxService doesn't know about it yet).
        # A follow-up task will add `codex_bin` to TmuxService.__init__.
        import shutil
        codex_bin = shutil.which("codex") or "codex"
        bin_q = shlex.quote(codex_bin)

        # --no-alt-screen runs Codex's TUI on the main screen instead of the
        # alternate buffer, so xterm.js / tmux can show conversation history in
        # their normal scrollback. Codex's docs list this flag specifically for
        # terminal multiplexers (Zellij, tmux) — which is exactly how we host
        # it. Without it the chat history is unreachable: Codex's TUI binds
        # mouse wheel only to its input box, so scrolling never reveals past
        # turns.
        parts = [bin_q, "--no-alt-screen"]
        if model:
            # `-c model="gpt-5"` (CLI parses value as TOML — quote it).
            parts.append("-c")
            parts.append(shlex.quote(f'model="{model}"'))

        if resume_session_id:
            parts.append("resume")
            parts.append(shlex.quote(resume_session_id))
        # Fresh sessions: bare `codex` opens the interactive picker/prompt.

        return " ".join(parts)

    def needs_proxy_tap(self) -> bool:
        return False  # Codex talks to OpenAI/configured provider directly.

    def find_newest_session_id(self, cwd: str) -> Optional[str]:
        return codex_session_reader.find_newest_codex_session_id(cwd)

    def enrich(self, agent_session_id: str, cwd: str) -> EnrichResult:
        data = codex_session_reader.enrich_codex_session(agent_session_id, cwd)
        return EnrichResult(
            title=data.get("title"),
            prompts=data.get("prompts", []),
            last_user_input_at=data.get("last_user_input_at"),
            search_text=None,
        )

    def get_conversation(
        self, agent_session_id: str, cwd: str, from_ts: float = 0.0
    ) -> list[dict]:
        return codex_session_reader.get_codex_conversation(
            agent_session_id, cwd, from_ts=from_ts
        )

    def search_conversation(self, agent_session_id: str, cwd: str, query: str) -> bool:
        return codex_session_reader.search_codex_conversation(agent_session_id, cwd, query)

    def list_external_sessions(self, cwd_filter: Optional[str] = None) -> list[dict]:
        if cwd_filter:
            return codex_session_reader.list_codex_sessions(cwd_filter)
        return codex_session_reader.list_all_codex_sessions_global(set())

    def get_jsonl_path(self, agent_session_id: str, cwd: str) -> Optional[Path]:
        return codex_session_reader._find_rollout(agent_session_id, cwd)

    def list_local_sessions(self, cwd: str) -> list[dict]:
        raw = codex_session_reader.list_codex_sessions(cwd)
        return [
            {
                "agent_session_id": s.get("codex_session_id") or s.get("session_id"),
                "title": s.get("title"),
                "mtime": s.get("mtime"),
            }
            for s in raw
        ]

    def get_waiting_state(
        self,
        *,
        agent_session_id: Optional[str],
        agent_pid: Optional[int],
        cwd: str,
    ) -> Optional[WaitingState]:
        # Phase 3: derive from app-server pending requests table. For now None.
        return None

    def list_models(self) -> list[dict[str, Any]]:
        return CODEX_MODELS


_INSTANCE: CodexAdapter | None = None


def get() -> CodexAdapter:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = CodexAdapter()
    return _INSTANCE
