# Codex Session Workflows

Operating procedure for the two Codex-related flows in ClaudeManager:

1. **Create a new Codex session**
2. **Load an existing Codex session** (from rollout JSONL on disk)

Scope: two transport modes coexist as of Phase 3 — see the
"Codex transport modes" section below for which to pick.

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

---

## Codex transport modes

Each Codex session picks a `codex_transport` at creation time, stored on
the session row. The default is `"tui"`. The selector is only visible
when the create modal's tool picker is set to `codex`.

### `tui` (default)

- Codex runs inside a tmux window, identical to the Phase-2 flow.
- xterm.js in the right panel attaches directly to that tmux session.
- Live waiting-state (AUQ / plan / approval) is **not surfaced**:
  `CodexAdapter.get_waiting_state` returns `None`. Approvals must be
  answered inside the TUI.
- Resume from external rollout uses `codex resume <UUID>` (see Flow B).

### `app_server` (experimental)

- Backend spawns `codex app-server` directly (not in tmux) via
  `codex_appserver_manager.start(session_id, ...)`. The manager owns
  one `CodexAppServerClient` per session, talking JSON-RPC 2.0 over
  stdio.
- No xterm.js: the terminal tab shows a placeholder. The chat tab
  pins a `CodexChatInput` below `ConversationPane`. Sending a
  message hits `POST /api/sessions/{id}/codex-message`, which calls
  `turn/start` on the app-server.
- Live waiting-state **is** surfaced: the manager subscribes to
  `turn/plan/updated` + `thread/compacted` notifications and
  intercepts the six AUQ / approval `ServerRequest` methods listed in
  `codex_appserver_manager.py`. The pending request stays cached
  until the user resolves it via:
    - `POST /api/sessions/{id}/codex-auq`   `{text}`
    - `POST /api/sessions/{id}/codex-approve` `{allow, feedback}`
- Lifecycle: `terminate_session` calls `csm.stop(session_id)` instead
  of `tmux.terminate(...)`. `delete_session` calls `csm.stop()` as a
  belt-and-suspenders cleanup for crash cases.

### Which mode to pick

- **Use TUI** when you need interactive paste, ANSI rendering, or
  expect to operate Codex like a terminal app.
- **Use app-server** when you need live AUQ/approval state in the
  SessionCard, want to drive Codex programmatically, or are running
  on a host where tmux is unavailable.

### Switching modes

Modes are per-session. To switch, terminate the existing session and
create a new one with the desired transport — there is no in-place
mode swap. Resume-from-external (Flow B) currently always creates a
TUI session; app-server resume by thread id is not yet wired through
the create endpoint.

