#!/usr/bin/env python3
"""ClaudeManager – single-binary entry point.

Commands:
  claudemanager client       # start server in background + open UI window
  claudemanager server       # start server as background daemon, then exit
  claudemanager              # print help and exit
"""
from __future__ import annotations

import argparse
import os
import secrets
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

DEFAULT_PORT = 19098
DEFAULT_HOST = "127.0.0.1"

_ZOOM_STEP = 0.1
_ZOOM_MIN = 0.25
_ZOOM_MAX = 4.0


def _default_data_dir() -> Path:
    return Path.home() / ".claudemanager"


def _zoom_file(data_dir: Path) -> Path:
    return data_dir / "zoom.txt"


def _load_zoom(data_dir: Path) -> float:
    try:
        z = float(_zoom_file(data_dir).read_text().strip())
        return max(_ZOOM_MIN, min(_ZOOM_MAX, z))
    except Exception:
        return 1.0


def _save_zoom(data_dir: Path, z: float) -> None:
    try:
        _zoom_file(data_dir).write_text(str(z))
    except Exception:
        pass


def _get_icon_path() -> str | None:
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).parent / "frontend" / "src" / "assets"
    icon = base / "claudecode.png"
    return str(icon) if icon.exists() else None


def _is_port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _wait_for_server(host: str, port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_port_open(host, port):
            return True
        time.sleep(0.25)
    return False


def _write_pid(data_dir: Path, pid: int) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "claudemanager.pid").write_text(str(pid))


def _read_pid(data_dir: Path) -> int | None:
    try:
        return int((data_dir / "claudemanager.pid").read_text().strip())
    except (OSError, ValueError):
        return None


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_existing_server(data_dir: Path) -> None:
    pid = _read_pid(data_dir)
    if pid and _is_process_alive(pid):
        try:
            os.kill(pid, 15)  # SIGTERM
            time.sleep(1.5)
            if _is_process_alive(pid):
                os.kill(pid, 9)  # SIGKILL
        except OSError:
            pass
    pid_file = data_dir / "claudemanager.pid"
    pid_file.unlink(missing_ok=True)


def _build_server_cmd(host: str, port: int, data_dir: Path) -> list[str]:
    """Return the command list to run the server process."""
    if getattr(sys, "frozen", False):
        # Running as PyInstaller binary: re-exec ourselves in internal serve mode
        return [sys.executable, "_serve", "--host", host, "--port", str(port)]
    # Dev mode: use uvicorn directly from the venv
    project_root = Path(__file__).parent
    venv_python = Path(sys.executable)
    return [
        str(venv_python), "-c",
        (
            "import sys, os; "
            f"sys.path.insert(0, {str(project_root)!r}); "
            f"os.environ['CLAUDEMANAGER_DATA_DIR'] = {str(data_dir)!r}; "
            "import uvicorn; "
            f"uvicorn.run('app.claudemanager:app', host={host!r}, port={port})"
        ),
    ]


def _write_autologin_token(data_dir: Path) -> str:
    token = secrets.token_urlsafe(32)
    data_dir.mkdir(parents=True, exist_ok=True)
    token_file = data_dir / "autologin.token"
    # Format: <token>:<unix_timestamp>  so the server can enforce TTL.
    token_file.write_text(f"{token}:{time.time()}")
    token_file.chmod(0o600)
    return token


