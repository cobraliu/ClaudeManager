from __future__ import annotations

import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.config import get_default_workspace
from app.models.session import (
    AttachRequest,
    AttachResponse,
    ScheduledTask,
    SessionCreateRequest,
    SessionListResponse,
    SessionMetadata,
    SessionStatus,
    SessionStatusListResponse,
    SessionStatusView,
    SessionView,
    TaskCreateRequest,
    TaskView,
)
from app.observability import audit_event
from app.security import AdminUser, CurrentUser, CurrentUserInfo
from app.services.claude_pid import format_tui_hint, get_pid_waiting_state, parse_auq_from_screen
from app.services.claude_session_reader import enrich_session, find_newest_claude_session_id, get_latest_turn_info, get_conversation, search_conversation, list_project_session_ids, list_subagents, get_subagent_lines, list_all_claude_sessions_global, get_todo_plans
from app.services.claude_goals import read_goals
from app.services.claude_auqs import list_auqs
from app.agents import get_adapter
from app.services.cursor_session_reader import list_all_cursor_sessions_global
from app.url_prefix import public_path
from app.services.git_service import (
    git_add_commit, git_checkout_branch, git_checkout_remote_branch, git_clone,
    git_conflict_file_versions, git_file_diff, git_file_log, git_file_show,
    git_get_remote, git_graph_log, git_init, git_is_dirty, git_list_branches, git_log,
    git_merge_abort, git_merge_continue, git_merge_file_diff, git_merge_preview, git_merge_start, git_merge_status,
    git_pull, git_push, git_resolve_file, git_search_commits, git_set_remote,
    git_show_commit, is_git_repo,
    make_commit_message, make_commit_summary,
)
from app.services.session_store import SessionStore
from app.services.tmux_service import TmuxError, TmuxService

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

models_router = APIRouter(prefix="/api/models", tags=["models"])

@models_router.get("")
def list_models(tool: str = "claude") -> list[dict]:
    from app.agents import get_adapter
    return get_adapter(tool).list_models()

_store: SessionStore | None = None
_tmux: TmuxService | None = None

# Matches the percentage portion of a "Compacting conversation… 45%" line in TUI.
_COMPACT_PCT_RE = re.compile(r"(\d{1,3})\s*%")
# Progress-bar glyphs Claude Code's TUI uses for the compaction bar.  These
# obscure Unicode rectangles are essentially absent from normal chat text, so
# requiring one nearby is enough to keep user-typed or assistant-quoted
# "Compacting conversation…" strings from triggering a false-positive — even
# without an additional timer-format discriminator.  (Earlier versions of
# Claude Code paint a "(NNs)" / "(Xm Ys)" elapsed-time counter next to the
# title, but it isn't always rendered — observed cases where the title shows
# just "Compacting conversation…" with no timer at all — so we can't rely on
# it.)
_COMPACT_BAR_CHARS = ("▰", "▱")
# How many trailing lines of the TUI pane to scan for the compaction banner.
# Claude Code renders the spinner near the bottom of the frame, but BELOW it
# there are several rendered rows: a blank, the "Tip:" line, the multi-line
# input box (╭ │ ╰), and the status line — typically 7–8 lines.  So the
# "Compacting conversation…" header sits roughly 8–12 rows above the bottom.
# A tail of 20 comfortably covers it across terminal sizes.  False-positive
# risk is bounded by the bar-glyph discriminator applied below.
_COMPACT_TUI_TAIL_LINES = 20
# Stale-marker timeout for the PreCompact hook signal.  A real compaction
# completes well within this window; if the marker hangs around longer it
# means the hook fired but no compact_boundary was ever written (compaction
# aborted, session killed, etc.), so we drop the signal.
_COMPACT_HOOK_TIMEOUT_S = 15 * 60

# ── Hook file reader ─────────────────────────────────────────────────────────
# Cache: (file_mtime, {session_id: {"auq": dict|None, "approve": dict|None}})
# Per-session hook cache: {claude_session_id: (mtime, {auq, approve})}
_hooks_cache: dict[str, tuple[float, dict]] = {}

def _read_session_hooks(claude_session_id: str) -> dict:
    """Return {auq, approve} for a specific claude session from its per-session hook file."""
    import json as _json
    from pathlib import Path as _Path
    from collections import deque as _deque
    global _hooks_cache
    hook_file = _Path.home() / ".claude_manager" / "hooks" / f"{claude_session_id}.jsonl"
    if not hook_file.exists():
        return {"auq": None, "approve": None}
    try:
        mtime = hook_file.stat().st_mtime
        cached = _hooks_cache.get(claude_session_id)
        if cached and cached[0] == mtime:
            return cached[1]
        with open(hook_file) as f:
            tail = _deque(f, maxlen=500)
        entry: dict = {"auq": None, "approve": None}
        for line in tail:
            try:
                d = _json.loads(line)
                tool = d.get("tool_name", "")
                if not tool:
                    continue
                if tool == "AskUserQuestion":
                    entry["auq"] = d.get("tool_input", {})
                else:
                    entry["approve"] = {"tool_name": tool, "tool_input": d.get("tool_input", {})}
            except Exception:
                pass
        _hooks_cache[claude_session_id] = (mtime, entry)
        return entry
    except OSError:
        return {"auq": None, "approve": None}


def _cleanup_session_hooks(claude_session_id: str) -> None:
    """Delete the hook file for a terminated session."""
    from pathlib import Path as _Path
    hook_file = _Path.home() / ".claude_manager" / "hooks" / f"{claude_session_id}.jsonl"
    try:
        hook_file.unlink(missing_ok=True)
    except OSError:
        pass
    compact_marker = _Path.home() / ".claude_manager" / "compact_state" / f"{claude_session_id}.json"
    try:
        compact_marker.unlink(missing_ok=True)
    except OSError:
        pass
    _hooks_cache.pop(claude_session_id, None)
    _plan_pending_cache.pop(claude_session_id, None)


def _is_compacting_via_hook(
    claude_session_id: str,
    cwd: str,
    tui_screen: str | None = None,
) -> bool:
    """Return True if the PreCompact hook fired and compaction is in flight.

    Self-clears the marker (so future polls return False) when any of:
      - stale: marker older than _COMPACT_HOOK_TIMEOUT_S,
      - JSONL has any entry timestamped >= marker.started + grace (covers
        compact_boundary and any post-compaction activity — Claude writes
        nothing to JSONL *during* compaction, so any new line means we've
        moved past it),
      - tui_screen was captured but no longer shows "Compacting conversation"
        (catches cases where the process exited or the hook misfired).
    """
    import json as _json, time as _time
    from pathlib import Path as _Path
    from collections import deque as _deque
    from datetime import datetime as _dt
    from app.services.claude_session_reader import _find_session_jsonl

    marker = _Path.home() / ".claude_manager" / "compact_state" / f"{claude_session_id}.json"
    if not marker.exists():
        return False

    def _drop() -> bool:
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass
        return False

    try:
        meta = _json.loads(marker.read_text())
        started = float(meta.get("started_at_epoch", 0.0))
    except Exception:
        started = 0.0

    if started <= 0.0 or (_time.time() - started) > _COMPACT_HOOK_TIMEOUT_S:
        return _drop()

    # JSONL-side resolution: any entry timestamped past started+grace.
    # Grace absorbs clock skew + the PreCompact hook fire moment itself.
    GRACE_S = 2.0
    jsonl = _find_session_jsonl(claude_session_id, cwd)
    if jsonl is not None:
        try:
            with open(jsonl) as f:
                tail = _deque(f, maxlen=200)
            for line in reversed(tail):
                if '"timestamp"' not in line:
                    continue
                try:
                    d = _json.loads(line)
                except Exception:
                    continue
                ts = d.get("timestamp")
                if not isinstance(ts, str):
                    continue
                try:
                    iso = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
                    when = _dt.fromisoformat(iso).timestamp()
                except Exception:
                    continue
                if when >= started + GRACE_S:
                    return _drop()
        except OSError:
            pass

    # TUI-side resolution: if we have a fresh screen capture and it doesn't
    # show the live compaction status bar, the hook signal is stale. Same
    # discriminator as path (b): "Compacting conversation…" together with a
    # ▰/▱ progress-bar glyph in the next line — those glyphs are obscure
    # Unicode rectangles that don't appear in normal chat text, so quoted /
    # typed conversation referencing compaction can't keep a stale hook
    # marker alive.
    if tui_screen is not None:
        tail_lines = tui_screen.splitlines()[-_COMPACT_TUI_TAIL_LINES:]
        active = False
        for i, ln in enumerate(tail_lines):
            if "Compacting conversation" not in ln:
                continue
            window = "\n".join(tail_lines[i:min(i + 2, len(tail_lines))])
            if any(ch in window for ch in _COMPACT_BAR_CHARS):
                active = True
                break
        if not active:
            return _drop()

    return True


def _pending_auq_from_hooks(claude_session_id: str, cwd: str) -> dict | None:
    """Return the latest unanswered AUQ tool_input from the hook file, or None.

    Background: in Claude Code 2.1.142 the PID file no longer reliably sets
    `status: "waiting"` / `waitingFor: "AskUserQuestion ..."` when AUQ is
    pending, so the PID-driven `get_pid_waiting_state` path can miss pending
    AUQs entirely.

    The PreToolUse hook (~/.claude/auq-hook.py) fires when Claude *calls* AUQ,
    well before the user answers. We use that as the primary signal:
      1. Find the most recent AUQ entry in the per-session hook file.
      2. Check the session JSONL — if the AUQ's tool_use_id already has a
         corresponding tool_result, the user has already answered, return None.
      3. Otherwise it's a pending AUQ; return its tool_input.
    """
    import json as _json
    from pathlib import Path as _Path
    from collections import deque as _deque

    hook_file = _Path.home() / ".claude_manager" / "hooks" / f"{claude_session_id}.jsonl"
    if not hook_file.exists():
        return None

    try:
        with open(hook_file) as f:
            tail = _deque(f, maxlen=500)
    except OSError:
        return None

    latest_auq_id: str | None = None
    latest_auq_input: dict | None = None
    for line in reversed(tail):
        try:
            d = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if d.get("tool_name") == "AskUserQuestion":
            latest_auq_id = d.get("tool_use_id")
            latest_auq_input = d.get("tool_input")
            break

    if not latest_auq_id or not latest_auq_input:
        return None

    # Check whether this AUQ has already been answered (tool_result in JSONL).
    # A simple substring check on tool_use_id is sufficient — tool_use_ids are
    # 25-char random suffixes, no false positives expected.
    from app.services.claude_session_reader import _find_session_jsonl as _find_jsonl
    jsonl = _find_jsonl(claude_session_id, cwd)
    if jsonl is not None:
        try:
            with open(jsonl) as f:
                for line in f:
                    if latest_auq_id in line and '"tool_result"' in line:
                        return None  # Already answered
        except OSError:
            pass

    return latest_auq_input


# mtime-keyed cache for plan-pending status: {claude_session_id: (mtime, bool)}
# Avoids re-scanning every session JSONL on every 3s status poll.
_plan_pending_cache: dict[str, tuple[float, bool]] = {}


