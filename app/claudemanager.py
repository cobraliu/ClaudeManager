from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth as auth_api
from app.api import claude_caps_api
from app.api import code_api
from app.api import config_api
from app.api import files as files_api
from app.api import fs as fs_api
from app.api import sessions as sessions_api
from app.api import usage_api
from app.config import (
    get_claude_bin,
    get_claude_shell,
    get_cursor_bin,
    get_default_admin,
    get_session_db_path,
    get_term_idle_grace_seconds,
    get_term_standby_grace_seconds,
)
from app.services.git_service import git_add_commit, git_is_dirty, is_git_repo, make_commit_message
from app.services.claude_session_reader import find_newest_claude_session_id, get_latest_turn_info
from app.services.cursor_session_reader import find_newest_cursor_session_id
from app.models.user import UserRole
from app.models.session import SessionStatus
from app.observability import configure_logging
from app.services.bash_term_service import TerminalManager
from app.services.proxy_tap_cleanup import run_once as proxy_tap_cleanup_once
from app.services.session_store import SessionStore
from app.services.tmux_service import TmuxService
from app.services.user_store import UserStore
from app.ws import terminal_ws, shell_ws, term_ws
from app.api import terminals as terminals_api

# Init stores (both share the same database file)
_db_path = get_session_db_path()
user_store = UserStore(_db_path)
session_store = SessionStore(db_path=_db_path)
if os.environ.get("CLAUDE_PROXY_DISABLED", "").strip() in ("1", "true", "True", "yes"):
    _anthropic_proxy_port = 0
else:
    _anthropic_proxy_port = int(os.environ.get("CLAUDE_PROXY_PORT", "19098"))
tmux = TmuxService(
    claude_bin=get_claude_bin(),
    claude_shell=get_claude_shell(),
    cursor_bin=get_cursor_bin(),
    anthropic_proxy_port=_anthropic_proxy_port,
)
term_mgr = TerminalManager(
    tmux,
    idle_grace_getter=lambda: float(get_term_idle_grace_seconds()),
    standby_grace_getter=lambda: float(get_term_standby_grace_seconds()),
    # Persist alongside data.db so terminals survive a backend restart as long
    # as their tmux session is still alive.
    persist_path=_db_path.parent / "terms.json",
)
# Reconcile persisted records with live cmterm-* tmux sessions: restore the
# matches and reap the rest.
try:
    term_mgr.reap_orphan_tmux_sessions()
except Exception:
    pass

# Create default admin if no users exist
if user_store.is_empty():
    admin_cfg = get_default_admin()
    user_store.create_user(admin_cfg["username"], admin_cfg["password"], UserRole.ADMIN)
    user_store.set_is_admin(admin_cfg["username"], True)

# Sync session state with actual tmux processes on startup
for s in session_store.all():
    if s.status in (SessionStatus.RUNNING, SessionStatus.CREATING, SessionStatus.DETACHED):
        if not tmux.has_session(s.tmux_session_name):
            # tmux session gone – mark terminated
            try:
                session_store.force_status(s.id, SessionStatus.TERMINATED)
            except Exception:
                pass
        elif s.status == SessionStatus.CREATING:
            try:
                session_store.force_status(s.id, SessionStatus.RUNNING)
            except Exception:
                pass
    # Reset attached_clients on restart (no live WS connections)
    if s.attached_clients > 0:
        session_store.reset_attached_clients(s.id)

# Wire up modules
auth_api.configure(user_store)
sessions_api.configure(session_store, tmux)
files_api.configure(session_store)
code_api.configure(session_store)
terminal_ws.configure(session_store, tmux)
shell_ws.configure(tmux, sessions_api._shell_tokens)
term_ws.configure(tmux, term_mgr)
terminals_api.configure(session_store, term_mgr)

configure_logging()


