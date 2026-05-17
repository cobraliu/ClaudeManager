from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.config import get_proxy
from app.security import CurrentUser

router = APIRouter(prefix="/api", tags=["usage"])

_cache: dict = {}
_cache_ts: float = 0.0
_CACHE_TTL = 300.0  # 5 minutes

_CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_BETA = "oauth-2025-04-20"

# (result_key, header_abbrev)
_WINDOWS = [("five_hour", "5h"), ("seven_day", "7d")]


def _get_access_token() -> str | None:
    try:
        creds = json.loads(_CREDENTIALS_FILE.read_text())
        return creds.get("claudeAiOauth", {}).get("accessToken")
    except Exception:
        return None


def _make_opener() -> urllib.request.OpenerDirector:
    """Use the proxy configured in Admin → Proxy Settings, same as Claude processes."""
    proxy = get_proxy()
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


def _fetch_rate_limits() -> dict:
    token = _get_access_token()
    if not token:
        return {}

    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "."}],
    }).encode()

    req = urllib.request.Request(
        _MESSAGES_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": _ANTHROPIC_BETA,
            "Content-Type": "application/json",
            "anthropic-version": _ANTHROPIC_VERSION,
        },
    )

    opener = _make_opener()
    with opener.open(req, timeout=15) as resp:
        hdrs = dict(resp.headers)

    result: dict = {}
    for window, abbrev in _WINDOWS:
        util = hdrs.get(f"anthropic-ratelimit-unified-{abbrev}-utilization")
        reset_ts = hdrs.get(f"anthropic-ratelimit-unified-{abbrev}-reset")
        if util is not None:
            resets_at = (
                datetime.fromtimestamp(int(reset_ts), tz=timezone.utc).isoformat()
                if reset_ts else None
            )
            result[window] = {
                "utilization": float(util),
                "resets_at": resets_at,
            }

    return result


@router.get("/usage")
async def get_usage(_user: CurrentUser) -> dict:
    import asyncio
    global _cache, _cache_ts
    now = time.time()
    if _cache and now - _cache_ts < _CACHE_TTL:
        return _cache

    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, _fetch_rate_limits)
    except Exception as e:
        if _cache:
            return _cache  # return stale cache on error
        raise HTTPException(status_code=502, detail=f"Failed to fetch usage: {e}")

    _cache = data
    _cache_ts = now
    return data
