"""Async JSON-RPC 2.0 client for `codex app-server` over stdio.

The codex app-server protocol is full bidirectional JSON-RPC 2.0, line-delimited
(one JSON object per line on stdin/stdout). Three message kinds:

  * outbound request   → server returns response by id (resolved via Future)
  * outbound notification → no response
  * inbound notification (ServerNotification, e.g. `turn/plan/updated`)
       → dispatched to subscribers
  * inbound request    (ServerRequest, e.g. approval prompts)
       → server expects a response by the same id

This client handles the framing; it has zero knowledge of codex-specific
method names. The manager layer (codex_appserver_manager.py) sits on top
and maps notifications + server requests onto Claude-parity state slots
(pending AUQ / pending approval / plan / compaction).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


class CodexAppServerError(Exception):
    """Wraps a JSON-RPC error response from the server."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class CodexAppServerClient:
    """One client = one `codex app-server` subprocess.

    Not thread-safe; expected to be driven from a single asyncio loop. All
    `async` methods must be awaited on that loop. `subscribe` and
    `set_server_request_handler` are sync setters; call them before `spawn`
    or while the client is idle.
    """

    def __init__(
        self,
        *,
        cwd: str,
        env: dict[str, str] | None = None,
        codex_bin: str = "codex",
        subcommand: list[str] | tuple[str, ...] = ("app-server",),
        extra_args: list[str] | None = None,
    ) -> None:
        self._cwd = cwd
        self._env = env
        self._codex_bin = codex_bin
        self._subcommand = list(subcommand)
        self._extra_args = list(extra_args) if extra_args else []
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
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

    async def spawn(self) -> int:
        """Start the subprocess; return its PID."""
        if self._proc is not None:
            raise RuntimeError("already spawned")
        args = [self._codex_bin, *self._subcommand, *self._extra_args]
        # Pass env explicitly so callers can scope HOME/CODEX_HOME per session.
        env = {**os.environ, **self._env} if self._env else None
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=self._cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="codex-appserver-reader"
        )
        self._stderr_task = asyncio.create_task(
            self._stderr_loop(), name="codex-appserver-stderr"
        )
        return self._proc.pid

    async def close(self, *, timeout: float = 3.0) -> None:
        """Terminate subprocess and cancel reader tasks. Idempotent."""
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
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                if proc.stdin is not None and not proc.stdin.is_closing():
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await proc.wait()
                except Exception:
                    pass
            except ProcessLookupError:
                pass
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    def is_alive(self) -> bool:
        return (
            self._proc is not None
            and self._proc.returncode is None
            and not self._closed
        )

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

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
        or RuntimeError if the client is closed / not spawned.
        """
        if self._proc is None or self._closed:
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
        if self._proc is None or self._closed:
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
        ServerRequest, which is the correct default until C3 wires up
        approval semantics.
        """
        self._server_request_handler = handler

    # ── internal ─────────────────────────────────────────────────────────

    async def _write_line(self, payload: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self._proc.stdin.write(line)
        try:
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            logger.warning("codex app-server stdin closed: %s", exc)
            raise

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break  # EOF — subprocess exited
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "codex app-server: malformed JSON line: %s — %r",
                        exc,
                        line[:200],
                    )
                    continue
                try:
                    self._dispatch(msg)
                except Exception:
                    logger.exception("codex app-server: dispatch error for %r", msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("codex app-server: reader crashed")
        finally:
            # If the reader exits, fail any still-pending requests.
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(
                        CodexAppServerError(-32001, "subprocess exited")
                    )
            self._pending.clear()

    async def _stderr_loop(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        stderr = self._proc.stderr
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    break
                logger.info(
                    "codex-appserver[%s]: %s",
                    self._proc.pid,
                    line.decode("utf-8", errors="replace").rstrip(),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("codex app-server: stderr reader crashed")

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
