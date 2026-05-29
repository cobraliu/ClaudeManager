"""Read Claude CLI session data from ~/.claude/ for display purposes."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from threading import Lock
from time import monotonic

# Matches the Claude CLI's slash-command wrapper, e.g.:
#   <command-name>/goal</command-name>
#       <command-message>goal</command-message>
#       <command-args> ... </command-args>
# We normalize that to "/goal <args>" so it renders cleanly in session lists.
_SLASH_CMD_RE = re.compile(
    r"^\s*<command-name>(/[^<\s]+)</command-name>"
    r".*?<command-args>(.*?)</command-args>\s*$",
    re.DOTALL,
)

# Simple TTL cache for enrichment results
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = Lock()
_CACHE_TTL = 30.0  # seconds

_COMPACT_PREFIX = "This session is being continued from a previous conversation"
_SYSTEM_INJECTION_PREFIXES = (
    _COMPACT_PREFIX,
    "<task-notification>",
    "<system-reminder>",
)


def list_project_session_ids(cwd: str) -> list[dict]:
    """Return all JSONL session stems in the Claude project dir for cwd, with mtime and title."""
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.is_dir():
        return []
    cwd = cwd.rstrip("/")
    for encoded in [cwd.replace("/", "-").replace("_", "-"), cwd.replace("/", "-")]:
        project_dir = projects_dir / encoded
        if project_dir.is_dir():
            results = []
            for f in project_dir.iterdir():
                if f.suffix == ".jsonl" and f.is_file():
                    stem = f.stem
                    mtime = f.stat().st_mtime
                    data = enrich_session(stem, cwd)
                    results.append({
                        "agent_session_id": stem,
                        "mtime": mtime,
                        "title": data.get("title"),
                    })
            results.sort(key=lambda x: x["mtime"], reverse=True)
            return results
    return []


def find_newest_claude_session_id(cwd: str) -> str | None:
    """Return the stem of the most-recently-modified JSONL in the Claude project dir for cwd."""
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.is_dir():
        return None
    cwd = cwd.rstrip("/")
    for encoded in [cwd.replace("/", "-").replace("_", "-"), cwd.replace("/", "-")]:
        project_dir = projects_dir / encoded
        if project_dir.is_dir():
            best, best_mtime = None, 0.0
            for f in project_dir.iterdir():
                if f.suffix == ".jsonl" and f.is_file():
                    mt = f.stat().st_mtime
                    if mt > best_mtime:
                        best_mtime, best = mt, f.stem
            return best
    return None


def _find_session_jsonl(claude_session_id: str, cwd: str) -> Path | None:
    """Find the JSONL conversation file for a Claude session."""
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.is_dir():
        return None

    cwd = cwd.rstrip("/")
    # Claude encodes: /mnt/hdd2/foo_bar → -mnt-hdd2-foo-bar
    for encoded in [
        cwd.replace("/", "-").replace("_", "-"),
        cwd.replace("/", "-"),
    ]:
        jsonl = projects_dir / encoded / f"{claude_session_id}.jsonl"
        if jsonl.exists():
            return jsonl

    # Fallback: search all project dirs
    for d in projects_dir.iterdir():
        if d.is_dir():
            candidate = d / f"{claude_session_id}.jsonl"
            if candidate.exists():
                return candidate
    return None


def _normalize_slash_cmd(text: str) -> str:
    """If text is a CLI slash-command wrapper, collapse to '/cmd <args>'."""
    m = _SLASH_CMD_RE.match(text)
    if not m:
        return text
    cmd = m.group(1)
    args = (m.group(2) or "").strip()
    return f"{cmd} {args}" if args else cmd


def _extract_text(msg: dict) -> str:
    """Extract plain text from a message dict. Ignores tool_result content."""
    content = msg.get("content", "")
    if isinstance(content, list):
        parts = [
            c.get("text", "")
            for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        raw = " ".join(parts).strip()
    else:
        raw = str(content).strip()
    return _normalize_slash_cmd(raw)


def _is_user_message(d: dict) -> bool:
    """True if this JSONL entry is a real user prompt (not a tool result)."""
    if d.get("type") != "user":
        return False
    msg = d.get("message", {})
    if msg.get("role") != "user":
        return False
    # Exclude tool results (content list whose first item is type=="tool_result")
    content = msg.get("content", "")
    if isinstance(content, list):
        if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
            return False
    return True


def _is_compact_message(text: str) -> bool:
    """Return True if this message is system-injected (compaction, task-notification, etc.)."""
    return any(text.startswith(p) for p in _SYSTEM_INJECTION_PREFIXES)


def _is_turn_complete(d: dict) -> bool:
    """True if this entry marks the end of a complete Claude turn."""
    return d.get("type") == "system" and d.get("subtype") == "turn_duration"


def iter_user_prompts(claude_session_id: str, cwd: str):
    """Yield (text, sent_at_unix) tuples for every real user prompt in the
    Claude JSONL. Skips tool_result entries and compaction-injected system
    messages. Timestamps with no parseable value yield 0.0 (caller can drop).
    Used by the prompt-history backfill scanner to populate the DB from
    sessions that pre-date the on-send recording hook."""
    jsonl = _find_session_jsonl(claude_session_id, cwd)
    if jsonl is None:
        return
    try:
        with open(jsonl) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not _is_user_message(d):
                    continue
                text = _extract_text(d.get("message", {}))
                if not text or _is_compact_message(text):
                    continue
                ts_str = d.get("timestamp", "")
                ts_unix = 0.0
                if ts_str:
                    try:
                        from datetime import datetime
                        ts_unix = datetime.fromisoformat(
                            ts_str.replace("Z", "+00:00")
                        ).timestamp()
                    except Exception:
                        ts_unix = 0.0
                yield text, ts_unix
    except OSError:
        return


def enrich_session(claude_session_id: str, cwd: str) -> dict:
    """
    Single-pass reader: returns {title, prompts, search_text} from one file read.
    Results are cached for _CACHE_TTL seconds.
    """
    cache_key = claude_session_id

    with _cache_lock:
        if cache_key in _cache:
            ts, data = _cache[cache_key]
            if monotonic() - ts < _CACHE_TTL:
                return data

    jsonl = _find_session_jsonl(claude_session_id, cwd)
    if jsonl is None:
        result = {"title": None, "prompts": [], "search_text": []}
        with _cache_lock:
            _cache[cache_key] = (monotonic(), result)
        return result

    user_msgs: list[str] = []
    user_timestamps: list[str] = []
    all_texts: list[str] = []

    try:
        with open(jsonl) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if _is_user_message(d):
                    text = _extract_text(d.get("message", {}))
                    if not text or _is_compact_message(text):
                        continue
                    all_texts.append(text[:500])
                    user_msgs.append(text)
                    ts = d.get("timestamp", "")
                    if ts:
                        user_timestamps.append(ts)
                elif d.get("type") == "queue-operation" and d.get("operation") == "enqueue":
                    content = d.get("content", "").strip()
                    if content and not _is_compact_message(content):
                        all_texts.append(content[:500])
                        user_msgs.append(content)
                elif d.get("type") == "assistant":
                    text = _extract_text(d.get("message", {}))
                    if text:
                        all_texts.append(text[:500])
    except OSError:
        pass

    # Title = first user prompt
    title = user_msgs[0][:80] if user_msgs else None

    # Display prompts: first + last (deduplicated if only one message)
    prompts: list[str] = []
    if user_msgs:
        prompts.append(user_msgs[0][:200])
        if len(user_msgs) >= 2:
            prompts.append(user_msgs[-1][:200])

    last_user_input_at = user_timestamps[-1] if user_timestamps else None

    result = {"title": title, "prompts": prompts, "search_text": all_texts, "last_user_input_at": last_user_input_at}

    with _cache_lock:
        _cache[cache_key] = (monotonic(), result)

    return result


def get_latest_turn_info(claude_session_id: str, cwd: str, since_ts: float = 0.0) -> dict:
    """
    Return {
      "turn_ts": float,           # max turn_duration timestamp (seconds since epoch), 0 if none
      "last_summary": str,        # assistant text from the turn with the latest timestamp
      "prompts_since": list[dict] # [{text, ts, time_str}] for all turns whose ts > since_ts
    }.
    Always reads from disk (bypasses cache) for real-time watchdog use.
    """
    from datetime import datetime, timezone

    jsonl = _find_session_jsonl(claude_session_id, cwd)
    if jsonl is None:
        return {"turn_ts": 0.0, "last_summary": "", "prompts_since": []}

    max_ts = 0.0
    last_text = ""
    prompts_since: list[dict] = []
    pending_user_prompt = ""
    pending_queued_prompts: list[str] = []
    pending_assistant_text = ""

    try:
        with open(jsonl) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if _is_user_message(d):
                    t = _extract_text(d.get("message", {}))
                    if t and not _is_compact_message(t):
                        pending_user_prompt = t

                elif d.get("type") == "queue-operation" and d.get("operation") == "enqueue":
                    # Queued prompts: messages the user typed while Claude was responding
                    content = d.get("content", "").strip()
                    if content and not _is_compact_message(content):
                        pending_queued_prompts.append(content)

                elif d.get("type") == "assistant":
                    text = _extract_text(d.get("message", {}))
                    if text:
                        pending_assistant_text = text

                elif _is_turn_complete(d):
                    ts_str = d.get("timestamp", "")
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        ts = 0.0
                    if ts > max_ts:
                        max_ts = ts
                        if pending_assistant_text:
                            last_text = pending_assistant_text
                    if ts > since_ts:
                        time_str = datetime.fromtimestamp(ts).strftime("%y%m%d %H:%M:%S") if ts else ""
                        all_prompts = ([pending_user_prompt] if pending_user_prompt else []) + pending_queued_prompts
                        for pt in all_prompts:
                            prompts_since.append({"text": pt, "ts": ts, "time_str": time_str})
                    pending_assistant_text = ""
                    pending_user_prompt = ""
                    pending_queued_prompts = []

    except OSError:
        pass

    return {"turn_ts": max_ts, "last_summary": last_text, "prompts_since": prompts_since}


def _find_subagents_dir(claude_session_id: str, cwd: str) -> Path | None:
    """Find the subagents directory for a Claude session."""
    jsonl = _find_session_jsonl(claude_session_id, cwd)
    if jsonl is None:
        return None
    subagents = jsonl.parent / claude_session_id / "subagents"
    return subagents if subagents.is_dir() else None


def list_subagents(claude_session_id: str, cwd: str) -> list[dict]:
    """List all sub-agents with their description and metadata."""
    subagents_dir = _find_subagents_dir(claude_session_id, cwd)
    if subagents_dir is None:
        return []
    results = []
    for meta_file in sorted(subagents_dir.glob("agent-*.meta.json")):
        try:
            meta = json.loads(meta_file.read_text())
            # "agent-abc123.meta.json" → stem = "agent-abc123.meta" → strip
            agent_id = meta_file.stem.removeprefix("agent-").removesuffix(".meta")
            jsonl_file = subagents_dir / f"agent-{agent_id}.jsonl"
            mtime = jsonl_file.stat().st_mtime if jsonl_file.exists() else 0.0
            results.append({
                "agentId": agent_id,
                "description": meta.get("description", ""),
                "agentType": meta.get("agentType", ""),
                "mtime": mtime,
            })
        except Exception:
            continue
    return results


def _parse_iso_ts(ts_str: str) -> float:
    if not ts_str:
        return 0.0
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _todos_kset(todos: list[dict]) -> set:
    """Key set for plan-merge: emits BOTH `id:<tid>` and `c:<content>` for each task.

    Why both: anonymous TaskCreate snapshots (no id assigned yet) need to merge with
    the subsequent task_reminder snapshot that does assign ids — they share content,
    not ids. Conversely, a task whose subject was renamed via TaskUpdate keeps the
    same id, so id-based intersection still pairs the two snapshots.
    """
    keys: set = set()
    for t in todos:
        tid = t.get("id")
        if tid is not None and tid != "":
            keys.add(f"id:{tid}")
        c = t.get("content")
        if c:
            keys.add(f"c:{c}")
    return keys


# Incremental scan cache for get_todo_plans: jsonl path -> (consumed_offset, snapshots).
# The session JSONL is append-only, so the snapshot list only ever grows; a poll
# seeks to the cached offset and parses just the newly-appended lines instead of
# the whole 100MB+ file. Keyed by path (owner-independent). Copy-on-read for
# concurrency safety (idempotent if two callers race on the same delta).
_todo_scan_cache: dict[str, tuple[int, list]] = {}


def get_todo_plans(claude_session_id: str, cwd: str, window_bytes: int | None = None) -> dict:
    """Scan the session JSONL for plan snapshots and group them into plans.

    `window_bytes` (bounded tail mode, used by the status-bar hot path): read
    only the last `window_bytes` and forward-parse, never the whole file —
    O(window) regardless of file size, restart-safe, no cache. History is
    partial in this mode (only plans whose snapshots fall in the window); the
    "active" slice is correct as long as the window covers the last plan's
    starting anchor, which the caller (get_active_todos) guarantees by widening
    the window until an anchor is captured. Default (None) = full incremental
    scan with cache, used by the on-demand dock history endpoints.

    Two data sources, unified into one view:
      A. `TodoWrite` tool_use blocks (assistant entries) — emitted by the agent calling
         the TodoWrite tool. Todo shape: {content, activeForm?, status, priority?}.
      B. `attachment.task_reminder` entries — Claude CLI injects these from the agent's
         TaskCreate/TaskUpdate task list (visible in the TUI). Task shape:
         {id, subject, description, activeForm?, status, ...}. Normalize to the same
         shape as TodoWrite via subject→content, id carried through.

    Each snapshot belongs to a plan. Consecutive snapshots that share at least one
    task id (B) or content (A) extend the same plan; otherwise a new plan starts.
    A and B never share keys so they form independent plans.

    Returns:
      {
        "active": [todo, ...]  # latest plan's todos if NOT all completed; else []
        "history": [           # all earlier plans + the latest if all completed
          {"todos": [todo, ...], "created_ts": float, "completed_ts": float},
          ...
        ]
      }
    Each todo: {content, status: 'pending'|'in_progress'|'completed', priority?, id?}
    Timestamps are unix seconds; completed_ts is the ts of the LAST snapshot in the plan.
    """
    jsonl = _find_session_jsonl(claude_session_id, cwd)
    if jsonl is None:
        return {"active": [], "history": []}

    # (ts, todos, key_set, is_anchor). Anchor = full snapshot from TodoWrite/task_reminder
    # (authoritative task list at that moment). Delta = TaskCreate/TaskUpdate (partial mid-turn
    # update). Plan boundaries are decided by comparing each new anchor to the PRIOR anchor — not
    # to the cumulative plan kset — so a delta that carries forward old ids can't bridge two
    # disjoint anchors into one plan.
    try:
        size = jsonl.stat().st_size
    except OSError:
        return {"active": [], "history": []}
    _pk = str(jsonl)
    if window_bytes is not None:
        # Bounded tail mode: read only the last window_bytes, forward-parse, no
        # cache. Active state lives at the tail (the CLI re-emits a full
        # task_reminder anchor ~every turn), so a suffix that contains the last
        # plan's anchor yields the same "active" as a full scan.
        _start = max(0, size - window_bytes)
        snapshots: list[tuple[float, list[dict], set, bool]] = []
    else:
        _cached = _todo_scan_cache.get(_pk)
        if _cached is not None and size >= _cached[0]:
            _start = _cached[0]
            snapshots = list(_cached[1])
        else:
            _start = 0
            snapshots = []
    _consumed = _start
    try:
        # Read only the appended region [consumed_offset, size). A line worth
        # parsing always contains one of these literals, so the byte pre-filter
        # skips json.loads on the overwhelming majority (plain chat turns) — this
        # alone slashes even the one-time cold pass from ~48k parses to ~1k.
        with open(jsonl, "rb") as f:
            f.seek(_start)
            _data = f.read(max(0, size - _start))
            if window_bytes is not None and _start > 0:
                # Started mid-file: drop the partial first line so parsing begins
                # on a clean record boundary.
                _fnl = _data.find(b"\n")
                _data = _data[_fnl + 1:] if _fnl != -1 else b""
            _last_nl = _data.rfind(b"\n")
            if _last_nl != -1:
                _consumed = _start + _last_nl + 1
            _lines = _data[:_last_nl + 1].split(b"\n") if _last_nl != -1 else []
            for line in _lines:
                if not line:
                    continue
                if (
                    b"TodoWrite" not in line
                    and b"TaskCreate" not in line
                    and b"TaskUpdate" not in line
                    and b"task_reminder" not in line
                ):
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = d.get("type")
                ts = _parse_iso_ts(d.get("timestamp", ""))

                if t == "assistant":
                    msg = d.get("message") or {}
                    content = msg.get("content") or []
                    if not isinstance(content, list):
                        continue
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") != "tool_use":
                            continue
                        name = b.get("name")
                        inp = b.get("input") or {}
                        if name == "TodoWrite":
                            todos = inp.get("todos")
                            if isinstance(todos, str):
                                try:
                                    todos = json.loads(todos)
                                except json.JSONDecodeError:
                                    todos = None
                            if not isinstance(todos, list):
                                continue
                            clean = [x for x in todos if isinstance(x, dict)]
                            kset = {f"c:{x.get('content')}" for x in clean if x.get("content")}
                            snapshots.append((ts, clean, kset, True))
                        elif name == "TaskCreate":
                            # Append the new task onto the prior snapshot. Emit a combined
                            # id-and-content key set so anonymous TaskCreates (no id yet) can
                            # still chain into the next task_reminder snapshot that assigns ids.
                            subject = inp.get("subject") or ""
                            description = inp.get("description")
                            if not subject:
                                # Legacy: some callers only pass `description` — promote it
                                # to subject so the task isn't dropped.
                                subject = description or ""
                                description = None
                            if not subject:
                                continue
                            tid = inp.get("taskId") or inp.get("id")
                            new_task = {
                                "id": tid,
                                "content": subject,
                                "description": description,
                                "activeForm": inp.get("activeForm"),
                                "status": "pending",
                                "priority": inp.get("priority"),
                            }
                            # Carry forward the prior task list ONLY if it has work in progress.
                            # If the prior snapshot was all-completed, this TaskCreate is starting
                            # a fresh batch — emit as an ANCHOR with just the new task so the
                            # disjoint-kset boundary check splits it into a new plan rather than
                            # polluting the prior plan with anonymous ghost entries.
                            prev_todos = snapshots[-1][1] if snapshots else []
                            prior_all_done = bool(prev_todos) and all(
                                (t.get("status") == "completed") for t in prev_todos
                            )
                            if prior_all_done or not prev_todos:
                                merged = [new_task]
                                is_anchor_create = True
                            else:
                                merged = list(prev_todos) + [new_task]
                                is_anchor_create = False
                            kset = _todos_kset(merged)
                            snapshots.append((ts, merged, kset, is_anchor_create))
                        elif name == "TaskUpdate":
                            # Delta against the most recent snapshot. If the referenced id is
                            # NOT in the prior task list, skip — don't synthesize. Synthesizing
                            # would resurrect rotated-out ids and falsely bridge unrelated plans.
                            tid = inp.get("taskId") or inp.get("id")
                            if not tid or not snapshots:
                                continue
                            prev_todos = snapshots[-1][1]
                            updated: list[dict] = []
                            touched = False
                            for x in prev_todos:
                                if str(x.get("id")) == str(tid):
                                    nx = dict(x)
                                    if inp.get("status"):
                                        nx["status"] = inp.get("status")
                                    if inp.get("subject"):
                                        nx["content"] = inp.get("subject")
                                    if "description" in inp:
                                        nx["description"] = inp.get("description")
                                    if inp.get("activeForm"):
                                        nx["activeForm"] = inp.get("activeForm")
                                    updated.append(nx)
                                    touched = True
                                else:
                                    updated.append(x)
                            if not touched:
                                continue
                            snapshots.append((ts, updated, _todos_kset(updated), False))

                elif t == "attachment":
                    att = d.get("attachment") or {}
                    if not isinstance(att, dict) or att.get("type") != "task_reminder":
                        continue
                    items = att.get("content")
                    if not isinstance(items, list):
                        continue
                    norm: list[dict] = []
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        subject = it.get("subject") or it.get("content") or ""
                        if not subject:
                            continue
                        norm.append({
                            "id": it.get("id"),
                            "content": subject,
                            "description": it.get("description"),
                            "activeForm": it.get("activeForm"),
                            "status": it.get("status", "pending"),
                            "priority": it.get("priority"),
                        })
                    # ALWAYS emit the snapshot, even when empty. An empty task_reminder
                    # signals "the task list was reset" — it must break the snapshot chain
                    # so the next TaskCreate doesn't accumulate onto the previous batch.
                    snapshots.append((ts, norm, _todos_kset(norm), True))
            if window_bytes is None:
                _todo_scan_cache[_pk] = (_consumed, snapshots)
    except OSError:
        return {"active": [], "history": []}

    plans: list[dict] = []
    last_anchor_kset: set | None = None
    for ts, todos, kset, is_anchor in snapshots:
        # Empty anchor = explicit reset. Break the chain and forget the prior anchor.
        if not todos:
            if plans and plans[-1]["todos"]:
                plans.append({"todos": [], "created_ts": ts, "last_ts": ts, "_kset": set()})
            last_anchor_kset = None
            continue

        # Anchor (task_reminder / TodoWrite): compare against the PRIOR anchor's kset only.
        # If disjoint, the CLI rotated the task list to a different batch — start a new plan
        # even when no empty task_reminder separates the two.
        if is_anchor:
            if last_anchor_kset is not None and not (kset & last_anchor_kset):
                plans.append({"todos": todos, "created_ts": ts, "last_ts": ts, "_kset": kset})
                last_anchor_kset = kset
                continue
            last_anchor_kset = kset

        # Default: extend the current open plan, or start one if none is open.
        if plans and plans[-1]["todos"] and (not is_anchor or (kset & plans[-1]["_kset"])):
            plans[-1]["todos"] = todos
            plans[-1]["last_ts"] = ts
            plans[-1]["_kset"] = kset | plans[-1]["_kset"]
        else:
            plans.append({"todos": todos, "created_ts": ts, "last_ts": ts, "_kset": kset})

    # Drop the sentinel reset plans before assembling output.
    plans = [p for p in plans if p["todos"]]
    if not plans:
        return {"active": [], "history": []}

    active: list[dict] = []
    history: list[dict] = []
    for i, p in enumerate(plans):
        all_done = all(t.get("status") == "completed" for t in p["todos"])
        is_latest = (i == len(plans) - 1)
        if is_latest and not all_done:
            active = p["todos"]
        else:
            history.append({
                "todos": p["todos"],
                "created_ts": p["created_ts"],
                "completed_ts": p["last_ts"],
            })
    history.reverse()
    return {"active": active, "history": history}


# Window sizes for the bounded tail scan, smallest first. An active plan's anchor
# (task_reminder / TodoWrite) is re-emitted ~every turn, so the smallest window
# almost always captures it; we widen only when a window saw no plan at all.
_TODO_TAIL_WINDOWS = (256 * 1024, 2 * 1024 * 1024, 16 * 1024 * 1024)


def get_active_todos(claude_session_id: str, cwd: str) -> list[dict]:
    """Active todo list for the status-bar hot path WITHOUT reading the full file.

    Reads a bounded tail window and runs the exact get_todo_plans grouping on it,
    widening the window only if it captured no plan (so the file may be larger
    than the window yet hold an anchor further back). The active plan's snapshots
    live at the tail, so a suffix that contains them yields the same "active" as a
    full scan — verified by the golden test in tests/. Returns get_todo_plans()'s
    "active" slice (latest plan's todos if not all-completed, else []).
    """
    jsonl = _find_session_jsonl(claude_session_id, cwd)
    if jsonl is None:
        return []
    try:
        size = jsonl.stat().st_size
    except OSError:
        return []
    res = {"active": [], "history": []}
    for wb in _TODO_TAIL_WINDOWS:
        res = get_todo_plans(claude_session_id, cwd, window_bytes=wb)
        # Definitive when the window covers the whole file, or it saw at least one
        # plan (=> it holds the most recent anchor, which decides "active").
        if size <= wb or res["active"] or res["history"]:
            break
    return res["active"]


def get_subagent_lines(claude_session_id: str, cwd: str, agent_id: str, from_line: int = 0) -> dict:
    """Return raw JSONL lines from a sub-agent file starting at from_line."""
    subagents_dir = _find_subagents_dir(claude_session_id, cwd)
    if subagents_dir is None:
        return {"lines": [], "total": 0}
    jsonl_file = subagents_dir / f"agent-{agent_id}.jsonl"
    if not jsonl_file.exists():
        return {"lines": [], "total": 0}
    try:
        all_lines = [ln.rstrip("\n\r") for ln in jsonl_file.open() if ln.strip()]
        return {"lines": all_lines[from_line:], "total": len(all_lines)}
    except OSError:
        return {"lines": [], "total": 0}


def search_conversation(claude_session_id: str, cwd: str, query: str) -> bool:
    """Check if any conversation text matches the query (for server-side filtering)."""
    data = enrich_session(claude_session_id, cwd)
    q = query.lower()
    for t in data.get("search_text", []):
        if q in t.lower():
            return True
    return False


def _read_session_cwd(jsonl_path: Path) -> str | None:
    """Read the cwd field from the first few JSONL entries."""
    try:
        with open(jsonl_path) as f:
            for i, line in enumerate(f):
                if i >= 10:
                    break
                try:
                    d = json.loads(line)
                    cwd = d.get("cwd")
                    if cwd:
                        return cwd
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return None


def list_all_claude_sessions_global(excluded_ids: set[str]) -> list[dict]:
    """Scan all ~/.claude/projects dirs and return sessions grouped by cwd.

    Excludes sessions whose agent_session_id is in excluded_ids.
    Returns [{"dir": cwd, "sessions": [{agent_session_id, mtime, title, prompts, cwd}]}]
    sorted by most-recently-active dir first.
    """
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.is_dir():
        return []

    by_cwd: dict[str, list[dict]] = {}

    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl_file in project_dir.iterdir():
            if jsonl_file.suffix != ".jsonl" or not jsonl_file.is_file():
                continue
            stem = jsonl_file.stem
            if stem in excluded_ids:
                continue

            # Decoded directory name is the authoritative "where this file lives now"
            # (works for manually-moved files where internal cwd points to the old location).
            decoded_dir = "/" + project_dir.name[1:].replace("-", "/")
            internal_cwd = _read_session_cwd(jsonl_file)
            # Trust the internal cwd only when it re-encodes to the same dir name —
            # this preserves disambiguation when "_" → "-" collapsing makes decode lossy.
            if internal_cwd and internal_cwd.replace("/", "-").replace("_", "-") == project_dir.name:
                cwd = internal_cwd
            else:
                cwd = decoded_dir

            mtime = jsonl_file.stat().st_mtime
            data = enrich_session(stem, cwd)

            entry = {
                "agent_session_id": stem,
                "mtime": mtime,
                "title": data.get("title"),
                "prompts": data.get("prompts", []),
                "cwd": cwd,
            }
            by_cwd.setdefault(cwd, []).append(entry)

    result = []
    for cwd, sessions in by_cwd.items():
        sessions.sort(key=lambda x: x["mtime"], reverse=True)
        result.append({
            "dir": cwd,
            "dir_exists": Path(cwd).is_dir(),
            "sessions": sessions,
            "latest_mtime": sessions[0]["mtime"] if sessions else 0.0,
        })

    result.sort(key=lambda x: x["latest_mtime"], reverse=True)
    return result


def _parse_ts(d: dict) -> float:
    """Extract Unix timestamp from a JSONL entry's 'timestamp' field, or 0.0."""
    ts_str = d.get("timestamp", "")
    if not ts_str:
        return 0.0
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def get_conversation(claude_session_id: str, cwd: str, from_ts: float = 0.0) -> list[dict]:
    """
    Return conversation turns as [{"role", "text", "streaming", "ts"}].

    from_ts: only return confirmed turns whose turn_duration timestamp > from_ts.
             Unconfirmed turns (current in-progress exchange) are always included.
             from_ts=0 returns everything (initial full load).

    Confirmed turns carry ts from turn_duration.
    Pending/streaming turns carry ts from their own JSONL entry timestamp.
    """
    jsonl = _find_session_jsonl(claude_session_id, cwd)
    if jsonl is None:
        return []

    confirmed: list[dict] = []      # fully confirmed turns (with ts)
    current_group: list[dict] = []  # turns since last turn_duration (unconfirmed)
    pending_assistant: dict | None = None
    latest_streaming: dict | None = None
    accumulated_assistant: list[str] = []
    in_compaction = False  # True between compact prefix and turn_duration

    try:
        with open(jsonl) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if _is_user_message(d):
                    if pending_assistant:
                        current_group.append(pending_assistant)
                        pending_assistant = None
                    accumulated_assistant = []
                    latest_streaming = None
                    text = _extract_text(d.get("message", {}))
                    if text and _is_compact_message(text):
                        in_compaction = True  # compaction phase started
                    elif text:
                        in_compaction = False
                        current_group.append({"role": "user", "text": text, "streaming": False, "ts": _parse_ts(d), "pending": True})

                elif d.get("type") == "queue-operation" and d.get("operation") == "enqueue":
                    content = d.get("content", "").strip()
                    if content and not _is_compact_message(content):
                        current_group.append({"role": "user", "text": content, "streaming": False, "ts": _parse_ts(d), "pending": True})

                elif d.get("type") == "assistant":
                    msg = d.get("message", {})
                    stop_reason = msg.get("stop_reason")
                    if stop_reason == "end_turn":
                        text = _extract_text(msg)
                        parts = [t for t in accumulated_assistant + ([text] if text else []) if t]
                        accumulated_assistant = []
                        if parts:
                            pending_assistant = {"role": "assistant", "text": "\n\n".join(parts), "streaming": False, "ts": _parse_ts(d), "pending": True}
                            if in_compaction:
                                pending_assistant["compacting"] = True
                        latest_streaming = None
                    elif stop_reason is None:
                        text = _extract_text(msg)
                        if text:
                            latest_streaming = {"role": "assistant", "text": text, "streaming": True, "ts": _parse_ts(d)}
                            if in_compaction:
                                latest_streaming["compacting"] = True
                    elif stop_reason == "tool_use":
                        text = _extract_text(msg)
                        if text:
                            accumulated_assistant.append(text)

                elif _is_turn_complete(d):
                    if pending_assistant:
                        current_group.append(pending_assistant)
                        pending_assistant = None
                    accumulated_assistant = []
                    latest_streaming = None
                    in_compaction = False

                    # Extract turn_duration timestamp and tag the whole group
                    ts_str = d.get("timestamp", "")
                    ts = 0.0
                    if ts_str:
                        try:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                        except ValueError:
                            pass
                    for turn in current_group:
                        turn["ts"] = ts
                        turn.pop("pending", None)  # now confirmed
                    confirmed.extend(current_group)
                    current_group = []

    except OSError:
        pass

    # Filter confirmed turns by from_ts
    result = [t for t in confirmed if t["ts"] > from_ts]

    # Always include the in-progress exchange (current_group + streaming/pending)
    if latest_streaming:
        result.extend(current_group)
        result.append(latest_streaming)
    elif pending_assistant:
        result.extend(current_group)
        result.append(pending_assistant)
    elif in_compaction and not current_group:
        # Compaction in progress: Claude is generating the summary (thinking phase,
        # no text content yet). Show a placeholder so Chat mode isn't blank.
        result.append({"role": "assistant", "text": "Compacting conversation…", "streaming": True, "ts": 0.0, "compacting": True})
    elif current_group:
        result.extend(current_group)

    return result


# ── Conversation share helpers ──────────────────────────────────────────────


def _effective_timestamps(entries: list[dict]) -> list[float]:
    """Effective ts per entry: its own timestamp, or the previous entry's
    effective ts when untimed (so untimed entries stay glued to neighbors
    instead of bubbling). Mirrors the resort logic in sessions.get_raw_messages.
    """
    eff: list[float] = []
    prev = 0.0
    for d in entries:
        ts = _parse_ts(d) if isinstance(d, dict) else 0.0
        if ts == 0.0:
            eff.append(prev)
        else:
            eff.append(ts)
            prev = ts
    return eff


def read_raw_messages_page(
    jsonl_path: Path,
    tail: int,
    cutoff_ts: float | None = None,
    offset: int | None = None,
) -> dict:
    """Return JSONL entries (chronologically sorted, oldest→newest) + total.

    Forward-scans the whole file, re-sorts by effective timestamp, applies the
    optional `cutoff_ts` filter (keep entries with effective ts <= cutoff_ts —
    used to freeze limited shares), then windows the result:
      - offset is not None → ascending slice `[offset : offset+tail]` (forward
                             pagination for the share viewer; `tail<=0` means
                             "to the end").
      - offset is None     → the last `tail` entries (legacy tail behavior;
                             `tail<=0` means "everything").

    `total` is the count of eligible entries (after the optional cutoff filter),
    so the viewer can tell how much is left to load.
    """
    try:
        entries: list[dict] = []
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        return {"messages": [], "total": 0}

    if not entries:
        return {"messages": [], "total": 0}

    eff = _effective_timestamps(entries)
    indexed = sorted(enumerate(entries), key=lambda pair: (eff[pair[0]], pair[0]))

    if cutoff_ts is not None:
        indexed = [(i, d) for (i, d) in indexed if eff[i] <= cutoff_ts]

    ordered = [d for _, d in indexed]
    total = len(ordered)
    if offset is not None:
        ordered = ordered[offset: offset + tail] if tail > 0 else ordered[offset:]
    elif tail > 0 and total > tail:
        ordered = ordered[-tail:]
    return {"messages": ordered, "total": total}


def _entry_user_text(entry: dict) -> str:
    """Plain text of a user-message entry (joined text blocks, or raw string)."""
    msg = entry.get("message") if isinstance(entry, dict) else None
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return ""


def compute_share_cutoff(jsonl_path: Path, after_uuid: str) -> dict | None:
    """Compute the limited-share cutoff for a chosen user message.

    Finds the entry with uuid == `after_uuid`, then the FIRST `turn_duration`
    system entry after it (in effective-timestamp order); the cutoff is that
    marker's effective timestamp — i.e. the chosen turn's full output is
    included. If no turn_duration follows yet (turn still in progress), falls
    back to the latest entry's timestamp so everything currently present is
    included.

    Returns {"cutoff_ts": float, "cutoff_msg_text": str} or None if the uuid
    wasn't found.
    """
    try:
        entries: list[dict] = []
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        return None

    if not entries:
        return None

    eff = _effective_timestamps(entries)
    order = sorted(range(len(entries)), key=lambda i: (eff[i], i))

    sel_pos = None  # position within `order`
    for pos, idx in enumerate(order):
        if entries[idx].get("uuid") == after_uuid:
            sel_pos = pos
            break
    if sel_pos is None:
        return None

    sel_idx = order[sel_pos]
    cutoff_ts = eff[order[-1]]  # fallback: latest entry
    for idx in order[sel_pos + 1:]:
        if _is_turn_complete(entries[idx]):
            cutoff_ts = eff[idx]
            break

    return {
        "cutoff_ts": cutoff_ts,
        "cutoff_msg_text": _entry_user_text(entries[sel_idx]),
    }