def _pending_plan_from_jsonl(claude_session_id: str, cwd: str) -> bool:
    """Return True iff the session JSONL says a plan is awaiting user approval.

    Has to handle two distinct on-disk formats because Claude CLI changed how
    plan mode is recorded:

    - **Legacy (CLI ≤ 2.1.144)**: plan approval was an `ExitPlanMode` tool_use.
      An assistant message carried `{"type":"tool_use","name":"ExitPlanMode"}`,
      and the user's Approve/Reject decision arrived as a `tool_result` with
      a matching `tool_use_id`. Pending = at least one ExitPlanMode id has no
      paired tool_result. Mirrors `ConversationPane.tsx` (`!toolResults.has`).

    - **Current (CLI 2.1.150+)**: ExitPlanMode is gone. Plan mode lifecycle is
      written as two distinct streams of signals that both need to be tracked
      as a *rolling* in-plan state — we can't pick one slot and call it the
      winner, because approval doesn't always toggle the same slot it entered
      on:
        * `permission-mode` entries — `permissionMode == "plan"` enters,
          anything else exits.
        * `attachment` entries — `attachment.type == "plan_mode"` enters,
          `attachment.type == "plan_mode_exit"` exits (this is what CLI
          writes when the user clicks Approve — there is **no** matching
          `permission-mode` flip-out, which is why the prior "latest
          permission-mode wins" heuristic stuck on True after approval).

      So we walk the file once and treat each in/out signal as a state
      transition. The final `in_plan_state` after the scan is the answer.

    Either format counts as pending — covers users on either CLI release.
    """
    import json as _json
    from app.services.claude_session_reader import _find_session_jsonl as _find_jsonl
    jsonl = _find_jsonl(claude_session_id, cwd)
    if jsonl is None:
        return False
    try:
        mtime = jsonl.stat().st_mtime
    except OSError:
        return False
    cached = _plan_pending_cache.get(claude_session_id)
    if cached and cached[0] == mtime:
        return cached[1]

    # Legacy-format accumulators.
    legacy_plan_ids: set[str] = set()
    legacy_resolved_ids: set[str] = set()
    # New-format rolling state. Flipped True/False as we walk in/out signals
    # in file order; whatever value it holds at EOF is the verdict.
    in_plan_state = False

    try:
        with open(jsonl) as f:
            for raw in f:
                # Quick substring pre-check: skip lines that can't contribute.
                # We need ExitPlanMode (legacy), tool_result (legacy),
                # permission-mode (new), or plan_mode / plan_mode_exit (new
                # attachment — plan_mode_exit substring is covered by "plan_mode").
                if (
                    "ExitPlanMode" not in raw
                    and '"tool_result"' not in raw
                    and '"permission-mode"' not in raw
                    and "plan_mode" not in raw
                ):
                    continue
                try:
                    d = _json.loads(raw)
                except Exception:
                    continue

                # New format: top-level "permission-mode" toggles mode.
                if d.get("type") == "permission-mode":
                    pm = d.get("permissionMode")
                    if isinstance(pm, str):
                        in_plan_state = (pm == "plan")
                    continue

                # New format: "attachment" of type plan_mode / plan_mode_exit
                # is the actual approve/reject signal CLI 2.1.150+ writes.
                if d.get("type") == "attachment":
                    att = d.get("attachment")
                    if isinstance(att, dict):
                        att_type = att.get("type")
                        if att_type == "plan_mode":
                            in_plan_state = True
                        elif att_type == "plan_mode_exit":
                            in_plan_state = False
                    continue

                # Legacy format: scan message.content for ExitPlanMode tool_use
                # and any tool_result blocks.
                msg = d.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    btype = b.get("type")
                    if btype == "tool_use" and b.get("name") == "ExitPlanMode":
                        bid = b.get("id")
                        if isinstance(bid, str):
                            legacy_plan_ids.add(bid)
                    elif btype == "tool_result":
                        tid = b.get("tool_use_id")
                        if isinstance(tid, str):
                            legacy_resolved_ids.add(tid)
    except OSError:
        _plan_pending_cache[claude_session_id] = (mtime, False)
        return False

    pending = in_plan_state or bool(legacy_plan_ids - legacy_resolved_ids)

    _plan_pending_cache[claude_session_id] = (mtime, pending)
    return pending


def configure(store: SessionStore, tmux: TmuxService) -> None:
    global _store, _tmux
    _store = store
    _tmux = tmux


def _get_store() -> SessionStore:
    assert _store is not None
    return _store


def _get_tmux() -> TmuxService:
    assert _tmux is not None
    return _tmux



@router.post("", status_code=status.HTTP_201_CREATED)
def create_session(body: SessionCreateRequest, user_id: CurrentUser) -> SessionMetadata:
    store = _get_store()
    tmux = _get_tmux()

    ts = int(time.time())
    tmux_name = f"claude-{user_id}-{body.project}-{ts}"

    if body.cwd:
        cwd = body.cwd
    else:
        cwd = os.path.join(get_default_workspace(), user_id, body.project)

    cwd_exists = os.path.isdir(cwd)

    if body.git_repo_url and not cwd_exists:
        # Clone the repo into cwd
        result = git_clone(body.git_repo_url, cwd)
        if not result["ok"]:
            raise HTTPException(status_code=400, detail=f"git clone failed: {result['output']}")
    else:
        os.makedirs(cwd, exist_ok=True)
        # Auto-init git repo with .gitignore if not already a repo
        if not is_git_repo(cwd):
            git_init(cwd)

    # Codex app-server transport is only valid for the codex tool. Silently
    # downgrade any stray "app_server" on non-codex sessions (frontend
    # validates, this is defense-in-depth).
    codex_transport = body.codex_transport if body.tool == "codex" else "tui"

    session = SessionMetadata(
        owner_id=user_id,
        name=tmux_name,
        project=body.project,
        cwd=cwd,
        env=body.env,
        model=body.model,
        tmux_session_name=tmux_name,
        resume_session_id=body.resume_session_id,
        git_repo_url=body.git_repo_url if body.git_repo_url and not cwd_exists else None,
        tool=body.tool,
        codex_transport=codex_transport,
    )
    store.create(session)

    is_new = not body.resume_session_id

    if body.tool == "codex" and codex_transport == "app_server":
        # App-server mode: skip tmux. Spawn `codex app-server` directly.
        from app.services import codex_appserver_manager as csm
        try:
            pid, port = csm.start(
                session.id,
                cwd=cwd,
                env=body.env or None,
                model=body.model,
            )
        except Exception as exc:
            store.transition(session.id, SessionStatus.TERMINATED)
            raise HTTPException(
                status_code=500,
                detail=f"failed to start codex app-server: {exc}",
            ) from exc
        store.update_codex_appserver_endpoint(session.id, pid, port)
        # Pick up thread id once it's available
        tid = csm.get_thread_id(session.id)
        if tid:
            store.update_agent_session_id(session.id, tid)
        store.transition(session.id, SessionStatus.RUNNING)
        audit_event(
            user_id,
            "create",
            session.id,
            {"project": body.project, "tool": body.tool, "transport": "app_server"},
        )
        return session

    try:
        tmux.create_session(
            session_name=tmux_name,
            cwd=cwd,
            env=body.env,
            claude_model=body.model,
            resume_session_id=body.resume_session_id,
            inner_id=session.id if (is_new and body.tool == "claude") else None,
            tool=body.tool,
        )
    except TmuxError as exc:
        store.transition(session.id, SessionStatus.TERMINATED)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    store.transition(session.id, SessionStatus.RUNNING)
    audit_event(user_id, "create", session.id, {"project": body.project, "tool": body.tool})

    # For cursor/codex sessions, resume_session_id IS the agent session UUID — store immediately.
    if body.tool in ("cursor", "codex") and body.resume_session_id:
        store.update_agent_session_id(session.id, body.resume_session_id)

    if body.tool == "claude":
        def _resolve():
            if is_new:
                claude_sid, proc_pid = tmux.resolve_agent_session_id_by_inner_id(session.id, timeout=15)
            else:
                claude_sid, proc_pid = tmux.resolve_agent_session_id(tmux_name, timeout=15)
            if proc_pid:
                store.update_claude_proc_pid(session.id, proc_pid)
            if claude_sid:
                store.update_agent_session_id(session.id, claude_sid)

        threading.Thread(target=_resolve, daemon=True).start()
    elif body.tool == "codex" and is_new:
        # Fresh Codex session: poll for the newest rollout file in cwd to pick up
        # the session UUID once the CLI has written its session_meta line.
        def _resolve_codex():
            from app.agents import get_adapter
            adapter = get_adapter("codex")
            deadline = time.time() + 20.0
            while time.time() < deadline:
                sid = adapter.find_newest_session_id(cwd)
                if sid:
                    store.update_agent_session_id(session.id, sid)
                    return
                time.sleep(1.0)

        threading.Thread(target=_resolve_codex, daemon=True).start()

    return session


_STREAMING_THRESHOLD_SECS = 8  # watchdog runs every 5s; use 8s to avoid false negatives in the gap


def _enrich(
    session: SessionMetadata,
    new_output_map: dict[str, bool] | None = None,
    task_map: dict[str, list] | None = None,
) -> SessionView:
    """Add Claude title, display prompts, new-output flag, and streaming flag."""
    from app.agents import get_adapter
    view = SessionView(**session.model_dump())
    if session.agent_session_id:
        data = get_adapter(session.tool).enrich(session.agent_session_id, session.cwd)
        view.claude_title = data.get("title")
        view.prompts = data.get("prompts", [])
        view.last_user_input_at = data.get("last_user_input_at")
        # search_text intentionally NOT included — search is done server-side
    if new_output_map is not None:
        view.has_new_output = new_output_map.get(session.id, False)
    if task_map is not None:
        view.scheduled_tasks = [
            TaskView(
                id=t.id,
                command=t.command,
                run_at=t.run_at.isoformat(),
                status=t.status,
                created_at=t.created_at.isoformat(),
                loop_seconds=t.loop_seconds,
            )
            for t in task_map.get(session.id, [])
        ]
    # is_streaming: recent tmux activity AND that activity is after the last completed turn.
    # attached_clients is intentionally NOT checked — show the indicator regardless of whether
    # anyone is currently watching, so the session list always reflects real state.
    if session.status not in (SessionStatus.TERMINATED, SessionStatus.ARCHIVED) and session.last_activity_at:
        now = datetime.now(timezone.utc)
        last = session.last_activity_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed = (now - last).total_seconds()
        if elapsed < _STREAMING_THRESHOLD_SECS:
            last_turn = session.last_turn_at
            if last_turn is not None and last_turn.tzinfo is None:
                last_turn = last_turn.replace(tzinfo=timezone.utc)
            # Streaming: no completed turn yet, or activity is after the last completed turn
            if last_turn is None or last > last_turn:
                view.is_streaming = True
    return view


def _filter_sessions(
    sessions: list[SessionMetadata], q: str | None
) -> list[SessionMetadata]:
    """Server-side search across metadata + full conversation text."""
    if not q or not q.strip():
        return sessions
    ql = q.strip().lower()
    results: list[SessionMetadata] = []
    for s in sessions:
        # Search metadata fields first (cheap)
        if (
            ql in s.project.lower()
            or ql in s.cwd.lower()
            or ql in (s.agent_session_id or "").lower()
            or ql in s.status.value.lower()
            or ql in (s.model or "").lower()
            or ql in s.owner_id.lower()
        ):
            results.append(s)
            continue
        # Search full conversation text (cached)
        if s.agent_session_id and search_conversation(s.agent_session_id, s.cwd, ql):
            results.append(s)
    return results


@router.get("")
def list_sessions(
    user_id: CurrentUser,
    q: str | None = Query(default=None, max_length=200),
) -> SessionListResponse:
    store = _get_store()
    items = store.list_for_owner(user_id)
    items = _filter_sessions(items, q)
    session_ids = [s.id for s in items]
    new_output_map = store.get_has_new_output_bulk(session_ids, user_id)
    task_map = store.list_pending_tasks_for_sessions(session_ids)
    views = [_enrich(s, new_output_map, task_map) for s in items]
    return SessionListResponse(items=views, total=len(views))


@router.get("/all")
def list_all_sessions(
    admin: AdminUser,
    q: str | None = Query(default=None, max_length=200),
) -> SessionListResponse:
    store = _get_store()
    items = list(store.all())
    items = _filter_sessions(items, q)
    views = [_enrich(s) for s in items]
    return SessionListResponse(items=views, total=len(views))


@router.get("/external")
def browse_external_sessions(user_id: CurrentUser) -> list[dict]:
    """Return all Claude sessions from ~/.claude/projects not already in ClaudeManager."""
    store = _get_store()
    occupied = store.get_all_agent_session_ids()
    return list_all_claude_sessions_global(occupied)


