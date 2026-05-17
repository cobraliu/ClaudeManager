# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for ClaudeManager."""

import os
from pathlib import Path

ROOT = Path(SPECPATH)
FRONTEND_DIST = ROOT / "frontend" / "dist"

a = Analysis(
    [str(ROOT / "claudemanager_cli.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # Bundle the built frontend
        (str(FRONTEND_DIST), "frontend/dist"),
        # Bundle the app Python source (needed at import time)
        (str(ROOT / "app"), "app"),
        # Window icon
        (str(ROOT / "frontend" / "src" / "assets" / "claudecode.png"), "."),
    ],
    hiddenimports=[
        # FastAPI / Starlette
        "fastapi",
        "fastapi.middleware",
        "fastapi.middleware.cors",
        "fastapi.staticfiles",
        "fastapi.responses",
        "starlette",
        "starlette.middleware",
        "starlette.middleware.cors",
        "starlette.routing",
        "starlette.responses",
        "starlette.staticfiles",
        "starlette.datastructures",
        "starlette.background",
        "starlette.concurrency",
        "starlette.types",
        "starlette.websockets",
        "starlette.formparsers",
        # Uvicorn — click is imported unconditionally at module level in uvicorn.config / uvicorn.main
        "click",
        "uvicorn",
        "uvicorn.main",
        "uvicorn.config",
        "uvicorn.server",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.http.httptools_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.websockets_impl",
        # wsproto_impl omitted — wsproto is not installed; uvicorn falls back to websockets_impl
        "uvicorn.loops",
        "uvicorn.loops.asyncio",
        "uvicorn.loops.uvloop",
        "uvicorn.logging",
        "uvicorn.middleware",
        "uvicorn.middleware.proxy_headers",
        # Pydantic v2 + runtime deps
        "pydantic",
        "pydantic_core",
        "annotated_types",
        # PyYAML
        "yaml",
        # PyJWT
        "jwt",
        # websockets (13.x — legacy subpackage removed)
        "websockets",
        # python-multipart: starlette 1.x tries python_multipart first, then multipart
        "multipart",
        "multipart.multipart",
        "python_multipart",
        "python_multipart.multipart",
        "python_multipart.decoders",
        "python_multipart.exceptions",
        # httptools / uvloop (optional uvicorn extras)
        "httptools",
        "uvloop",
        # h11
        "h11",
        # anyio + exceptiongroup (required on Python < 3.11)
        "anyio",
        "anyio.from_thread",
        "anyio._backends._asyncio",
        "exceptiongroup",
        # standard library extras
        "sqlite3",
        "email.mime",
        "email.mime.text",
        # pywebview (client mode GUI window)
        "webview",
        "webview.platforms.qt",
        "webview.guilib",
        "webview.http",
        "webview.util",
        "webview.window",
        "webview.event",
        "webview.dom",
        "webview.menu",
        "webview.screen",
        "bottle",
        "proxy_tools",
        # PyQt6 + WebEngine (used by pywebview qt backend)
        "PyQt6",
        "PyQt6.QtCore",
        "PyQt6.QtGui",
        "PyQt6.QtWidgets",
        "PyQt6.QtNetwork",
        "PyQt6.QtWebEngineWidgets",
        "PyQt6.QtWebEngineCore",
        "PyQt6.QtWebChannel",
        "PyQt6.sip",
        # App modules
        "app.claudemanager",
        "app.config",
        "app.security",
        "app.observability",
        "app.api.auth",
        "app.api.sessions",
        "app.api.files",
        "app.api.code_api",
        "app.api.config_api",
        "app.api.fs",
        "app.api.usage_api",
        "app.models.session",
        "app.models.user",
        "app.services.session_store",
        "app.services.user_store",
        "app.services.tmux_service",
        "app.services.claude_session_reader",
        "app.services.cursor_session_reader",
        "app.services.git_service",
        "app.ws.terminal_ws",
        "app.ws.shell_ws",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="claudemanager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
