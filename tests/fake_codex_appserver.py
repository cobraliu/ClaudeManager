"""Minimal JSON-RPC 2.0 over stdio fake — stands in for `codex app-server`
in test_codex_appserver_client.py. Speaks the same line-delimited framing.

Supported test methods (all called by CodexAppServerClient.send_request):
  - echo                  → returns params
  - delayed_echo          → returns params after `ms` ms
  - explode               → returns an error response using params.code/message
  - blackhole             → never responds
  - blackhole_then_exit   → never responds; meanwhile awaits exit_now
  - emit_notification     → sends a ServerNotification with given method/params,
                            then returns {}
  - send_server_request   → sends a ServerRequest to the client, waits for its
                            response, then returns {client_reply: ..., client_error: ...}
  - write_garbage_then_echo → writes a garbage non-JSON line, then returns params
  - exit_now              → notification: causes the process to exit
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any


def _write(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


class FakeServer:
    def __init__(self) -> None:
        self._next_server_request_id = 1_000_000  # avoid collision w/ client ids
        self._pending_client_responses: dict[int, asyncio.Future] = {}
        self._stop = asyncio.Event()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        try:
            while not self._stop.is_set():
                line = await reader.readline()
                if not line:
                    return
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                asyncio.create_task(self._handle(msg))
        finally:
            sys.stdout.flush()

    async def _handle(self, msg: dict) -> None:
        msg_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if method is None and msg_id is not None:
            # This is the client's response to one of OUR ServerRequests
            fut = self._pending_client_responses.pop(int(msg_id), None)
            if fut and not fut.done():
                fut.set_result(msg)
            return
        if method == "exit_now":
            self._stop.set()
            sys.exit(0)
        try:
            result = await self._dispatch_method(method, params)
            if msg_id is not None:
                _write({"jsonrpc": "2.0", "id": msg_id, "result": result})
        except _MethodError as exc:
            if msg_id is not None:
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": exc.code, "message": exc.message},
                    }
                )
        except Exception as exc:
            if msg_id is not None:
                _write(
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
        if method == "blackhole_then_exit":
            await self._stop.wait()
            return None
        if method == "emit_notification":
            _write(
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
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": params.get("method", "unknown"),
                    "params": params.get("params") or {},
                }
            )
            # Wait for the client to respond
            resp = await asyncio.wait_for(fut, timeout=3.0)
            out = {"client_reply": None, "client_error": None}
            if "result" in resp:
                out["client_reply"] = resp["result"]
            elif "error" in resp:
                out["client_error"] = resp["error"]
            return out
        if method == "write_garbage_then_echo":
            # Write a non-JSON line on stdout first
            sys.stdout.write("this is not json at all\n")
            sys.stdout.flush()
            return params
        raise _MethodError(-32601, f"method not found: {method}")


class _MethodError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def main() -> None:
    # Note: codex_appserver_client passes ["app-server", ...extras] as argv.
    # We ignore argv entirely; we're just being a JSON-RPC server.
    asyncio.run(FakeServer().run())


if __name__ == "__main__":
    main()