def _startup_ensure_bypass_accepted() -> None:
    """At server startup: if skipDangerousModePermissionPrompt is explicitly False,
    launch a throwaway Claude session, accept the bypass-permissions dialog, then
    kill the session. No-op if the flag is already True or missing (missing is handled
    per-session by _ensure_bypass_accepted in sessions.py).
    """
    sf = Path.home() / ".claude" / "settings.json"
    try:
        settings: dict = {}
        if sf.exists():
            settings = json.loads(sf.read_text())
        if settings.get("skipDangerousModePermissionPrompt") is True:
            return  # already accepted
    except (json.JSONDecodeError, OSError):
        pass  # treat as not accepted, proceed

    session_name = f"bypass-init-{int(time.time())}"
    claude_bin = shlex.quote(get_claude_bin())
    logger.info("skipDangerousModePermissionPrompt is False — launching throwaway session to accept dialog")

    try:
        tmux._run(
            "new-session", "-d", "-s", session_name,
            f"{claude_bin} --dangerously-skip-permissions",
        )

        deadline = time.monotonic() + 15.0
        accepted = False
        while time.monotonic() < deadline:
            time.sleep(0.4)
            try:
                screen = tmux.capture_visible_screen(session_name)
                if "Yes, I accept" in screen and "No, exit" in screen:
                    lines = screen.splitlines()
                    cursor_idx = next((i for i, l in enumerate(lines) if "❯" in l), None)
                    accept_idx = next((i for i, l in enumerate(lines) if "Yes, I accept" in l), None)
                    if cursor_idx is not None and accept_idx is not None:
                        diff = accept_idx - cursor_idx
                        key = "Down" if diff > 0 else "Up"
                        for _ in range(abs(diff)):
                            tmux._run("send-keys", "-t", session_name, key)
                            time.sleep(0.05)
                    time.sleep(0.15)
                    tmux._run("send-keys", "-t", session_name, "Enter")
                    accepted = True
                    time.sleep(1.0)  # let Claude write settings.json
                    break
            except Exception:
                pass

        if accepted:
            logger.info("bypass permissions accepted at startup")
        else:
            logger.warning("bypass permissions dialog not detected within 15s — skipping")
    except Exception as exc:
        logger.warning("startup bypass-accept failed: %s", exc)
    finally:
        try:
            tmux._run("kill-session", "-t", session_name)
        except Exception:
            pass


_startup_ensure_bypass_accepted()


def _startup_ensure_hook_injected() -> None:
    """Ensure the PreToolUse auq-hook.py is configured in ~/.claude/settings.json.

    Writes the hook script if missing and adds the PreToolUse entry to settings.
    Idempotent — safe to call on every startup.
    """
    hook_script = Path.home() / ".claude" / "auq-hook.py"
    settings_file = Path.home() / ".claude" / "settings.json"
    hook_command = f"python3 {hook_script}"

    # Write hook script if it doesn't exist or is outdated
    hook_source = (
        '#!/usr/bin/env python3\n'
        '"""PreToolUse hook: write tool events to per-session files under ~/.claude_manager/hooks/."""\n'
        'import json, sys\n'
        'from pathlib import Path\n'
        '\n'
        'try:\n'
        '    data = json.load(sys.stdin)\n'
        '    sid = data.get("session_id", "")\n'
        '    if sid:\n'
        '        hooks_dir = Path.home() / ".claude_manager" / "hooks"\n'
        '        hooks_dir.mkdir(parents=True, exist_ok=True)\n'
        '        hook_file = hooks_dir / f"{sid}.jsonl"\n'
        '        with open(hook_file, "a") as f:\n'
        '            f.write(json.dumps(data, ensure_ascii=False) + "\\n")\n'
        'except Exception:\n'
        '    pass\n'
        'sys.exit(0)\n'
    )
    try:
        if not hook_script.exists() or hook_script.read_text() != hook_source:
            hook_script.write_text(hook_source)
            hook_script.chmod(0o755)
            logger.info("auq-hook.py written to %s", hook_script)
    except OSError as e:
        logger.warning("failed to write auq-hook.py: %s", e)
        return

    # Inject PreToolUse entry into settings.json if not already present
    try:
        settings: dict = {}
        if settings_file.exists():
            settings = json.loads(settings_file.read_text())

        hooks = settings.setdefault("hooks", {})
        pre_tool_use: list = hooks.setdefault("PreToolUse", [])

        # Check if our hook command is already registered in any entry
        for entry in pre_tool_use:
            for h in entry.get("hooks", []):
                if h.get("command", "") == hook_command:
                    return  # already injected

        our_hook = {
            "type": "command",
            "command": hook_command,
            "timeout": 5000,
            "async": True,
        }
        # Merge into existing no-matcher (catch-all) entry if one exists,
        # otherwise append a new entry.
        catch_all = next((e for e in pre_tool_use if not e.get("matcher")), None)
        if catch_all is not None:
            catch_all.setdefault("hooks", []).append(our_hook)
        else:
            pre_tool_use.append({"hooks": [our_hook]})
        settings_file.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
        logger.info("PreToolUse auq-hook injected into %s", settings_file)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("failed to inject auq-hook into settings.json: %s", e)


