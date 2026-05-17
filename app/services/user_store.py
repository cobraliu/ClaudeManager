from __future__ import annotations

import hashlib
import secrets
import sqlite3
from pathlib import Path
from threading import Lock

from app.models.user import User, UserRole

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username       TEXT PRIMARY KEY,
    password_hash  TEXT NOT NULL DEFAULT '',
    salt           TEXT NOT NULL DEFAULT '',
    role           TEXT NOT NULL DEFAULT 'user',
    is_admin       INTEGER NOT NULL DEFAULT 0,
    google_email   TEXT
);
"""


def _hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000)
    return h.hex(), salt


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        username=row["username"],
        password_hash=row["password_hash"],
        role=UserRole(row["role"]),
        is_admin=bool(row["is_admin"]),
        google_email=row["google_email"],
    )


class UserStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = str(db_path)
        self._lock = Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def create_user(self, username: str, password: str, role: UserRole = UserRole.USER) -> User:
        with self._lock:
            pw_hash, salt = _hash_password(password)
            try:
                self._conn.execute(
                    "INSERT INTO users (username, password_hash, salt, role, is_admin) VALUES (?,?,?,?,0)",
                    (username, pw_hash, salt, role.value),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                raise ValueError(f"user '{username}' already exists")
            row = self._conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            return _row_to_user(row)

    def verify_password(self, username: str, password: str) -> User | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            return None
        pw_hash, _ = _hash_password(password, row["salt"])
        if pw_hash == row["password_hash"]:
            return _row_to_user(row)
        return None

    def change_password(self, username: str, new_password: str) -> bool:
        with self._lock:
            pw_hash, salt = _hash_password(new_password)
            cur = self._conn.execute(
                "UPDATE users SET password_hash = ?, salt = ? WHERE username = ?",
                (pw_hash, salt, username),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get(self, username: str) -> User | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            return _row_to_user(row) if row else None

    def list_users(self) -> list[User]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM users").fetchall()
            return [_row_to_user(r) for r in rows]

    def delete_user(self, username: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM users WHERE username = ?", (username,))
            self._conn.commit()
            return cur.rowcount > 0

    def set_is_admin(self, username: str, is_admin: bool) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET is_admin = ? WHERE username = ?",
                (1 if is_admin else 0, username),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def find_by_google_email(self, email: str) -> User | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE google_email = ?", (email,)).fetchone()
            return _row_to_user(row) if row else None

    def link_google_email(self, username: str, email: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET google_email = ? WHERE username = ?",
                (email, username),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def create_google_user(self, username: str, email: str, role: UserRole = UserRole.USER) -> User:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO users (username, password_hash, salt, role, is_admin, google_email) VALUES (?,?,?,?,0,?)",
                    (username, "", "", role.value, email),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                raise ValueError(f"user '{username}' already exists")
            row = self._conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            return _row_to_user(row)

    def is_empty(self) -> bool:
        with self._lock:
            count = self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            return count == 0
