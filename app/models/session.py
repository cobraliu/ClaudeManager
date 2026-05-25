from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SessionStatus(str, Enum):
    CREATING = "creating"
    RUNNING = "running"
    DETACHED = "detached"
    ARCHIVED = "archived"
    TERMINATED = "terminated"


class SessionCreateRequest(BaseModel):
    project: str = Field(min_length=1, max_length=80)
    cwd: str | None = Field(default=None, max_length=500)
    env: dict[str, str] = Field(default_factory=dict)
    model: str | None = Field(default=None, max_length=120)
    resume_session_id: str | None = Field(default=None, max_length=200)
    git_repo_url: str | None = Field(default=None, max_length=500)
    tool: Literal["claude", "cursor", "codex"] = "claude"


class SessionMetadata(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    owner_id: str
    name: str
    project: str
    cwd: str
    env: dict[str, str] = Field(default_factory=dict)
    model: str | None = None
    tool: str = "claude"
    status: SessionStatus = SessionStatus.CREATING
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    attached_clients: int = 0
    last_output_offset: int = 0
    last_activity_at: datetime | None = None
    last_turn_at: datetime | None = None
    ws_token: str | None = None
    tmux_session_name: str
    resume_session_id: str | None = None
    agent_session_id: str | None = None
    claude_proc_pid: int | None = None
    git_auto_commit: bool = False
    git_commit_msg_count: int = 0
    git_repo_url: str | None = None


class ScheduledTask(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    owner_id: str
    command: str
    run_at: datetime
    status: str = "pending"  # pending | sent | cancelled | failed
    created_at: datetime = Field(default_factory=utc_now)
    sent_at: datetime | None = None
    error: str | None = None
    # When set, the task re-schedules itself: after a successful send a new
    # pending row is inserted with run_at = now + loop_seconds. None means
    # the task fires once.
    loop_seconds: int | None = None


class TaskCreateRequest(BaseModel):
    command: str = Field(min_length=1, max_length=2000)
    delay_seconds: int = Field(ge=1, le=604800)  # max 7 days
    # Optional: when set, the task repeats every loop_seconds after the
    # initial fire (capped at 7 days, same as delay_seconds).
    loop_seconds: int | None = Field(default=None, ge=1, le=604800)


class TaskView(BaseModel):
    id: str
    command: str
    run_at: str   # ISO string
    status: str
    created_at: str
    loop_seconds: int | None = None


class SessionView(SessionMetadata):
    """SessionMetadata + display fields from Claude CLI data."""
    claude_title: str | None = None
    prompts: list[str] = Field(default_factory=list)
    last_user_input_at: str | None = None
    has_new_output: bool = False
    is_streaming: bool = False
    scheduled_tasks: list[TaskView] = Field(default_factory=list)


class SessionListResponse(BaseModel):
    items: list[SessionView]
    total: int


class SessionStatusView(BaseModel):
    """Lightweight status-only view for high-frequency polling."""
    id: str
    status: SessionStatus
    attached_clients: int
    has_new_output: bool
    is_streaming: bool
    is_compacting: bool = False
    compacting_progress: str | None = None  # e.g. "45%" parsed from TUI when available
    scheduled_tasks: list[TaskView] = Field(default_factory=list)
    tui_hint: str | None = None
    tui_auq_data: dict | None = None
    tui_approve_data: dict | None = None


class SessionStatusListResponse(BaseModel):
    items: list[SessionStatusView]
    total: int


class AttachRequest(BaseModel):
    client_name: str | None = Field(default=None, max_length=120)


class AttachResponse(BaseModel):
    session_id: str
    ws_token: str
    ws_url: str
    status: SessionStatus


class OutputChunk(BaseModel):
    session_id: str
    offset: int
    data: str
    has_more: bool = False


class WsInputMessage(BaseModel):
    type: Literal["input"]
    data: str


class WsResizeMessage(BaseModel):
    type: Literal["resize"]
    cols: int = Field(ge=20, le=500)
    rows: int = Field(ge=5, le=200)


class WsPingMessage(BaseModel):
    type: Literal["ping"]
    ts: int | None = None


class WsClientMessage(BaseModel):
    type: Literal["input", "resize", "ping", "search-init", "search-next", "scroll", "exit-copy-mode", "refresh"]
    data: str | None = None
    cols: int | None = None
    rows: int | None = None
    ts: int | None = None
    query: str | None = None
    delta: int | None = None  # scroll: negative=up lines, positive=down lines
    pane: str | None = None  # when set, route input to this specific pane via send-keys instead of PTY


class WsOutputEvent(BaseModel):
    type: Literal["output"] = "output"
    payload: OutputChunk


class WsStateEvent(BaseModel):
    type: Literal["state"] = "state"
    payload: dict[str, Any]


class WsPongEvent(BaseModel):
    type: Literal["pong"] = "pong"
    payload: dict[str, int | None]
