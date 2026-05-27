"""Path-prefix reverse proxy: /serverforward/{port}/{path:path}

Forwards HTTP and WebSocket traffic to 127.0.0.1:{port}. Single external
ingress (the ClaudeManager port) covers every dev server bound to localhost
on the host machine, without needing per-forward listening ports.

Tradeoff: SPAs whose `base` is `/` will emit broken absolute asset URLs
when accessed through the prefix. Users typically configure the upstream
dev server with the matching base (e.g. Vite `base: "/serverforward/5173/"`).
Pure HTTP APIs work without changes.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp
from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import PlainTextResponse, StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# RFC 7230 §6.1 hop-by-hop headers — must not be forwarded by an intermediary.
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade",
})

# Ports we refuse to proxy. ClaudeManager's own port is here to prevent the
# proxy from looping back on itself if the target_port were ever set to 19099.
_BLOCKED_PORTS = {19099}

_HTTP_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]


def _filter_request_headers(src: dict[str, str], target_host: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in src.items():
        kl = k.lower()
        if kl in _HOP_BY_HOP or kl == "host" or kl == "content-length":
            continue
        out[k] = v
    out["host"] = target_host
    return out


def _filter_response_headers(items) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for k, v in items:
        kl = k.lower()
        if kl in _HOP_BY_HOP or kl == "content-length":
            # Length is recomputed by StreamingResponse / framework.
            continue
        out.append((k, v))
    return out


def _validate_port(port: int) -> str | None:
    if port < 1 or port > 65535:
        return "invalid port"
    if port in _BLOCKED_PORTS:
        return "port forbidden"
    return None


@router.api_route(
    "/serverforward/{port}/{path:path}",
    methods=_HTTP_METHODS,
    include_in_schema=False,
)
async def server_forward_http(port: int, path: str, request: Request):
    err = _validate_port(port)
    if err is not None:
        return PlainTextResponse(err, status_code=400 if err == "invalid port" else 403)

    target_url = f"http://127.0.0.1:{port}/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    headers = _filter_request_headers(dict(request.headers), f"127.0.0.1:{port}")
    # Read entire body. Streaming uploads of >100MB would benefit from passing
    # request.stream() as data, but that path is rare for dev-server use.
    body = await request.body()

    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, connect=10),
        auto_decompress=False,
    )
    try:
        upstream = await session.request(
            request.method,
            target_url,
            headers=headers,
            data=body if body else None,
            allow_redirects=False,
        )
    except aiohttp.ClientConnectorError:
        await session.close()
        return PlainTextResponse(
            f"upstream connection refused: 127.0.0.1:{port}",
            status_code=502,
        )
    except Exception as e:
        await session.close()
        return PlainTextResponse(f"upstream error: {e!s}", status_code=502)

    response_headers = _filter_response_headers(upstream.headers.items())

    async def body_iter():
        try:
            async for chunk in upstream.content.iter_any():
                yield chunk
        finally:
            upstream.release()
            await session.close()

    return StreamingResponse(
        body_iter(),
        status_code=upstream.status,
        headers=dict(response_headers),
    )


@router.websocket("/serverforward/{port}/{path:path}")
async def server_forward_ws(ws: WebSocket, port: int, path: str):
    err = _validate_port(port)
    if err is not None:
        await ws.close(code=4400, reason=err)
        return

    target = f"ws://127.0.0.1:{port}/{path}"
    qs = str(ws.url.query) if ws.url.query else ""
    if qs:
        target += f"?{qs}"

    requested_subprotocols = ws.scope.get("subprotocols") or []

    # Connect upstream first so we can negotiate the subprotocol before
    # accepting the client (Starlette requires accept() to choose one).
    session = aiohttp.ClientSession()
    try:
        upstream = await session.ws_connect(
            target,
            protocols=requested_subprotocols,
            autoping=True,
            heartbeat=20,
        )
    except Exception as e:
        await session.close()
        try:
            await ws.close(code=4502, reason=f"upstream ws: {e!s}"[:120])
        except Exception:
            pass
        return

    selected = upstream.protocol or None
    try:
        await ws.accept(subprotocol=selected)
    except Exception:
        await upstream.close()
        await session.close()
        return

    async def client_to_upstream():
        try:
            while True:
                msg = await ws.receive()
                t = msg.get("type")
                if t == "websocket.disconnect":
                    break
                if msg.get("text") is not None:
                    await upstream.send_str(msg["text"])
                elif msg.get("bytes") is not None:
                    await upstream.send_bytes(msg["bytes"])
        except Exception:
            pass
        finally:
            try:
                await upstream.close()
            except Exception:
                pass

    async def upstream_to_client():
        try:
            async for m in upstream:
                if m.type == aiohttp.WSMsgType.TEXT:
                    await ws.send_text(m.data)
                elif m.type == aiohttp.WSMsgType.BINARY:
                    await ws.send_bytes(m.data)
                elif m.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        except Exception:
            pass
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    try:
        await asyncio.gather(
            client_to_upstream(),
            upstream_to_client(),
            return_exceptions=True,
        )
    finally:
        await session.close()


# A bare /serverforward/{port} request (no trailing slash) would otherwise
# 404; redirect to the canonical /serverforward/{port}/ so browsers don't
# have to know about the slash convention.
@router.get("/serverforward/{port}", include_in_schema=False)
async def server_forward_root_redirect(port: int, request: Request):
    err = _validate_port(port)
    if err is not None:
        return PlainTextResponse(err, status_code=400 if err == "invalid port" else 403)
    from fastapi.responses import RedirectResponse
    qs = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/serverforward/{port}/{qs}", status_code=307)
