"""Minimal JSON-RPC 2.0 over websockets — stands in for `codex app-server`
in test_codex_appserver_client.py.

Speaks the same JSON-RPC framing the real codex ws transport uses: each
frame is one JSON object. The methods below mirror what the old stdio fake
exposed; see test_codex_appserver_client.py for usage.

Supported test methods (all called by CodexAppServerClient.send_request):
  - echo                  → returns params
  - delayed_echo          → returns params after `ms` ms
  - explode               → returns an error response using params.code/message
  - blackhole             → never responds
  - emit_notification     → sends a ServerNotification with given method/params,
                            then returns {}
  - send_server_request   → sends a ServerRequest to the client, waits for its
                            response, then returns {client_reply: ..., client_error: ...}
  - write_garbage_then_echo → writes a garbage non-JSON frame, then returns params
  - exit_now              → server closes the websocket
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

import websockets
from websockets import WebSocketServerProtocol


class _MethodError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class FakeServer:
    """Per-connection JSON-RPC handler."""

    def __init__(self, ws: WebSocketServerProtocol) -> None:
        self._ws = ws
        self._next_server_request_id = 1_000_000  # avoid collision w/ client ids
        self._pending_client_responses: dict[int, asyncio.Future] = {}
        self._close = asyncio.Event()

    async def run(self) -> None:
        try:
            async for raw in self._ws:
                if self._close.is_set():
                    break
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                asyncio.create_task(self._handle(msg))
        except websockets.ConnectionClosed:
            pass

    async def _write(self, payload: dict) -> None:
        await self._ws.send(json.dumps(payload))

    async def _handle(self, msg: dict) -> None:
        msg_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if method is None and msg_id is not None:
            # client's response to one of OUR ServerRequests
            fut = self._pending_client_responses.pop(int(msg_id), None)
            if fut and not fut.done():
                fut.set_result(msg)
            return
        if method == "exit_now":
            self._close.set()
            await self._ws.close()
            return
        try:
            result = await self._dispatch_method(method, params)
            if msg_id is not None:
                await self._write({"jsonrpc": "2.0", "id": msg_id, "result": result})
        except _MethodError as exc:
            if msg_id is not None:
                await self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": exc.code, "message": exc.message},
                    }
                )
        except Exception as exc:
            if msg_id is not None:
                await self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32603, "message": str(exc)},
                    }
                )

    async def _dispatch_method(self, method: str, params: dict) -> Any:
        if method == "echo":
            return params
        if method == "delayed_echo":
            ms = int(params.get("ms", 0))
            await asyncio.sleep(ms / 1000.0)
            return params
        if method == "explode":
            raise _MethodError(
                int(params.get("code", -32000)),
                str(params.get("message", "boom")),
            )
        if method == "blackhole":
            await asyncio.Event().wait()  # never returns
            return None
        if method == "emit_notification":
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "method": params.get("method", "noop"),
                    "params": params.get("params") or {},
                }
            )
            return {}
        if method == "send_server_request":
            req_id = self._next_server_request_id
            self._next_server_request_id += 1
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending_client_responses[req_id] = fut
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": params.get("method", "unknown"),
                    "params": params.get("params") or {},
                }
            )
            resp = await asyncio.wait_for(fut, timeout=3.0)
            out = {"client_reply": None, "client_error": None}
            if "result" in resp:
                out["client_reply"] = resp["result"]
            elif "error" in resp:
                out["client_error"] = resp["error"]
            return out
        if method == "write_garbage_then_echo":
            await self._ws.send("this is not json at all")
            return params
        raise _MethodError(-32601, f"method not found: {method}")


@asynccontextmanager
async def serve_fake(host: str = "127.0.0.1", port: int = 0):
    """Run the fake server on an ephemeral port; yield its ws URL."""
    async def _handler(ws: WebSocketServerProtocol):
        await FakeServer(ws).run()

    server = await websockets.serve(_handler, host, port)
    actual_port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://{host}:{actual_port}/"
    finally:
        server.close()
        await server.wait_closed()