@router.get("/external-cursor")
def browse_cursor_sessions(user_id: CurrentUser) -> list[dict]:
    """Return all Cursor agent sessions from ~/.cursor/projects not already in ClaudeManager."""
    store = _get_store()
    occupied = store.get_all_agent_session_ids()
    return list_all_cursor_sessions_global(occupied)


@router.get("/external-codex")
def browse_codex_sessions(user_id: CurrentUser) -> list[dict]:
    """Return all Codex sessions from ~/.codex/sessions/ not already in ClaudeManager."""
    from app.services.codex_session_reader import list_all_codex_sessions_global as _list_codex
    store = _get_store()
    occupied = store.get_all_agent_session_ids()
    return _list_codex(occupied)


@router.get("/external-preview")
def get_external_preview(agent_session_id: str, cwd: str, user_id: CurrentUser, tool: str = "claude") -> dict:
    """Return first 100 + last 100 turns of an external session for preview."""
    from app.agents import get_adapter
    turns = get_adapter(tool).get_conversation(agent_session_id, cwd)
    total = len(turns)
    if total <= 200:
        preview = turns
        truncated_before = 0
    else:
        preview = turns[:100] + turns[total - 100:]
        truncated_before = total - 200
    return {"turns": preview, "total": total, "truncated_before": truncated_before}


@router.get("/status")
def list_sessions_status(user_id: CurrentUser) -> SessionStatusListResponse:
    """Lightweight endpoint: returns only status/activity fields, no Claude enrichment."""
    store = _get_store()
    tmux = _get_tmux()
    items = store.list_for_owner(user_id)
    session_ids = [s.id for s in items]
    new_output_map = store.get_has_new_output_bulk(session_ids, user_id)
    task_map = store.list_pending_tasks_for_sessions(session_ids)
    views = []
    for s in items:
        tasks = task_map.get(s.id, [])
        task_views = [
            TaskView(
                id=t.id,
                command=t.command,
                run_at=t.run_at.isoformat(),
                status=t.status,
                created_at=t.created_at.isoformat(),
                loop_seconds=t.loop_seconds,
            )
            for t in tasks
        ]
        # Codex app-server transport bypasses the Claude PID/hook machinery
        # entirely. The manager owns the live AUQ/approval cache; route via
        # the adapter so this branch stays explicit at the top.
        if s.tool == "codex" and s.codex_transport == "app_server":
            from app.agents import get_adapter
            ws = None
            if s.status == SessionStatus.RUNNING:
                try:
                    ws = get_adapter("codex").get_waiting_state(
                        agent_session_id=s.agent_session_id,
                        agent_pid=None,
                        cwd=s.cwd,
                        session_id=s.id,
                    )
                except Exception:
                    ws = None
            tui_auq_data = ws.get("raw") if ws and ws.get("kind") == "auq" else None
            tui_approve_data = ws.get("raw") if ws and ws.get("kind") == "approve" else None
            tui_hint = ws.get("hint") if ws else None
            is_compacting = bool(ws and ws.get("kind") == "compacting")
            views.append(
                SessionStatusView(
                    id=s.id,
                    status=s.status,
                    attached_clients=s.attached_clients,
                    has_new_output=new_output_map.get(s.id, False),
                    is_streaming=False,
                    is_compacting=is_compacting,
                    compacting_progress=None,
                    scheduled_tasks=task_views,
                    tui_hint=tui_hint,
                    tui_auq_data=tui_auq_data,
                    tui_approve_data=tui_approve_data,
                )
            )
            continue
        # PID file is checked first, but Claude Code 2.1.142+ no longer reliably
        # sets waitingFor for AUQ, so we additionally consult the PreToolUse hook
        # file — it captures the AUQ call before the user answers.
        pid_state = get_pid_waiting_state(s.claude_proc_pid) if s.status == SessionStatus.RUNNING else None
        tui_auq_data: dict | None = None
        tui_approve_data: dict | None = None
        if pid_state:
            hook_entry = _read_session_hooks(s.agent_session_id) if s.agent_session_id else {"auq": None, "approve": None}
            if pid_state[1] == "auq":
                tui_auq_data = hook_entry.get("auq")
                if not tui_auq_data:
                    screen = tmux.capture_visible_screen(s.tmux_session_name)
                    if screen:
                        tui_auq_data = parse_auq_from_screen(screen)
            elif pid_state[1] == "approve":
                # Under --dangerously-skip-permissions (always set by ClaudeManager
                # in tmux_service.py) Claude CLI still briefly writes the tool name
                # to ~/.claude/sessions/{pid}.json:waitingFor while it's executing
                # a tool — even though no approval prompt is actually rendered.
                # Combined with the hook file's "approve" slot (which is just the
                # most recent non-AUQ tool call, with no resolved-check), this
                # produces a ghost approval card. Verify the approval widget is
                # actually on screen before surfacing it.
                screen = tmux.capture_visible_screen(s.tmux_session_name)
                if screen and "Claude wants to use" in screen:
                    tui_approve_data = hook_entry.get("approve")
        # Fallback path: AUQ from hooks when the PID file didn't flag waiting.
        # We only consider a hook AUQ "pending" if its tool_use_id has no
        # corresponding tool_result in the JSONL yet.
        if tui_auq_data is None and s.status == SessionStatus.RUNNING and s.agent_session_id:
            pending = _pending_auq_from_hooks(s.agent_session_id, s.cwd)
            if pending:
                tui_auq_data = pending
        # If we have AUQ data but no formatted hint, surface the standard hint
        # so the frontend's isWaitingForAuq gating works (it looks for the
        # "asking a question" substring).
        if tui_auq_data and not pid_state:
            tui_hint = "Claude is asking a question — answer in Chat or switch to TUI"
        else:
            tui_hint = format_tui_hint(pid_state[0], pid_state[1]) if pid_state else store.get_tui_hint(s.id)
        # Two detection paths:
        #   (a) PreCompact hook signal — authoritative on/off; set as soon as
        #       /compact starts, self-clears on compact_boundary, any newer
        #       JSONL activity, stale-timeout, or "no longer in TUI".
        #   (b) TUI pane scan — same-frame "Compacting conversation" + bar
        #       glyph + percentage; only source for the percentage value.
        # is_compacting = (a) OR (b); compacting_progress comes only from (b).
        is_compacting = False
        compacting_progress: str | None = None
        tui_screen_for_hook: str | None = None

        eligible = s.status in (SessionStatus.RUNNING, SessionStatus.DETACHED)
        # Capture the TUI once if recent activity warrants it — feeds both
        # the percentage scan and the hook helper's "no longer in TUI" check.
        if (
            eligible
            and tui_auq_data is None
            and tui_approve_data is None
            and s.last_activity_at is not None
        ):
            now = datetime.now(timezone.utc)
            last = s.last_activity_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if (now - last).total_seconds() < 60:
                try:
                    tui_screen_for_hook = tmux.capture_visible_screen(s.tmux_session_name)
                except Exception:
                    tui_screen_for_hook = None

        if eligible and s.agent_session_id:
            if _is_compacting_via_hook(s.agent_session_id, s.cwd, tui_screen_for_hook):
                is_compacting = True

        screen = tui_screen_for_hook
        if screen:
            tail_lines = screen.splitlines()[-_COMPACT_TUI_TAIL_LINES:]
            for i, ln in enumerate(tail_lines):
                if "Compacting conversation" not in ln:
                    continue
                # Claude Code paints the live status across two lines: the
                # title line "· Compacting conversation…" (sometimes with a
                # "(NNs)" / "(Xm Ys)" timer appended, sometimes not), and
                # the "▰▰▰▱▱▱ 45%" bar line below it.  We discriminate on the
                # bar glyph alone — those obscure Unicode rectangles don't
                # appear in chat text, so the pair {"Compacting conversation"
                # line + bar glyph in the next line} is enough to rule out
                # quoted/typed false positives without depending on a timer
                # that isn't always rendered.
                window = tail_lines[i:min(i + 2, len(tail_lines))]
                window_text = "\n".join(window)
                if not any(ch in window_text for ch in _COMPACT_BAR_CHARS):
                    continue
                pct: str | None = None
                for wl in window:
                    m = _COMPACT_PCT_RE.search(wl)
                    if m:
                        val = int(m.group(1))
                        if 0 <= val <= 100:
                            pct = f"{val}%"
                            break
                is_compacting = True
                compacting_progress = pct
                break
        # Plan-approval attention: scan JSONL for ExitPlanMode tool_use without
        # a matching tool_result. Cheap (mtime-cached) and covers sessions
        # nobody has attached to.
        tui_plan_pending = False
        if eligible and s.agent_session_id:
            try:
                tui_plan_pending = _pending_plan_from_jsonl(s.agent_session_id, s.cwd)
            except Exception:
                tui_plan_pending = False
        views.append(SessionStatusView(
            id=s.id,
            status=s.status,
            attached_clients=s.attached_clients,
            has_new_output=new_output_map.get(s.id, False),
            is_streaming=False,  # streaming detected via WS, not REST
            is_compacting=is_compacting,
            compacting_progress=compacting_progress,
            scheduled_tasks=task_views,
            tui_hint=tui_hint,
            tui_auq_data=tui_auq_data,
            tui_approve_data=tui_approve_data,
            tui_plan_pending=tui_plan_pending,
        ))
    return SessionStatusListResponse(items=views, total=len(views))


@router.get("/{session_id}")
def get_session(session_id: str, user_id: CurrentUser) -> SessionView:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    return _enrich(session)


@router.get("/{session_id}/prompt-history")
def get_prompt_history(
    session_id: str,
    user_id: CurrentUser,
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None),
) -> dict:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    query = (q or "").strip() or None
    entries = store.list_prompt_history(
        session_id, limit=limit, offset=offset, query=query
    )
    total = store.count_prompt_history(session_id, query=query)
    return {"entries": entries, "total": total}


@router.delete(
    "/{session_id}/prompt-history/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_prompt_history_entry(
    session_id: str,
    entry_id: int,
    user_id: CurrentUser,
) -> None:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    store.delete_prompt_history_entry(session_id, entry_id)


@router.get("/{session_id}/search")
def search_session(
    session_id: str,
    q: str = Query(..., min_length=1),
    user_id: CurrentUser = None,  # type: ignore[assignment]
) -> list[dict]:
    store = _get_store()
    tmux = _get_tmux()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")

    if session.status in (SessionStatus.RUNNING, SessionStatus.DETACHED):
        raw = tmux.capture_full_history(session.tmux_session_name, ansi=False)
    else:
        raw = ""

    if not raw:
        return []

    clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw)
    lines = clean.splitlines()

    results: list[dict] = []
    q_lower = q.lower()
    q_len = len(q)
    for i, line in enumerate(lines):
        line_lower = line.lower()
        start = 0
        while True:
            pos = line_lower.find(q_lower, start)
            if pos == -1:
                break
            ctx_start = max(0, i - 1)
            ctx_end = min(len(lines), i + 2)
            context = "\n".join(lines[ctx_start:ctx_end])
            # Show the matched snippet with surrounding chars
            snip_start = max(0, pos - 20)
            snip_end = min(len(line), pos + q_len + 40)
            snippet = line[snip_start:snip_end].strip()
            results.append({
                "line": i + 1,
                "col": pos + 1,
                "text": snippet,
                "context": context,
            })
            start = pos + q_len
            if len(results) >= 200:
                break
        if len(results) >= 200:
            break
    return results



@router.post("/{session_id}/attach")
def attach_session(session_id: str, body: AttachRequest, user_id: CurrentUser) -> AttachResponse:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if session.status == SessionStatus.TERMINATED:
        raise HTTPException(status_code=409, detail="session terminated")

    if session.status == SessionStatus.DETACHED:
        store.transition(session_id, SessionStatus.RUNNING)

    token = store.issue_ws_token(session_id)
    store.update_attached_clients(session_id, +1)
    store.mark_viewed(session_id, user_id)
    audit_event(user_id, "attach", session_id)
    return AttachResponse(
        session_id=session_id,
        ws_token=token,
        ws_url=public_path(f"/ws/sessions/{session_id}?token={token}"),
        status=session.status,
    )