def cmd_client(args: argparse.Namespace) -> None:
    """Default mode: start server if not already running, then open browser."""
    host = args.host
    port = args.port
    data_dir = _default_data_dir()

    _kill_existing_server(data_dir)
    print(f"[claudemanager] Starting server on {host}:{port} …")
    env = os.environ.copy()
    env["CLAUDEMANAGER_DATA_DIR"] = str(data_dir)

    cmd = _build_server_cmd(host, port, data_dir)
    proc = subprocess.Popen(
        cmd,
        env=env,
        cwd=str(Path(__file__).parent) if not getattr(sys, "frozen", False) else None,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _write_pid(data_dir, proc.pid)

    if not _wait_for_server(host, port, timeout=20):
        print("[claudemanager] ERROR: server failed to start within 20 seconds.")
        sys.exit(1)
    print("[claudemanager] Server ready.")

    token = _write_autologin_token(data_dir)
    url = f"http://{host}:{port}/?autologin={token}"
    print(f"[claudemanager] Opening {url}")
    _open_webview(url, data_dir)


_RELOAD_JS = """
(function() {
    if (window.__cmReloadBound) return;
    window.__cmReloadBound = true;
    window.addEventListener('keydown', function(e) {
        if (e.key === 'F5' || ((e.ctrlKey || e.metaKey) && e.key === 'r')) {
            e.preventDefault();
            location.reload();
        }
    }, true);
})();
"""


_qt_setup_worker = None  # kept alive so it isn't GC'd before the slot fires


def _open_webview(url: str, data_dir: Path) -> None:
    try:
        import webview  # noqa: PLC0415
        window = webview.create_window("ClaudeManager", url, width=1440, height=900, text_select=True)
        window.events.loaded += lambda: window.evaluate_js(_RELOAD_JS)
        # Force Qt backend so pywebview skips the GTK probe entirely.
        webview.start(func=_schedule_qt_setup, args=[window, data_dir], gui="qt")
        _kill_existing_server(data_dir)
    except Exception as exc:
        print(f"[claudemanager] webview unavailable ({exc}), falling back to browser")
        webbrowser.open(url)


def _schedule_qt_setup(window, data_dir: Path) -> None:
    """Called in pywebview's func (daemon) thread.

    QTimer.singleShot from a non-GUI thread fires in the *calling* thread's
    event loop — which doesn't exist here, so the callback never runs.
    The correct pattern is to create a QObject, move it to the GUI thread, and
    emit a queued signal so Qt delivers the call in the right thread.
    """
    global _qt_setup_worker
    time.sleep(0.5)  # allow BrowserView to be constructed
    try:
        from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot, Qt  # noqa: PLC0415
        from PyQt6.QtWidgets import QApplication  # noqa: PLC0415

        app = QApplication.instance()
        if app is None:
            return

        class _SetupWorker(QObject):
            go = pyqtSignal()

            @pyqtSlot()
            def execute(self) -> None:
                _install_qt_features(window, data_dir)

        worker = _SetupWorker()
        # Move to the GUI thread so the queued signal is delivered there.
        worker.moveToThread(app.thread())
        worker.go.connect(worker.execute, Qt.ConnectionType.QueuedConnection)
        _qt_setup_worker = worker  # prevent GC before the slot fires
        worker.go.emit()
    except Exception as exc:
        print(f"[zoom] schedule failed: {exc}")


def _install_qt_features(window, data_dir: Path) -> None:
    """Runs on the Qt GUI thread: sets icon + installs Ctrl+scroll/±/0 zoom."""
    try:
        from webview.platforms.qt import BrowserView  # noqa: PLC0415
        from PyQt6.QtCore import QObject, QEvent, Qt  # noqa: PLC0415
        from PyQt6.QtGui import QIcon  # noqa: PLC0415
        from PyQt6.QtWidgets import QApplication  # noqa: PLC0415

        bv = BrowserView.instances.get(window.uid)
        if bv is None:
            return
        wv = bv.webview  # QWebEngineView subclass
        app = QApplication.instance()

        # ── Window icon ────────────────────────────────────────────────────
        icon_path = _get_icon_path()
        if icon_path:
            icon = QIcon(icon_path)
            bv.setWindowIcon(icon)
            app.setWindowIcon(icon)

        # ── Zoom ───────────────────────────────────────────────────────────
        wv.setZoomFactor(_load_zoom(data_dir))

        # Restore zoom after SPA navigation / redirects
        def _on_load_finished(_ok: bool) -> None:
            wv.setZoomFactor(_load_zoom(data_dir))

        wv.page().loadFinished.connect(_on_load_finished)

        class _ZoomFilter(QObject):
            def eventFilter(self, obj, event: QEvent) -> bool:  # type: ignore[override]
                t = event.type()
                if t == QEvent.Type.Wheel:
                    if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                        delta = event.angleDelta().y()
                        z = wv.zoomFactor()
                        z += _ZOOM_STEP if delta > 0 else -_ZOOM_STEP
                        z = max(_ZOOM_MIN, min(_ZOOM_MAX, round(z * 10) / 10))
                        wv.setZoomFactor(z)
                        _save_zoom(data_dir, z)
                        return True  # consume — prevent page scroll while zooming
                elif t == QEvent.Type.KeyPress:
                    if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                        k = event.key()
                        z = wv.zoomFactor()
                        if k in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
                            z = min(_ZOOM_MAX, round((z + _ZOOM_STEP) * 10) / 10)
                        elif k == Qt.Key.Key_Minus:
                            z = max(_ZOOM_MIN, round((z - _ZOOM_STEP) * 10) / 10)
                        elif k == Qt.Key.Key_0:
                            z = 1.0
                        else:
                            return False
                        wv.setZoomFactor(z)
                        _save_zoom(data_dir, z)
                        return True
                return False

        # Install on QApplication so the filter receives events for ALL widgets,
        # including WebEngine's internal RenderWidgetHostViewQtDelegateWidget
        # children that actually receive the raw mouse/keyboard input.
        zf = _ZoomFilter(bv)  # parent = bv keeps it alive for the window's lifetime
        app.installEventFilter(zf)

    except Exception as exc:
        print(f"[zoom/icon] install failed: {exc}")


def _run_uvicorn_foreground(args: argparse.Namespace) -> None:
    """Internal: run uvicorn in the foreground (used by spawned server subprocess)."""
    existing = os.environ.get("CLAUDEMANAGER_DATA_DIR")
    data_dir = Path(existing) if existing else _default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["CLAUDEMANAGER_DATA_DIR"] = str(data_dir)

    _write_pid(data_dir, os.getpid())

    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        sys.path.insert(0, sys._MEIPASS)
    else:
        project_root = str(Path(__file__).parent)
        if project_root not in sys.path:
            sys.path.insert(0, project_root)

    import uvicorn  # noqa: PLC0415
    uvicorn.run(
        "app.claudemanager:app",
        host=args.host,
        port=args.port,
        log_level="info",
    )


def cmd_server(args: argparse.Namespace) -> None:
    """Server mode: spawn uvicorn as a background daemon, then exit."""
    host = args.host
    port = args.port
    data_dir = _default_data_dir()

    _kill_existing_server(data_dir)
    print(f"[claudemanager] Starting server on {host}:{port} …")

    env = os.environ.copy()
    env["CLAUDEMANAGER_DATA_DIR"] = str(data_dir)

    cmd = _build_server_cmd(host, port, data_dir)
    proc = subprocess.Popen(
        cmd,
        env=env,
        cwd=str(Path(__file__).parent) if not getattr(sys, "frozen", False) else None,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # detach from terminal — survives shell exit
    )
    _write_pid(data_dir, proc.pid)

    if not _wait_for_server(host, port, timeout=20):
        print("[claudemanager] ERROR: server failed to start within 20 seconds.")
        sys.exit(1)

    print(f"[claudemanager] Server running at http://{host}:{port}/")
    print(f"[claudemanager] PID {proc.pid} saved to {data_dir / 'claudemanager.pid'}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="claudemanager",
        description="ClaudeManager – web UI for Claude Code sessions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Commands:\n"
            f"  client  Start server in background + open UI window  (port default: {DEFAULT_PORT})\n"
            f"  server  Start server as background daemon, then exit (port default: {DEFAULT_PORT})\n"
            "\n"
            "Examples:\n"
            "  claudemanager client\n"
            "  claudemanager server\n"
            "  claudemanager server --host 0.0.0.0 --port 8080\n"
        ),
    )

    # _serve is an internal-only subcommand used by _build_server_cmd when the
    # frozen binary re-execs itself to run uvicorn in the foreground.  Handle it
    # before argparse so it never appears in the public help/choices list.
    if len(sys.argv) >= 2 and sys.argv[1] == "_serve":
        _internal = argparse.ArgumentParser()
        _internal.add_argument("--port", type=int, default=DEFAULT_PORT)
        _internal.add_argument("--host", default=DEFAULT_HOST)
        _run_uvicorn_foreground(_internal.parse_args(sys.argv[2:]))
        return

    subparsers = parser.add_subparsers(dest="command")

    client_p = subparsers.add_parser("client", help="Start server + open UI window")
    client_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    client_p.add_argument("--host", default=DEFAULT_HOST)

    server_p = subparsers.add_parser("server", help="Start server as background daemon")
    server_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    server_p.add_argument("--host", default=DEFAULT_HOST)

    args = parser.parse_args()

    if args.command == "client":
        cmd_client(args)
    elif args.command == "server":
        cmd_server(args)
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