_startup_ensure_hook_injected()


def _startup_ensure_precompact_hook_injected() -> None:
    """Ensure the PreCompact hook is configured in ~/.claude/settings.json.

    The hook drops a marker file at ~/.claude_manager/compact_state/<sid>.json
    when /compact starts.  /status reads it together with the session JSONL
    (compact_boundary entry) to flag is_compacting=True even when the user
    isn't watching the TUI.  Idempotent — safe to call on every startup.
    """
    hook_script = Path.home() / ".claude" / "precompact-hook.py"
    settings_file = Path.home() / ".claude" / "settings.json"
    hook_command = f"python3 {hook_script}"

    hook_source = (
        '#!/usr/bin/env python3\n'
        '"""PreCompact hook: write a marker file so ClaudeManager can detect '
        'compaction-in-progress even before the TUI percentage renders."""\n'
        'import json, sys, time\n'
        'from pathlib import Path\n'
        '\n'
        'try:\n'
        '    data = json.load(sys.stdin)\n'
        '    sid = data.get("session_id", "")\n'
        '    if sid:\n'
        '        d = Path.home() / ".claude_manager" / "compact_state"\n'
        '        d.mkdir(parents=True, exist_ok=True)\n'
        '        marker = d / f"{sid}.json"\n'
        '        marker.write_text(json.dumps({\n'
        '            "started_at_epoch": time.time(),\n'
        '            "trigger": data.get("trigger", ""),\n'
        '        }))\n'
        'except Exception:\n'
        '    pass\n'
        'sys.exit(0)\n'
    )
    try:
        if not hook_script.exists() or hook_script.read_text() != hook_source:
            hook_script.write_text(hook_source)
            hook_script.chmod(0o755)
            logger.info("precompact-hook.py written to %s", hook_script)
    except OSError as e:
        logger.warning("failed to write precompact-hook.py: %s", e)
        return

    try:
        settings: dict = {}
        if settings_file.exists():
            settings = json.loads(settings_file.read_text())

        hooks = settings.setdefault("hooks", {})
        pre_compact: list = hooks.setdefault("PreCompact", [])

        for entry in pre_compact:
            for h in entry.get("hooks", []):
                if h.get("command", "") == hook_command:
                    return  # already injected

        our_hook = {
            "type": "command",
            "command": hook_command,
            "timeout": 5000,
            "async": True,
        }
        catch_all = next((e for e in pre_compact if not e.get("matcher")), None)
        if catch_all is not None:
            catch_all.setdefault("hooks", []).append(our_hook)
        else:
            pre_compact.append({"hooks": [our_hook]})
        settings_file.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
        logger.info("PreCompact hook injected into %s", settings_file)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("failed to inject precompact-hook into settings.json: %s", e)


_startup_ensure_precompact_hook_injected()

# In-memory: session_id → timestamp (float) of the last turn_duration that triggered a commit.
# Starts empty on every process start; first watchdog scan sets the baseline without committing.
_last_committed_turn_ts: dict[str, float] = {}
_last_stored_turn_ts: dict[str, float] = {}  # session_id -> turn_ts already written to DB


def _baseline_file(session_id: str, cwd: str) -> Path:
    """Persistent baseline stored inside .git — survives backend restarts."""
    return Path(cwd) / ".git" / f"claude_autocommit_{session_id}"


def _read_baseline(session_id: str, cwd: str) -> float | None:
    try:
        return float(_baseline_file(session_id, cwd).read_text().strip())
    except (OSError, ValueError):
        return None


