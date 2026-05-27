"""Background backfill of prompt_history from agent JSONL files.

Sessions created before the on-send recording hook landed (and any session
whose prompts were keyed in via xterm rather than the chat WS) have empty
prompt_history rows in the DB. This module scans every session at startup
and on a slow loop, locates the matching JSONL, and seeds prompt_history
from the user-type entries — so the history button surfaces the past as
well as new sends.

Marker table: prompt_history_backfill (one row per completed session).
"""
from __future__ import annotations

import asyncio
import logging
import time

from app.models.session import SessionMetadata
from app.services.claude_session_reader import iter_user_prompts
from app.services.session_store import SessionStore

logger = logging.getLogger(__name__)

# How often the scanner loop fires after the initial pass.
_LOOP_SECONDS = 5 * 60


def backfill_session(store: SessionStore, session: SessionMetadata) -> int:
    """Backfill one session if not already done. Returns inserted-row count."""
    sid = session.id
    if store.is_prompt_history_backfilled(sid):
        return 0
    # We only know how to backfill Claude JSONL right now. Cursor + Codex
    # follow-up: mark them complete with 0 inserts so the scanner stops
    # re-checking. They can be re-driven by clearing the row.
    inserted = 0
    if session.tool == "claude" and session.agent_session_id and session.cwd:
        try:
            entries: list[tuple[str, float, str | None]] = []
            for text, ts in iter_user_prompts(session.agent_session_id, session.cwd):
                if not text:
                    continue
                entries.append((text, ts if ts > 0 else time.time(), "0"))
            inserted = store.bulk_insert_prompt_history(sid, entries)
        except Exception:
            logger.exception("backfill failed for session %s", sid)
            # Don't mark complete on failure — let the next loop retry.
            return 0
    store.mark_prompt_history_backfilled(sid, time.time())
    if inserted:
        logger.info("prompt_history backfill: session=%s inserted=%d", sid, inserted)
    return inserted


def backfill_pending(store: SessionStore) -> tuple[int, int]:
    """Scan all sessions, backfill the ones missing a marker row.
    Returns (sessions_touched, rows_inserted)."""
    touched = 0
    rows = 0
    for session in store.all():
        if store.is_prompt_history_backfilled(session.id):
            continue
        touched += 1
        rows += backfill_session(store, session)
    return touched, rows


async def run_loop(store: SessionStore, stop_event: asyncio.Event) -> None:
    """Run an initial backfill pass then sleep-and-repeat. Yield to the event
    loop between sessions so the scanner can't block the API."""
    loop = asyncio.get_running_loop()
    while not stop_event.is_set():
        try:
            touched, rows = await loop.run_in_executor(None, backfill_pending, store)
            if touched:
                logger.info(
                    "prompt_history backfill scan: %d session(s) touched, %d row(s) inserted",
                    touched, rows,
                )
        except Exception:
            logger.exception("prompt_history backfill scan failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_LOOP_SECONDS)
        except asyncio.TimeoutError:
            pass
