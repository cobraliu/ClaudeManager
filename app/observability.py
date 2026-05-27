from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger("claude-web-audit")


def _get_log_dir() -> Path:
    env = os.getenv("CLAUDEMANAGER_LOG_DIR")
    if env:
        p = Path(env)
        p.mkdir(parents=True, exist_ok=True)
        return p
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        p = Path.home() / ".claudemanager" / "logs"
        p.mkdir(parents=True, exist_ok=True)
        return p
    # Dev: app/observability.py → app/ → project root → logs/
    p = Path(__file__).resolve().parent.parent / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def configure_logging() -> None:
    log_dir = _get_log_dir()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = log_dir / f"{today}.log"

    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(log_file),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
        utc=True,
    )
    # Rotated files get named YYYY-MM-DD.log by stripping the base name
    file_handler.namer = lambda name: str(log_dir / (Path(name).suffix.lstrip(".") + ".log"))
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s [%(filename)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(file_handler)
        root.addHandler(stream_handler)
        root.setLevel(logging.INFO)

    if not logger.handlers:
        logger.addHandler(file_handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

    # The watchfiles library logs an INFO line for every batch of detected
    # changes ("1 change detected"). With one fs/watch WS per session × three
    # frontend hooks (MobilePage, FileEditorModal, CodePane), the log file
    # can balloon to tens of MB per day. Drop these to WARNING — real
    # problems (filter errors, rust notify exceptions) still surface.
    logging.getLogger("watchfiles.main").setLevel(logging.WARNING)


def audit_event(user_id: str, action: str, session_id: str, extra: dict[str, Any] | None = None) -> None:
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "action": action,
        "session_id": session_id,
    }
    if extra:
        payload["extra"] = extra
    logger.info(json.dumps(payload, ensure_ascii=True))
