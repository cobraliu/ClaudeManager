"""Async JSON-RPC 2.0 client for `codex app-server` over WebSockets.

Codex's `app-server` subcommand can listen on three transports: `stdio://`
(default), `unix://`, and `ws://IP:PORT`. We pick **ws** because:

  * the codex process survives our backend restarts — it doesn't share our
    process group and isn't bound by the parent-child stdin link
  * we can spawn it once and reconnect after backend redeploys
  * codex's bubblewrap sandbox preflight is fatal in `--listen unix://` mode
    but only logs in ws mode (same forgiving behavior as stdio)
  * codex exposes `/readyz` and `/healthz` on the same port, so the manager
    can health-check without owning the JSON-RPC connection

This client speaks the same JSON-RPC 2.0 framing as before but over a
websocket frame instead of a stdout line. Three message kinds:

  * outbound request   → server returns response by id (resolved via Future)
  * outbound notification → no response
  * inbound notification (ServerNotification, e.g. `turn/plan/updated`)
       → dispatched to subscribers
  * inbound request    (ServerRequest, e.g. approval prompts)
       → server expects a response by the same id

Subprocess management lives in the manager. This client is pure transport.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets import WebSocketClientProtocol

logger = logging.getLogger(__name__)


class CodexAppServerError(Exception):
    """Wraps a JSON-RPC error response from the server."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class CodexAppServerClient:
    """One client = one websocket connection to a `codex app-server` process.

    Not thread-safe; expected to be driven from a single asyncio loop. All
    `async` methods must be awaited on that loop. `subscribe` and
    `set_server_request_handler` are sync setters; call them before
    `connect` or while the client is idle.
    """

    def __init__(self, *, ws_url: str) -> None:
        self._ws_url = ws_url
        self._ws: WebSocketClientProtocol | None = None
        self._reader_task: asyncio.Task | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._notification_handlers: dict[
            str, list[Callable[[dict], None]]
        ] = {}
        self._server_request_handler: (
            Callable[[str, dict, Any], Awaitable[dict]] | None
        ) = None
        self._closed = False

    # ── lifecycle ────────────────────────────────────────────────────────

    @property
    def ws_url(self) -> str:
        return self._ws_url

    async def connect(self, *, timeout: float = 5.0) -> None:
        """Open the websocket and start the reader loop."""
        if self._ws is not None:
            raise RuntimeError("already connected")
        if self._closed:
            raise RuntimeError("client is closed")
        self._ws = await asyncio.wait_for(
            websockets.connect(self._ws_url, ping_interval=None),
            timeout=timeout,
        )
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="codex-appserver-ws-reader"
        )

    async def close(self, *, timeout: float = 3.0) -> None:
        """Close the websocket and cancel reader. Idempotent."""
        if self._closed:
            return
        self._closed = True
        # Cancel any pending requests so callers don't hang forever.
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(
                    CodexAppServerError(-32000, "client closed before response")
                )
        self._pending.clear()
        ws = self._ws
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("codex app-server: ws close timed out, forcing")
            except Exception:
                logger.debug("codex app-server: ws close error", exc_info=True)
        task = self._reader_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def is_alive(self) -> bool:
        ws = self._ws
        return ws is not None and not ws.closed and not self._closed

    # ── outgoing ─────────────────────────────────────────────────────────

    async def send_request(
        self,
        method: str,
        params: dict | None = None,
        *,
        timeout: float = 30.0,
    ) -> Any:
        """Send a JSON-RPC request and return the `result` field of the response.

        Raises CodexAppServerError if the server returned an error object,
        TimeoutError if the response doesn't arrive in `timeout` seconds,
        or RuntimeError if the client is closed / not connected.
        """
        if self._ws is None or self._closed:
            raise RuntimeError("client not running")
        req_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        try:
            await self._write_line(payload)
        except Exception:
            self._pending.pop(req_id, None)
            raise
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise

    async def send_notification(self, method: str, params: dict | None = None) -> None:
        """Fire-and-forget notification (no id, no response)."""
        if self._ws is None or self._closed:
            raise RuntimeError("client not running")
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        await self._write_line(payload)

    # ── incoming dispatch ────────────────────────────────────────────────

    def subscribe(self, method: str, handler: Callable[[dict], None]) -> None:
        """Register a handler for ServerNotification with `method`.

        Multiple handlers per method are supported (called in registration
        order). Handlers run synchronously inside the reader loop; if a
        handler raises, the exception is logged but does not kill the loop.
        """
        self._notification_handlers.setdefault(method, []).append(handler)

    def set_server_request_handler(
        self,
        handler: Callable[[str, dict, Any], Awaitable[dict]] | None,
    ) -> None:
        """Register the callback that produces responses for incoming
        ServerRequests. The signature is `(method, params, request_id) -> result_dict`.

        If unset, the client responds with method-not-found for every
        ServerRequest, which is the correct default until the manager
        wires up approval / AUQ semantics.
        """
        self._server_request_handler = handler

    # ── internal ─────────────────────────────────────────────────────────

    async def _write_line(self, payload: dict) -> None:
        assert self._ws is not None
        try:
            await self._ws.send(json.dumps(payload, ensure_ascii=False))
        except websockets.ConnectionClosed as exc:
            logger.warning("codex app-server ws closed during send: %s", exc)
            raise

    async def _read_loop(self) -> None:
        assert self._ws is not None
        ws = self._ws
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "codex app-server: malformed JSON frame: %s — %r",
                        exc,
                        raw[:200],
                    )
                    continue
                try:
                    self._dispatch(msg)
                except Exception:
                    logger.exception("codex app-server: dispatch error for %r", msg)
        except asyncio.CancelledError:
            raise
        except websockets.ConnectionClosed:
            logger.info("codex app-server: ws connection closed")
        except Exception:
            logger.exception("codex app-server: reader crashed")
        finally:
            # If the reader exits, fail any still-pending requests.
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(
                        CodexAppServerError(-32001, "ws connection closed")
                    )
            self._pending.clear()

    def _dispatch(self, msg: dict) -> None:
        msg_id = msg.get("id")
        method = msg.get("method")
        if msg_id is not None and method is None:
            # Response to one of our requests
            fut = self._pending.pop(int(msg_id), None) if isinstance(msg_id, (int, str)) else None
            if fut is None or fut.done():
                logger.debug("codex app-server: orphan response id=%r", msg_id)
                return
            if "error" in msg:
                err = msg["error"] or {}
                fut.set_exception(
                    CodexAppServerError(
                        int(err.get("code", -32000)),
                        str(err.get("message", "")),
                        err.get("data"),
                    )
                )
            else:
                fut.set_result(msg.get("result"))
            return
        if method is None:
            logger.debug("codex app-server: message without method or id: %r", msg)
            return
        params = msg.get("params") or {}
        if msg_id is not None:
            # ServerRequest: server wants a response
            asyncio.create_task(
                self._handle_server_request(msg_id, method, params),
                name=f"codex-appserver-srvreq-{msg_id}",
            )
            return
        # ServerNotification
        handlers = self._notification_handlers.get(method, [])
        for h in handlers:
            try:
                h(params)
            except Exception:
                logger.exception(
                    "codex app-server: notification handler %r failed for %s",
                    h,
                    method,
                )

    async def _handle_server_request(
        self, request_id: Any, method: str, params: dict
    ) -> None:
        handler = self._server_request_handler
        response: dict
        if handler is None:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"method not found: {method}",
                },
            }
        else:
            try:
                result = await handler(method, params, request_id)
                response = {"jsonrpc": "2.0", "id": request_id, "result": result}
            except CodexAppServerError as exc:
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "data": exc.data,
                    },
                }
            except Exception as exc:
                logger.exception(
                    "codex app-server: server-request handler raised for %s", method
                )
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32603, "message": f"internal error: {exc}"},
                }
        try:
            await self._write_line(response)
        except Exception:
            logger.exception(
                "codex app-server: failed to write response for request id=%r",
                request_id,
            )
