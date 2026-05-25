"""Cursor `agent` CLI adapter."""
from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from app.agents.base import AgentAdapter, AgentKind, EnrichResult, WaitingState
from app.services import cursor_session_reader


_cursor_models_cache: list[dict] | None = None


def _fetch_cursor_models() -> list[dict]:
    global _cursor_models_cache
    if _cursor_models_cache is not None:
        return _cursor_models_cache
    agent_bin = shutil.which("agent")
    if not agent_bin:
        return []
    try:
        out = subprocess.check_output([agent_bin, "--list-models"], text=True, timeout=10)
    except Exception:
        return []
    models = []
    for line in out.splitlines():
        line = line.strip()
        if " - " not in line:
            continue
        mid, _, name = line.partition(" - ")
        mid = mid.strip()
        name = name.strip()
        if mid and name:
            models.append({"id": mid, "name": name})
    _cursor_models_cache = models
    return models


class CursorAdapter:
    kind: AgentKind = "cursor"

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
        bin_q = shlex.quote(cursor_bin)
        if resume_session_id:
            return f"{bin_q} --yolo --resume {shlex.quote(resume_session_id)}"
        return f"{bin_q} --yolo"

    def needs_proxy_tap(self) -> bool:
        return False

    def find_newest_session_id(self, cwd: str) -> Optional[str]:
        return cursor_session_reader.find_newest_cursor_session_id(cwd)

    def enrich(self, agent_session_id: str, cwd: str) -> EnrichResult:
        data = cursor_session_reader.enrich_cursor_session(agent_session_id, cwd)
        return EnrichResult(
            title=data.get("title"),
            prompts=data.get("prompts", []),
            last_user_input_at=data.get("last_user_input_at"),
            search_text=data.get("search_text"),
        )

    def get_conversation(
        self, agent_session_id: str, cwd: str, from_ts: float = 0.0
    ) -> list[dict]:
        return cursor_session_reader.get_cursor_conversation(
            agent_session_id, cwd, from_ts=from_ts
        )

    def search_conversation(self, agent_session_id: str, cwd: str, query: str) -> bool:
        # Cursor reader has no dedicated search helper; conservative: always False.
        # The metadata-side search in api/sessions.py still catches matches on
        # project / cwd / chat-id / model / owner.
        return False

    def list_external_sessions(self, cwd_filter: Optional[str] = None) -> list[dict]:
        if cwd_filter:
            return cursor_session_reader.list_cursor_sessions(cwd_filter)
        return cursor_session_reader.list_all_cursor_sessions_global(set())

    def get_jsonl_path(self, agent_session_id: str, cwd: str) -> Optional[Path]:
        return cursor_session_reader._find_jsonl(agent_session_id, cwd)

    def list_local_sessions(self, cwd: str) -> list[dict]:
        raw = cursor_session_reader.list_cursor_sessions(cwd)
        return [
            {
                "agent_session_id": s["cursor_session_id"],
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
        return None  # Cursor has no waiting-state mechanism we can observe.

    def list_models(self) -> list[dict[str, Any]]:
        return _fetch_cursor_models()


_INSTANCE: CursorAdapter | None = None


def get() -> CursorAdapter:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = CursorAdapter()
    return _INSTANCE
