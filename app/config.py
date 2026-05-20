from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from pathlib import Path

# ── DB bootstrap (no YAML dependency) ─────────────────────────────────────────

_DB_CONN: sqlite3.Connection | None = None
_DB_LOCK = threading.Lock()


def _data_dir() -> Path | None:
    """Returns the data directory override, or None for dev (relative) behavior."""
    env = os.getenv("CLAUDEMANAGER_DATA_DIR")
    if env:
        p = Path(env)
        p.mkdir(parents=True, exist_ok=True)
        return p
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        p = Path.home() / ".claudemanager"
        p.mkdir(parents=True, exist_ok=True)
        return p
    return None


def _db_file() -> Path:
    dd = _data_dir()
    if dd is not None:
        return dd / "data.db"
    p = Path(__file__).resolve().parent.parent / "data" / "data.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def get_session_db_path() -> Path:
    return _db_file()


def _get_conn() -> sqlite3.Connection:
    global _DB_CONN
    if _DB_CONN is not None:
        return _DB_CONN
    with _DB_LOCK:
        if _DB_CONN is None:
            conn = sqlite3.connect(str(_db_file()), check_same_thread=False)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS configs (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.commit()
            _DB_CONN = conn
    return _DB_CONN


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _get(key: str, default: str = "") -> str:
    conn = _get_conn()
    with _DB_LOCK:
        row = conn.execute("SELECT value FROM configs WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default


def _set(key: str, value: str) -> None:
    conn = _get_conn()
    with _DB_LOCK:
        conn.execute(
            "INSERT OR REPLACE INTO configs (key, value) VALUES (?, ?)", (key, value)
        )
        conn.commit()


def _get_json(key: str, default: dict) -> dict:
    raw = _get(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


# ── Public config API ─────────────────────────────────────────────────────────

def get_proxy() -> str:
    return _get("proxy")


def set_proxy(value: str) -> None:
    _set("proxy", value)


def get_proxy_env() -> dict[str, str]:
    """Return proxy as env-var dict for subprocess injection."""
    p = get_proxy()
    if not p:
        return {}
    return {
        "http_proxy": p, "HTTP_PROXY": p,
        "https_proxy": p, "HTTPS_PROXY": p,
    }


def get_default_workspace() -> str:
    val = _get("default_workspace")
    if not val:
        val = str(Path.home() / "Projs")
        _set("default_workspace", val)
    Path(val).mkdir(parents=True, exist_ok=True)
    return val


def set_default_workspace(path: str) -> None:
    _set("default_workspace", path)


def get_jwt_secret() -> str:
    val = _get("jwt_secret")
    if not val:
        import secrets as _secrets
        val = _secrets.token_hex(32)
        _set("jwt_secret", val)
    return val


def get_claude_bin() -> str:
    val = _get("claude_bin")
    if not val:
        val = str(Path.home() / ".local" / "bin" / "claude")
    return val


def set_claude_bin(path: str) -> None:
    _set("claude_bin", path)


def get_claude_shell() -> str:
    return _get("claude_shell")


def get_cursor_bin() -> str:
    return _get("cursor_bin", "agent")


def set_cursor_bin(path: str) -> None:
    _set("cursor_bin", path)


_DEFAULT_TERMINAL_FONT = '"Ubuntu Sans Mono", "WenQuanYi Micro Hei Mono", "WenQuanYi Zen Hei Mono", monospace'


def get_terminal_font() -> str:
    return _get("terminal_font", _DEFAULT_TERMINAL_FONT)


def set_terminal_font(font: str) -> None:
    _set("terminal_font", font)


def get_google_client_id() -> str:
    return _get("google_client_id", os.getenv("GOOGLE_CLIENT_ID", ""))


def get_default_admin() -> dict[str, str]:
    return _get_json("default_admin", {"username": "admin", "password": "admin123"})


# ── Bash terminal lifecycle tuning ────────────────────────────────────────────
# Defaults match the documented behavior: 10-min idle window, 30-s standby
# grace. Kept as separate config keys so users can shrink them for testing or
# stretch them on a workstation that's left running over lunch.

_DEFAULT_TERM_IDLE_GRACE_S = 600
_DEFAULT_TERM_STANDBY_GRACE_S = 30


def get_term_idle_grace_seconds() -> int:
    raw = _get("term_idle_grace_seconds")
    try:
        v = int(raw) if raw else _DEFAULT_TERM_IDLE_GRACE_S
    except ValueError:
        v = _DEFAULT_TERM_IDLE_GRACE_S
    return max(10, v)


def set_term_idle_grace_seconds(value: int) -> None:
    _set("term_idle_grace_seconds", str(max(10, int(value))))


def get_term_standby_grace_seconds() -> int:
    raw = _get("term_standby_grace_seconds")
    try:
        v = int(raw) if raw else _DEFAULT_TERM_STANDBY_GRACE_S
    except ValueError:
        v = _DEFAULT_TERM_STANDBY_GRACE_S
    return max(5, v)


def set_term_standby_grace_seconds(value: int) -> None:
    _set("term_standby_grace_seconds", str(max(5, int(value))))
