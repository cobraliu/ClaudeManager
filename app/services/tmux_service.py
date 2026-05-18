from __future__ import annotations

import fcntl
import json
import logging
import os
import pty
import select
import shlex
import signal
import struct
import subprocess
import termios
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Substrings that uniquely identify Claude CLI's workspace-trust dialog.
# Multiple candidates for version drift tolerance — any one match is enough.
_TRUST_DIALOG_PATTERNS = (
    "Yes, I trust this folder",
    "Accessing workspace",
)


def _looks_like_trust_dialog(screen: str) -> bool:
    if not screen:
        return False
    return any(p in screen for p in _TRUST_DIALOG_PATTERNS)


class TmuxError(RuntimeError):
    pass


@dataclass
class PtyHandle:
    """A PTY attached to a tmux session for raw terminal I/O."""
    master_fd: int
    child_pid: int

    def read(self, timeout: float = 1.0) -> bytes:
        """Read available data from the PTY. Returns b'' on timeout."""
        try:
            r, _, _ = select.select([self.master_fd], [], [], timeout)
            if r:
                return os.read(self.master_fd, 65536)
        except (OSError, ValueError):
            pass
        return b""

    def write(self, data: bytes) -> None:
        """Write data to the PTY (sends input to tmux).

        Handles partial writes: the PTY kernel buffer is ~4096 bytes, so large
        pastes require multiple os.write calls until all bytes are sent.
        Safe to call from a thread (not the async event loop).
        """
        try:
            while data:
                n = os.write(self.master_fd, data)
                data = data[n:]
        except OSError:
            pass

    def resize(self, cols: int, rows: int) -> None:
        """Resize the PTY window."""
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    def close(self) -> None:
        """Close the PTY and clean up the child process."""
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        try:
            os.kill(self.child_pid, signal.SIGHUP)
        except OSError:
            pass
        try:
            os.waitpid(self.child_pid, os.WNOHANG)
        except (OSError, ChildProcessError):
            pass


