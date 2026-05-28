"""In-memory cache of currently-valid conversation shares.

The public share viewer hits this cache on every paginated request, so it stays
hot and avoids a DB round-trip per page. The durable copy lives in the `shares`
SQLite table; this cache is warmed at startup (`load`) and kept in sync by the
authed create/delete endpoints (`put`/`remove`) plus the periodic cleanup loop
(`purge_expired`). A lookup of an expired entry is evicted lazily on read.
"""

from __future__ import annotations

import time
from threading import Lock
from typing import Iterable

from app.models.session import ShareRecord


class ShareCache:
    def __init__(self) -> None:
        self._d: dict[str, ShareRecord] = {}
        self._lock = Lock()

    def load(self, records: Iterable[ShareRecord]) -> None:
        with self._lock:
            self._d = {r.hash: r for r in records}

    def put(self, record: ShareRecord) -> None:
        with self._lock:
            self._d[record.hash] = record

    def remove(self, hash: str) -> None:
        with self._lock:
            self._d.pop(hash, None)

    def get(self, hash: str) -> ShareRecord | None:
        """Return the share if present and not expired; evict lazily otherwise."""
        with self._lock:
            rec = self._d.get(hash)
            if rec is None:
                return None
            if rec.expires_at <= time.time():
                self._d.pop(hash, None)
                return None
            return rec

    def purge_expired(self, now: float) -> list[str]:
        with self._lock:
            expired = [h for h, r in self._d.items() if r.expires_at <= now]
            for h in expired:
                self._d.pop(h, None)
            return expired


# Module-level singleton shared across requests and the cleanup loop.
share_cache = ShareCache()
