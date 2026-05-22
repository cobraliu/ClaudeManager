"""Agent adapter registry."""
from __future__ import annotations

from app.agents.base import AgentAdapter, AgentKind
from app.agents.claude import get as _claude
from app.agents.codex import get as _codex
from app.agents.cursor import get as _cursor


def get_adapter(tool: str) -> AgentAdapter:
    """Return the adapter for the given tool name.

    Falls back to the Claude adapter for unknown values, matching the
    historical default in SessionMetadata.tool.
    """
    if tool == "cursor":
        return _cursor()
    if tool == "codex":
        return _codex()
    return _claude()


def list_supported_tools() -> list[AgentKind]:
    return ["claude", "cursor", "codex"]


__all__ = ["AgentAdapter", "AgentKind", "get_adapter", "list_supported_tools"]
