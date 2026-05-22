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
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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

# Cap accumulated SSE buffer to 4 MB. If upstream sends a stream that never
# yields an event separator (broken HTML error page misadvertised as SSE,
# etc.), the per-request buffer would otherwise grow unbounded.
SSE_BUFFER_MAX = 4 * 1024 * 1024

# session_id arrives in a request header; treat it as untrusted input even
# though Claude CLI is the only known producer. Restrict to a UUID-ish alphabet
# so we never form a path that escapes CACHE_ROOT.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

log = logging.getLogger("anthropic_proxy")

# Lifetime counters surfaced via /_proxy_health. Plain dict — proxy is
# single-process asyncio so we don't need locking.
_STATS: dict[str, Any] = {
    "requests_total": 0,
    "sse_streams_total": 0,
    "snapshots_total": 0,
    "client_disconnects_total": 0,
    "upstream_errors_total": 0,
}
_START_MONOTONIC: float = 0.0  # set in main() at server start


def _ts_ns() -> int:
    return time.time_ns()


def _safe_session_dir(session_id: str) -> Path | None:
    if not SESSION_ID_RE.match(session_id or ""):
        return None
    d = CACHE_ROOT / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_snapshot(session_dir: Path, payload: dict[str, Any]) -> bool:
    """Atomic write of one snapshot file. Returns True on success."""
    name = f"{payload['ts_ns']}.json"
    tmp = session_dir / f".{name}.tmp"
    final = session_dir / name
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(final)
        _STATS["snapshots_total"] += 1
        log.debug("snapshot session=%s kind=%s blocks=%d → %s",
                  payload.get("session_id"), payload.get("kind"),
                  len(payload.get("content") or []), final)
        return True
    except Exception as exc:
        log.warning("snapshot write failed for %s: %s", final, exc)
        return False


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
        # per-request counters for the access log line
        self.events_seen = 0
        self.snapshots_taken = 0

    def feed_event(self, event_name: str, data: dict[str, Any]) -> None:
        self.events_seen += 1
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
        if _write_snapshot(self.session_dir, payload):
            self.snapshots_taken += 1
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
    _STATS["requests_total"] += 1
    t0 = time.monotonic()
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

    log.debug("req start method=%s path=%s session=%s body_bytes=%d",
              request.method, request.path, session_id or "-", len(body))

    timeout = aiohttp.ClientTimeout(total=None, sock_read=600, connect=30)
    client_gone = False
    resp_prepared = False
    status = 0
    bytes_sent = 0
    is_sse = False
    sse_buffer_overflow = False
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
                status = upstream.status
                resp = web.StreamResponse(status=status, headers=resp_headers)
                await resp.prepare(request)
                resp_prepared = True

                ctype = (upstream.headers.get("Content-Type") or "").lower()
                is_sse = "text/event-stream" in ctype and aggregator is not None
                if is_sse:
                    _STATS["sse_streams_total"] += 1
                sse_buf = ""

                async for chunk in upstream.content.iter_any():
                    try:
                        await resp.write(chunk)
                        bytes_sent += len(chunk)
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
                        # Defensive cap: a stream that never produces an event
                        # separator would otherwise hold the whole response in
                        # memory. Drop the buffer and emit one warning per req.
                        if len(sse_buf) > SSE_BUFFER_MAX:
                            if not sse_buffer_overflow:
                                log.warning(
                                    "sse buffer cap hit (%d bytes) session=%s url=%s — "
                                    "dropping accumulated buf; further parsing may be partial",
                                    len(sse_buf), session_id or "-", url)
                            sse_buffer_overflow = True
                            sse_buf = ""
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
                        client_gone = True
                return resp
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        # aiohttp.ClientError does NOT subclass asyncio.TimeoutError; list both
        # explicitly so sock-read/connect timeouts return a clean 502 instead
        # of bubbling up as unhandled task exceptions.
        #
        # Two distinct origins funnel here:
        #   (a) the request never connected upstream / upstream errored before
        #       resp.prepare() — we own the response and should send a 502.
        #   (b) we already prepared the response and were mid-stream, then
        #       aiohttp surfaced "Cannot write to closing transport" on
        #       context-exit because the *client* (Claude CLI) hung up. The
        #       response is already in-flight; returning a new web.Response is
        #       illegal and aiohttp will discard or raise — log it as a
        #       client-driven event and return whatever we had.
        if resp_prepared:
            client_gone = True
            log.info("client disconnected mid-stream url=%s session=%s after=%dB events=%d snapshots=%d cause=%s",
                     url, session_id or "-", bytes_sent,
                     aggregator.events_seen if aggregator else 0,
                     aggregator.snapshots_taken if aggregator else 0,
                     exc)
            # `resp` was created inside the inner `async with` whose exit
            # raised; it's out of scope here. Return a placeholder — aiohttp
            # ignores the body once the response is in-flight.
            return web.Response(status=status or 200)
        _STATS["upstream_errors_total"] += 1
        log.warning("upstream error url=%s session=%s: %s", url, session_id or "-", exc)
        return web.Response(status=502, text=f"proxy upstream error: {exc}")
    finally:
        if client_gone:
            _STATS["client_disconnects_total"] += 1
        dur_ms = int((time.monotonic() - t0) * 1000)
        log.info(
            "req method=%s path=%s session=%s status=%d sse=%s dur_ms=%d bytes=%d events=%d snapshots=%d%s%s",
            request.method, request.path, session_id or "-",
            status, "Y" if is_sse else "N",
            dur_ms, bytes_sent,
            aggregator.events_seen if aggregator else 0,
            aggregator.snapshots_taken if aggregator else 0,
            " client_gone=Y" if client_gone else "",
            " sse_overflow=Y" if sse_buffer_overflow else "",
        )


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
        uptime = time.monotonic() - _START_MONOTONIC if _START_MONOTONIC else 0.0
        return web.json_response({
            "ok": True,
            "pid": os.getpid(),
            "upstream_proxy": upstream_proxy or "",
            "snapshot_dir": str(CACHE_ROOT),
            "uptime_s": round(uptime, 1),
            **_STATS,
        })

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

    # Self-loop guard: if --upstream-proxy points at the same host:port we're
    # binding, we'd recursively forward to ourselves. The 12:28 incident on
    # 2026-05-22 hit this with --port 8118 --upstream-proxy http://127.0.0.1:8118
    # and was only saved by another process already holding 8118. Catch it
    # before we start so a future free-port case doesn't silently loop.
    if args.upstream_proxy:
        try:
            up = urlparse(args.upstream_proxy)
            up_host, up_port = up.hostname, up.port
        except Exception:
            up_host, up_port = None, None
        loopback_hosts = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
        bind_is_loopback = args.host in loopback_hosts
        upstream_is_loopback = up_host in loopback_hosts
        if up_port == args.port and bind_is_loopback and upstream_is_loopback:
            log.critical(
                "self-loop refused: --port %d and --upstream-proxy %s share host:port; "
                "set ANTHROPIC_PROXY_UPSTREAM to a different proxy or empty",
                args.port, args.upstream_proxy,
            )
            return 2

    global _START_MONOTONIC
    _START_MONOTONIC = time.monotonic()

    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    log.info("starting pid=%d listening on %s:%d", os.getpid(), args.host, args.port)
    log.info("upstream proxy: %s", args.upstream_proxy or "(direct)")
    log.info("snapshot dir:   %s", CACHE_ROOT)
    log.info("sse buffer cap: %d bytes", SSE_BUFFER_MAX)

    web.run_app(
        make_app(args.upstream_proxy or None),
        host=args.host, port=args.port,
        access_log=None, print=lambda *_a, **_kw: None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
