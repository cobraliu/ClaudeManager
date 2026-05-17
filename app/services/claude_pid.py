from __future__ import annotations

import json
import re
from pathlib import Path

# Matches ANSI escape sequences (colors, cursor movement, etc.)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[=>]|\r")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def parse_auq_from_screen(screen: str) -> dict | None:
    """Parse AskUserQuestion data from a captured tmux TUI screen.

    Returns:
      {
        "question": str,
        "header": str,
        "multiSelect": bool,
        "allowFreeform": bool,   # True when "Type something." option exists
        "options": [{"label": str, "description": str}]
      }
    or None if the screen doesn't show an AUQ widget.
    """
    clean = _strip_ansi(screen)
    lines = clean.splitlines()

    header = ""
    question = ""
    options: list[dict] = []
    multi_select = False
    allow_freeform = False
    state = "before"  # before → header_found → question_found → options

    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        i += 1

        if state == "before":
            if "☐" in stripped:
                header = stripped.replace("☐", "").strip()
                state = "header_found"
            continue

        if state == "header_found":
            if stripped:
                question = stripped
                state = "question_found"
            continue

        if state == "question_found":
            # Multi-select: options have [ ] or [x] prefix
            multi_m = re.match(r"^[❯>]?\s*\[[ x]\]\s+(.+)$", stripped)
            if multi_m:
                multi_select = True
                label = multi_m.group(1).strip()
                checked = "[x]" in stripped
                if label.lower() not in ("type something.", "chat about this"):
                    options.append({"label": label, "description": "", "checked": checked})
                elif label.lower() == "type something.":
                    allow_freeform = True
                continue

            # Single-select: numbered options
            opt_m = re.match(r"^[❯>]?\s*(\d+)\.\s+(.+)$", stripped)
            if opt_m:
                label = opt_m.group(2).strip()
                if label.lower() == "type something.":
                    allow_freeform = True
                elif label.lower() != "chat about this":
                    desc = ""
                    if i < len(lines):
                        next_stripped = lines[i].strip()
                        if next_stripped and not re.match(r"^[❯>]?\s*\d+\.", next_stripped):
                            if "Enter to select" not in next_stripped and "─" not in next_stripped:
                                desc = next_stripped
                                i += 1
                    options.append({"label": label, "description": desc})
            elif "Enter to select" in stripped or ("space to select" in stripped.lower()) or ("─" * 10 in stripped and options):
                break

    if question and (options or allow_freeform):
        return {
            "question": question,
            "header": header,
            "multiSelect": multi_select,
            "allowFreeform": allow_freeform,
            "options": options,
        }
    return None


def get_pid_waiting_state(pid: int | None) -> tuple[str, str] | None:
    """Return (waitingFor, hint_type) if Claude CLI is paused waiting for user input.

    hint_type is one of:
      "auq"     — AskUserQuestion: user should reply in Chat
      "approve" — tool approval: user must interact in TUI

    Returns None when the process is not waiting.
    """
    if not pid:
        return None
    try:
        sf = Path.home() / ".claude" / "sessions" / f"{pid}.json"
        if not sf.exists():
            return None
        data = json.loads(sf.read_text())
        if data.get("status") != "waiting":
            return None
        waiting_for: str = data.get("waitingFor") or ""
        if not waiting_for:
            return None
        if "AskUserQuestion" in waiting_for:
            return (waiting_for, "auq")
        return (waiting_for, "approve")
    except (json.JSONDecodeError, OSError):
        return None


def format_tui_hint(waiting_for: str, hint_type: str) -> str:
    if hint_type == "auq":
        return "Claude is asking a question — answer in Chat or switch to TUI"
    # approve * → extract tool name
    tool = waiting_for.removeprefix("approve ").strip() or "action"
    _labels = {
        "Bash": "run a command",
        "Write": "write a file",
        "Edit": "edit a file",
        "MultiEdit": "edit files",
        "Read": "read a file",
        "WebFetch": "fetch a URL",
        "WebSearch": "perform a search",
    }
    desc = _labels.get(tool, f"use {tool}")
    return f"Claude needs approval to {desc} — go to TUI"