def _write_baseline(session_id: str, cwd: str, ts: float) -> None:
    try:
        _baseline_file(session_id, cwd).write_text(str(ts))
    except OSError:
        pass


def _try_resolve_claude_sid_from_files(cwd: str) -> str | None:
    """Try to find a Claude session ID from ~/.claude/projects/{encoded_cwd}/."""
    from pathlib import Path
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.is_dir():
        return None
    # Claude encodes cwd: /mnt/hdd2/foo_bar → -mnt-hdd2-foo-bar
    # Replaces / with -, replaces _ with -, keeps leading -
    encoded = cwd.replace("/", "-").replace("_", "-")
    project_dir = projects_dir / encoded
    if not project_dir.is_dir():
        # Fallback: try without underscore replacement
        encoded_alt = cwd.replace("/", "-")
        project_dir = projects_dir / encoded_alt
    if not project_dir.is_dir():
        return None
    # Find the most recent .jsonl file (name = session ID)
    best_sid = None
    best_mtime = 0.0
    for f in project_dir.iterdir():
        if f.suffix == ".jsonl" and f.is_file():
            mt = f.stat().st_mtime
            if mt > best_mtime:
                best_mtime = mt
                best_sid = f.stem
    return best_sid


def _sync_turn_ts(s) -> None:
    """Detect new turn_duration in JSONL and persist it to last_turn_at in the DB.
    Runs for all sessions regardless of git_auto_commit setting."""
    if not s.agent_session_id:
        return
    if s.tool == "cursor":
        # Cursor has no turn markers; use file mtime as a proxy
        from app.services.cursor_session_reader import _find_jsonl as _find_cursor_jsonl
        p = _find_cursor_jsonl(s.agent_session_id, s.cwd)
        if p is None:
            return
        latest_ts = p.stat().st_mtime
    else:
        info = get_latest_turn_info(s.agent_session_id, s.cwd, since_ts=0.0)
        latest_ts = info["turn_ts"]
        if latest_ts == 0.0:
            return
    if _last_stored_turn_ts.get(s.id) == latest_ts:
        return  # already written this value
    session_store.update_last_turn_at(s.id, latest_ts)
    _last_stored_turn_ts[s.id] = latest_ts


def _maybe_auto_commit(s) -> None:
    """Trigger a git commit when a new turn_duration appears in the session JSONL.

    Baseline turn_ts is persisted to .git/claude_autocommit_<session_id> so it
    survives backend restarts — no spurious skips after restart.
    """
    if not is_git_repo(s.cwd):
        return
    if not s.agent_session_id:
        return

    prev_ts = _last_committed_turn_ts.get(s.id)

    # On first in-memory lookup, restore persisted baseline from .git dir
    if prev_ts is None:
        persisted = _read_baseline(s.id, s.cwd)
        if persisted is not None:
            prev_ts = persisted
            _last_committed_turn_ts[s.id] = persisted

    # Pass prev_ts so the reader only returns prompts since last commit
    info = get_latest_turn_info(s.agent_session_id, s.cwd, since_ts=prev_ts or 0.0)
    latest_ts = info["turn_ts"]

    if latest_ts == 0.0:
        # No completed turns in the JSONL yet
        return

    if prev_ts is None:
        # Truly first time ever — record baseline persistently, do not commit
        _last_committed_turn_ts[s.id] = latest_ts
        _write_baseline(s.id, s.cwd, latest_ts)
        return

    if latest_ts <= prev_ts:
        # Same or older turn_duration — nothing new
        return

    # New turn_duration detected: commit if dirty, then update the baseline
    if git_is_dirty(s.cwd):
        prompts = info["prompts_since"]
        msg = make_commit_message(prompts, info["last_summary"]) if info["last_summary"] else "Claude auto-commit"
        result = git_add_commit(s.cwd, msg, author=s.owner_id)
        if not result.get("ok"):
            logger.warning("auto-commit failed for session %s: %s", s.id, result.get("output", ""))
            return  # don't advance baseline — retry on next turn

    _last_committed_turn_ts[s.id] = latest_ts
    _write_baseline(s.id, s.cwd, latest_ts)