@router.post("/{session_id}/detach", status_code=status.HTTP_204_NO_CONTENT)
def detach_session(session_id: str, user_id: CurrentUser) -> None:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")

    store.update_attached_clients(session_id, -1)
    updated = store.get(session_id)
    if updated and updated.attached_clients <= 0 and updated.status == SessionStatus.RUNNING:
        store.transition(session_id, SessionStatus.DETACHED)
    audit_event(user_id, "detach", session_id)


@router.post("/{session_id}/terminate", status_code=status.HTTP_204_NO_CONTENT)
def terminate_session(session_id: str, user_id: CurrentUser) -> None:
    store = _get_store()
    tmux = _get_tmux()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if session.status == SessionStatus.TERMINATED:
        return

    if not session.agent_session_id:
        # Try to capture claude_session_id before killing the process.
        # Strategy 1: read the PID file written by our sh wrapper — it exists
        # as soon as sh runs (before exec claude), so it's available quickly.
        # Then wait briefly for Claude to write its session JSON.
        import json as _json
        from pathlib import Path as _Path
        _pid_file = _Path(f"/tmp/claude-inner-{session_id}.pid")
        _resolved = False
        if _pid_file.exists():
            try:
                _pid = int(_pid_file.read_text().strip())
                _sf = _Path.home() / ".claude" / "sessions" / f"{_pid}.json"
                for _ in range(10):  # wait up to 3s for Claude to init
                    if _sf.exists():
                        _data = _json.loads(_sf.read_text())
                        _sid = _data.get("sessionId")
                        if _sid:
                            store.update_agent_session_id(session_id, _sid)
                            _resolved = True
                            try:
                                _pid_file.unlink(missing_ok=True)
                            except OSError:
                                pass
                            break
                    time.sleep(0.3)
            except Exception:
                pass
        # Strategy 2: fall back to descendant-PID scan if pid file not ready
        if not _resolved and tmux.has_session(session.tmux_session_name):
            try:
                claude_sid, proc_pid = tmux.resolve_agent_session_id(session.tmux_session_name, timeout=2)
                if proc_pid:
                    store.update_claude_proc_pid(session_id, proc_pid)
                if claude_sid:
                    store.update_agent_session_id(session_id, claude_sid)
            except Exception:
                pass

    if session.tool == "codex" and session.codex_transport == "app_server":
        # App-server mode never spawned a tmux session — kill the detached
        # codex process and clear pid+port.
        from app.services import codex_appserver_manager as csm
        try:
            csm.stop(session_id, terminate_process=True)
        except Exception:
            pass
        store.update_codex_appserver_endpoint(session_id, None, None)
    else:
        try:
            tmux.terminate(session.tmux_session_name)
        except TmuxError:
            pass
    # The Claude process is now dead — clear the stale PID so the next resume's
    # watchdog doesn't try to read /proc/{dead_pid}/sessions/.
    store.update_claude_proc_pid(session_id, None)
    store.transition(session_id, SessionStatus.TERMINATED)
    if session.agent_session_id:
        _cleanup_session_hooks(session.agent_session_id)
    audit_event(user_id, "terminate", session_id)


@router.post("/{session_id}/resume")
def resume_session(session_id: str, user_id: CurrentUser) -> SessionMetadata:
    store = _get_store()
    tmux = _get_tmux()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if session.status != SessionStatus.TERMINATED:
        raise HTTPException(status_code=409, detail="only terminated sessions can be resumed")

    os.makedirs(session.cwd, exist_ok=True)

    # Codex app-server sessions have no tmux pane — resume means re-spawning the
    # codex subprocess and re-attaching to the previous thread via thread/resume.
    if session.tool == "codex" and session.codex_transport == "app_server":
        from app.services import codex_appserver_manager as csm
        try:
            pid, port = csm.start(
                session_id,
                cwd=session.cwd,
                env=session.env or None,
                model=session.model,
                resume_thread_id=session.agent_session_id or None,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"failed to resume codex app-server: {exc}",
            ) from exc
        store.update_codex_appserver_endpoint(session_id, pid, port)
        # thread/resume preserves the id, but pick it up defensively in case
        # the underlying codex returned a forked id.
        tid = csm.get_thread_id(session_id)
        if tid and tid != session.agent_session_id:
            store.update_agent_session_id(session_id, tid)
        store.transition(session_id, SessionStatus.RUNNING)
        store.reset_attached_clients(session_id)
        audit_event(user_id, "resume", session_id, {"transport": "app_server"})
        return store.get(session_id)  # type: ignore[return-value]

    ts = int(time.time())
    tmux_name = f"claude-{user_id}-{session.project}-{ts}"

    # If we have a stored claude_session_id, hand it to `claude --resume` so the
    # conversation continues where it left off. Otherwise start fresh and let the
    # watchdog discover the new id from ~/.claude/sessions/{pid}.json.
    resume_sid = session.agent_session_id or None

    try:
        tmux.create_session(
            session_name=tmux_name,
            cwd=session.cwd,
            env=session.env or {},
            claude_model=session.model,
            resume_session_id=resume_sid,
            tool=session.tool,
        )
    except TmuxError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    store.update_tmux_session_name(session_id, tmux_name)
    store.transition(session_id, SessionStatus.RUNNING)
    store.reset_attached_clients(session_id)
    audit_event(user_id, "resume", session_id)

    if session.tool == "cursor":
        # Keep existing claude_session_id (cursor chat ID) — --resume reuses same chat
        pass
    else:
        # Old PID is dead — clear it so the watchdog stops querying it and falls
        # back to a tmux scan. claude_session_id is left alone: the watchdog will
        # overwrite it once the new process publishes its pid.json.
        store.update_claude_proc_pid(session_id, None)
        def _resolve():
            claude_sid, proc_pid = tmux.resolve_agent_session_id(tmux_name, timeout=15)
            if proc_pid:
                store.update_claude_proc_pid(session_id, proc_pid)
            if claude_sid:
                store.update_agent_session_id(session_id, claude_sid)
        threading.Thread(target=_resolve, daemon=True).start()

    return store.get(session_id)  # type: ignore[return-value]


class _RewindBody(BaseModel):
    message_uuid: str


@router.post("/{session_id}/rewind")
def rewind_session(session_id: str, body: _RewindBody, user_id: CurrentUser) -> dict:
    """Rewind conversation to a specific user message, restoring file snapshots."""
    import json as _json
    import shutil as _shutil
    from pathlib import Path as _Path
    from app.services.claude_session_reader import _find_session_jsonl

    store = _get_store()
    tmux = _get_tmux()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not session.agent_session_id:
        raise HTTPException(status_code=400, detail="no claude session attached")

    jsonl_path = _find_session_jsonl(session.agent_session_id, session.cwd)
    if not jsonl_path:
        raise HTTPException(status_code=404, detail="session JSONL not found")

    lines = _Path(jsonl_path).read_text().splitlines()
    target = body.message_uuid

    # Find the keep boundary: the file-history-snapshot whose messageId matches the
    # target user message (it records file state at that turn, comes right after the
    # user message line). Keep up to and including it; if no snapshot exists, keep up
    # to and including the user message itself.
    keep_until: int | None = None
    snapshot_backups: dict = {}

    for idx, raw in enumerate(lines):
        try:
            d = _json.loads(raw)
        except Exception:
            continue
        if d.get("uuid") == target:
            keep_until = idx
        if d.get("type") == "file-history-snapshot" and d.get("messageId") == target:
            snapshot_backups = d.get("snapshot", {}).get("trackedFileBackups", {})
            keep_until = idx  # snapshot line comes after user message; update boundary

    if keep_until is None:
        raise HTTPException(status_code=404, detail="message UUID not found in JSONL")

    # Restore files from snapshot
    file_history_dir = _Path.home() / ".claude" / "file-history" / session.agent_session_id
    restored: list[str] = []
    for rel_path, info in snapshot_backups.items():
        if not isinstance(info, dict):
            continue
        backup_name = info.get("backupFileName", "")
        if not backup_name:
            continue
        src = file_history_dir / backup_name
        if not src.exists():
            continue
        dst = _Path(session.cwd) / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        _shutil.copy2(src, dst)
        restored.append(rel_path)

    # Truncate JSONL
    truncated = lines[: keep_until + 1]
    _Path(jsonl_path).write_text("\n".join(truncated) + "\n")

    # Kill running session
    if session.status == SessionStatus.RUNNING:
        try:
            tmux.terminate(session.tmux_session_name)
        except TmuxError:
            pass
        store.transition(session_id, SessionStatus.TERMINATED)

    # Restart with same claude_session_id so Claude reads the truncated JSONL
    ts = int(time.time())
    tmux_name = f"claude-{user_id}-{session.project}-{ts}"
    os.makedirs(session.cwd, exist_ok=True)
    try:
        tmux.create_session(
            session_name=tmux_name,
            cwd=session.cwd,
            env=session.env or {},
            claude_model=session.model,
            resume_session_id=session.agent_session_id,
        )
    except TmuxError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    store.update_tmux_session_name(session_id, tmux_name)
    store.transition(session_id, SessionStatus.RUNNING)
    store.reset_attached_clients(session_id)
    audit_event(user_id, "rewind", session_id)

    # Resolve new inner claude_session_id asynchronously (--resume gives a new ID)
    store.clear_agent_session_id(session_id)
    def _resolve_new_sid():
        sid, proc_pid = tmux.resolve_agent_session_id(tmux_name, timeout=20)
        if proc_pid:
            store.update_claude_proc_pid(session_id, proc_pid)
        if sid:
            store.update_agent_session_id(session_id, sid)
    threading.Thread(target=_resolve_new_sid, daemon=True).start()

    return {"ok": True, "restored_files": restored, "kept_lines": len(truncated)}


class RenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@router.patch("/{session_id}/name")
def rename_session(session_id: str, body: RenameRequest, user_id: CurrentUser) -> SessionMetadata:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    store.update_project(session_id, body.name.strip())
    return store.get(session_id)


class SetModelRequest(BaseModel):
    model: str | None = Field(default=None, max_length=120)


@router.patch("/{session_id}/model")
def set_session_model(session_id: str, body: SetModelRequest, user_id: CurrentUser) -> SessionMetadata:
    store = _get_store()
    tmux = _get_tmux()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if session.status in (SessionStatus.RUNNING, SessionStatus.DETACHED) and body.model:
        try:
            tmux.send_keys(session.tmux_session_name, f"/model {body.model}")
        except TmuxError:
            pass
    store.update_model(session_id, body.model or None)
    return store.get(session_id)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(session_id: str, user_id: CurrentUser) -> None:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if session.status != SessionStatus.TERMINATED:
        raise HTTPException(status_code=409, detail="only terminated sessions can be deleted")
    if session.tool == "codex" and session.codex_transport == "app_server":
        # Belt-and-suspenders: terminate already calls stop(), but if a user
        # deletes a session that crashed without going through terminate,
        # there may still be a live client.
        from app.services import codex_appserver_manager as csm
        try:
            csm.stop(session_id)
        except Exception:
            pass
    store.delete_tasks_for_session(session_id)
    store.delete(session_id)
    audit_event(user_id, "delete", session_id)


# ── Codex app-server transport ────────────────────────────────────────────


class CodexMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)


