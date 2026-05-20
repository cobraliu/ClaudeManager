"""End-to-end integration test for the bash terminal feature.

Spins up a real uvicorn instance bound to 127.0.0.1, talks to it with httpx +
websockets, and verifies the full lifecycle:
  1. Create ephemeral → attach → close → tmux session is gone
  2. Create + rename to named → attach → close → tmux session survives
  3. Multi-attach to named (attach_count == 2)
  4. Duplicate name yields 409
  5. Explicit DELETE kills the tmux session
  6. No zombie [bash] <defunct> children left

Run with:
    NO_PROXY=127.0.0.1,localhost .venv/bin/python scripts/term_integration_test.py
"""
from __future__ import annotations

# IMPORTANT: strip outbound-proxy env vars *before* importing httpx /
# websockets / uvicorn, so they don't route loopback traffic through the
# user's Privoxy (which we know returns 502 for 127.0.0.1).
import os
for _v in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_v, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1"
os.environ["no_proxy"] = "127.0.0.1,localhost,::1"

import asyncio
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn
import websockets


def _pick_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_server(url: str, timeout: float = 10.0) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with httpx.Client(trust_env=False, timeout=1.0) as c:
                r = c.get(url)
                if r.status_code < 500:
                    return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"server didn't come up at {url}")