class TmuxService:
    """
    Thin tmux wrapper with strict command surface.
    """

    def __init__(
        self,
        socket_name: str = "claude-web",
        proxy: str = "",
        claude_bin: str = "claude",
        claude_shell: str = "",
        cursor_bin: str = "agent",
    ) -> None:
        self.socket_name = socket_name
        self._tmux = os.getenv("TMUX_BIN", "tmux")
        self.proxy_env = (
            {"http_proxy": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "HTTPS_PROXY": proxy}
            if proxy else {}
        )
        self.claude_bin = claude_bin or "claude"
        self.cursor_bin = cursor_bin or "agent"
        # Optional shell wrapper, e.g. "bash -l" for WSL/nvm users whose PATH
        # is only set up in .profile/.bashrc (login shell).
        self.claude_shell = claude_shell or ""
        # Ensure tmux server uses latest-client window sizing
        try:
            self._run("set-option", "-g", "window-size", "latest")
        except TmuxError:
            pass  # server not started yet

    def _run(self, *args: str) -> str:
        cmd = [self._tmux, "-L", self.socket_name, *args]
        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
            return result.stdout.strip()
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            quoted = " ".join(shlex.quote(part) for part in cmd)
            raise TmuxError(f"tmux failed: {quoted} ({stderr})") from exc

    def _build_env_args(self, env: dict[str, str]) -> list[str]:
        """Build tmux -e flags for proxy env + user env + PATH with claude_bin dir."""
        merged = {**self.proxy_env, **env}
        # Always forward the current process PATH so the tmux session can find
        # binaries installed via nvm/pyenv/etc even when uvicorn was started
        # without a login shell (common in WSL).
        if "PATH" not in merged:
            merged["PATH"] = os.environ.get("PATH", "")
        # If claude_bin is an absolute path, prepend its directory to PATH.
        if os.path.isabs(self.claude_bin):
            bin_dir = str(Path(self.claude_bin).parent)
            if bin_dir not in merged["PATH"].split(os.pathsep):
                merged["PATH"] = f"{bin_dir}{os.pathsep}{merged['PATH']}"
        args: list[str] = []
        for k, v in merged.items():
            # Skip empty values — passing empty proxy strings can confuse some tools
            if v:
                args.extend(["-e", f"{k}={v}"])
        return args

    def create_session(
        self,
        session_name: str,
        cwd: str,
        env: dict[str, str],
        claude_model: str | None = None,
        resume_session_id: str | None = None,
        inner_id: str | None = None,
        tool: str = "claude",
    ) -> None:
        env_args = self._build_env_args(env)

        if tool == "cursor":
            cursor_bin = shlex.quote(self.cursor_bin)
            if resume_session_id:
                command = f"{cursor_bin} --yolo --resume {shlex.quote(resume_session_id)}"
            else:
                command = f"{cursor_bin} --yolo"
        else:
            claude_bin = shlex.quote(self.claude_bin)
            if resume_session_id:
                claude_cmd = f"{claude_bin} --dangerously-skip-permissions --resume {shlex.quote(resume_session_id)}"
                if inner_id:
                    pid_file = f"/tmp/claude-inner-{inner_id}.pid"
                    claude_cmd = f"sh -c 'echo $$ > {pid_file} && exec {claude_cmd}'"
            else:
                if claude_model:
                    claude_cmd = f"{claude_bin} --dangerously-skip-permissions --model {shlex.quote(claude_model)}"
                else:
                    claude_cmd = f"{claude_bin} --dangerously-skip-permissions"

                if inner_id:
                    pid_file = f"/tmp/claude-inner-{inner_id}.pid"
                    claude_cmd = f"sh -c 'echo $$ > {pid_file} && exec {claude_cmd}'"

            # Optionally wrap with a login shell (e.g. "bash -l") so that .profile
            # is sourced and nvm/pyenv paths are available — useful for WSL.
            if self.claude_shell:
                command = f"{self.claude_shell} -c {shlex.quote(claude_cmd)}"
            else:
                command = claude_cmd

        self._run("new-session", "-d", "-s", session_name, "-c", cwd, *env_args, command)
        try:
            self._run("set-option", "-t", session_name, "mouse", "off")
            self._run("set-option", "-t", session_name, "history-limit", "50000")
            self._run("set-option", "-t", session_name, "mode-keys", "vi")
        except TmuxError:
            pass

        if tool == "claude":
            threading.Thread(
                target=self._auto_accept_trust_dialog,
                args=(session_name,),
                daemon=True,
            ).start()

    def attach_pty(self, session_name: str, cols: int = 80, rows: int = 24) -> PtyHandle:
        """Attach to a tmux session via a PTY for raw terminal I/O."""
        # Poll until the tmux session exists (guards against create→attach races on Mac).
        deadline = time.monotonic() + 5.0
        while not self.has_session(session_name):
            if time.monotonic() > deadline:
                raise TmuxError(f"timed out waiting for session: {session_name}")
            time.sleep(0.15)

        master_fd, slave_fd = pty.openpty()

        # Set initial terminal size
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

        pid = os.fork()
        if pid == 0:
            # Child process
            os.close(master_fd)
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)
            os.execvp(
                self._tmux,
                [self._tmux, "-L", self.socket_name, "attach-session", "-t", session_name],
            )
        else:
            # Parent process
            os.close(slave_fd)
            return PtyHandle(master_fd=master_fd, child_pid=pid)

    def get_pane_pid(self, session_name: str) -> int | None:
        """Get the PID of the process running in the tmux pane."""
        try:
            out = self._run(
                "list-panes", "-t", session_name, "-F", "#{pane_pid}"
            )
            pid_str = out.strip().splitlines()[0]
            return int(pid_str)
        except (TmuxError, ValueError, IndexError):
            return None

    def resolve_claude_session_id_by_inner_id(self, inner_id: str, timeout: float = 15.0) -> tuple[str | None, int | None]:
        """Resolve Claude session ID for a new session using the PID file written by the wrapper sh.
        Returns (sessionId, pid) — either or both may be None on failure."""
        pid_file = Path(f"/tmp/claude-inner-{inner_id}.pid")
        claude_sessions_dir = Path.home() / ".claude" / "sessions"
        deadline = time.monotonic() + timeout
        found_pid: int | None = None

        while time.monotonic() < deadline:
            if pid_file.exists():
                try:
                    found_pid = int(pid_file.read_text().strip())
                    session_file = claude_sessions_dir / f"{found_pid}.json"
                    if session_file.exists():
                        data = json.loads(session_file.read_text())
                        sid = data.get("sessionId")
                        if sid:
                            try:
                                pid_file.unlink()
                            except OSError:
                                pass
                            return sid, found_pid
                except (ValueError, json.JSONDecodeError, OSError):
                    pass
            time.sleep(0.3)

        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass
        return None, found_pid

    def resolve_claude_session_id(self, session_name: str, timeout: float = 15.0) -> tuple[str | None, int | None]:
        """
        Wait for Claude CLI to start and write its session file,
        then read the Claude session ID from ~/.claude/sessions/{pid}.json.

        Strategy: find all descendant PIDs of the tmux pane, then check if
        ~/.claude/sessions/{pid}.json exists for any of them.
        Returns (sessionId, pid) — either or both may be None on failure.
        """
        claude_sessions_dir = Path.home() / ".claude" / "sessions"
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            pane_pid = self.get_pane_pid(session_name)
            if pane_pid is not None:
                # Check pane PID itself and all descendants
                descendants = [pane_pid] + self._get_descendants(pane_pid)
                for dpid in descendants:
                    session_file = claude_sessions_dir / f"{dpid}.json"
                    if session_file.exists():
                        try:
                            data = json.loads(session_file.read_text())
                            sid = data.get("sessionId")
                            if sid:
                                return sid, dpid
                        except (json.JSONDecodeError, OSError):
                            continue
            time.sleep(0.3)

        return None, None

    @staticmethod
    def _get_descendants(pid: int) -> list[int]:
        """Get all descendant PIDs of a process via /proc."""
        # Build parent→children map from /proc
        children_map: dict[int, list[int]] = {}
        proc = Path("/proc")
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text()
                parts = stat.split(")")[-1].split()
                ppid = int(parts[1])
                child = int(entry.name)
                children_map.setdefault(ppid, []).append(child)
            except (OSError, ValueError, IndexError):
                continue
        # BFS from pid
        result: list[int] = []
        queue = children_map.get(pid, [])
        while queue:
            current = queue.pop(0)
            result.append(current)
            queue.extend(children_map.get(current, []))
        return result

    def capture_full_history(self, session_name: str, ansi: bool = True) -> str:
        """Capture the entire scrollback + visible content of a tmux pane."""
        try:
            args = ["capture-pane", "-p", "-S", "-", "-E", "-", "-t", session_name]
            if ansi:
                args.insert(2, "-e")  # include escape sequences
            return self._run(*args)
        except TmuxError:
            return ""

    def capture_visible_screen(self, session_name: str) -> str:
        """Capture only the currently visible pane content (no scrollback, no ANSI)."""
        try:
            return self._run("capture-pane", "-p", "-t", session_name)
        except TmuxError:
            return ""

    def _auto_accept_trust_dialog(self, session_name: str, timeout: float = 8.0) -> None:
        """Poll the TUI after startup; if Claude's workspace-trust dialog shows,
        press Enter to accept the default first option ("Yes, I trust this folder").

        Returns silently if no dialog appears within `timeout`. Never raises —
        failure here must not affect session creation.
        """
        time.sleep(0.8)  # let the TUI render before first scan
        start = time.monotonic()
        sent = False
        while time.monotonic() - start < timeout:
            screen = self.capture_visible_screen(session_name)
            is_trust = _looks_like_trust_dialog(screen)
            if is_trust and not sent:
                try:
                    self._run("send-keys", "-t", session_name, "Enter")
                except TmuxError:
                    return
                sent = True
                logger.info("auto-accepted trust dialog for %s", session_name)
                time.sleep(0.5)
                continue
            if sent and not is_trust:
                return  # dialog cleared — success
            time.sleep(0.4)
        if sent:
            logger.warning(
                "trust dialog Enter sent but dialog did not clear within %.1fs (%s)",
                timeout, session_name,
            )

    def search_init_pty(self, pty_handle: "PtyHandle", query: str) -> None:
        """Enter copy-mode, go to top, search forward for query, center result."""
        import time as _time
        w = pty_handle.write
        # Exit any existing copy-mode
        w(b"q")
        _time.sleep(0.15)
        # Ctrl-B [ → copy-mode
        w(b"\x02")
        _time.sleep(0.05)
        w(b"[")
        _time.sleep(0.2)
        # g → top of history
        w(b"g")
        _time.sleep(0.15)
        # /query Enter → first match
        w(b"/")
        _time.sleep(0.05)
        w(query.encode("utf-8"))
        w(b"\r")
        _time.sleep(0.15)
        # z → center
        w(b"z")

    def search_next_pty(self, pty_handle: "PtyHandle") -> None:
        """Jump to the next search match (must already be in copy-mode with active search)."""
        import time as _time
        pty_handle.write(b"n")
        _time.sleep(0.1)
        pty_handle.write(b"z")

    def get_pane_history_size(self, session_name: str) -> int | None:
        """Return the current tmux pane history size (grows as output arrives)."""
        try:
            out = self._run("display-message", "-p", "-t", session_name, "#{history_size}")
            return int(out.strip())
        except (TmuxError, ValueError):
            return None

    def list_sessions(self) -> list[str]:
        out = self._run("list-sessions", "-F", "#{session_name}")
        return [row for row in out.splitlines() if row] if out else []

    def terminate(self, session_name: str) -> None:
        self._run("kill-session", "-t", session_name)

    def has_session(self, session_name: str) -> bool:
        try:
            self._run("has-session", "-t", session_name)
            return True
        except TmuxError:
            return False

    def send_keys(self, session_name: str, text: str) -> None:
        """Send text followed by Enter to a tmux session (as if typed by the user)."""
        self._run("send-keys", "-t", session_name, text, "Enter")