def _require_codex_appserver(session_id: str, user_id: str) -> SessionMetadata:
    """Common 404/409 guard for the app-server endpoints below."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if session.tool != "codex" or session.codex_transport != "app_server":
        raise HTTPException(
            status_code=400,
            detail="endpoint only valid for codex sessions with app-server transport",
        )
    return session


@router.post("/{session_id}/codex-message")
def post_codex_message(
    session_id: str, body: CodexMessageRequest, user_id: CurrentUser
) -> dict:
    """Send a user message to a codex app-server session."""
    _require_codex_appserver(session_id, user_id)
    from app.services import codex_appserver_manager as csm
    try:
        result = csm.send_user_message(session_id, body.text)
    except KeyError:
        raise HTTPException(status_code=409, detail="codex app-server is not running for this session")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"codex send failed: {exc}") from exc
    return {"ok": True, "result": result}


class CodexAuqResolveRequest(BaseModel):
    answers: dict[str, list[str]] = Field(default_factory=dict)


@router.post("/{session_id}/codex-auq")
def post_codex_auq_resolve(
    session_id: str, body: CodexAuqResolveRequest, user_id: CurrentUser
) -> dict:
    """Resolve a pending tool/requestUserInput in a codex app-server session."""
    _require_codex_appserver(session_id, user_id)
    from app.services import codex_appserver_manager as csm
    try:
        csm.resolve_auq(session_id, body.answers)
    except KeyError:
        raise HTTPException(status_code=409, detail="no pending AUQ for this session")
    return {"ok": True}


class CodexApprovalResolveRequest(BaseModel):
    allow: bool
    feedback: str | None = Field(default=None, max_length=2_000)


@router.post("/{session_id}/codex-approve")
def post_codex_approval_resolve(
    session_id: str, body: CodexApprovalResolveRequest, user_id: CurrentUser
) -> dict:
    """Resolve a pending approval ServerRequest in a codex app-server session."""
    _require_codex_appserver(session_id, user_id)
    from app.services import codex_appserver_manager as csm
    try:
        csm.resolve_approval(session_id, allow=body.allow, feedback=body.feedback)
    except KeyError:
        raise HTTPException(status_code=409, detail="no pending approval for this session")
    return {"ok": True}


# ── Scheduled Tasks ──────────────────────────────────────────────────────────

@router.post("/{session_id}/tasks", status_code=status.HTTP_201_CREATED)
def create_task(session_id: str, body: TaskCreateRequest, user_id: CurrentUser) -> TaskView:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if session.status == SessionStatus.TERMINATED:
        raise HTTPException(status_code=409, detail="session is terminated")

    from datetime import timedelta
    run_at = datetime.now(timezone.utc) + timedelta(seconds=body.delay_seconds)
    task = ScheduledTask(
        session_id=session_id,
        owner_id=user_id,
        command=body.command,
        run_at=run_at,
        loop_seconds=body.loop_seconds,
    )
    store.create_task(task)
    return TaskView(
        id=task.id,
        command=task.command,
        run_at=task.run_at.isoformat(),
        status=task.status,
        created_at=task.created_at.isoformat(),
        loop_seconds=task.loop_seconds,
    )


@router.get("/{session_id}/tasks")
def list_tasks(session_id: str, user_id: CurrentUser) -> list[TaskView]:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    tasks = store.list_tasks_for_session(session_id)
    return [
        TaskView(
            id=t.id,
            command=t.command,
            run_at=t.run_at.isoformat(),
            status=t.status,
            created_at=t.created_at.isoformat(),
            loop_seconds=t.loop_seconds,
        )
        for t in tasks
    ]


@router.delete("/{session_id}/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_task(session_id: str, task_id: str, user_id: CurrentUser) -> None:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    cancelled = store.cancel_task(task_id)
    if not cancelled:
        raise HTTPException(status_code=404, detail="task not found or already completed")


# ── /goal history ─────────────────────────────────────────────────────────────

@router.get("/{session_id}/goals")
def list_goals(session_id: str, user_id: CurrentUser) -> dict:
    """Return active + historical /goal entries scraped from the session JSONL."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not session.agent_session_id:
        return {"active": None, "history": []}
    return read_goals(session.agent_session_id, session.cwd)


# ── Todos (TodoWrite from JSONL) ──────────────────────────────────────────────

@router.get("/{session_id}/todos")
def list_session_todos(session_id: str, user_id: CurrentUser) -> dict:
    """Return TodoWrite snapshots grouped into active + history plans."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not session.agent_session_id:
        return {"active": [], "history": []}
    return get_todo_plans(session.agent_session_id, session.cwd)


# ── AUQ history ───────────────────────────────────────────────────────────────

@router.get("/{session_id}/auqs")
def list_session_auqs(session_id: str, user_id: CurrentUser) -> list[dict]:
    """Return chronologically ordered AskUserQuestion history for this session."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not session.agent_session_id:
        return []
    return list_auqs(session.agent_session_id, session.cwd)


# ── Shell sessions (ephemeral bash terminals) ─────────────────────────────────

# In-memory token store: token → {cwd, user_id}
# No tmux involved: the WebSocket handler spawns bash directly in a PTY so
# xterm.js gets full native scrollback without tmux getting in the way.
_shell_tokens: dict[str, dict] = {}


class ShellResponse(AttachResponse):
    pass


@router.post("/{session_id}/shell")
def open_shell(session_id: str, user_info: CurrentUserInfo) -> AttachResponse:
    """Issue a one-time token for opening a direct bash PTY in the session's cwd."""
    import pathlib
    store = _get_store()
    session = store.get(session_id)
    if session is None or (not user_info.is_admin and session.owner_id != user_info.username):
        raise HTTPException(status_code=404, detail="session not found")

    if not pathlib.Path(session.cwd).is_dir():
        raise HTTPException(status_code=400, detail=f"Directory not found: {session.cwd}")

    token = secrets.token_urlsafe(32)
    _shell_tokens[token] = {"cwd": session.cwd, "user_id": user_info.username, "unrestricted": user_info.is_admin}

    return AttachResponse(
        session_id=token,
        ws_token=token,
        ws_url=public_path(f"/ws/shell?token={token}"),
        status="running",
    )


# ── Git management ────────────────────────────────────────────────────────────

class GitAutoCommitRequest(BaseModel):
    enabled: bool

class GitManualCommitRequest(BaseModel):
    message: str | None = Field(default=None, max_length=200)

class GitRollbackRequest(BaseModel):
    commit_hash: str = Field(min_length=4, max_length=64)

class GitDiffRequest(BaseModel):
    old_hash: str = Field(min_length=4, max_length=64)
    new_hash: str = Field(min_length=4, max_length=64)


@router.get("/{session_id}/pane-history")
def get_pane_history(
    session_id: str,
    lines: int = Query(default=20000, ge=50, le=20000),
    _user: CurrentUser = None,
) -> dict:
    """Return recent pane output from tmux capture-pane with ANSI color codes."""
    import subprocess
    store = _get_store()
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    tmux_name = getattr(session, "tmux_session_name", None)
    if not tmux_name:
        return {"content": ""}
    try:
        r = subprocess.run(
            # -e: preserve escape sequences (colors/styles); -p: plain text output
            ["tmux", "capture-pane", "-p", "-e", "-t", tmux_name, "-S", f"-{lines}"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return {"content": ""}

    # Keep only SGR color/style sequences (\x1b[...m) and strip everything else
    # (cursor movement, clear-screen, alt-screen, charset switches, etc.) so that
    # writing the captured content to a fresh xterm.js terminal doesn't garble layout.
    sgr_re = re.compile(r"\x1b\[[\d;]*m")  # matches \x1b[...m (SGR only)
    esc_re = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-9;]*[A-Za-z]|\][^\x07\x1b]*(?:\x07|\x1b\\))")

    def clean_line(line: str) -> str:
        # Replace all escape sequences with empty string, then re-insert SGR ones
        sgr_tokens = sgr_re.findall(line)
        stripped = esc_re.sub("", line)
        # SGR tokens were removed too — re-attach them in original order
        # by doing a two-pass: first collect positions, then rebuild
        result = []
        pos = 0
        sgr_idx = 0
        for m in sgr_re.finditer(line):
            # text before this SGR token (with non-SGR escapes removed)
            before = esc_re.sub("", line[pos:m.start()])
            result.append(before)
            result.append(m.group())
            pos = m.end()
        result.append(esc_re.sub("", line[pos:]))
        return "".join(result)

    cleaned = "\r\n".join(clean_line(ln) for ln in r.stdout.splitlines())
    return {"content": cleaned}


@router.get("/{session_id}/git")
def get_git_info(session_id: str, user_id: CurrentUser) -> dict:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    # Auto-init if not already a repo (handles sessions created before this feature)
    if not is_git_repo(session.cwd):
        git_init(session.cwd)
    all_log = git_log(session.cwd, n=10000)
    gitignore_path = os.path.join(session.cwd, ".gitignore")
    gitignore_content = ""
    try:
        with open(gitignore_path) as f:
            gitignore_content = f.read()
    except OSError:
        pass
    remote = git_get_remote(session.cwd)
    return {
        "is_repo": True,
        "auto_commit": session.git_auto_commit,
        "log": all_log,
        "gitignore": gitignore_content,
        "remote": remote,
    }


@router.get("/{session_id}/git/search")
def search_git_commits(session_id: str, user_id: CurrentUser, q: str = Query(default="")) -> list:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not q.strip() or not is_git_repo(session.cwd):
        return []
    return git_search_commits(session.cwd, q.strip())


@router.get("/{session_id}/git/branches")
def list_git_branches(session_id: str, user_id: CurrentUser) -> dict:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not is_git_repo(session.cwd):
        return {"current": "", "local": []}
    info = git_list_branches(session.cwd)
    info["dirty"] = git_is_dirty(session.cwd)
    return info


@router.get("/{session_id}/git/graph")
def get_git_graph(
    session_id: str,
    user_id: CurrentUser,
    scope: str = Query(default="current"),
    n: int = Query(default=500, ge=1, le=5000),
) -> list[dict]:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not is_git_repo(session.cwd):
        return []
    return git_graph_log(session.cwd, scope=scope, n=n)


@router.get("/{session_id}/git/active-cwd-sessions")
def list_active_cwd_sessions(session_id: str, user_id: CurrentUser) -> dict:
    """Return RUNNING/DETACHED sessions sharing this session's cwd (excluding self).
    Used by the Revert/checkout confirm dialog to warn about concurrent edits.
    """
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    sessions = store.list_for_owner(user_id)
    target_cwd = os.path.realpath(session.cwd)
    active = []
    for s in sessions:
        if s.id == session_id:
            continue
        if s.status not in (SessionStatus.RUNNING, SessionStatus.DETACHED):
            continue
        try:
            if os.path.realpath(s.cwd) != target_cwd:
                continue
        except OSError:
            continue
        active.append({
            "id": s.id,
            "name": s.name,
            "status": s.status.value,
            "tool": s.tool,
            "last_activity_at": s.last_activity_at.isoformat() if s.last_activity_at else None,
        })
    return {"sessions": active}


class GitCheckoutRequest(BaseModel):
    branch: str = Field(min_length=1, max_length=200)
    stash: bool = False
    # remote=True → fetch origin/<branch> and create a local tracking branch first.
    remote: bool = False


@router.post("/{session_id}/git/checkout")
def checkout_git_branch(
    session_id: str, body: GitCheckoutRequest, user_id: CurrentUser,
) -> dict:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not is_git_repo(session.cwd):
        raise HTTPException(status_code=400, detail="not a git repo")
    if body.remote:
        result = git_checkout_remote_branch(session.cwd, body.branch, stash=body.stash)
    else:
        result = git_checkout_branch(session.cwd, body.branch, stash=body.stash)
    if not result["ok"]:
        if result.get("conflict"):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "conflict",
                    "message": "local changes would be overwritten by checkout; commit or stash them first",
                    "conflicting_files": result.get("conflicting_files", []),
                },
            )
        raise HTTPException(status_code=400, detail=result["output"])
    return {
        "ok": True,
        "branch": body.branch,
        "output": result["output"],
        "stashed": result.get("stashed", False),
    }


