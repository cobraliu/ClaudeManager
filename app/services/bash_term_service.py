"""Manage named + ephemeral bash terminals as tmux sessions.

Each terminal is a tmux session named ``cmterm-<term_id>``. The same tmux
session can be attached by multiple PTYs (one per WebSocket client), which
gives the user "open this terminal from multiple places" out of the box.

Lifecycle:
    - Named terminals (``name`` is set) survive disconnects. Only an explicit
      ``delete()`` kills them.
    - Ephemeral terminals (``name`` is None) are killed once the last attached
      client disconnects.
"""
from __future__ import annotations

import logging
import secrets
import shlex
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from app.services.tmux_service import TmuxService, TmuxError

logger = logging.getLogger(__name__)

TMUX_PREFIX = "cmterm-"


@dataclass
class TermRecord:
    term_id: str
    tmux_name: str
    session_id: str
    user_id: str
    cwd: str
    name: Optional[str]
    created_at: float
    attach_count: int = 0

    @property
    def is_named(self) -> bool:
        return self.name is not None and self.name != ""

    def public(self) -> dict:
        return {
            "term_id": self.term_id,
            "session_id": self.session_id,
            "name": self.name,
            "cwd": self.cwd,
            "is_named": self.is_named,
            "attach_count": self.attach_count,
            "created_at": self.created_at,
        }


