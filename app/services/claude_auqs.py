"""Read AskUserQuestion (AUQ) history from a session JSONL.

AUQ data lives in two places per call:
  * `type: "assistant"` entry with a tool_use block named "AskUserQuestion".
    The `input.questions` list carries the prompts and option metadata.
  * `type: "user"` entry whose content[*].tool_use_id matches; the entry's
    top-level `toolUseResult.answers` dict maps question text → chosen label.
"""
from __future__ import annotations

import json
from datetime import datetime

from app.services.claude_session_reader import _find_session_jsonl


def _parse_ts(d: dict) -> float:
    ts = d.get("timestamp", "")
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def list_auqs(claude_session_id: str, cwd: str) -> list[dict]:
    """Return all AUQ rounds in this session, in chronological order.

    Each item:
      {
        "tool_use_id": str,
        "ts": float,                  # when Claude asked
        "answered_ts": float | None,  # when the user answered (None if pending)
        "questions": [{question, header, multiSelect, options}],
        "answers": {question_text: answer_label, ...} | None,
      }
    """
    jsonl = _find_session_jsonl(claude_session_id, cwd)
    if jsonl is None:
        return []

    asks: dict[str, dict] = {}   # tool_use_id → ask record
    order: list[str] = []        # insertion order of tool_use_ids

    try:
        with open(jsonl) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue

                t = d.get("type")

                # ─ Claude asks ──────────────────────────────────────────────
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
                        if b.get("name") != "AskUserQuestion":
                            continue
                        tuid = b.get("id")
                        if not tuid or tuid in asks:
                            continue
                        inp = b.get("input") or {}
                        questions = inp.get("questions") or []
                        # Some entries store questions as a JSON-encoded string.
                        if isinstance(questions, str):
                            try:
                                questions = json.loads(questions)
                            except json.JSONDecodeError:
                                questions = []
                        if not isinstance(questions, list):
                            questions = []
                        asks[tuid] = {
                            "tool_use_id": tuid,
                            "ts": _parse_ts(d),
                            "answered_ts": None,
                            "questions": questions,
                            "answers": None,
                        }
                        order.append(tuid)

                # ─ User answers ─────────────────────────────────────────────
                elif t == "user":
                    msg = d.get("message") or {}
                    content = msg.get("content") or []
                    if not isinstance(content, list):
                        continue
                    matched_id = None
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            tuid = b.get("tool_use_id")
                            if tuid and tuid in asks:
                                matched_id = tuid
                                break
                    if not matched_id:
                        continue
                    tur = d.get("toolUseResult")
                    asks[matched_id]["answered_ts"] = _parse_ts(d)
                    if isinstance(tur, dict):
                        ans = tur.get("answers")
                        if isinstance(ans, dict):
                            asks[matched_id]["answers"] = ans
    except OSError:
        return []

    return [asks[tuid] for tuid in order]
