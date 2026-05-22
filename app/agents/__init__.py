"""Agent adapter registry."""
from __future__ import annotations

from app.agents.base import AgentAdapter, AgentKind
from app.agents.claude import get as _claude
from app.agents.cursor import get as _cursor


def get_adapter(tool: str) -> AgentAdapter:
    """Return the adapter for the given tool name.

    Falls back to the Claude adapter for unknown values, matching the
    historical default in SessionMetadata.tool.
    """
    if tool == "cursor":
        return _cursor()
    # claude (or anything else for now — Codex slot will hook in via #170+1)
    return _claude()


def list_supported_tools() -> list[AgentKind]:
    return ["claude", "cursor"]


__all__ = ["AgentAdapter", "AgentKind", "get_adapter", "list_supported_tools"]