def _tmux_has(name: str) -> bool:
    r = subprocess.run(
        ["tmux", "has-session", "-t", name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


def _list_cmterm_sessions() -> list[str]:
    r = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return []
    return [s for s in r.stdout.split() if s.startswith("cmterm-")]


def _count_zombie_bash() -> int:
    """Return number of `[bash] <defunct>` processes."""
    r = subprocess.run(["ps", "-eo", "stat,comm"], capture_output=True, text=True)
    n = 0
    for line in r.stdout.splitlines()[1:]:
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].startswith("Z") and "bash" in parts[1]:
            n += 1
    return n


async def _drain_until(ws, marker: bytes, timeout: float) -> bytes:
    """Read frames until ``marker`` appears in accumulated bytes, or timeout."""
    acc = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = deadline - time.time()
        try:
            frame = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if isinstance(frame, bytes):
            acc += frame
        elif isinstance(frame, str):
            # state messages are JSON strings
            pass
        if marker in acc:
            return acc
    return acc


def _make_admin_jwt() -> str:
    # Use the app's own helper so it works regardless of secret config.
    from app.security import create_jwt
    from app.models.user import UserRole
    return create_jwt("smoketest", UserRole.ADMIN, is_admin=True)


def _seed_session() -> str:
    """Insert a SessionMetadata row owned by 'smoketest' user."""
    from app.claudemanager import session_store
    from app.models.session import SessionMetadata, SessionStatus
    import uuid

    sid = str(uuid.uuid4())
    md = SessionMetadata(
        id=sid,
        owner_id="smoketest",
        name="smoketest",
        project="smoketest",
        cwd=str(Path(tempfile.gettempdir()) / "cmterm-test"),
        tmux_session_name=f"smoketest-{sid[:8]}",
        status=SessionStatus.RUNNING,
    )
    Path(md.cwd).mkdir(parents=True, exist_ok=True)
    session_store.create(md)
    return sid


async def main() -> int:
    # Pre-import app (also drives migrations / store init in main thread).
    from app.claudemanager import app  # noqa: F401
    token = _make_admin_jwt()
    sid = _seed_session()

    port = _pick_port()
    base = f"http://127.0.0.1:{port}"
    ws_base = f"ws://127.0.0.1:{port}"
    headers = {"Authorization": f"Bearer {token}"}

    cfg = uvicorn.Config(
        "app.claudemanager:app",
        host="127.0.0.1",
        port=port,
        log_level="warning",
        loop="asyncio",
    )
    server = uvicorn.Server(cfg)

    server_thread = threading.Thread(
        target=lambda: asyncio.run(server.serve()), daemon=True
    )
    server_thread.start()
    _wait_server(base + "/api/health" if False else base + "/")  # any route

    fails: list[str] = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)
            print("  ✗", msg)
        else:
            print("  ✓", msg)

    pre_zombies = _count_zombie_bash()

    try:
        async with httpx.AsyncClient(
            base_url=base, headers=headers, trust_env=False, timeout=10.0,
        ) as http:

            # ── Test 1: ephemeral lifecycle ──────────────────────────────
            print("\n[1] Ephemeral terminal kill-on-detach")
            r = await http.post(f"/api/sessions/{sid}/terminals", json={})
            check(r.status_code == 200, f"create ephemeral 200 (got {r.status_code}: {r.text[:200]})")
            if r.status_code != 200:
                raise SystemExit(1)
            term = r.json()
            tmux_name = f"cmterm-{term['term_id']}"
            check(_tmux_has(tmux_name), "tmux session exists after create")

            async with websockets.connect(ws_base + term["ws_url"]) as ws:
                # send a probe via input msg
                await ws.send(json.dumps({"type": "input", "data": "echo PROBE_EPH_OK\r"}))
                out = await _drain_until(ws, b"PROBE_EPH_OK", timeout=4.0)
                check(b"PROBE_EPH_OK" in out, "echo round-trip works (ephemeral)")

            # Give backend a moment to detect detach and kill tmux
            await asyncio.sleep(0.4)
            check(not _tmux_has(tmux_name), "tmux session killed after WS close (ephemeral)")

            # ── Test 2: named terminal survives detach ──────────────────
            print("\n[2] Named terminal survives detach")
            r = await http.post(f"/api/sessions/{sid}/terminals", json={"name": "build"})
            check(r.status_code == 200, f"create named 200 (got {r.status_code}: {r.text[:200]})")
            named = r.json()
            named_tmux = f"cmterm-{named['term_id']}"
            check(named["is_named"] and named["name"] == "build", "is_named=true name=build")

            async with websockets.connect(ws_base + named["ws_url"]) as ws:
                await ws.send(json.dumps({"type": "input", "data": "echo PROBE_NAMED_OK\r"}))
                out = await _drain_until(ws, b"PROBE_NAMED_OK", timeout=4.0)
                check(b"PROBE_NAMED_OK" in out, "echo round-trip works (named)")

            await asyncio.sleep(0.3)
            check(_tmux_has(named_tmux), "tmux session survives close (named)")

            # ── Test 3: multi-attach to named ───────────────────────────
            print("\n[3] Multi-attach increments attach_count")
            r1 = await http.post(f"/api/sessions/{sid}/terminals/{named['term_id']}/token")
            r2 = await http.post(f"/api/sessions/{sid}/terminals/{named['term_id']}/token")
            check(r1.status_code == 200 and r2.status_code == 200, "two tokens issued")
            tok1 = r1.json(); tok2 = r2.json()

            async with websockets.connect(ws_base + tok1["ws_url"]) as ws_a:
                # ensure first attach lands
                await asyncio.sleep(0.2)
                async with websockets.connect(ws_base + tok2["ws_url"]) as ws_b:
                    # let both finish on_attach
                    await asyncio.sleep(0.3)
                    r = await http.get(f"/api/sessions/{sid}/terminals")
                    items = {t["term_id"]: t for t in r.json()["items"]}
                    ac = items[named["term_id"]]["attach_count"]
                    check(ac == 2, f"attach_count == 2 (got {ac})")

            await asyncio.sleep(0.3)
            r = await http.get(f"/api/sessions/{sid}/terminals")
            items = {t["term_id"]: t for t in r.json()["items"]}
            ac = items[named["term_id"]]["attach_count"]
            check(ac == 0, f"attach_count back to 0 (got {ac})")
            check(_tmux_has(named_tmux), "named still alive after both detached")

            # ── Test 4: duplicate name → 409 ────────────────────────────
            print("\n[4] Duplicate name rejected")
            r = await http.post(f"/api/sessions/{sid}/terminals", json={"name": "build"})
            check(r.status_code == 409, f"second 'build' returns 409 (got {r.status_code})")

            # ── Test 5: rename ephemeral → named ────────────────────────
            print("\n[5] Rename ephemeral → named promotes lifecycle")
            r = await http.post(f"/api/sessions/{sid}/terminals", json={})
            check(r.status_code == 200, "create ephemeral for rename")
            eph = r.json()
            r = await http.post(
                f"/api/sessions/{sid}/terminals/{eph['term_id']}/rename",
                json={"name": "test"},
            )
            check(r.status_code == 200 and r.json()["is_named"], "rename → named ok")
            check(_tmux_has(f"cmterm-{eph['term_id']}"), "tmux still alive after rename")

            # ── Test 6: DELETE kills any kind ───────────────────────────
            print("\n[6] DELETE kills tmux session")
            r = await http.delete(f"/api/sessions/{sid}/terminals/{named['term_id']}")
            check(r.status_code == 200, "DELETE named 200")
            await asyncio.sleep(0.2)
            check(not _tmux_has(named_tmux), "named tmux gone after DELETE")

            r = await http.delete(f"/api/sessions/{sid}/terminals/{eph['term_id']}")
            check(r.status_code == 200, "DELETE renamed 200")
            await asyncio.sleep(0.2)
            check(not _tmux_has(f"cmterm-{eph['term_id']}"), "renamed tmux gone after DELETE")

            # ── Test 7: invalid token rejected (4001 close or HTTP 403) ──
            # Starlette converts a pre-accept ws.close() into an HTTP 403
            # during the handshake; either rejection mode is acceptable.
            print("\n[7] Invalid WS token rejected")
            rejected = False
            reason = ""
            try:
                async with websockets.connect(
                    f"{ws_base}/ws/terminals/does_not_exist?token=garbage"
                ) as ws:
                    await ws.recv()
            except websockets.exceptions.ConnectionClosed as exc:
                rejected = exc.code in (4001, 4004)
                reason = f"close code {exc.code}"
            except websockets.exceptions.InvalidStatusCode as exc:
                rejected = exc.status_code == 403
                reason = f"http {exc.status_code}"
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
            check(rejected, f"invalid token rejected ({reason})")

        # ── Final: zombie check ────────────────────────────────────────
        print("\n[Z] Zombie check")
        await asyncio.sleep(0.5)
        post_zombies = _count_zombie_bash()
        check(
            post_zombies <= pre_zombies,
            f"no new zombies (pre={pre_zombies} post={post_zombies})",
        )

        # ── Stragglers: any cmterm-* sessions left behind? ─────────────
        leftover = _list_cmterm_sessions()
        check(not leftover, f"no leftover cmterm tmux sessions (got {leftover})")

    finally:
        # cleanup
        for s in _list_cmterm_sessions():
            subprocess.run(["tmux", "kill-session", "-t", s], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        server.should_exit = True
        time.sleep(0.4)

    print()
    if fails:
        print(f"❌ {len(fails)} failure(s):")
        for f in fails:
            print("  -", f)
        return 1
    print(f"✅ all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
