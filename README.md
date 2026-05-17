# ClaudeManager

A self-hosted web UI for running and managing multiple [Claude Code](https://github.com/anthropics/claude-code) sessions from a browser.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![Node](https://img.shields.io/badge/node-18%2B-green.svg)](https://nodejs.org/)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macOS-lightgrey.svg)](#requirements)

Each session runs inside a **tmux** pane — you get a full terminal, a conversation viewer, a file browser, and a code diff panel, all accessible from a single tab.

---

## Why ClaudeManager?

If you juggle several Claude Code sessions across different projects, the usual workflow — SSH + tmux + manual `claude --resume` — gets old fast. ClaudeManager keeps every session alive in a tmux pane on the server and gives you one browser tab to attach, inspect conversation history, view git diffs, and schedule follow-up commands. Run it on your workstation, a homelab box, or a VPS.

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Security](#security)
- [Usage](#usage)
- [Configuration Reference](#configuration-reference)
- [Updating](#updating)
- [Standalone Binary](#standalone-binary)
- [Bypass Permissions Mode](#bypass-permissions-mode)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## Features

- **Multi-session dashboard** — create, resume, attach, and terminate Claude Code sessions
- **Embedded terminal** — full xterm.js terminal with direct tmux passthrough
- **Chat view** — conversation bubbles parsed from the Claude JSONL in real time
- **Code panel** — git diff of changed files with side-by-side diff viewer; full file tree
- **JSONL viewer** — inspect the raw Claude conversation log for any session
- **Load existing sessions** — import sessions from `~/.claude/projects/` by browsing the JSONL history
- **Scheduled tasks** — send commands to a session at a future time, with a live floating panel showing pending / sent / failed tasks and per-task countdowns
- **Goals (`/goal`)** — track active and historical session goals parsed from the JSONL; click any goal to refill the input
- **AUQs panel** — browse every `AskUserQuestion` interaction (question, options, and the answer — including free-text answers tagged distinctly), with ascending / descending sort
- **Right-side dock** — Tasks / Goals / AUQs render as collapsible sections beside the conversation; toolbar buttons pulse green / blue when something is currently executing
- **Slash-command badges** — `/goal`, `/compact`, etc. surface as styled badges in the conversation and session list, distinct from regular user input
- **Compaction banner** — Chat shows a TUI-style `Compacting … XX%` banner with a live progress bar while `/compact` is running, driven by a `PreToolUse` hook plus strict TUI tail-screen detection (no false positives from chat history mentioning "compacting")
- **Mermaid rendering** — ` ```mermaid ` blocks in assistant replies render as inline SVG (theme-aware, with cache); also embedded in the HTML export
- **Export Chat to HTML** — download a self-contained HTML file of the full conversation (inline CSS, light + dark theme, no external resources)
- **File copy** — one-click "Copy to clipboard" button in both the file viewer modal and the inline file pane (rejects files >500 KB)
- **Resizable chat input** — drag the textarea corner to enlarge for editing longer prompts
- **Mobile-friendly** — swipe-to-scroll TUI history, floating ▲/▼ pagination buttons on touch devices, touch-to-select-and-copy in the TUI / shell pane, and Tasks / Goals / AUQs panels available on mobile
- **Shell access** — sandboxed shell (firejail on Linux) in the session's working directory
- **Git panel** — view git log and diffs per session
- **User accounts** — JWT-based auth with admin and regular user roles; Google OAuth optional
- **PreToolUse hook auto-injection** — server startup idempotently injects the per-session tool-call hook into `~/.claude/settings.json` (used for inline tool approval, AskUserQuestion prompts, and compaction detection)
- **Standalone binary** — single-file distribution via PyInstaller (no Python install required)

---

## Architecture

- **Backend**: FastAPI + Uvicorn, WebSocket for terminal streaming, SQLite for accounts/sessions, tmux as the process supervisor.
- **Frontend**: Vite + xterm.js for the terminal, plus a diff viewer and chat renderer for the JSONL log.
- **Session model**: each Claude Code instance lives in its own tmux session under the server user; ClaudeManager only attaches to / detaches from those panes — it never holds the Claude process directly.

---

## Requirements

| Dependency | Version | Notes |
|---|---|---|
| Linux or macOS | — | Windows not supported |
| Python | 3.11+ | Backend |
| Node.js | 18+ | Frontend build |
| tmux | any | Session isolation |
| Claude Code CLI | latest | `claude` must be on `$PATH` |
| firejail *(Linux only)* | any | Shell sandbox |

---

## Quick Start

### 1. Clone

```bash
git clone https://github.com/cobraliu/ClaudeManager.git
cd ClaudeManager
```

### 2. Install dependencies and build frontend

```bash
bash init.sh
```

For users in China (faster pip/npm mirrors):

```bash
bash init.sh --mirror
```

The script is idempotent — safe to re-run after `git pull`.

### 3. Start

```bash
bash restart.sh             # default port 19099
bash restart.sh 8080        # custom port
```

Open **http://localhost:19099** and log in with the credentials above.

Logs are written to `logs/YYYY-MM-DD.log` and rotated daily.

---

## Security

ClaudeManager exposes a Claude Code shell, a file browser, and a sandboxed terminal over HTTP. **Read this before deploying anywhere reachable beyond `localhost`.**

- **Change the defaults.** Replace the `default_admin.password` and `jwt_secret` in `config/config.yaml` before the first start. The example values are widely known.
- **Don't expose it directly to the public internet.** Put it behind a reverse proxy with TLS (Caddy / Nginx / Traefik) and, ideally, an additional layer of access control (basic auth, OAuth proxy, VPN, or IP allowlist).
- **Treat every account as a shell account.** Even non-admin users can open shells and edit files inside the configured workspace. Only invite people you would otherwise hand SSH access.
- **Sandboxing is best-effort.** firejail reduces blast radius but is not a security boundary; assume a determined user can read anything the server process can read.

---

## Usage

### Creating a session

1. Click **New Session**
2. Enter a project name and working directory (autocomplete is available)
3. Optionally paste a Git URL — ClaudeManager will clone it first
4. Click **Create**

Claude Code starts in a tmux pane and the session appears in the list within seconds.

### Session views

| View | Description |
|---|---|
| **Terminal** | Full xterm.js terminal forwarded directly to the tmux pane |
| **Chat** | Read-only conversation bubbles parsed from the Claude JSONL, with mermaid rendering, resizable input, and a right-side dock for Tasks / Goals / AUQs |
| **Code** | Changed files (git diff) and full file tree with diff viewer; one-click copy file contents to clipboard |

### Session actions

| Action | Description |
|---|---|
| **Attach** | Open terminal/chat for a running session |
| **Detach** | Disconnect without stopping the session |
| **Resume** | Restart a terminated session, resuming the last conversation |
| **Terminate** | Stop the tmux session and Claude process |
| **Delete** | Remove the session record (files are not deleted) |
| **Git** | View git log and diff for the working directory |
| **Shell** | Open a plain shell in the working directory |
| **JSONL viewer** | Inspect the raw Claude conversation JSONL |
| **Export Chat** | Download a self-contained HTML file of the full conversation |

### Loading an existing session

Click **Load Session** to browse sessions from `~/.claude/projects/`. Selecting one imports it into ClaudeManager with the working directory detected automatically.

---

## Configuration Reference

All configuration is stored in the SQLite database (`data/data.db` in dev mode, `~/.claudemanager/data.db` in binary mode) and editable from the **Settings** page in the UI. There is no config file.

Key settings:

| Setting | Default | Description |
|---|---|---|
| `default_admin` | `admin` / `admin123` | Admin credentials created on first run — **change immediately** |
| `jwt_secret` | auto-generated | JWT signing key, auto-generated on first run |
| `default_workspace` | `~/Projs` | Root directory for working-directory autocomplete |
| `claude_bin` | `~/.local/bin/claude` | Path to the Claude Code CLI binary |
| `claude_shell` | *(empty)* | Shell wrapper for Claude, e.g. `bash -l` for nvm/rbenv users |
| `google_client_id` | *(empty)* | Google OAuth client ID (optional) |

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `CLAUDE_WEB_PORT` | `19099` | Port for the backend server |
| `CLAUDEMANAGER_DATA_DIR` | `~/.claudemanager` | Data directory (binary mode) |
| `GOOGLE_CLIENT_ID` | *(empty)* | Google OAuth client ID override |

---

## Updating

```bash
git pull
bash init.sh      # reinstall deps and rebuild frontend
bash restart.sh   # restart backend
```

---

## Standalone Binary

Build a single self-contained executable with PyInstaller:

```bash
bash build_binary.sh
# output: dist/claudemanager
```

The binary embeds the frontend and reads config from `~/.claudemanager/config.yaml`.

```
Usage: claudemanager <command> [--host HOST] [--port PORT]

Commands:
  client       Start server (if not running) and open the UI in a desktop window
  server       Start server as a background daemon, then exit
  (no command) Print help and exit

Options:
  --host HOST  Bind address (default: 127.0.0.1)
  --port PORT  Listen port (default: 19099)
```

Examples:

```bash
./dist/claudemanager client            # start server + open UI window
./dist/claudemanager server            # start server in background
./dist/claudemanager server --port 8080
```

---

## Bypass Permissions Mode

ClaudeManager always runs Claude Code in **Bypass Permissions mode** (`--dangerously-skip-permissions`). This is handled automatically — you do not need to accept any confirmation dialog manually.

## PreToolUse Hook Auto-Injection

At server startup, ClaudeManager automatically injects a `PreToolUse` hook into `~/.claude/settings.json`. The hook writes each tool call event to a per-session file at `~/.claude_manager/hooks/<session_id>.jsonl`, which the backend reads to display inline tool approval and AskUserQuestion prompts in the Chat UI.

The injection is idempotent: if the hook is already registered, nothing changes. If a catch-all (no `matcher`) `PreToolUse` entry already exists, the hook is merged into its `hooks` array rather than creating a duplicate entry.

---

## Troubleshooting

**Session stuck in "creating"**
Claude Code CLI may not be on `$PATH` inside the tmux pane. Set `claude_bin` to the absolute path in `config/config.yaml`, or set `claude_shell: "bash -l"` to source your profile.

**JSONL viewer button missing**
The Claude session ID is resolved from `~/.claude/sessions/{pid}.json` a few seconds after the session starts. If it never appears, verify the `claude` process is running as a direct child of the tmux pane.

**Terminal not connecting**
Check `logs/YYYY-MM-DD.log` for WebSocket errors. Ensure the backend port is reachable and no reverse proxy is stripping the `Upgrade: websocket` header.

**"No conversation yet" in Chat view**
The session has no JSONL history yet, or ClaudeManager hasn't resolved the session file. Send at least one message to Claude to populate it.

---

## License

[MIT](LICENSE)
