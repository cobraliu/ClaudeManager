"""Manage named + ephemeral bash terminals as tmux sessions.

Each terminal is a tmux session named ``cmterm-<term_id>``. The same tmux
session can be attached by multiple PTYs (one per WebSocket client), which
gives the user "open this terminal from multiple places" out of the box.

Lifecycle:
    - Named terminals (``name`` is set) survive disconnects. Only an explicit
      ``delete()`` kills them.
    - Ephemeral terminals (``name`` is None) follow a four-state machine that
      avoids killing them just because the browser refreshed:
        * attached: ``attach_count > 0``. Always visible. Heartbeats no-op.
        * idle:     ``attach_count == 0``, last holder seen within
                    ``EPHEMERAL_IDLE_GRACE``. Still visible in the list so
                    cached term_ids can reattach. Heartbeats refresh the
                    "last holder" timestamp.
        * standby:  no holder for longer than ``EPHEMERAL_IDLE_GRACE``.
                    Hidden from the list (a fresh client won't see it), but
                    a client that already cached the term_id can still call
                    the token / heartbeat endpoints. Standby lasts
                    ``STANDBY_GRACE`` then the tmux session is killed.
        * kept:     promoted from standby when a heartbeat or attach revives
                    it. ``kept`` terminals are no longer subject to auto-close
                    (behaves like a named one) — the assumption is that any
                    client that bothered to revive it really wants it.
"""
from __future__ import annotations

import logging
import secrets
import shlex
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from app.services.tmux_service import TmuxService, TmuxError

logger = logging.getLogger(__name__)

TMUX_PREFIX = "cmterm-"

# Default idle / standby grace if the caller doesn't supply config getters
# (e.g. unit tests). The production wiring in ``claudemanager.py`` reads from
# ``app.config`` so the user can tune via the API.
DEFAULT_IDLE_GRACE_S = 600.0
DEFAULT_STANDBY_GRACE_S = 30.0
SWEEPER_TICK_S = 5.0  # how often the sweeper checks — not user-configurable


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
    # "Last holder" timestamp: refreshed on attach, detach-to-zero, and
    # heartbeat. The sweeper uses this to decide when to enter standby.
    last_holder_at: float = 0.0
    # Set to the wall time when the record enters standby; None otherwise.
    standby_at: Optional[float] = None
    # True once promoted from standby (heartbeat or reattach revived it).
    # Behaves like a named terminal from then on — sweeper won't touch it.
    kept: bool = False

    @property
    def is_named(self) -> bool:
        return self.name is not None and self.name != ""

    @property
    def is_immortal(self) -> bool:
        """Named or kept terminals don't get auto-closed by the sweeper."""
        return self.is_named or self.kept

    @property
    def is_standby(self) -> bool:
        return self.standby_at is not None

    def public(self) -> dict:
        return {
            "term_id": self.term_id,
            "session_id": self.session_id,
            "name": self.name,
            "cwd": self.cwd,
            "is_named": self.is_named,
            "attach_count": self.attach_count,
            "created_at": self.created_at,
            "kept": self.kept,
        }


