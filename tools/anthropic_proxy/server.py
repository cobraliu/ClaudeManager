"""
Anthropic API reverse proxy with SSE tap.

Sits between Claude CLI and api.anthropic.com. Forwards every request via an
optional configurable upstream proxy, and for SSE responses it snapshots the
in-flight assistant content to ~/.claude/cached_messages/{session_id}/{ts_ns}.json
roughly every 500 ms so the frontend can preview text/tool_use blocks before the
Claude CLI flushes its JSONL.

Run standalone:
    python3 tools/anthropic_proxy/server.py \
        --port 19098 \
        --upstream-proxy http://127.0.0.1:8118
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

UPSTREAM_HOST = "https://api.anthropic.com"
SNAPSHOT_INTERVAL_S = 0.5
CACHE_ROOT = Path.home() / ".claude" / "cached_messages"
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length", "content-encoding",
}
SESSION_HEADER = "x-claude-code-session-id"

log = logging.getLogger("anthropic_proxy")


def _ts_ns() -> int:
    return time.time_ns()


def _safe_session_dir(session_id: str) -> Path | None:
    if not session_id or "/" in session_id or ".." in session_id:
        return None
    d = CACHE_ROOT / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_snapshot(session_dir: Path, payload: dict[str, Any]) -> None:
    """Atomic write of one snapshot file."""
    name = f"{payload['ts_ns']}.json"
    tmp = session_dir / f".{name}.tmp"
    final = session_dir / name
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(final)
    except Exception as exc:
        log.warning("snapshot write failed for %s: %s", final, exc)


class StreamAggregator:
    """
    Consume Anthropic SSE events and maintain an evolving `content[]` array.

    Anthropic message stream shape (relevant subset):
        event: message_start          → data.message.id
        event: content_block_start    → data.index, data.content_block
        event: content_block_delta    → data.index, data.delta
        event: content_block_stop     → data.index
        event: message_stop
    """

    def __init__(self, session_id: str, session_dir: Path) -> None:
        self.session_id = session_id
        self.session_dir = session_dir
        self.request_id: str | None = None
        self.blocks: dict[int, dict[str, Any]] = {}
        self._last_snapshot_ts: float = 0.0
        self._dirty = False

    def feed_event(self, event_name: str, data: dict[str, Any]) -> None:
        try:
            if event_name == "message_start":
                self.request_id = (data.get("message") or {}).get("id")
            elif event_name == "content_block_start":
                idx = data["index"]
                self.blocks[idx] = dict(data.get("content_block") or {})
                self._dirty = True
            elif event_name == "content_block_delta":
                idx = data["index"]
                delta = data.get("delta") or {}
                self._apply_delta(idx, delta)
                self._dirty = True
            elif event_name == "content_block_stop":
                idx = data["index"]
                blk = self.blocks.get(idx)
                if blk and "_partial_json" in blk:
                    raw = blk.pop("_partial_json", "")
                    if raw:
                        try:
                            blk["input"] = json.loads(raw)
                        except Exception:
                            blk["input"] = {"_raw": raw}
                self._dirty = True
        except Exception as exc:
            log.debug("aggregator feed error (%s): %s", event_name, exc)

    def _apply_delta(self, idx: int, delta: dict[str, Any]) -> None:
        blk = self.blocks.setdefault(idx, {})
        dtype = delta.get("type")
        if dtype == "text_delta":
            blk.setdefault("type", "text")
            blk["text"] = (blk.get("text") or "") + (delta.get("text") or "")
        elif dtype == "input_json_delta":
            blk.setdefault("type", "tool_use")
            blk["_partial_json"] = (blk.get("_partial_json") or "") + (delta.get("partial_json") or "")
        elif dtype == "thinking_delta":
            blk.setdefault("type", "thinking")
            blk["thinking"] = (blk.get("thinking") or "") + (delta.get("thinking") or "")
        elif dtype == "signature_delta":
            blk["signature"] = (blk.get("signature") or "") + (delta.get("signature") or "")

    def maybe_snapshot(self, kind: str = "snapshot") -> None:
        now = time.monotonic()
        if kind == "snapshot":
            if not self._dirty:
                return
            if now - self._last_snapshot_ts < SNAPSHOT_INTERVAL_S:
                return
        content = []
        for idx in sorted(self.blocks.keys()):
            blk = dict(self.blocks[idx])
            raw = blk.pop("_partial_json", None)
            if raw is not None and "input" not in blk:
                blk["input"] = {"_partial_raw": raw}
                blk["partial"] = True
            content.append(blk)
        payload = {
            "session_id": self.session_id,
            "request_id": self.request_id,
            "ts_ns": _ts_ns(),
            "kind": kind,
            "content": content,
        }
        _write_snapshot(self.session_dir, payload)
        self._last_snapshot_ts = now
        self._dirty = False


def _parse_sse_chunk(buf: str) -> tuple[list[tuple[str, dict[str, Any]]], str]:
    """Parse complete SSE events out of `buf`. Returns (events, leftover).

    SSE allows \\n\\n or \\r\\n\\r\\n as event separators; normalise to \\n so a
    single buf.find handles both upstream conventions.
    """
    buf = buf.replace("\r\n", "\n")
    events: list[tuple[str, dict[str, Any]]] = []
    while True:
        sep = buf.find("\n\n")
        if sep < 0:
            return events, buf
        raw, buf = buf[:sep], buf[sep + 2:]
        event_name = ""
        data_parts: list[str] = []
        for line in raw.splitlines():
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_parts.append(line[5:].lstrip())
        if not event_name:
            continue
        data_str = "\n".join(data_parts)
        try:
            data = json.loads(data_str) if data_str else {}
        except Exception:
            data = {}
        events.append((event_name, data))


async def handle(
    request: web.Request,
    upstream_proxy: str | None,
    shared_connector: aiohttp.TCPConnector,
) -> web.StreamResponse:
    url = f"{UPSTREAM_HOST}{request.path_qs}"
    body = await request.read()

    fwd_headers = {}
    for k, v in request.headers.items():
        if k.lower() in HOP_BY_HOP:
            continue
        fwd_headers[k] = v

    session_id = request.headers.get(SESSION_HEADER) or request.headers.get(SESSION_HEADER.title()) or ""
    session_dir = _safe_session_dir(session_id) if session_id else None
    aggregator = StreamAggregator(session_id, session_dir) if session_dir else None

    timeout = aiohttp.ClientTimeout(total=None, sock_read=600, connect=30)
    client_gone = False
    try:
        # connector_owner=False keeps the app-wide TCPConnector alive across requests
        # so TLS handshakes to api.anthropic.com get reused (big win for SSE).
        async with aiohttp.ClientSession(
            connector=shared_connector,
            connector_owner=False,
            timeout=timeout,
        ) as s:
            async with s.request(
                request.method, url,
                data=body if body else None,
                headers=fwd_headers,
                proxy=upstream_proxy or None,
                allow_redirects=False,
            ) as upstream:
                resp_headers = {
                    k: v for k, v in upstream.headers.items()
                    if k.lower() not in HOP_BY_HOP
                }
                resp = web.StreamResponse(status=upstream.status, headers=resp_headers)
                await resp.prepare(request)

                ctype = (upstream.headers.get("Content-Type") or "").lower()
                is_sse = "text/event-stream" in ctype and aggregator is not None
                sse_buf = ""

                async for chunk in upstream.content.iter_any():
                    try:
                        await resp.write(chunk)
                    except ConnectionResetError:
                        client_gone = True
                        break
                    except asyncio.CancelledError:
                        # Task cancellation must propagate after we flush the
                        # final snapshot — swallowing it here would mask
                        # caller-driven shutdowns and leak the upstream socket.
                        client_gone = True
                        if is_sse and aggregator and aggregator._dirty:
                            aggregator.maybe_snapshot(kind="final")
                        raise
                    if is_sse:
                        try:
                            sse_buf += chunk.decode("utf-8", errors="replace")
                        except Exception:
                            continue
                        events, sse_buf = _parse_sse_chunk(sse_buf)
                        for name, data in events:
                            aggregator.feed_event(name, data)
                            if name == "message_stop":
                                aggregator.maybe_snapshot(kind="final")
                        aggregator.maybe_snapshot(kind="snapshot")

                if is_sse and aggregator and aggregator._dirty:
                    aggregator.maybe_snapshot(kind="final")

                if not client_gone:
                    try:
                        await resp.write_eof()
                    except (ConnectionResetError, asyncio.CancelledError):
                        # Client vanished between last chunk and EOF — ignore
                        # the broken write so we don't log a spurious 500.
                        pass
                return resp
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        # aiohttp.ClientError does NOT subclass asyncio.TimeoutError; list both
        # explicitly so sock-read/connect timeouts return a clean 502 instead
        # of bubbling up as unhandled task exceptions.
        log.warning("upstream error for %s: %s", url, exc)
        return web.Response(status=502, text=f"proxy upstream error: {exc}")


def make_app(upstream_proxy: str | None) -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)

    # One TCPConnector shared by every request: lets aiohttp reuse the TLS
    # session to api.anthropic.com instead of doing a fresh handshake per call.
    # Created/closed via aiohttp app lifecycle hooks so it's torn down cleanly.
    async def _on_startup(app_: web.Application) -> None:
        app_["shared_connector"] = aiohttp.TCPConnector(limit=0, ttl_dns_cache=300)

    async def _on_cleanup(app_: web.Application) -> None:
        conn = app_.get("shared_connector")
        if conn is not None:
            await conn.close()

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    async def health(_req: web.Request) -> web.Response:
        return web.json_response({"ok": True, "upstream_proxy": upstream_proxy or ""})

    async def _dispatch(req: web.Request) -> web.StreamResponse:
        return await handle(req, upstream_proxy, req.app["shared_connector"])

    app.router.add_get("/_proxy_health", health)
    app.router.add_route("*", "/{tail:.*}", _dispatch)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Anthropic API reverse proxy + SSE tap.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("ANTHROPIC_PROXY_PORT", "19098")))
    parser.add_argument(
        "--upstream-proxy",
        default=os.environ.get("ANTHROPIC_PROXY_UPSTREAM", ""),
        help="Upstream HTTP(S) proxy URL; pass empty to disable.",
    )
    parser.add_argument("--log-level", default=os.environ.get("ANTHROPIC_PROXY_LOG", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    log.info("listening on %s:%d", args.host, args.port)
    log.info("upstream proxy: %s", args.upstream_proxy or "(direct)")
    log.info("snapshot dir:   %s", CACHE_ROOT)

    web.run_app(
        make_app(args.upstream_proxy or None),
        host=args.host, port=args.port,
        access_log=None, print=lambda *_a, **_kw: None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
