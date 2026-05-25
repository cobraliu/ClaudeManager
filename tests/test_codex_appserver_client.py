"""Protocol-level tests for CodexAppServerClient.

These tests stand up an in-process `websockets.serve` fake server that
speaks JSON-RPC 2.0 the same way codex's `app-server --listen ws://`
endpoint does. The fake exercises:

  * request/response correlation by id
  * notification dispatch
  * incoming ServerRequests (server → client) and our response back
  * error responses
  * connection closure during a pending request
  * graceful close() with no leaked tasks / pending futures

We don't use pytest-asyncio (not installed); each async test body is
wrapped in `asyncio.run()` from a sync test function.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.codex_appserver_client import (
    CodexAppServerClient,
    CodexAppServerError,
)
from tests.fake_codex_appserver import serve_fake


def _run(coro):
    return asyncio.run(coro)


# ── basic framing ────────────────────────────────────────────────────────


def test_connect_and_close_round_trip():
    async def body():
        async with serve_fake() as url:
            client = CodexAppServerClient(ws_url=url)
            await client.connect()
            assert client.is_alive() is True
            await client.close()
            assert client.is_alive() is False

    _run(body())


def test_request_response_correlation_by_id():
    async def body():
        async with serve_fake() as url:
            client = CodexAppServerClient(ws_url=url)
            await client.connect()
            try:
                res = await client.send_request("echo", {"value": "hello"}, timeout=5.0)
                assert res == {"value": "hello"}
            finally:
                await client.close()

    _run(body())


def test_request_response_multiple_ids_interleaved():
    """Send several requests without awaiting; verify each Future resolves
    to the right response. Catches id-correlation bugs."""

    async def body():
        async with serve_fake() as url:
            client = CodexAppServerClient(ws_url=url)
            await client.connect()
            try:
                tasks = [
                    asyncio.create_task(
                        client.send_request(
                            "delayed_echo", {"value": i, "ms": 50}, timeout=5.0
                        )
                    )
                    for i in range(5)
                ]
                results = await asyncio.gather(*tasks)
                assert [r["value"] for r in results] == [0, 1, 2, 3, 4]
            finally:
                await client.close()

    _run(body())


def test_server_error_response_raises_with_code():
    async def body():
        async with serve_fake() as url:
            client = CodexAppServerClient(ws_url=url)
            await client.connect()
            try:
                with pytest.raises(CodexAppServerError) as ei:
                    await client.send_request(
                        "explode", {"code": 12345, "message": "boom"}, timeout=5.0
                    )
                assert ei.value.code == 12345
                assert "boom" in ei.value.message
            finally:
                await client.close()

    _run(body())


def test_request_timeout_clears_pending_entry():
    async def body():
        async with serve_fake() as url:
            client = CodexAppServerClient(ws_url=url)
            await client.connect()
            try:
                with pytest.raises(asyncio.TimeoutError):
                    await client.send_request("blackhole", {}, timeout=0.3)
                assert client._pending == {}
            finally:
                await client.close()

    _run(body())


# ── notifications ────────────────────────────────────────────────────────


def test_subscribe_receives_server_notifications():
    async def body():
        async with serve_fake() as url:
            client = CodexAppServerClient(ws_url=url)
            seen: list[dict] = []
            client.subscribe("turn/plan/updated", lambda p: seen.append(p))
            await client.connect()
            try:
                await client.send_request(
                    "emit_notification",
                    {"method": "turn/plan/updated", "params": {"plan": "test-plan"}},
                    timeout=5.0,
                )
                for _ in range(50):
                    if seen:
                        break
                    await asyncio.sleep(0.02)
                assert seen == [{"plan": "test-plan"}]
            finally:
                await client.close()

    _run(body())


def test_notification_handler_exception_does_not_kill_loop():
    async def body():
        async with serve_fake() as url:
            client = CodexAppServerClient(ws_url=url)

            def bad_handler(_):
                raise RuntimeError("handler exploded")

            seen: list[dict] = []
            client.subscribe("topic", bad_handler)
            client.subscribe("topic", lambda p: seen.append(p))
            await client.connect()
            try:
                await client.send_request(
                    "emit_notification",
                    {"method": "topic", "params": {"v": 1}},
                    timeout=5.0,
                )
                for _ in range(50):
                    if seen:
                        break
                    await asyncio.sleep(0.02)
                assert seen == [{"v": 1}]
                echo = await client.send_request("echo", {"value": "after-bad"}, timeout=5.0)
                assert echo == {"value": "after-bad"}
            finally:
                await client.close()

    _run(body())


# ── ServerRequest (inbound) ──────────────────────────────────────────────


def test_server_request_handler_receives_method_and_responds():
    async def body():
        async with serve_fake() as url:
            client = CodexAppServerClient(ws_url=url)
            captured: list[tuple[str, dict]] = []

            async def handler(method: str, params: dict, _request_id):
                captured.append((method, params))
                return {"approved": True, "echo": params.get("ask")}

            client.set_server_request_handler(handler)
            await client.connect()
            try:
                roundtrip = await client.send_request(
                    "send_server_request",
                    {"method": "execCommandApproval", "params": {"ask": "rm -rf /"}},
                    timeout=5.0,
                )
                assert roundtrip["client_reply"] == {"approved": True, "echo": "rm -rf /"}
                assert captured == [("execCommandApproval", {"ask": "rm -rf /"})]
            finally:
                await client.close()

    _run(body())


def test_server_request_without_handler_returns_method_not_found():
    async def body():
        async with serve_fake() as url:
            client = CodexAppServerClient(ws_url=url)
            await client.connect()
            try:
                roundtrip = await client.send_request(
                    "send_server_request",
                    {"method": "execCommandApproval", "params": {}},
                    timeout=5.0,
                )
                assert roundtrip["client_error"]["code"] == -32601
            finally:
                await client.close()

    _run(body())


# ── lifecycle / resilience ───────────────────────────────────────────────


def test_connection_close_fails_pending_requests():
    async def body():
        async with serve_fake() as url:
            client = CodexAppServerClient(ws_url=url)
            await client.connect()
            fut = asyncio.create_task(
                client.send_request("blackhole", {}, timeout=5.0)
            )
            await asyncio.sleep(0.1)
            # Tell the fake server to close the connection
            try:
                await client.send_notification("exit_now", {})
            except Exception:
                pass
            with pytest.raises(CodexAppServerError) as ei:
                await asyncio.wait_for(fut, timeout=3.0)
            assert "closed" in ei.value.message.lower()
            await client.close()

    _run(body())


def test_close_is_idempotent_and_safe_to_call_twice():
    async def body():
        async with serve_fake() as url:
            client = CodexAppServerClient(ws_url=url)
            await client.connect()
            await client.close()
            await client.close()

    _run(body())


def test_close_cancels_pending_requests():
    async def body():
        async with serve_fake() as url:
            client = CodexAppServerClient(ws_url=url)
            await client.connect()
            fut = asyncio.create_task(client.send_request("blackhole", {}, timeout=10.0))
            await asyncio.sleep(0.05)
            await client.close()
            with pytest.raises(CodexAppServerError):
                await asyncio.wait_for(fut, timeout=1.0)

    _run(body())


def test_malformed_frame_from_server_does_not_kill_reader():
    async def body():
        async with serve_fake() as url:
            client = CodexAppServerClient(ws_url=url)
            await client.connect()
            try:
                res = await client.send_request(
                    "write_garbage_then_echo", {"value": "v"}, timeout=5.0
                )
                assert res == {"value": "v"}
            finally:
                await client.close()

    _run(body())