@router.post("/{session_id}/git/init")
def init_git_repo(session_id: str, user_id: CurrentUser) -> dict:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    result = git_init(session.cwd)
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result["output"])
    return result


class GitignoreUpdateRequest(BaseModel):
    content: str = Field(max_length=100_000)


@router.put("/{session_id}/git/gitignore")
def update_gitignore(session_id: str, body: GitignoreUpdateRequest, user_id: CurrentUser) -> dict:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    gitignore_path = os.path.join(session.cwd, ".gitignore")
    try:
        with open(gitignore_path, "w") as f:
            f.write(body.content)
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True}


class GitRemoteRequest(BaseModel):
    url: str = Field(max_length=500)


@router.put("/{session_id}/git/remote")
def set_remote(session_id: str, body: GitRemoteRequest, user_id: CurrentUser) -> dict:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    result = git_set_remote(session.cwd, body.url.strip())
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result["output"])
    return {"ok": True, "remote": body.url.strip()}


@router.post("/{session_id}/git/push")
def push_to_remote(session_id: str, user_id: CurrentUser) -> dict:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    remote = git_get_remote(session.cwd)
    if not remote:
        raise HTTPException(status_code=400, detail="no remote configured")
    result = git_push(session.cwd)
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result["output"])
    return {"ok": True, "output": result["output"]}


@router.post("/{session_id}/git/pull")
def pull_from_remote(session_id: str, user_id: CurrentUser) -> dict:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not is_git_repo(session.cwd):
        raise HTTPException(status_code=400, detail="not a git repo")
    result = git_pull(session.cwd)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["output"])
    return {"ok": True, "output": result["output"]}


# ── Merge endpoints (VSCode-style conflict resolution) ─────────────────────

class GitMergeStartRequest(BaseModel):
    source: str = Field(min_length=1, max_length=200)
    target: str = Field(min_length=1, max_length=200)


class GitResolveRequest(BaseModel):
    path: str = Field(min_length=1)
    content: str


class GitMergeContinueRequest(BaseModel):
    message: str | None = None


def _require_session_repo(session_id: str, user_id: str):
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not is_git_repo(session.cwd):
        raise HTTPException(status_code=400, detail="not a git repo")
    return session


@router.get("/{session_id}/git/merge/status")
def get_merge_status(session_id: str, user_id: CurrentUser) -> dict:
    session = _require_session_repo(session_id, user_id)
    return git_merge_status(session.cwd)


@router.get("/{session_id}/git/merge/preview")
def preview_merge(session_id: str, source: str, target: str, user_id: CurrentUser) -> dict:
    session = _require_session_repo(session_id, user_id)
    return git_merge_preview(session.cwd, source, target)


@router.get("/{session_id}/git/merge/file-diff")
def preview_merge_file_diff(
    session_id: str, source: str, target: str, path: str, user_id: CurrentUser,
) -> dict:
    session = _require_session_repo(session_id, user_id)
    return git_merge_file_diff(session.cwd, source, target, path)


@router.post("/{session_id}/git/merge/start")
def start_merge(session_id: str, body: GitMergeStartRequest, user_id: CurrentUser) -> dict:
    session = _require_session_repo(session_id, user_id)
    result = git_merge_start(session.cwd, body.source, body.target)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["output"])
    return result


@router.get("/{session_id}/git/merge/file")
def get_merge_conflict_file(session_id: str, path: str, user_id: CurrentUser) -> dict:
    session = _require_session_repo(session_id, user_id)
    if not (Path(session.cwd) / ".git" / "MERGE_HEAD").exists():
        raise HTTPException(status_code=400, detail="no merge in progress")
    return git_conflict_file_versions(session.cwd, path)


@router.post("/{session_id}/git/merge/resolve")
def resolve_merge_file(session_id: str, body: GitResolveRequest, user_id: CurrentUser) -> dict:
    session = _require_session_repo(session_id, user_id)
    if not (Path(session.cwd) / ".git" / "MERGE_HEAD").exists():
        raise HTTPException(status_code=400, detail="no merge in progress")
    result = git_resolve_file(session.cwd, body.path, body.content)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["output"])
    return {"ok": True, "status": git_merge_status(session.cwd)}


@router.post("/{session_id}/git/merge/continue")
def continue_merge(session_id: str, body: GitMergeContinueRequest, user_id: CurrentUser) -> dict:
    session = _require_session_repo(session_id, user_id)
    result = git_merge_continue(session.cwd, body.message)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["output"])
    return {"ok": True, "output": result["output"]}


@router.post("/{session_id}/git/merge/abort")
def abort_merge(session_id: str, user_id: CurrentUser) -> dict:
    session = _require_session_repo(session_id, user_id)
    result = git_merge_abort(session.cwd)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["output"])
    return {"ok": True, "output": result["output"]}


@router.put("/{session_id}/git/auto-commit")
def set_auto_commit(session_id: str, body: GitAutoCommitRequest, user_id: CurrentUser) -> dict:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if body.enabled and not is_git_repo(session.cwd):
        raise HTTPException(status_code=400, detail="not a git repo; run git init first")
    store.set_git_auto_commit(session_id, body.enabled)
    return {"auto_commit": body.enabled}


@router.post("/{session_id}/git/commit")
def manual_git_commit(session_id: str, body: GitManualCommitRequest, user_id: CurrentUser) -> dict:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not is_git_repo(session.cwd):
        raise HTTPException(status_code=400, detail="not a git repo")
    msg = body.message
    if not msg and session.agent_session_id:
        info = get_latest_turn_info(session.agent_session_id, session.cwd)
        if info["last_summary"]:
            msg = make_commit_message(info["prompts_since"], info["last_summary"])
        else:
            msg = "Manual commit"
    result = git_add_commit(session.cwd, msg or "Manual commit", author=user_id)
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result["output"])
    return result


@router.post("/{session_id}/git/rollback")
def git_rollback_endpoint(session_id: str, body: GitRollbackRequest, user_id: CurrentUser) -> dict:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not is_git_repo(session.cwd):
        raise HTTPException(status_code=400, detail="not a git repo")
    from app.services.git_service import git_rollback
    result = git_rollback(session.cwd, body.commit_hash, author=user_id)
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result["output"])
    return result


@router.get("/{session_id}/git/show/{commit_hash}")
def git_show_endpoint(session_id: str, commit_hash: str, user_id: CurrentUser) -> dict:
    """Return full commit message + per-file old/new content (like git show)."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not is_git_repo(session.cwd):
        raise HTTPException(status_code=400, detail="not a git repo")
    return git_show_commit(session.cwd, commit_hash)


@router.get("/{session_id}/git/file-log")
def git_file_log_endpoint(
    session_id: str,
    path: str = Query(..., max_length=500),
    n: int = Query(default=50, ge=1, le=200),
    user_id: CurrentUser = None,  # type: ignore[assignment]
) -> list[dict]:
    """Return git commit history for a specific file."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not is_git_repo(session.cwd):
        raise HTTPException(status_code=400, detail="not a git repo")
    return git_file_log(session.cwd, path, n=n)


@router.get("/{session_id}/git/file-show")
def git_file_show_endpoint(
    session_id: str,
    path: str = Query(..., max_length=500),
    commit: str = Query(..., max_length=40),
    user_id: CurrentUser = None,  # type: ignore[assignment]
) -> dict:
    """Return file content at a specific commit."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not is_git_repo(session.cwd):
        raise HTTPException(status_code=400, detail="not a git repo")
    # Validate commit hash (alphanumeric only)
    if not commit.replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="invalid commit hash")
    content = git_file_show(session.cwd, path, commit)
    return {"content": content, "commit": commit, "path": path}


@router.get("/{session_id}/git/file-diff")
def git_file_diff_endpoint(
    session_id: str,
    path: str = Query(..., max_length=500),
    commit: str = Query(..., max_length=40),
    user_id: CurrentUser = None,  # type: ignore[assignment]
) -> dict:
    """Return unified diff for a specific file in a specific commit."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not is_git_repo(session.cwd):
        raise HTTPException(status_code=400, detail="not a git repo")
    if not commit.replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="invalid commit hash")
    diff = git_file_diff(session.cwd, path, commit)
    return {"diff": diff, "commit": commit, "path": path}


@router.get("/{session_id}/conversation")
def get_session_conversation(
    session_id: str,
    user_id: CurrentUser,
    from_ts: float = Query(default=0.0, description="Only return turns with ts > from_ts (0 = full load)"),
    tail: int | None = Query(default=None, description="Return only the last N confirmed turns (plus any in-progress pending/streaming)"),
) -> list[dict]:
    """Return user/assistant turns for mobile chat view.

    Pass from_ts (seconds since epoch) to fetch only turns newer than that timestamp.
    Pass tail=N (with from_ts=0) to get only the latest N confirmed turns.
    Each returned turn includes a 'ts' field for use in subsequent incremental requests.
    """
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    adapter = get_adapter(session.tool)
    chat_sid = session.agent_session_id or adapter.find_newest_session_id(session.cwd)
    if not chat_sid:
        return []
    turns = adapter.get_conversation(chat_sid, session.cwd, from_ts=from_ts)
    if tail is not None and tail > 0 and from_ts == 0.0:
        confirmed = [t for t in turns if not t.get("pending") and not t.get("streaming")]
        rest = [t for t in turns if t.get("pending") or t.get("streaming")]
        turns = confirmed[-tail:] + rest
    return turns