class TerminalManager:
    """Holds bash-terminal records and tokens. Thread-safe via a single lock."""

    def __init__(
        self,
        tmux: TmuxService,
        *,
        idle_grace_getter: Optional[Callable[[], float]] = None,
        standby_grace_getter: Optional[Callable[[], float]] = None,
    ) -> None:
        self._tmux = tmux
        self._terms: dict[str, TermRecord] = {}
        self._tokens: dict[str, str] = {}        # token → term_id
        self._lock = threading.RLock()
        # Read the timings via callbacks so changes via the config API apply
        # without a server restart. Fall back to constants for tests.
        self._idle_grace_getter: Callable[[], float] = (
            idle_grace_getter or (lambda: DEFAULT_IDLE_GRACE_S)
        )
        self._standby_grace_getter: Callable[[], float] = (
            standby_grace_getter or (lambda: DEFAULT_STANDBY_GRACE_S)
        )
        # Background sweeper drives the ephemeral lifecycle. Daemon so it
        # doesn't block process shutdown; uvicorn reload spawns a fresh one.
        self._sweeper_stop = threading.Event()
        self._sweeper_thread = threading.Thread(
            target=self._sweeper_loop, name="term-sweeper", daemon=True,
        )
        self._sweeper_thread.start()

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

        now = time.time()
        rec = TermRecord(
            term_id=term_id,
            tmux_name=tmux_name,
            session_id=session_id,
            user_id=user_id,
            cwd=cwd,
            name=name,
            created_at=now,
            last_holder_at=now,  # start the idle clock from creation
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
                if r.session_id == session_id
                and (is_admin or r.user_id == user_id)
                # Hide standby items from the dropdown — a fresh tab won't see
                # them, but a tab that cached the term_id can still revive via
                # heartbeat or token/attach.
                and not r.is_standby
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
            # An attach during the standby window counts as a "client cared
            # enough to come back" — revive and promote so future detaches
            # don't tear the terminal down again.
            if rec.is_standby:
                rec.standby_at = None
                rec.kept = True
                logger.info("term.revive id=%s via=attach", term_id)
            rec.attach_count += 1
            rec.last_holder_at = time.time()
            logger.debug("term.attach id=%s count=%d", term_id, rec.attach_count)
            return rec.attach_count

    def on_detach(self, term_id: str) -> tuple[int, bool]:
        """Decrement attach count. Returns (new_count, was_killed).

        Ephemerals are no longer killed immediately when the last client
        disconnects — the sweeper handles idle→standby→kill so a quick page
        refresh can reattach to the same tmux session.
        """
        with self._lock:
            rec = self._terms.get(term_id)
            if rec is None:
                return (0, False)
            rec.attach_count = max(0, rec.attach_count - 1)
            count = rec.attach_count
            if count == 0:
                # Start the idle countdown from "right now".
                rec.last_holder_at = time.time()
        logger.debug("term.detach id=%s count=%d", term_id, count)
        return (count, False)

    def heartbeat(self, term_id: str) -> Optional[TermRecord]:
        """A client with a cached term_id reports it still wants this terminal.

        Returns the (possibly updated) record, or None if the term is gone.

        - In ``idle``: refreshes ``last_holder_at`` so standby doesn't fire.
        - In ``standby``: revives and promotes to ``kept`` (no auto-close).
        - For named/kept: idempotent touch.
        """
        with self._lock:
            rec = self._terms.get(term_id)
            if rec is None:
                return None
            if rec.is_standby:
                rec.standby_at = None
                rec.kept = True
                logger.info("term.revive id=%s via=heartbeat", term_id)
            rec.last_holder_at = time.time()
            return rec

    # ── ephemeral lifecycle sweeper ───────────────────────────────────────

    def _sweeper_loop(self) -> None:
        while not self._sweeper_stop.is_set():
            try:
                self._sweep_once()
            except Exception:
                logger.exception("term sweeper tick failed")
            # Event.wait returns True if stop signalled, so we exit promptly.
            if self._sweeper_stop.wait(SWEEPER_TICK_S):
                return

    def _sweep_once(self) -> None:
        """One sweeper tick: drive idle→standby and standby→kill transitions."""
        # Read once per tick — keeps the sweeper cheap while still picking up
        # live config changes without a restart.
        idle_grace = float(self._idle_grace_getter())
        standby_grace = float(self._standby_grace_getter())
        now = time.time()
        to_kill: list[TermRecord] = []
        with self._lock:
            for rec in list(self._terms.values()):
                if rec.is_immortal or rec.attach_count > 0:
                    continue
                if rec.standby_at is None:
                    if now - rec.last_holder_at > idle_grace:
                        rec.standby_at = now
                        logger.info(
                            "term.standby id=%s idle-for=%.0fs",
                            rec.term_id, now - rec.last_holder_at,
                        )
                else:
                    if now - rec.standby_at > standby_grace:
                        to_kill.append(rec)
                        self._terms.pop(rec.term_id, None)
                        stale = [t for t, tid in self._tokens.items() if tid == rec.term_id]
                        for t in stale:
                            self._tokens.pop(t, None)
        for rec in to_kill:
            try:
                self._tmux.terminate(rec.tmux_name)
            except TmuxError as exc:
                logger.warning("term auto-close kill failed id=%s: %s", rec.term_id, exc)
            logger.info("term.auto-close id=%s standby-elapsed", rec.term_id)

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
