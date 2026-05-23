# Codex Session Workflows

Operating procedure for the two Codex-related flows in ClaudeManager:

1. **Create a new Codex session**
2. **Load an existing Codex session** (from rollout JSONL on disk)

Scope: Phase 2 — interactive Codex TUI launched inside tmux, with history
sourced from `~/.codex/sessions/**/rollout-*.jsonl`. App-server JSON-RPC
integration (live AUQ / plan / approval state) is Phase 3 and not covered
here.

---

## Prerequisites

- `codex` CLI installed and on `PATH` (verify with `which codex`).
- A configured `model_provider` in `~/.codex/config.toml` (OpenAI by default).
- For "load existing": at least one rollout file under `~/.codex/sessions/`
  for the target `cwd`. The reader matches by `cwd` field inside the
  `session_meta` event of each rollout.
- ClaudeManager backend running with the Codex adapter registered
  (`get_adapter("codex")` returns `CodexAdapter`).

---

## Flow A — Create a new Codex session

1. On the Sessions page, click **New Session** to open the create modal.
2. In the agent picker row, click **codex** (the row shows only `claude`
   and `codex`; Cursor is intentionally hidden in this phase).
3. Fill in:
   - **Project name** — free-form label.
   - **Working directory (cwd)** — absolute path where `codex` will run.
   - **Git repo URL** (optional) — cloned into `cwd` if `cwd` is empty.
4. Click **Create**. Frontend calls `POST /api/sessions` with
   `tool: "codex"`. Backend dispatches to `CodexAdapter.build_command`,
   which resolves the `codex` binary via `shutil.which("codex")` and
   spawns it inside a fresh tmux window.
5. The new session is auto-attached. The conversation pane is initially
   empty because no rollout file exists yet — once Codex emits its first
   `session_meta` event, `find_newest_codex_session_id(cwd)` will pick it
   up and history starts flowing.

**Backend dispatch path:** `api/sessions.py::create_session` →
`get_adapter("codex").build_command(...)` →
`TmuxService.create_window(cmd, cwd)`.

**Phase-2 caveats:**
- Live waiting-state (AUQ / plan / approval) is not surfaced —
  `CodexAdapter.get_waiting_state` returns `None` until Phase 3.
- Claude-only UI features (UsageBar, Auqs button, Goals button, todos
  polling) are gated off via `isClaudeSession` and do not render for
  Codex sessions.

---

## Flow B — Load an existing Codex session

1. On the Sessions page, click **Browse external sessions** to open the
   panel.
2. In the panel header, click the **codex** tab. The fetcher switches to
   `browseCodexSessions()` (`GET /api/sessions/external-codex`), which
   scans `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` and groups by
   `cwd`.
3. Expand a `cwd` group, locate the session row, click **Load**.
4. The frontend dispatches to `handleLoadCodex(ext)`, which calls
   `POST /api/sessions` with:
   ```
   {
     project: "<last path segment of cwd>",
     cwd: "<original cwd>",
     resume_session_id: "<codex rollout UUID>",
     tool: "codex"
   }
   ```
5. Backend's `CodexAdapter.build_command` appends `resume <UUID>` to the
   `codex` invocation so the CLI re-attaches to the existing rollout.
6. The created session is auto-attached. The conversation pane is
   populated by `CodexAdapter.get_conversation(session_id, cwd, from_ts)`,
   which reads the rollout JSONL and returns Claude-shaped turn objects.

**Reader resolution:** `codex_session_reader._find_rollout(session_id, cwd)`
extracts the UUID from the rollout filename and verifies the rollout's
`session_meta.cwd` matches the requested `cwd` — this prevents
cross-project rollout pickup if two projects share a session UUID prefix.

**If Load fails:**
- "Directory does not exist" — the rollout's recorded `cwd` no longer
  exists on disk. The Load button stays disabled until `dir_exists` flips.
- 404 from `/api/sessions/external-codex/<id>` — rollout deleted between
  list and load; refresh the panel.

---

## Endpoints touched

| Endpoint | Purpose |
| --- | --- |
| `POST /api/sessions` | Create-new and load-existing both go here; the `tool` and `resume_session_id` fields select the path. |
| `GET /api/sessions/external-codex` | Browse panel data source. |
| `GET /api/sessions/{id}/conversation` | History stream — dispatches via `get_adapter(session.tool).get_conversation(...)`. |
| `GET /api/sessions/{id}/jsonl-path` | Returns the absolute rollout JSONL path for the active Codex session. |
| `GET /api/sessions/{id}/available-claude-sessions` | Despite the name, returns Codex rollouts for the session's `cwd` (used by the "switch session" picker inside an attached window). |

---

## Verification checklist (manual smoke test)

When you do test, the minimum-confidence path is:

- [ ] Create a Codex session — modal shows `claude` + `codex` (no
      `cursor`), and submitting routes to `tool: "codex"`.
- [ ] Type a prompt inside the tmux pane; confirm Codex responds.
- [ ] Leave the session, return to Sessions page, click **Browse
      external**, switch to **codex** tab, expand the cwd, click
      **Load** on the same rollout.
- [ ] New session attaches; the conversation pane shows the prompt and
      response from step 2.
- [ ] In the side dock, confirm `UsageBar`, `Auqs`, and `Goals` are
      *not* visible (Claude-only gating works).

If any step fails, check the browser console + `uvicorn` log; the most
likely culprit is `get_adapter` falling back to Claude (means `tool`
wasn't persisted as `"codex"` in the DB row).