def _fire_due_tasks() -> None:
    """Execute scheduled tasks whose run_at is now in the past."""
    due = session_store.list_due_tasks()
    for task in due:
        session = session_store.get(task.session_id)
        if session is None or session.status in (SessionStatus.TERMINATED,):
            session_store.update_task_status(task.id, "failed", "session not running")
            logger.warning("task %s failed: session %s not running", task.id, task.session_id)
            continue
        if not tmux.has_session(session.tmux_session_name):
            session_store.update_task_status(task.id, "failed", "tmux session gone")
            logger.warning("task %s failed: tmux session gone (%s)", task.id, session.tmux_session_name)
            continue
        try:
            tmux.send_keys(session.tmux_session_name, task.command)
            session_store.update_task_status(task.id, "sent")
            logger.info("task %s sent to session %s: %r", task.id, task.session_id, task.command)
            # Repeating task: spawn the next iteration as a fresh pending row.
            # Anchored on now, not task.run_at, so a missed-tick won't pile up.
            if task.loop_seconds:
                from datetime import datetime as _dt, timezone as _tz, timedelta
                from app.models.session import ScheduledTask as _ScheduledTask
                next_task = _ScheduledTask(
                    session_id=task.session_id,
                    owner_id=task.owner_id,
                    command=task.command,
                    run_at=_dt.now(_tz.utc) + timedelta(seconds=task.loop_seconds),
                    loop_seconds=task.loop_seconds,
                )
                session_store.create_task(next_task)
                logger.info("task %s scheduled next loop iteration %s at +%ds",
                            task.id, next_task.id, task.loop_seconds)
        except Exception as exc:
            session_store.update_task_status(task.id, "failed", str(exc))
            logger.exception("task %s failed to send: %s", task.id, exc)


def _lookup_session_id_by_pid(pid: int) -> str | None:
    """Read ~/.claude/sessions/{pid}.json if the process is still alive."""
    if not Path(f"/proc/{pid}").exists():
        return None
    session_file = Path.home() / ".claude" / "sessions" / f"{pid}.json"
    try:
        data = json.loads(session_file.read_text())
        return data.get("sessionId") or None
    except (json.JSONDecodeError, OSError):
        return None