@router.get("/{session_id}/conversation/jsonl")
def get_conversation_jsonl_raw(
    session_id: str,
    user_id: CurrentUser,
    page: int = Query(default=0, ge=0),
    page_size: int = Query(default=200, ge=1, le=1000),
):
    """Return paginated lines of the raw JSONL file using head/tail strategy."""
    import math as _math
    import itertools as _itertools
    from collections import deque as _deque
    from app.services.claude_session_reader import _find_session_jsonl
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    adapter = get_adapter(session.tool)
    chat_sid = session.agent_session_id or adapter.find_newest_session_id(session.cwd)
    if not chat_sid:
        raise HTTPException(status_code=404, detail=f"no {session.tool} session")
    jsonl_path = adapter.get_jsonl_path(chat_sid, session.cwd)
    if jsonl_path is None:
        raise HTTPException(status_code=404, detail="jsonl file not found")
    try:
        # Pass 1: count non-empty lines (fast sequential scan)
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
            total = sum(1 for line in f if line.strip())

        # Clamp page to valid range
        if total > 0 and page * page_size >= total:
            page = max(0, _math.ceil(total / page_size) - 1)
        offset = page * page_size

        # Pass 2: read the page efficiently
        if total == 0 or offset >= total:
            page_lines: list[str] = []
        elif offset * 2 <= total:
            # First half: islice forward from start — stops early, O(offset+page_size)
            with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
                non_empty = (line for line in f if line.strip())
                page_lines = list(_itertools.islice(
                    _itertools.islice(non_empty, offset, None),
                    page_size,
                ))
        else:
            # Second half: deque tail — reads whole file but keeps only tail in RAM
            tail_n = total - offset
            with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
                non_empty = (line for line in f if line.strip())
                tail = _deque(non_empty, maxlen=tail_n)
            page_lines = list(_itertools.islice(tail, page_size))

        return {
            "lines": [line.rstrip("\n\r") for line in page_lines],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/{session_id}/available-claude-sessions")
def list_available_claude_sessions(session_id: str, user_id: CurrentUser) -> list[dict]:
    """List all Claude session IDs in the project dir, excluding occupied ones."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")

    all_sessions = get_adapter(session.tool).list_local_sessions(session.cwd)

    # Collect occupied IDs: any session (other than this one) with an agent_session_id
    occupied = store.get_all_agent_session_ids(exclude_session_id=session_id)
    # Also exclude the current session's own agent_session_id from results
    current_sid = session.agent_session_id

    return [
        s for s in all_sessions
        if s["agent_session_id"] not in occupied and s["agent_session_id"] != current_sid
    ]


@router.get("/{session_id}/subagents")
def get_subagents(session_id: str, user_id: CurrentUser) -> list[dict]:
    """List sub-agents for the current Claude session (description, agentId, mtime)."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    chat_sid = session.agent_session_id or find_newest_claude_session_id(session.cwd)
    if not chat_sid:
        return []
    return list_subagents(chat_sid, session.cwd)


@router.get("/{session_id}/subagents/{agent_id}")
def get_subagent_content(
    session_id: str,
    agent_id: str,
    user_id: CurrentUser,
    from_line: int = Query(default=0, ge=0),
) -> dict:
    """Return raw JSONL lines from a sub-agent file, starting at from_line."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    chat_sid = session.agent_session_id or find_newest_claude_session_id(session.cwd)
    if not chat_sid:
        raise HTTPException(status_code=404, detail="no claude session")
    return get_subagent_lines(chat_sid, session.cwd, agent_id, from_line)


class SetAgentSessionIdRequest(BaseModel):
    agent_session_id: str


@router.put("/{session_id}/claude-session-id", status_code=status.HTTP_204_NO_CONTENT)
def set_agent_session_id(session_id: str, body: SetAgentSessionIdRequest, user_id: CurrentUser) -> None:
    """Update the agent_session_id used when resuming this session.

    URL path keeps the historical `/claude-session-id` segment for callers that
    haven't migrated yet; the body field is the new `agent_session_id`.
    """
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    store.update_agent_session_id(session_id, body.agent_session_id)


@router.get("/{session_id}/raw-messages")
def get_raw_messages(
    session_id: str,
    user_id: CurrentUser,
    tail: int = Query(default=100, ge=10, le=2000),
) -> dict:
    """Return the last `tail` JSONL entries.

    Uses mmap + reverse scan so only the last `tail` lines are JSON-parsed.
    Total line count is derived from a fast binary newline scan (no full parse).
    """
    import json as _json
    import mmap as _mmap
    from app.services.claude_session_reader import _find_session_jsonl

    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")

    adapter = get_adapter(session.tool)
    chat_sid = session.agent_session_id or adapter.find_newest_session_id(session.cwd)

    if session.tool == "codex" and chat_sid:
        from app.services.codex_session_reader import get_codex_raw_messages
        return get_codex_raw_messages(chat_sid, session.cwd, tail=tail)

    jsonl_path = adapter.get_jsonl_path(chat_sid, session.cwd) if chat_sid else None

    if jsonl_path is None:
        return {"messages": [], "total": 0}

    try:
        with jsonl_path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return {"messages": [], "total": 0}

            with _mmap.mmap(f.fileno(), 0, access=_mmap.ACCESS_READ) as mm:
                # ── Total line count: fast chunk-based newline scan, no JSON parse ──
                total = 0
                chunk = 65536
                for start in range(0, size, chunk):
                    total += mm[start:start + chunk].count(b"\n")
                # If the file doesn't end with \n, the last line is uncounted
                if mm[size - 1] != 10:  # ord('\n') == 10
                    total += 1

                # ── Reverse scan: parse only last `tail` lines ──
                collected: list = []
                pos = size
                # Skip trailing newlines
                while pos > 0 and mm[pos - 1] == 10:
                    pos -= 1

                while pos > 0 and len(collected) < tail:
                    nl = mm.rfind(b"\n", 0, pos)
                    line_bytes = mm[nl + 1:pos] if nl != -1 else mm[0:pos]
                    pos = nl if nl != -1 else 0
                    line = line_bytes.strip()
                    if line:
                        try:
                            collected.append(_json.loads(line))
                        except _json.JSONDecodeError:
                            pass

                collected.reverse()

                # Claude CLI sometimes appends JSONL entries out of chronological
                # order — most notably the `system/local_command` record for
                # `/compact`, which is flushed only after compaction completes
                # and lands AFTER the `compact_boundary` it triggered (its own
                # `timestamp` is earlier). Do a local stable re-sort by effective
                # timestamp so consumers see chronological order.
                #
                # "Effective timestamp" = the entry's own `timestamp` if present,
                # else the previous entry's effective timestamp (so untimed
                # entries stay glued to their neighbors instead of bubbling).
                from datetime import datetime as _dt

                def _parse_ts(d: object) -> float | None:
                    if not isinstance(d, dict):
                        return None
                    raw = d.get("timestamp")
                    if not isinstance(raw, str) or not raw:
                        return None
                    try:
                        return _dt.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        return None

                if collected:
                    prev_eff = 0.0
                    eff: list[float] = []
                    for d in collected:
                        ts = _parse_ts(d)
                        if ts is None:
                            eff.append(prev_eff)
                        else:
                            eff.append(ts)
                            prev_eff = ts
                    indexed = list(enumerate(collected))
                    indexed.sort(key=lambda pair: (eff[pair[0]], pair[0]))
                    collected = [d for _, d in indexed]

                # Cursor JSONL uses {"role": ...} at top level; transform to Claude format
                # so ConversationPane can render it without changes.
                if session.tool == "cursor":
                    from app.services.cursor_session_reader import _strip_user_query_tags
                    transformed = []
                    for idx, d in enumerate(collected):
                        role = d.get("role")
                        if role not in ("user", "assistant"):
                            continue
                        raw_content = d.get("message", {}).get("content", [])
                        clean_blocks = []
                        for block in (raw_content if isinstance(raw_content, list) else []):
                            if block.get("type") == "text":
                                text = _strip_user_query_tags(block.get("text", ""))
                                if text:
                                    clean_blocks.append({"type": "text", "text": text})
                            else:
                                clean_blocks.append(block)
                        if not clean_blocks:
                            continue
                        transformed.append({
                            "type": role,
                            "uuid": f"cursor-{idx}",
                            "parentUuid": f"cursor-{idx - 1}" if idx > 0 else None,
                            "timestamp": "",
                            "message": {"role": role, "content": clean_blocks},
                        })
                    collected = transformed
                    total = len(transformed)
                elif chat_sid:
                    # Merge in-flight Anthropic-proxy snapshots so the frontend
                    # sees streaming assistant content before the CLI flushes
                    # the JSONL line. Snapshots live in
                    # ~/.claude/cached_messages/{csid}/{ts_ns}.json and are
                    # written by tools/anthropic_proxy/server.py. Dedup against
                    # JSONL by message.id; for one in-flight request we keep
                    # only the latest snapshot. The cleanup loop in
                    # app/services/proxy_tap_cleanup.py drops snapshots whose
                    # request_id has been written into JSONL.
                    from pathlib import Path as _Path
                    cache_dir = _Path.home() / ".claude" / "cached_messages" / chat_sid
                    if cache_dir.is_dir():
                        seen_ids: set[str] = set()
                        # Track the max JSONL timestamp; any snapshot whose ts_ns
                        # is already ≤ this is "stale" — the CLI has flushed past
                        # it, even though its request_id never landed in
                        # message.id. The canonical case is /compact: the
                        # compact assistant response IS captured by the proxy,
                        # but the CLI writes a `compact_boundary` + synthetic
                        # continuation user prompt instead of a normal assistant
                        # entry, so dedup-by-message.id alone leaves the
                        # snapshot stranded until proxy_tap_cleanup catches up
                        # (~30s). The timestamp filter drops it immediately.
                        max_ts_ns: int = 0
                        for entry in collected:
                            if not isinstance(entry, dict):
                                continue
                            if entry.get("type") == "assistant":
                                msg = entry.get("message")
                                if isinstance(msg, dict):
                                    mid = msg.get("id")
                                    if isinstance(mid, str):
                                        seen_ids.add(mid)
                            raw_ts = entry.get("timestamp")
                            if isinstance(raw_ts, str) and raw_ts:
                                try:
                                    ts_s = _dt.fromisoformat(raw_ts.replace("Z", "+00:00")).timestamp()
                                    ts_ns_i = int(ts_s * 1_000_000_000)
                                    if ts_ns_i > max_ts_ns:
                                        max_ts_ns = ts_ns_i
                                except ValueError:
                                    pass
                        latest_by_req: dict[str, tuple[int, dict]] = {}
                        for snap_path in cache_dir.iterdir():
                            name = snap_path.name
                            if not name.endswith(".json") or name.startswith("."):
                                continue
                            try:
                                ts_ns = int(name[:-5])
                            except ValueError:
                                continue
                            if ts_ns <= max_ts_ns:
                                continue
                            try:
                                snap = _json.loads(snap_path.read_text())
                            except (OSError, _json.JSONDecodeError):
                                continue
                            req_id = snap.get("request_id")
                            if not isinstance(req_id, str) or req_id in seen_ids:
                                continue
                            cur = latest_by_req.get(req_id)
                            if cur is None or ts_ns > cur[0]:
                                latest_by_req[req_id] = (ts_ns, snap)
                        if latest_by_req:
                            from datetime import timezone as _tz
                            synth: list = []
                            last_real_uuid = collected[-1].get("uuid") if collected else None
                            for ts_ns, snap in latest_by_req.values():
                                iso = _dt.fromtimestamp(ts_ns / 1_000_000_000, tz=_tz.utc).isoformat().replace("+00:00", "Z")
                                synth.append({
                                    "type": "assistant",
                                    "uuid": f"tap-{snap.get('request_id')}",
                                    "parentUuid": last_real_uuid,
                                    "timestamp": iso,
                                    "sessionId": chat_sid,
                                    "_synthetic": True,
                                    "_snapshot_kind": snap.get("kind", "snapshot"),
                                    "message": {
                                        "role": "assistant",
                                        "id": snap.get("request_id"),
                                        "content": snap.get("content") or [],
                                    },
                                })
                            synth.sort(key=lambda d: d["timestamp"])
                            collected.extend(synth)

                return {"messages": collected, "total": total}

    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/{session_id}/raw-messages/all")
def get_raw_messages_all(
    session_id: str,
    user_id: CurrentUser,
) -> dict:
    """Return every JSONL entry, unbounded. Used by Chat export (no tail cap).

    Forward-scans the whole file; the standard `/raw-messages` endpoint is
    capped at 2000 for the live UI and uses reverse-scan instead.
    """
    import json as _json
    from app.services.claude_session_reader import _find_session_jsonl

    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")

    adapter = get_adapter(session.tool)
    chat_sid = session.agent_session_id or adapter.find_newest_session_id(session.cwd)

    if session.tool == "codex" and chat_sid:
        from app.services.codex_session_reader import get_codex_raw_messages
        return get_codex_raw_messages(chat_sid, session.cwd, tail=None)

    jsonl_path = adapter.get_jsonl_path(chat_sid, session.cwd) if chat_sid else None

    if jsonl_path is None:
        return {"messages": [], "total": 0}

    try:
        collected: list = []
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    collected.append(_json.loads(line))
                except _json.JSONDecodeError:
                    pass
        total = len(collected)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # Reorder so out-of-order entries (e.g. /compact's local_command flushed
    # after compact_boundary) appear chronologically. Mirrors the logic in
    # `/raw-messages`.
    from datetime import datetime as _dt

    def _parse_ts(d: object) -> float | None:
        if not isinstance(d, dict):
            return None
        raw = d.get("timestamp")
        if not isinstance(raw, str) or not raw:
            return None
        try:
            return _dt.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    if collected:
        prev_eff = 0.0
        eff: list[float] = []
        for d in collected:
            ts = _parse_ts(d)
            if ts is None:
                eff.append(prev_eff)
            else:
                eff.append(ts)
                prev_eff = ts
        indexed = list(enumerate(collected))
        indexed.sort(key=lambda pair: (eff[pair[0]], pair[0]))
        collected = [d for _, d in indexed]

    if session.tool == "cursor":
        from app.services.cursor_session_reader import _strip_user_query_tags
        transformed = []
        for idx, d in enumerate(collected):
            role = d.get("role")
            if role not in ("user", "assistant"):
                continue
            raw_content = d.get("message", {}).get("content", [])
            clean_blocks = []
            for block in (raw_content if isinstance(raw_content, list) else []):
                if block.get("type") == "text":
                    text = _strip_user_query_tags(block.get("text", ""))
                    if text:
                        clean_blocks.append({"type": "text", "text": text})
                else:
                    clean_blocks.append(block)
            if not clean_blocks:
                continue
            transformed.append({
                "type": role,
                "uuid": f"cursor-{idx}",
                "parentUuid": f"cursor-{idx - 1}" if idx > 0 else None,
                "timestamp": "",
                "message": {"role": role, "content": clean_blocks},
            })
        collected = transformed
        total = len(transformed)

    return {"messages": collected, "total": total}


class AuqAnswerItem(BaseModel):
    option_idx: int | None = None
    option_indices: list[int] | None = None
    n_options: int = Field(default=0, ge=0)
    custom_text: str | None = None
    is_multi: bool = False


class AuqAnswerRequest(BaseModel):
    answers: list[AuqAnswerItem]
    # Index of "Submit Answers" option in the final confirmation question that
    # Claude Code shows after all questions are answered (multi-question only).
    # None = no confirmation step (single question).
    submit_confirm_idx: int | None = Field(default=None, ge=0)


@router.post("/{session_id}/answer-auq")
def answer_auq(session_id: str, body: AuqAnswerRequest, user_id: CurrentUser) -> dict:
    """Submit structured answers to a Claude Code AskUserQuestion Ink TUI."""
    store = _get_store()
    tmux = _get_tmux()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")

    pane = f"{session.tmux_session_name}:0.0"

    for i, answer in enumerate(body.answers):
        if i > 0:
            time.sleep(1.2)  # wait for Ink TUI to render next question

        if answer.custom_text is not None:
            # Navigate to "Type something" first, wait for Ink to re-render and activate
            # the text input, THEN send the text. Sending nav+text atomically causes the
            # text to arrive before Ink activates the field and the characters get lost.
            nav = "\x1b[B" * answer.n_options
            text = answer.custom_text.strip()
            if nav:
                tmux._run("send-keys", "-t", pane, "-l", nav)
            time.sleep(0.2)  # wait for Ink to render text input at "Type something"
            if text:
                tmux._run("send-keys", "-t", pane, "-l", "--", text)
            if answer.is_multi:
                # Multi-select: navigate from "Type something" (n_options) to Submit/Next
                # (n_options+1) so the generic final Enter below hits Submit.
                time.sleep(0.1)
                tmux._run("send-keys", "-t", pane, "-l", "\x1b[B")
        elif answer.option_indices:
            # Multi-select: navigate to each option, Enter to select it (cursor stays),
            # then navigate to the Submit/Next button and leave final Enter to generic step.
            # Use separate send-keys calls with a short pause so Ink processes each selection.
            cur_pos = 0
            for idx in sorted(answer.option_indices):
                down_count = idx - cur_pos
                if down_count > 0:
                    tmux._run("send-keys", "-t", pane, "-l", "\x1b[B" * down_count)
                tmux._run("send-keys", "-t", pane, "Enter")  # select this option
                time.sleep(0.15)                              # let Ink register the selection
                cur_pos = idx
            # Navigate from last selected position to Submit/Next button (n_options+1)
            submit_pos = answer.n_options + 1
            down_to_submit = submit_pos - cur_pos
            if down_to_submit > 0:
                tmux._run("send-keys", "-t", pane, "-l", "\x1b[B" * down_to_submit)
        elif answer.option_idx is not None and answer.option_idx > 0:
            # Single-select: navigate Down × N
            nav = "\x1b[B" * answer.option_idx
            tmux._run("send-keys", "-t", pane, "-l", nav)

        # Confirm with Enter
        tmux._run("send-keys", "-t", pane, "Enter")

    # Final "Submit Answers" confirmation question (multi-question only).
    # Claude Code shows an extra Q after all questions asking whether to submit;
    # "Submit Answers" is typically at option index 1.
    if body.submit_confirm_idx is not None:
        time.sleep(1.2)
        nav = "\x1b[B" * body.submit_confirm_idx
        if nav:
            tmux._run("send-keys", "-t", pane, "-l", nav)
        tmux._run("send-keys", "-t", pane, "Enter")

    return {"ok": True}


class _AuqSubmitBody(BaseModel):
    answers: list  # list of str | list[str], one per question
    questions: list[dict] = []
    single_shot: bool = False  # True when caller handles one question at a time (skip final Enter)


@router.post("/{session_id}/auq/submit")
def auq_submit(session_id: str, body: _AuqSubmitBody, user_id: CurrentUser) -> dict:
    """Frontend submits AUQ answers via tmux send-keys."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")

    tmux = _get_tmux()
    pane = f"{session.tmux_session_name}:0.0"
    questions = body.questions

    for i, answer in enumerate(body.answers):
        if i > 0:
            time.sleep(1.2)

        q_def = questions[i] if i < len(questions) else {}
        raw_opts = q_def.get("options", [])
        option_labels: list[str] = [
            opt["label"] if isinstance(opt, dict) else str(opt) for opt in raw_opts
        ]
        n_opts = len(option_labels)
        is_multi = q_def.get("multiSelect", False)

        if isinstance(answer, list):
            # Multi-select: explicit action list, one entry per TUI row in order.
            # Each action: {"type": "option", "click": bool}
            #              {"type": "type_something", "value": str}
            #              {"type": "submit"}
            import logging as _log
            _log.warning("[AUQ] answer=%r", answer)
            for ai, action in enumerate(answer):
                atype = action.get("type") if isinstance(action, dict) else None
                if atype == "option":
                    if action.get("click"):
                        _log.warning("[AUQ] ai=%d option→Enter", ai)
                        tmux._run("send-keys", "-t", pane, "Enter")
                        time.sleep(0.3)
                elif atype == "type_something":
                    value = (action.get("value") or "").strip()
                    if value:
                        _log.warning("[AUQ] ai=%d type_something→Enter+type %r", ai, value)
                        tmux._run("send-keys", "-t", pane, "Enter")  # activate text input
                        time.sleep(0.2)
                        tmux._run("send-keys", "-t", pane, "-l", value)
                        time.sleep(0.3)
                elif atype == "submit":
                    _log.warning("[AUQ] ai=%d submit→Enter", ai)
                    tmux._run("send-keys", "-t", pane, "Enter")
                    break
                if ai < len(answer) - 1:
                    _log.warning("[AUQ] ai=%d →Down", ai)
                    tmux._run("send-keys", "-t", pane, "Down")
                    time.sleep(0.15)
            _log.warning("[AUQ] inner loop done, continue outer")
            continue  # Submit Enter already sent above; skip common per-answer Enter
        elif isinstance(answer, str) and answer not in option_labels:
            # Custom text
            nav = "\x1b[B" * n_opts
            if nav:
                tmux._run("send-keys", "-t", pane, "-l", nav)
                time.sleep(0.2)
            if answer.strip():
                tmux._run("send-keys", "-t", pane, "-l", answer.strip())
                time.sleep(0.1)
            if is_multi:
                tmux._run("send-keys", "-t", pane, "-l", "\x1b[B")
                time.sleep(0.1)
        else:
            # Single-select
            try:
                idx = option_labels.index(answer) if answer else 0
            except ValueError:
                idx = 0
            for _ in range(idx):
                tmux._run("send-keys", "-t", pane, "Down")
                time.sleep(0.15)
            if idx > 0:
                time.sleep(0.1)

        tmux._run("send-keys", "-t", pane, "Enter")

    if body.single_shot:
        # single_shot mode (pending AUQ: one question at a time).
        # After answering, check what the TUI shows:
        # - "Submit Answers" means this was the last question in a multi-question AUQ → confirm it.
        # - A new question (☐ header) means more questions follow → don't send Enter (polling handles it).
        time.sleep(1.0)
        try:
            screen = tmux._run("capture-pane", "-p", "-t", session.tmux_session_name)
            if screen and "Submit Answers" in screen and "☐" not in screen:
                tmux._run("send-keys", "-t", pane, "Enter")
        except Exception:
            pass
    else:
        # Batch mode: final Enter confirms "Submit Answers" for multi-question AUQs.
        time.sleep(1.2)
        tmux._run("send-keys", "-t", pane, "Enter")

    return {"ok": True, "via": "send-keys"}


