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


def get_todo_plans(claude_session_id: str, cwd: str) -> dict:
    """Scan the session JSONL for plan snapshots and group them into plans.

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
    snapshots: list[tuple[float, list[dict], set, bool]] = []
    try:
        with open(jsonl) as f:
            for line in f:
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