def _sync_session_states() -> None:
    """Check running/detached sessions against actual tmux processes.
    Also try to resolve missing agent_session_id, and detect new output
    in sessions that have no attached WS clients."""
    for s in session_store.all():
        if s.status in (SessionStatus.RUNNING, SessionStatus.CREATING, SessionStatus.DETACHED):
            if not tmux.has_session(s.tmux_session_name):
                try:
                    session_store.force_status(s.id, SessionStatus.TERMINATED)
                except Exception:
                    pass
            else:
                if s.status == SessionStatus.RUNNING:
                    # Re-resolve agent_session_id every tick when the process is alive:
                    # PID-based lookup is authoritative and catches the case where the
                    # session id changed (e.g. /resume created a new conversation file).
                    try:
                        sid: str | None = None
                        if s.tool == "cursor":
                            if not s.agent_session_id:
                                sid = find_newest_cursor_session_id(s.cwd)
                        else:
                            # Try stored PID first (cheapest, authoritative when alive).
                            if s.claude_proc_pid:
                                sid = _lookup_session_id_by_pid(s.claude_proc_pid)
                                if sid is None:
                                    # PID is stale (process died). Drop it so we fall
                                    # through to the tmux scan and pick up the new pid.
                                    session_store.update_claude_proc_pid(s.id, None)
                            if sid is None:
                                # No live PID — scan the tmux pane's descendants.
                                sid, proc_pid = tmux.resolve_agent_session_id(s.tmux_session_name, timeout=1)
                                if proc_pid:
                                    session_store.update_claude_proc_pid(s.id, proc_pid)
                        if sid and sid != s.agent_session_id:
                            session_store.update_agent_session_id(s.id, sid)
                            logger.info("watchdog updated agent_session_id for %s: %s → %s", s.id, s.agent_session_id, sid)
                    except Exception:
                        pass
                # Track turn completion for all sessions (drives has_new_output red dot)
                try:
                    _sync_turn_ts(s)
                except Exception:
                    logger.exception("turn-ts sync error for session %s", s.id)
                # Auto-commit after Claude replies
                if s.git_auto_commit:
                    try:
                        _maybe_auto_commit(s)
                    except Exception:
                        logger.exception("auto-commit error for session %s", s.id)
                # Detect new output for sessions nobody is currently watching.
                # Only run when attached_clients == 0: while a WS client is connected,
                # output_pusher updates last_activity_at directly. Running the offset
                # check while attached would falsely trigger for done sessions
                # (last_output_offset is stale during a WS session — it's only synced
                # at connect/disconnect time).
                if s.attached_clients == 0:
                    try:
                        history_size = tmux.get_pane_history_size(s.tmux_session_name)
                        if history_size is not None:
                            session_store.update_activity_if_offset_changed(s.id, history_size)
                    except Exception:
                        pass
        # For non-running sessions: check if agent_session_id is missing or stale
        if s.status in (SessionStatus.TERMINATED, SessionStatus.DETACHED):
            try:
                if s.tool == "cursor":
                    newest = find_newest_cursor_session_id(s.cwd)
                else:
                    newest = find_newest_claude_session_id(s.cwd)
                if newest and newest != s.agent_session_id:
                    session_store.update_agent_session_id(s.id, newest)
                    logger.info("watchdog updated agent_session_id for %s: %s → %s", s.id, s.agent_session_id, newest)
            except Exception:
                pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    async def _session_watchdog():
        while True:
            await asyncio.sleep(5)
            try:
                await asyncio.get_event_loop().run_in_executor(None, _sync_session_states)
            except Exception:
                logger.exception("session watchdog error")

    async def _task_scheduler():
        while True:
            await asyncio.sleep(2)
            try:
                await asyncio.get_event_loop().run_in_executor(None, _fire_due_tasks)
            except Exception:
                logger.exception("task scheduler error")

    async def _proxy_tap_cleanup_loop():
        # First pass after a short delay so startup isn't slowed by FS scans.
        await asyncio.sleep(15)
        while True:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, proxy_tap_cleanup_once, session_store
                )
            except Exception:
                logger.exception("proxy_tap cleanup error")
            await asyncio.sleep(30)

    watchdog = asyncio.create_task(_session_watchdog())
    scheduler = asyncio.create_task(_task_scheduler())
    proxy_tap_cleaner = asyncio.create_task(_proxy_tap_cleanup_loop())
    yield
    for t in (watchdog, scheduler, proxy_tap_cleaner):
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Claude Session Manager", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_api.router)
app.include_router(claude_caps_api.router)
app.include_router(config_api.router)
app.include_router(fs_api.router)
app.include_router(sessions_api.router)
app.include_router(sessions_api.models_router)
app.include_router(files_api.router)
app.include_router(code_api.router)
app.include_router(usage_api.router)
app.include_router(terminal_ws.router)
app.include_router(shell_ws.router)
app.include_router(term_ws.router)
app.include_router(terminals_api.router)


@app.get("/health")
def health():
    return {"status": "ok"}


# --- Serve frontend static files ---
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    # PyInstaller bundle: frontend is at <bundle>/frontend/dist
    _FRONTEND_DIST = Path(sys._MEIPASS) / "frontend" / "dist"
else:
    _FRONTEND_DIST = Path(os.getenv(
        "FRONTEND_DIST",
        str(Path(__file__).resolve().parent.parent / "frontend" / "dist"),
    ))

class _ImmutableStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if resp.status_code == 200:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


if _FRONTEND_DIST.is_dir():
    # Serve assets (js/css/images) — Vite emits content-hashed filenames, safe to cache forever
    app.mount("/assets", _ImmutableStaticFiles(directory=_FRONTEND_DIST / "assets"), name="assets")

    # index.html must never be cached — it pins which hashed bundle the client loads.
    _NO_CACHE_HEADERS = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    @app.get("/{full_path:path}")
    async def serve_spa(request: Request, full_path: str):
        file = _FRONTEND_DIST / full_path
        if file.is_file() and file.suffix.lower() != ".html":
            return FileResponse(file)
        return FileResponse(_FRONTEND_DIST / "index.html", headers=_NO_CACHE_HEADERS)