class _ToolApproveBody(BaseModel):
    decision: str  # "allow" | "deny"


@router.post("/{session_id}/tool-approve")
def tool_approve(session_id: str, body: _ToolApproveBody, user_id: CurrentUser) -> dict:
    """Approve or deny a pending tool permission request from Chat mode."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    tmux = _get_tmux()
    pane = f"{session.tmux_session_name}:0.0"
    if body.decision == "allow":
        # Claude Code approve dialog: press "y" then Enter
        tmux._run("send-keys", "-t", pane, "y")
        time.sleep(0.05)
        tmux._run("send-keys", "-t", pane, "Enter")
    else:
        # Deny: press "n" then Enter (or Escape)
        tmux._run("send-keys", "-t", pane, "n")
        time.sleep(0.05)
        tmux._run("send-keys", "-t", pane, "Enter")
    return {"ok": True}


class _PlanApproveBody(BaseModel):
    decision: str  # "approve" | "reject"


@router.post("/{session_id}/plan-approve")
def plan_approve(session_id: str, body: _PlanApproveBody, user_id: CurrentUser) -> dict:
    """Resolve a pending ExitPlanMode modal in the Claude CLI TUI.

    The modal shows a 4-option list:
        1. Yes, and bypass permissions
        2. Yes, manually approve edits
        3. No, refine with Ultraplan on Claude Code on the web
        4. Tell Claude what to change

    Approve maps to option 2 (manual approve preserves ClaudeManager's own
    per-tool permission UI). Reject maps to option 4 (asks Claude to redo);
    we submit it with no inline feedback — empty Enter confirms.

    Prior behavior routed Approve/Reject text through the normal chat
    `sendPrompt` path, which paste-buffer'd "Approved" / "Rejected" into
    the modal. Those strings match no option, but the trailing Enter
    submitted the default-highlighted option 1 — so Reject was silently
    approving the plan with bypassed permissions. Mirrors the
    `tool_approve` endpoint above; structured key sequence rather than
    free text.
    """
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    tmux = _get_tmux()
    pane = f"{session.tmux_session_name}:0.0"
    if body.decision == "approve":
        # option 2 — modal opens with option 1 highlighted, Down × 1 → Enter
        tmux._run("send-keys", "-t", pane, "Down")
        time.sleep(0.15)
        tmux._run("send-keys", "-t", pane, "Enter")
    elif body.decision == "reject":
        # option 4 — Down × 3 → Enter. The option may open a feedback
        # input; we don't pass any, so a follow-up Enter submits empty.
        for _ in range(3):
            tmux._run("send-keys", "-t", pane, "Down")
            time.sleep(0.15)
        tmux._run("send-keys", "-t", pane, "Enter")
        time.sleep(0.3)
        tmux._run("send-keys", "-t", pane, "Enter")
    else:
        raise HTTPException(status_code=400, detail="decision must be 'approve' or 'reject'")
    return {"ok": True}


@router.post("/{session_id}/git/diff")
def git_diff_endpoint(session_id: str, body: GitDiffRequest, user_id: CurrentUser) -> dict:
    """Return per-file diff between two commits: {files: [{path, old_content, new_content}]}."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not is_git_repo(session.cwd):
        raise HTTPException(status_code=400, detail="not a git repo")
    from app.services.git_service import git_diff_files, git_file_at_commit
    changed = git_diff_files(session.cwd, body.old_hash, body.new_hash)
    files = []
    for path in changed[:50]:  # cap at 50 files
        old_content = git_file_at_commit(session.cwd, body.old_hash, path)
        new_content = git_file_at_commit(session.cwd, body.new_hash, path)
        files.append({"path": path, "old_content": old_content, "new_content": new_content})
    return {"files": files, "old_hash": body.old_hash, "new_hash": body.new_hash}
