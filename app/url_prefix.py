"""Sub-path mount support — emit URLs with the configured root prefix.

The ROOT_PATH env var (consumed at import time) controls the public mount
prefix. When ClaudeManager sits behind nginx at e.g. `/rosaccm/`:

    server { listen 443 ssl;
        location /rosaccm/ { proxy_pass http://127.0.0.1:19099/; ... }
    }

set ROOT_PATH=/rosaccm so any URL we emit (ws_url, redirect targets, etc.)
includes the prefix the browser needs to reach us back through nginx. The
nginx config strips the prefix before forwarding, so our routers still
match bare `/api/...` and `/ws/...` paths.

Empty value (the default) preserves root-mount behaviour: `ws_path("/ws/x")`
returns "/ws/x" unchanged. Same code, same binary, env switches the mode.
"""
from __future__ import annotations

import os

ROOT_PATH: str = os.getenv("ROOT_PATH", "").rstrip("/")


def public_path(path: str) -> str:
    """Prepend ROOT_PATH to an absolute path served by this app.

    `path` must start with "/". Returns the original path unchanged when
    ROOT_PATH is empty (root-mounted deployments — subdomain or :port).
    """
    if not ROOT_PATH:
        return path
    if not path.startswith("/"):
        path = "/" + path
    return ROOT_PATH + path
