"""Agent adapter abstraction.

Each coding-CLI integration (Claude Code, Cursor's `agent`, Codex) implements
`AgentAdapter`. The rest of the codebase routes through `get_adapter(tool)`
instead of branching on `session.tool == "claude"` / `"cursor"` / `"codex"`.

Method semantics:
- `build_command`: returns (argv_string, extra_env). The tmux service prefixes
  this with `env K=V ...` so the spawned process gets the env we specify
  regardless of tmux server's stale globals.
- `needs_proxy_tap`: Claude=True (ANTHROPIC_BASE_URL injection), Cursor=False,
  Codex=False (Codex talks to OpenAI/OAI-compat directly or via app-server).
- `enrich`: title + prompts + last_user_input_at for the sessions list.
- `get_conversation`: full transcript turns (used by ConversationPane).
- `get_waiting_state`: live AUQ/approval state. Cursor returns None; Codex
  reads from its app-server `pending_requests` table.
- `list_models`: rendered in the create-session dialog.
- `find_newest_session_id`: rewind/discovery helper used by claudemanager.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional, Protocol, TypedDict, runtime_checkable

AgentKind = Literal["claude", "cursor", "codex"]


class WaitingState(TypedDict, total=False):
    kind: Literal["auq", "approve", "compacting"]
    hint: str
    raw: dict  # AUQ questions, approve tool_input, etc.


class EnrichResult(TypedDict, total=False):
    title: Optional[str]
    prompts: list[str]
    last_user_input_at: Optional[str]
    search_text: Optional[str]


@runtime_checkable
class AgentAdapter(Protocol):
    kind: AgentKind

    # ── Launch ──────────────────────────────────────────────────────────────
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
        """Return the full shell command string to spawn the agent.

        Implementations decide whether `inner_id` should produce a PID-file
        wrapper (Claude) or be ignored (Cursor/Codex). The tmux service prepends
        the `env K=V ...` prefix; do not include env here.
        """

    def needs_proxy_tap(self) -> bool: ...

    # ── Discovery / resolution ──────────────────────────────────────────────
    def find_newest_session_id(self, cwd: str) -> Optional[str]:
        """Newest agent session id for the given cwd (or None)."""

    # ── Conversation reading ────────────────────────────────────────────────
    def enrich(self, agent_session_id: str, cwd: str) -> EnrichResult: ...

    def get_conversation(
        self, agent_session_id: str, cwd: str, from_ts: float = 0.0
    ) -> list[dict]: ...

    def search_conversation(self, agent_session_id: str, cwd: str, query: str) -> bool: ...

    def list_external_sessions(self, cwd_filter: Optional[str] = None) -> list[dict]: ...

    def get_jsonl_path(self, agent_session_id: str, cwd: str) -> Optional[Path]:
        """Absolute path to the session's transcript JSONL, or None if not found.

        Used by raw-JSONL viewer endpoints. Each adapter resolves to its own
        on-disk format (Claude: ~/.claude/projects/, Cursor: ~/.cursor/...,
        Codex: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl).
        """

    def list_local_sessions(self, cwd: str) -> list[dict]:
        """Sessions for this cwd, shaped as {agent_session_id, title, mtime}.

        Used by the session-switcher dropdown. Distinct from
        list_external_sessions (which groups globally for the browse panel).
        """

    # ── Live state polling ──────────────────────────────────────────────────
    def get_waiting_state(
        self,
        *,
        agent_session_id: Optional[str],
        agent_pid: Optional[int],
        cwd: str,
    ) -> Optional[WaitingState]:
        """None for adapters that have no waiting-state mechanism (Cursor).

        Claude reads PID file + hook JSONL; Codex queries its app-server's
        pending requests table.
        """

    # ── Models ──────────────────────────────────────────────────────────────
    def list_models(self) -> list[dict[str, Any]]: ...
