from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Iterable

from app.models.session import SessionMetadata, SessionStatus, ScheduledTask, ShareRecord


_ALLOWED_TRANSITIONS: dict[SessionStatus, set[SessionStatus]] = {
    SessionStatus.CREATING: {SessionStatus.RUNNING, SessionStatus.TERMINATED},
    SessionStatus.RUNNING: {
        SessionStatus.DETACHED,
        SessionStatus.ARCHIVED,
        SessionStatus.TERMINATED,
    },
    SessionStatus.DETACHED: {
        SessionStatus.RUNNING,
        SessionStatus.ARCHIVED,
        SessionStatus.TERMINATED,
    },
    SessionStatus.ARCHIVED: {SessionStatus.RUNNING, SessionStatus.TERMINATED},
    SessionStatus.TERMINATED: {SessionStatus.RUNNING},  # resume
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS configs (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    owner_id    TEXT NOT NULL,
    command     TEXT NOT NULL,
    run_at      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL,
    sent_at     TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_session ON scheduled_tasks(session_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status_run ON scheduled_tasks(status, run_at);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    name TEXT NOT NULL,
    project TEXT NOT NULL,
    cwd TEXT NOT NULL,
    env TEXT NOT NULL DEFAULT '{}',
    model TEXT,
    status TEXT NOT NULL DEFAULT 'creating',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    attached_clients INTEGER NOT NULL DEFAULT 0,
    last_output_offset INTEGER NOT NULL DEFAULT 0,
    last_activity_at TEXT,
    ws_token TEXT,
    tmux_session_name TEXT NOT NULL,
    resume_session_id TEXT,
    agent_session_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions(owner_id);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE TABLE IF NOT EXISTS session_views (
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    last_viewed_at TEXT NOT NULL,
    PRIMARY KEY (session_id, user_id)
);
CREATE TABLE IF NOT EXISTS prompt_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    text       TEXT NOT NULL,
    sent_at    REAL NOT NULL,
    pane       TEXT
);
CREATE INDEX IF NOT EXISTS idx_prompt_history_session ON prompt_history(session_id, sent_at DESC);
CREATE TABLE IF NOT EXISTS prompt_history_backfill (
    session_id   TEXT PRIMARY KEY,
    completed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS shares (
    hash             TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    owner_id         TEXT NOT NULL,
    share_type       TEXT NOT NULL,
    cutoff_ts        REAL,
    cutoff_msg_uuid  TEXT,
    cutoff_msg_text  TEXT,
    created_at       REAL NOT NULL,
    expires_at       REAL NOT NULL,
    default_theme    TEXT NOT NULL DEFAULT 'light'
);
CREATE INDEX IF NOT EXISTS idx_shares_session ON shares(session_id, owner_id);
CREATE INDEX IF NOT EXISTS idx_shares_expires ON shares(expires_at);
"""


class SessionStore:
    def __init__(self, db_path: Path | str = ":memory:") -> None:
        self._db_path = str(db_path)
        self._lock = Lock()
        self._tui_hints: dict[str, str] = {}  # in-memory only; clears on restart
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        # Auto-migrate: rename claude_session_id → agent_session_id (column was
        # historically tied to Claude; now reused by Cursor/Codex). Try first so
        # the subsequent ADD COLUMN list sees the renamed column.
        try:
            self._conn.execute(
                "ALTER TABLE sessions RENAME COLUMN claude_session_id TO agent_session_id"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            pass  # already renamed, or column didn't exist
        # Auto-migrate: add columns that may not exist yet
        for col, coltype in [
            ("agent_session_id", "TEXT"),
            ("claude_proc_pid", "INTEGER"),
            ("last_activity_at", "TEXT"),
            ("git_auto_commit", "INTEGER NOT NULL DEFAULT 0"),
            ("git_commit_msg_count", "INTEGER NOT NULL DEFAULT 0"),
            ("git_repo_url", "TEXT"),
            ("last_turn_at", "TEXT"),
            ("tool", "TEXT NOT NULL DEFAULT 'claude'"),
            ("codex_transport", "TEXT NOT NULL DEFAULT 'tui'"),
            ("codex_appserver_pid", "INTEGER"),
            ("codex_appserver_port", "INTEGER"),
        ]:
            try:
                self._conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {coltype}")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists
        # Auto-migrate scheduled_tasks (loop_seconds added later for repeating tasks)
        try:
            self._conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN loop_seconds INTEGER")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass
        # Auto-migrate shares (default_theme added later; backfill existing rows to light)
        try:
            self._conn.execute(
                "ALTER TABLE shares ADD COLUMN default_theme TEXT NOT NULL DEFAULT 'light'"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            pass
        self._conn.commit()

    def _row_to_session(self, row: sqlite3.Row) -> SessionMetadata:
        last_activity_raw = row["last_activity_at"]
        last_turn_raw = row["last_turn_at"] if "last_turn_at" in row.keys() else None
        return SessionMetadata(
            id=row["id"],
            owner_id=row["owner_id"],
            name=row["name"],
            project=row["project"],
            cwd=row["cwd"],
            env=json.loads(row["env"]),
            model=row["model"],
            status=SessionStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            attached_clients=row["attached_clients"],
            last_output_offset=row["last_output_offset"],
            last_activity_at=datetime.fromisoformat(last_activity_raw) if last_activity_raw else None,
            last_turn_at=datetime.fromisoformat(last_turn_raw) if last_turn_raw else None,
            ws_token=row["ws_token"],
            tmux_session_name=row["tmux_session_name"],
            resume_session_id=row["resume_session_id"],
            agent_session_id=row["agent_session_id"],
            claude_proc_pid=row["claude_proc_pid"] if "claude_proc_pid" in row.keys() else None,
            git_auto_commit=bool(row["git_auto_commit"]),
            git_commit_msg_count=int(row["git_commit_msg_count"]),
            git_repo_url=row["git_repo_url"],
            tool=row["tool"] if "tool" in row.keys() else "claude",
            codex_transport=(row["codex_transport"] if "codex_transport" in row.keys() else "tui") or "tui",
            codex_appserver_pid=row["codex_appserver_pid"] if "codex_appserver_pid" in row.keys() else None,
            codex_appserver_port=row["codex_appserver_port"] if "codex_appserver_port" in row.keys() else None,
        )

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def create(self, session: SessionMetadata) -> SessionMetadata:
        with self._lock:
            self._conn.execute(
                """INSERT INTO sessions
                   (id, owner_id, name, project, cwd, env, model, status,
                    created_at, updated_at, attached_clients, last_output_offset,
                    last_activity_at, ws_token, tmux_session_name, resume_session_id,
                    agent_session_id, git_repo_url, tool, codex_transport,
                    codex_appserver_pid, codex_appserver_port)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    session.id,
                    session.owner_id,
                    session.name,
                    session.project,
                    session.cwd,
                    json.dumps(session.env),
                    session.model,
                    session.status.value,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                    session.attached_clients,
                    session.last_output_offset,
                    session.last_activity_at.isoformat() if session.last_activity_at else None,
                    session.ws_token,
                    session.tmux_session_name,
                    session.resume_session_id,
                    session.agent_session_id,
                    session.git_repo_url,
                    session.tool,
                    session.codex_transport,
                    session.codex_appserver_pid,
                    session.codex_appserver_port,
                ),
            )
            self._conn.commit()
            return session

    def get(self, session_id: str) -> SessionMetadata | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return self._row_to_session(row) if row else None

    def list_for_owner(self, owner_id: str) -> list[SessionMetadata]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM sessions WHERE owner_id = ?
                   ORDER BY
                     CASE status
                       WHEN 'running' THEN 0
                       WHEN 'creating' THEN 1
                       WHEN 'detached' THEN 2
                       WHEN 'archived' THEN 3
                       WHEN 'terminated' THEN 4
                     END,
                     updated_at DESC""",
                (owner_id,),
            ).fetchall()
            return [self._row_to_session(r) for r in rows]

    def transition(self, session_id: str, new_state: SessionStatus) -> SessionMetadata:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            current = SessionStatus(row["status"])
            if current == new_state:
                return self._row_to_session(row)
            if new_state not in _ALLOWED_TRANSITIONS[current]:
                raise ValueError(
                    f"invalid transition: {current.value} -> {new_state.value}"
                )
            now = self._now()
            extra = ", ws_token = NULL" if new_state == SessionStatus.TERMINATED else ""
            self._conn.execute(
                f"UPDATE sessions SET status = ?, updated_at = ?{extra} WHERE id = ?",
                (new_state.value, now, session_id),
            )
            self._conn.commit()
            return self._row_to_session(
                self._conn.execute(
                    "SELECT * FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
            )

    def update_attached_clients(self, session_id: str, delta: int) -> SessionMetadata:
        with self._lock:
            now = self._now()
            self._conn.execute(
                """UPDATE sessions
                   SET attached_clients = MAX(0, attached_clients + ?),
                       updated_at = ?
                   WHERE id = ?""",
                (delta, now, session_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return self._row_to_session(row) if row else None  # type: ignore[return-value]

    def update_output_offset(self, session_id: str, offset: int) -> SessionMetadata:
        with self._lock:
            now = self._now()
            self._conn.execute(
                """UPDATE sessions
                   SET last_output_offset = MAX(last_output_offset, ?),
                       updated_at = ?
                   WHERE id = ?""",
                (offset, now, session_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return self._row_to_session(row) if row else None  # type: ignore[return-value]

    def issue_ws_token(self, session_id: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT ws_token FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row and row["ws_token"]:
                return row["ws_token"]
            token = secrets.token_urlsafe(24)
            now = self._now()
            self._conn.execute(
                "UPDATE sessions SET ws_token = ?, updated_at = ? WHERE id = ?",
                (token, now, session_id),
            )
            self._conn.commit()
        return token

    def update_tmux_session_name(self, session_id: str, tmux_session_name: str) -> None:
        with self._lock:
            now = self._now()
            self._conn.execute(
                "UPDATE sessions SET tmux_session_name = ?, updated_at = ? WHERE id = ?",
                (tmux_session_name, now, session_id),
            )
            self._conn.commit()

    def update_claude_proc_pid(self, session_id: str, pid: int | None) -> None:
        with self._lock:
            now = self._now()
            self._conn.execute(
                "UPDATE sessions SET claude_proc_pid = ?, updated_at = ? WHERE id = ?",
                (pid, now, session_id),
            )
            self._conn.commit()

    def update_agent_session_id(self, session_id: str, agent_session_id: str) -> None:
        with self._lock:
            now = self._now()
            self._conn.execute(
                "UPDATE sessions SET agent_session_id = ?, updated_at = ? WHERE id = ?",
                (agent_session_id, now, session_id),
            )
            self._conn.commit()

    def get_all_agent_session_ids(self, exclude_session_id: str | None = None) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT agent_session_id FROM sessions WHERE agent_session_id IS NOT NULL AND id != ?",
                (exclude_session_id or "",),
            ).fetchall()
            return {r[0] for r in rows}

    def update_codex_appserver_endpoint(
        self, session_id: str, pid: int | None, port: int | None
    ) -> None:
        """Persist the detached app-server's PID + listen port so we can
        reconnect after a backend restart."""
        with self._lock:
            now = self._now()
            self._conn.execute(
                "UPDATE sessions SET codex_appserver_pid = ?, codex_appserver_port = ?, "
                "updated_at = ? WHERE id = ?",
                (pid, port, now, session_id),
            )
            self._conn.commit()

    def clear_agent_session_id(self, session_id: str) -> None:
        with self._lock:
            now = self._now()
            self._conn.execute(
                "UPDATE sessions SET agent_session_id = NULL, updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            self._conn.commit()

    def update_project(self, session_id: str, project: str) -> None:
        with self._lock:
            now = self._now()
            self._conn.execute(
                "UPDATE sessions SET project = ?, updated_at = ? WHERE id = ?",
                (project, now, session_id),
            )
            self._conn.commit()

    def update_model(self, session_id: str, model: str | None) -> None:
        with self._lock:
            now = self._now()
            self._conn.execute(
                "UPDATE sessions SET model = ?, updated_at = ? WHERE id = ?",
                (model, now, session_id),
            )
            self._conn.commit()

    def force_status(self, session_id: str, new_state: SessionStatus) -> None:
        """Set status without transition validation (for startup sync)."""
        with self._lock:
            now = self._now()
            extra = ", ws_token = NULL" if new_state == SessionStatus.TERMINATED else ""
            self._conn.execute(
                f"UPDATE sessions SET status = ?, updated_at = ?{extra} WHERE id = ?",
                (new_state.value, now, session_id),
            )
            self._conn.commit()

    def reset_attached_clients(self, session_id: str) -> None:
        with self._lock:
            now = self._now()
            self._conn.execute(
                "UPDATE sessions SET attached_clients = 0, updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            self._conn.commit()

    def sync_output_offset(self, session_id: str, offset: int) -> None:
        """Set last_output_offset to the current tmux history size WITHOUT touching
        last_activity_at.  Called at WS connect/disconnect to keep the baseline
        accurate so the watchdog can reliably detect genuinely new output."""
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET last_output_offset = ? WHERE id = ?",
                (offset, session_id),
            )
            self._conn.commit()

    def update_activity(self, session_id: str) -> None:
        """Record that this session produced output (for new-output notification)."""
        with self._lock:
            now = self._now()
            self._conn.execute(
                "UPDATE sessions SET last_activity_at = ? WHERE id = ?",
                (now, session_id),
            )
            self._conn.commit()

    def update_last_turn_at(self, session_id: str, turn_ts: float) -> None:
        """Record the timestamp of the latest completed Claude turn (from turn_duration)."""
        from datetime import datetime, timezone
        iso = datetime.fromtimestamp(turn_ts, tz=timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET last_turn_at = ? WHERE id = ? AND (last_turn_at IS NULL OR last_turn_at < ?)",
                (iso, session_id, iso),
            )
            self._conn.commit()

    def update_activity_if_offset_changed(self, session_id: str, new_offset: int) -> bool:
        """Update last_activity_at only if new_offset > stored last_output_offset.
        If new_offset < stored (window resize shrank scrollback), reset baseline only.
        Returns True if new activity was recorded."""
        with self._lock:
            row = self._conn.execute(
                "SELECT last_output_offset FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return False
            stored = row["last_output_offset"]
            if new_offset < stored:
                # Terminal was resized larger — scrollback shrank, reset baseline
                self._conn.execute(
                    "UPDATE sessions SET last_output_offset = ? WHERE id = ?",
                    (new_offset, session_id),
                )
                self._conn.commit()
                return False
            if new_offset == stored:
                return False
            # new_offset > stored → genuinely new output
            now = self._now()
            self._conn.execute(
                "UPDATE sessions SET last_activity_at = ?, last_output_offset = ? WHERE id = ?",
                (now, new_offset, session_id),
            )
            self._conn.commit()
            return True

    def mark_viewed(self, session_id: str, user_id: str) -> None:
        """Record that user_id has viewed this session (clears new-output flag)."""
        with self._lock:
            now = self._now()
            self._conn.execute(
                """INSERT INTO session_views (session_id, user_id, last_viewed_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(session_id, user_id) DO UPDATE SET last_viewed_at = excluded.last_viewed_at""",
                (session_id, user_id, now),
            )
            self._conn.commit()

    def get_has_new_output(self, session_id: str, user_id: str) -> bool:
        """Return True if a Claude turn completed after user_id last viewed the session."""
        with self._lock:
            row = self._conn.execute(
                "SELECT last_turn_at, status FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row or not row["last_turn_at"]:
                return False
            if row["status"] in ("terminated", "creating"):
                return False
            view_row = self._conn.execute(
                "SELECT last_viewed_at FROM session_views WHERE session_id = ? AND user_id = ?",
                (session_id, user_id),
            ).fetchone()
            if not view_row:
                return True
            return row["last_turn_at"] > view_row["last_viewed_at"]

    def get_has_new_output_bulk(self, session_ids: list[str], user_id: str) -> dict[str, bool]:
        """Batch version: returns {session_id: has_new_output} for all given session_ids."""
        if not session_ids:
            return {}
        with self._lock:
            placeholders = ",".join("?" * len(session_ids))
            rows = self._conn.execute(
                f"SELECT id, tool, last_turn_at, last_activity_at, status, attached_clients FROM sessions WHERE id IN ({placeholders})",
                session_ids,
            ).fetchall()
            view_rows = self._conn.execute(
                f"""SELECT session_id, last_viewed_at FROM session_views
                    WHERE session_id IN ({placeholders}) AND user_id = ?""",
                session_ids + [user_id],
            ).fetchall()

        viewed_at = {r["session_id"]: r["last_viewed_at"] for r in view_rows}
        result: dict[str, bool] = {}
        for row in rows:
            sid = row["id"]
            tool = row["tool"] if "tool" in row.keys() else "claude"
            # Cursor sessions have no JSONL turn tracking; use last_activity_at instead
            turn_at = row["last_activity_at"] if tool == "cursor" else row["last_turn_at"]
            if (not turn_at
                    or row["status"] in ("terminated", "creating")
                    or row["attached_clients"] > 0):
                result[sid] = False
                continue
            last_viewed = viewed_at.get(sid)
            if not last_viewed:
                result[sid] = True
            else:
                result[sid] = turn_at > last_viewed
        return result

    def delete(self, session_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM sessions WHERE id = ?", (session_id,)
            )
            self._conn.execute(
                "DELETE FROM session_views WHERE session_id = ?", (session_id,)
            )
            self._conn.execute(
                "DELETE FROM prompt_history WHERE session_id = ?", (session_id,)
            )
            self._conn.execute(
                "DELETE FROM prompt_history_backfill WHERE session_id = ?", (session_id,)
            )
            self._conn.execute(
                "DELETE FROM shares WHERE session_id = ?", (session_id,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def all(self) -> Iterable[SessionMetadata]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM sessions
                   ORDER BY
                     CASE status
                       WHEN 'running' THEN 0
                       WHEN 'creating' THEN 1
                       WHEN 'detached' THEN 2
                       WHEN 'archived' THEN 3
                       WHEN 'terminated' THEN 4
                     END,
                     updated_at DESC"""
            ).fetchall()
            return [self._row_to_session(r) for r in rows]

    # ── Scheduled Tasks ───────────────────────────────────────────────────────

    def _row_to_task(self, row: sqlite3.Row) -> ScheduledTask:
        loop_seconds = row["loop_seconds"] if "loop_seconds" in row.keys() else None
        return ScheduledTask(
            id=row["id"],
            session_id=row["session_id"],
            owner_id=row["owner_id"],
            command=row["command"],
            run_at=datetime.fromisoformat(row["run_at"]),
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            sent_at=datetime.fromisoformat(row["sent_at"]) if row["sent_at"] else None,
            error=row["error"],
            loop_seconds=int(loop_seconds) if loop_seconds is not None else None,
        )

    def create_task(self, task: ScheduledTask) -> ScheduledTask:
        with self._lock:
            self._conn.execute(
                """INSERT INTO scheduled_tasks
                   (id, session_id, owner_id, command, run_at, status, created_at, sent_at, error, loop_seconds)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (task.id, task.session_id, task.owner_id, task.command,
                 task.run_at.isoformat(), task.status,
                 task.created_at.isoformat(), None, None, task.loop_seconds),
            )
            self._conn.commit()
        return task

    def list_tasks_for_session(self, session_id: str) -> list[ScheduledTask]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM scheduled_tasks WHERE session_id = ? ORDER BY run_at ASC",
                (session_id,),
            ).fetchall()
            return [self._row_to_task(r) for r in rows]

    def list_pending_tasks_for_sessions(self, session_ids: list[str]) -> dict[str, list[ScheduledTask]]:
        """Return {session_id: [active tasks]} — pending tasks + sent tasks fired within the last 10 minutes.

        Recently-sent tasks are included so the UI can surface "currently executing"
        state for a brief window after Claude receives the command.
        """
        if not session_ids:
            return {}
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        with self._lock:
            placeholders = ",".join("?" * len(session_ids))
            rows = self._conn.execute(
                f"""SELECT * FROM scheduled_tasks
                    WHERE session_id IN ({placeholders})
                      AND (status = 'pending' OR (status = 'sent' AND sent_at > ?))
                    ORDER BY run_at ASC""",
                (*session_ids, cutoff),
            ).fetchall()
        result: dict[str, list[ScheduledTask]] = {sid: [] for sid in session_ids}
        for row in rows:
            result[row["session_id"]].append(self._row_to_task(row))
        return result

    def list_due_tasks(self) -> list[ScheduledTask]:
        """Return pending tasks whose run_at is in the past."""
        now = self._now()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM scheduled_tasks WHERE status = 'pending' AND run_at <= ? ORDER BY run_at ASC",
                (now,),
            ).fetchall()
            return [self._row_to_task(r) for r in rows]

    def update_task_status(self, task_id: str, status: str, error: str | None = None) -> None:
        with self._lock:
            now = self._now()
            if status == "sent":
                self._conn.execute(
                    "UPDATE scheduled_tasks SET status = ?, sent_at = ?, error = ? WHERE id = ?",
                    (status, now, error, task_id),
                )
            else:
                self._conn.execute(
                    "UPDATE scheduled_tasks SET status = ?, error = ? WHERE id = ?",
                    (status, error, task_id),
                )
            self._conn.commit()

    def cancel_task(self, task_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE scheduled_tasks SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
                (task_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def delete_tasks_for_session(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM scheduled_tasks WHERE session_id = ?", (session_id,)
            )
            self._conn.commit()

    def set_git_auto_commit(self, session_id: str, enabled: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET git_auto_commit = ? WHERE id = ?",
                (1 if enabled else 0, session_id),
            )
            self._conn.commit()

    def update_git_msg_count(self, session_id: str, count: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET git_commit_msg_count = ? WHERE id = ?",
                (count, session_id),
            )
            self._conn.commit()

    def set_tui_hint(self, session_id: str, hint: str | None) -> None:
        with self._lock:
            if hint:
                self._tui_hints[session_id] = hint
            else:
                self._tui_hints.pop(session_id, None)

    def get_tui_hint(self, session_id: str) -> str | None:
        return self._tui_hints.get(session_id)

    # ── Prompt history ────────────────────────────────────────────────────────

    def append_prompt_history(
        self,
        session_id: str,
        text: str,
        sent_at: float,
        pane: str | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO prompt_history (session_id, text, sent_at, pane) VALUES (?, ?, ?, ?)",
                (session_id, text, sent_at, pane),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    @staticmethod
    def _escape_like(q: str) -> str:
        # Escape SQL LIKE metacharacters so user queries can't smuggle
        # wildcards. The `\` is the ESCAPE char declared in the SQL.
        return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def list_prompt_history(
        self,
        session_id: str,
        limit: int = 50,
        offset: int = 0,
        query: str | None = None,
    ) -> list[dict]:
        """Most recent first. Pagination via (limit, offset) over the same
        session-scoped, sent_at-descending ordering. `query` is an optional
        case-insensitive substring filter on the prompt text."""
        capped_limit = max(1, min(limit, 500))
        capped_offset = max(0, offset)
        where = "session_id = ?"
        params: list = [session_id]
        if query:
            where += " AND text LIKE ? ESCAPE '\\'"
            params.append(f"%{self._escape_like(query)}%")
        sql = (
            "SELECT id, text, sent_at, pane FROM prompt_history "
            f"WHERE {where} ORDER BY sent_at DESC, id DESC LIMIT ? OFFSET ?"
        )
        params.extend([capped_limit, capped_offset])
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
            return [
                {"id": r["id"], "text": r["text"], "sent_at": r["sent_at"], "pane": r["pane"]}
                for r in rows
            ]

    def count_prompt_history(
        self,
        session_id: str,
        query: str | None = None,
    ) -> int:
        where = "session_id = ?"
        params: list = [session_id]
        if query:
            where += " AND text LIKE ? ESCAPE '\\'"
            params.append(f"%{self._escape_like(query)}%")
        sql = f"SELECT COUNT(*) AS n FROM prompt_history WHERE {where}"
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
            return int(row["n"]) if row else 0

    def delete_prompt_history_entry(self, session_id: str, entry_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM prompt_history WHERE session_id = ? AND id = ?",
                (session_id, entry_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def bulk_insert_prompt_history(
        self,
        session_id: str,
        entries: list[tuple[str, float, str | None]],
    ) -> int:
        """entries: list of (text, sent_at, pane). Returns inserted row count."""
        if not entries:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO prompt_history (session_id, text, sent_at, pane) VALUES (?, ?, ?, ?)",
                [(session_id, t, ts, pane) for (t, ts, pane) in entries],
            )
            self._conn.commit()
            return len(entries)

    def is_prompt_history_backfilled(self, session_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM prompt_history_backfill WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return row is not None

    def mark_prompt_history_backfilled(self, session_id: str, completed_at: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO prompt_history_backfill (session_id, completed_at) VALUES (?, ?)",
                (session_id, completed_at),
            )
            self._conn.commit()

    # ── Conversation shares ─────────────────────────────────────────────────────

    @staticmethod
    def _row_to_share(row: sqlite3.Row) -> ShareRecord:
        return ShareRecord(
            hash=row["hash"],
            session_id=row["session_id"],
            owner_id=row["owner_id"],
            share_type=row["share_type"],
            cutoff_ts=row["cutoff_ts"],
            cutoff_msg_uuid=row["cutoff_msg_uuid"],
            cutoff_msg_text=row["cutoff_msg_text"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            default_theme=(row["default_theme"] if "default_theme" in row.keys() else "light"),
        )

    def create_share(self, share: ShareRecord) -> ShareRecord:
        """Insert (or replace, if the same hash already exists) a share."""
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO shares
                   (hash, session_id, owner_id, share_type, cutoff_ts,
                    cutoff_msg_uuid, cutoff_msg_text, created_at, expires_at,
                    default_theme)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (share.hash, share.session_id, share.owner_id, share.share_type,
                 share.cutoff_ts, share.cutoff_msg_uuid, share.cutoff_msg_text,
                 share.created_at, share.expires_at, share.default_theme),
            )
            self._conn.commit()
        return share

    def get_share(self, hash: str) -> ShareRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM shares WHERE hash = ?", (hash,)
            ).fetchone()
            return self._row_to_share(row) if row else None

    def list_shares(self, session_id: str, owner_id: str) -> list[ShareRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM shares WHERE session_id = ? AND owner_id = ? ORDER BY created_at DESC",
                (session_id, owner_id),
            ).fetchall()
            return [self._row_to_share(r) for r in rows]

    def delete_share(self, hash: str, owner_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM shares WHERE hash = ? AND owner_id = ?",
                (hash, owner_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list_active_shares(self, now: float) -> list[ShareRecord]:
        """All non-expired shares — used to warm the in-memory cache at startup."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM shares WHERE expires_at > ?", (now,)
            ).fetchall()
            return [self._row_to_share(r) for r in rows]

    def delete_expired_shares(self, now: float) -> list[str]:
        """Delete shares whose expiry has passed; return their hashes."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT hash FROM shares WHERE expires_at <= ?", (now,)
            ).fetchall()
            hashes = [r["hash"] for r in rows]
            if hashes:
                self._conn.execute(
                    "DELETE FROM shares WHERE expires_at <= ?", (now,)
                )
                self._conn.commit()
            return hashes