class TerminalManager:
    """Holds bash-terminal records and tokens. Thread-safe via a single lock."""

    def __init__(self, tmux: TmuxService) -> None:
        self._tmux = tmux
        self._terms: dict[str, TermRecord] = {}
        self._tokens: dict[str, str] = {}        # token → term_id
        self._lock = threading.RLock()

    # ── lifecycle ──────────────────────────────────────────────────────────

    def create(self, session_id: str, user_id: str, cwd: str, name: Optional[str] = None) -> TermRecord:
        if name is not None:
            name = name.strip() or None
            if name is not None and self._find_named_locked(session_id, user_id, name):
                raise ValueError(f"a named terminal '{name}' already exists in this session")
        term_id = secrets.token_urlsafe(8).replace("-", "_").replace("=", "")
        tmux_name = f"{TMUX_PREFIX}{term_id}"
        # Start a detached tmux session running bash. We don't try to inherit
        # the user's ~/.bashrc deliberately (kept simple); users can `source`
        # if they want. The session is launched in `cwd`.
        # Use bash login mode so PATH from .profile is set up (matches Claude tmux).
        bash_cmd = "bash -l"
        try:
            self._tmux._run("new-session", "-d", "-s", tmux_name, "-c", cwd, bash_cmd)
            self._tmux._run("set-option", "-t", tmux_name, "history-limit", "50000")
            # mouse off: wheel events are intercepted by the frontend (scrollMode="tmux")
            # and translated into server-side `send-keys copy-mode` / `scroll-up`
            # commands, so we never need tmux to consume mouse events directly.
            # Leaving mouse on would also let clicks enter copy-mode unexpectedly.
            self._tmux._run("set-option", "-t", tmux_name, "mouse", "off")
            self._tmux._run("set-option", "-t", tmux_name, "mode-keys", "vi")
        except TmuxError as exc:
            raise RuntimeError(f"failed to create tmux session: {exc}") from exc

        rec = TermRecord(
            term_id=term_id,
            tmux_name=tmux_name,
            session_id=session_id,
            user_id=user_id,
            cwd=cwd,
            name=name,
            created_at=time.time(),
        )
        with self._lock:
            self._terms[term_id] = rec
        logger.info("term.create id=%s name=%r ephemeral=%s", term_id, name, name is None)
        return rec

    def rename(self, term_id: str, name: Optional[str]) -> TermRecord:
        with self._lock:
            rec = self._terms.get(term_id)
            if rec is None:
                raise KeyError(term_id)
            if name is not None:
                name = name.strip() or None
            if name is not None:
                other = self._find_named_locked(rec.session_id, rec.user_id, name)
                if other is not None and other.term_id != term_id:
                    raise ValueError(f"a named terminal '{name}' already exists in this session")
            rec.name = name
            return rec

    def delete(self, term_id: str) -> bool:
        with self._lock:
            rec = self._terms.pop(term_id, None)
        if rec is None:
            return False
        # Drop tokens for this term
        with self._lock:
            stale = [t for t, tid in self._tokens.items() if tid == term_id]
            for t in stale:
                self._tokens.pop(t, None)
        try:
            self._tmux.terminate(rec.tmux_name)
        except TmuxError as exc:
            logger.warning("term.delete tmux kill failed id=%s: %s", term_id, exc)
        logger.info("term.delete id=%s", term_id)
        return True

    def get(self, term_id: str) -> Optional[TermRecord]:
        with self._lock:
            return self._terms.get(term_id)

    def list_for(self, session_id: str, user_id: str, *, is_admin: bool = False) -> list[TermRecord]:
        with self._lock:
            out = [
                r for r in self._terms.values()
                if r.session_id == session_id and (is_admin or r.user_id == user_id)
            ]
        out.sort(key=lambda r: (r.name is None, r.created_at))
        return out

    # ── tokens ─────────────────────────────────────────────────────────────

    def issue_token(self, term_id: str) -> str:
        with self._lock:
            if term_id not in self._terms:
                raise KeyError(term_id)
            token = secrets.token_urlsafe(32)
            self._tokens[token] = term_id
            return token

    def consume_token(self, token: str) -> Optional[str]:
        """Pop a token and return the term_id, or None if invalid."""
        with self._lock:
            return self._tokens.pop(token, None)

    # ── attach refcount (driven by ws handler) ────────────────────────────

    def on_attach(self, term_id: str) -> int:
        with self._lock:
            rec = self._terms.get(term_id)
            if rec is None:
                return 0
            rec.attach_count += 1
            logger.debug("term.attach id=%s count=%d", term_id, rec.attach_count)
            return rec.attach_count

    def on_detach(self, term_id: str) -> tuple[int, bool]:
        """Decrement attach count. Returns (new_count, was_killed)."""
        with self._lock:
            rec = self._terms.get(term_id)
            if rec is None:
                return (0, False)
            rec.attach_count = max(0, rec.attach_count - 1)
            count = rec.attach_count
            should_kill = count == 0 and not rec.is_named
            if should_kill:
                self._terms.pop(term_id, None)
                stale = [t for t, tid in self._tokens.items() if tid == term_id]
                for t in stale:
                    self._tokens.pop(t, None)
        if should_kill:
            try:
                self._tmux.terminate(rec.tmux_name)
            except TmuxError as exc:
                logger.warning("term.detach tmux kill failed id=%s: %s", term_id, exc)
            logger.info("term.detach id=%s killed=ephemeral", term_id)
            return (count, True)
        logger.debug("term.detach id=%s count=%d", term_id, count)
        return (count, False)

    # ── startup reaping ──────────────────────────────────────────────────

    def reap_orphan_tmux_sessions(self) -> int:
        """On startup, kill any cmterm-* tmux sessions we don't know about.

        These are leftovers from a previous backend process. Since refcounts
        live in memory, we can't reliably reattach to them anyway.
        """
        try:
            sessions = self._tmux.list_sessions()
        except TmuxError:
            return 0
        n = 0
        for s in sessions:
            if s.startswith(TMUX_PREFIX):
                try:
                    self._tmux.terminate(s)
                    n += 1
                except TmuxError:
                    pass
        if n:
            logger.info("reaped %d orphan cmterm tmux session(s)", n)
        return n

    # ── internal ──────────────────────────────────────────────────────────

    def _find_named_locked(self, session_id: str, user_id: str, name: str) -> Optional[TermRecord]:
        for r in self._terms.values():
            if r.session_id == session_id and r.user_id == user_id and r.name == name:
                return r
        return None


# Re-export the bash command builder for unit testing if needed.
_BASH_CMD = "bash -l"  # noqa: F841 — referenced symbolically
