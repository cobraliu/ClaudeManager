from __future__ import annotations

import gzip
import bz2
import lzma
import os
import re
import shutil
import sqlite3
import tarfile
import time
import zipfile
from pathlib import Path

import mimetypes

import io
import subprocess
import tempfile
import uuid

import asyncio
import json

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from watchfiles import awatch, Change

from app.security import CurrentUser, _decode_token
from app.services.session_store import SessionStore

router = APIRouter(prefix="/api/sessions", tags=["files"])

_store: SessionStore | None = None


def configure(store: SessionStore) -> None:
    global _store
    _store = store


def _get_store() -> SessionStore:
    assert _store is not None
    return _store


MAX_FILE_SIZE = 1 * 1024 * 1024  # 1MB
MAX_DIR_ENTRIES = 500

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".json", ".json5", ".jsonl",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".py", ".pyx", ".pyi",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".css", ".scss", ".sass", ".less",
    ".html", ".htm", ".xml", ".svg",
    ".sh", ".bash", ".zsh", ".fish", ".ps1",
    ".go", ".rs", ".java", ".kt", ".scala",
    ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp",
    ".rb", ".php", ".swift", ".cs", ".fs",
    ".lua", ".vim", ".el",
    ".sql", ".graphql", ".proto",
    ".tf", ".hcl",
    ".r",
    ".csv", ".tsv",
    ".log", ".lock",
    ".gitignore", ".gitattributes", ".editorconfig",
}

SPECIAL_NAMES_TEXT = {
    "Makefile", "Dockerfile", "Vagrantfile", "Gemfile",
    "Rakefile", "Pipfile", "Procfile", "Brewfile",
    ".gitignore", ".gitattributes", ".env", ".editorconfig",
    "requirements.txt", "setup.py", "setup.cfg", "pyproject.toml",
    "package.json", "package-lock.json", "yarn.lock",
    "go.mod", "go.sum", "Cargo.toml", "Cargo.lock",
}

SQLITE_EXTENSIONS = {".db", ".sqlite", ".sqlite3", ".db3"}

# Directories to mark as skipped (too large / not useful to browse)
SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", ".next",
}


def _is_text(path: Path) -> bool:
    if path.name in SPECIAL_NAMES_TEXT:
        return True
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return True
    # Sniff for null bytes (binary indicator)
    try:
        chunk = path.read_bytes()[:8192]
        return b"\x00" not in chunk
    except OSError:
        return False


def _resolve(session_cwd: str, rel: str) -> Path:
    """Resolve rel path inside session cwd; raise 403 on traversal."""
    base = Path(session_cwd).resolve()
    if not rel or rel in ("", ".", "/"):
        return base
    target = (base / rel.lstrip("/")).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=403, detail="path traversal not allowed")
    return target


class FileEntry(BaseModel):
    name: str
    path: str       # relative to session cwd
    type: str       # 'file' | 'dir'
    size: int | None = None
    is_text: bool = False
    is_skipped: bool = False  # .git, node_modules, etc.
    is_sqlite: bool = False
    is_archive: bool = False


class FilesResponse(BaseModel):
    entries: list[FileEntry]
    path: str       # current dir relative to cwd


class FileContent(BaseModel):
    path: str
    content: str


class FileWriteRequest(BaseModel):
    path: str
    content: str
    # Optional optimistic-lock token captured when the client started editing.
    # If present and the on-disk mtime differs, the server returns 409 so the
    # client can warn the user before clobbering an external change. Pass
    # force=true to skip the check and overwrite anyway.
    expected_mtime: float | None = None
    force: bool = False


class SqliteResponse(BaseModel):
    tables: list[str]
    columns: list[str] = []
    rows: list[list] = []
    total: int = 0
    path: str


class SqliteExecRequest(BaseModel):
    path: str
    sql: str


class SqliteExecResponse(BaseModel):
    columns: list[str] = []
    rows: list[list] = []
    affected: int = 0
    message: str = ""


ARCHIVE_EXTENSIONS_SUFFIX = (
    ".zip", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz",
    ".tar", ".gz", ".bz2", ".xz",
)
ARCHIVE_MAX_SIZE = 100 * 1024 * 1024  # 100 MB


def _is_archive_name(name: str) -> bool:
    n = name.lower()
    return any(n.endswith(s) for s in ARCHIVE_EXTENSIONS_SUFFIX)


def _list_archive_entries(path: Path) -> list[str]:
    """Return paths inside an archive (up to 2000 entries, no extraction)."""
    n = path.name.lower()
    entries: list[str] = []
    try:
        if n.endswith(".zip"):
            with zipfile.ZipFile(path, "r") as zf:
                entries = zf.namelist()
        elif any(n.endswith(s) for s in (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar")):
            with tarfile.open(path, "r:*") as tf:
                entries = tf.getnames()
        elif n.endswith(".gz"):
            entries = [path.name[:-3]]
        elif n.endswith(".bz2"):
            entries = [path.name[:-4]]
        elif n.endswith(".xz"):
            entries = [path.name[:-3]]
    except Exception:
        pass
    return entries[:2000]


def _extract_archive(src: Path, dest: Path) -> None:
    """Extract archive src into dest directory (must not exist yet)."""
    dest.mkdir(parents=True, exist_ok=True)
    n = src.name.lower()
    if n.endswith(".zip"):
        with zipfile.ZipFile(src, "r") as zf:
            zf.extractall(dest)
    elif any(n.endswith(s) for s in (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar")):
        with tarfile.open(src, "r:*") as tf:
            # Safe extraction: skip absolute paths and .. traversal
            members = [m for m in tf.getmembers()
                       if not os.path.isabs(m.name) and ".." not in m.name]
            tf.extractall(dest, members=members)
    elif n.endswith(".gz"):
        out = dest / src.name[:-3]
        with gzip.open(src, "rb") as gf:
            out.write_bytes(gf.read())
    elif n.endswith(".bz2"):
        out = dest / src.name[:-4]
        with bz2.open(src, "rb") as bf:
            out.write_bytes(bf.read())
    elif n.endswith(".xz"):
        out = dest / src.name[:-3]
        with lzma.open(src, "rb") as xf:
            out.write_bytes(xf.read())
    else:
        raise ValueError(f"unsupported archive: {src.name}")


@router.get("/{session_id}/fs/list")
def list_files(
    session_id: str,
    path: str = Query(default="", max_length=500),
    hidden: bool = Query(default=False),
    user_id: CurrentUser = None,  # type: ignore[assignment]
) -> FilesResponse:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")

    target = _resolve(session.cwd, path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="path not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="not a directory")

    base = Path(session.cwd).resolve()
    rel_current = str(target.relative_to(base)) if target != base else ""

    try:
        raw = list(target.iterdir())
    except PermissionError:
        raw = []

    # Dirs first, then files; alphabetical within each group; optionally filter hidden
    if not hidden:
        raw = [e for e in raw if not e.name.startswith(".")]

    def _sort_key(e: Path) -> tuple:
        try:
            return (e.is_file(), e.name.lower())
        except OSError:
            return (True, e.name.lower())

    raw.sort(key=_sort_key)

    entries: list[FileEntry] = []
    for entry in raw[:MAX_DIR_ENTRIES]:
        try:
            rel = str(entry.relative_to(base))
            is_dir = entry.is_dir()
            skip = is_dir and entry.name in SKIP_DIRS
            is_sq = not is_dir and entry.suffix.lower() in SQLITE_EXTENSIONS
            is_arc = not is_dir and _is_archive_name(entry.name)
            try:
                size = entry.stat().st_size if not is_dir else None
            except OSError:
                size = None
            entries.append(FileEntry(
                name=entry.name,
                path=rel,
                type="dir" if is_dir else "file",
                size=size,
                is_text=_is_text(entry) if (not is_dir and not is_sq and not is_arc) else False,
                is_skipped=skip,
                is_sqlite=is_sq,
                is_archive=is_arc,
            ))
        except OSError:
            # Skip entries that can't be read (broken symlinks, permission errors, etc.)
            continue

    return FilesResponse(entries=entries, path=rel_current)


MAX_SEARCH_RESULTS = 200


@router.get("/{session_id}/fs/search")
def search_files(
    session_id: str,
    q: str = Query(..., min_length=1, max_length=200),
    hidden: bool = Query(default=False),
    user_id: CurrentUser = None,  # type: ignore[assignment]
) -> FilesResponse:
    """Recursively search for files/dirs whose name contains q (case-insensitive)."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")

    base = Path(session.cwd).resolve()
    needle = q.lower()
    results: list[FileEntry] = []

    for root, dirs, files in os.walk(base):
        # Skip hidden dirs and SKIP_DIRS
        dirs[:] = [
            d for d in dirs
            if (hidden or not d.startswith(".")) and d not in SKIP_DIRS
        ]
        root_path = Path(root)

        # Check dirs
        for d in dirs:
            if needle in d.lower():
                entry_path = root_path / d
                rel = str(entry_path.relative_to(base))
                results.append(FileEntry(name=d, path=rel, type="dir"))
                if len(results) >= MAX_SEARCH_RESULTS:
                    return FilesResponse(entries=results, path="")

        # Check files
        for f in files:
            if not hidden and f.startswith("."):
                continue
            if needle in f.lower():
                entry_path = root_path / f
                try:
                    rel = str(entry_path.relative_to(base))
                    is_sq = entry_path.suffix.lower() in SQLITE_EXTENSIONS
                    is_arc = _is_archive_name(f)
                    try:
                        size = entry_path.stat().st_size
                    except OSError:
                        size = None
                    results.append(FileEntry(
                        name=f,
                        path=rel,
                        type="file",
                        size=size,
                        is_text=_is_text(entry_path) if (not is_sq and not is_arc) else False,
                        is_sqlite=is_sq,
                        is_archive=is_arc,
                    ))
                except OSError:
                    continue
                if len(results) >= MAX_SEARCH_RESULTS:
                    return FilesResponse(entries=results, path="")

    return FilesResponse(entries=results, path="")


@router.get("/{session_id}/fs/read")
def read_file(
    session_id: str,
    path: str = Query(..., max_length=500),
    user_id: CurrentUser = None,  # type: ignore[assignment]
) -> FileContent:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")

    target = _resolve(session.cwd, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    if target.stat().st_size > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="file too large (>1MB)")
    if not _is_text(target):
        raise HTTPException(status_code=415, detail="binary file not supported")

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    rel = str(target.relative_to(Path(session.cwd).resolve()))
    return FileContent(path=rel, content=content)


MAX_DOWNLOAD_SIZE = 16 * 1024 * 1024   # 16 MB
MAX_UPLOAD_SIZE   = 16 * 1024 * 1024   # 16 MB

# Attachment uploads for in-chat AI analysis (images for visual analysis,
# any other file for the model to read/parse via Read). Stored under
# <session.cwd>/.claude/uploads/ so they sit alongside other Claude tooling
# state and are typically already gitignored. The absolute path is returned
# to the frontend so it can inject `@<path>` into the next prompt — Claude
# CLI's reference syntax then embeds image content blocks for image
# extensions, and the model reads non-image attachments via the Read tool.
MAX_ATTACHMENT_SIZE = 50 * 1024 * 1024  # 50 MB — matches MAX_TRANSFER_BYTES
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
ATTACHMENT_UPLOAD_SUBDIR = Path(".claude") / "uploads"
# Legacy aliases for the original image-only endpoint. Kept so older imports
# don't break; new code should use the ATTACHMENT_* names.
MAX_IMAGE_SIZE = MAX_ATTACHMENT_SIZE
IMAGE_UPLOAD_SUBDIR = ATTACHMENT_UPLOAD_SUBDIR
# Pattern matching the filenames serve_uploaded_image accepts: 32-hex uuid
# + image extension. Load-bearing path-traversal guard for inline rendering.
_IMAGE_FILENAME_RE = re.compile(r"^[a-f0-9]{32}\.(?:png|jpg|jpeg|gif|webp)$")
# Allowed shape of a stored attachment extension: alphanumeric, ≤ 16 chars.
# We sanitize at write time so the filename on disk is always safe to embed
# in shell arguments / log lines and so `@<path>` stays parseable.
_EXT_SANITIZE_RE = re.compile(r"[^A-Za-z0-9]")


@router.get("/{session_id}/fs/raw")
def serve_raw_file(
    session_id: str,
    path: str = Query(..., max_length=500),
    download: bool = Query(default=False),
    user_id: CurrentUser = None,  # type: ignore[assignment]
) -> StreamingResponse:
    """Serve a file as raw bytes. download=true adds Content-Disposition: attachment.

    Uses StreamingResponse (no Content-Length) so that files being actively written
    (e.g. log files) don't cause a Content-Length mismatch error.
    """
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")

    target = _resolve(session.cwd, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")

    size = target.stat().st_size
    if size > 100 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="file too large (>100MB)")

    mime, _ = mimetypes.guess_type(str(target))
    headers: dict[str, str] = {}
    if download:
        from urllib.parse import quote as _quote
        encoded = _quote(target.name, safe="")
        headers["Content-Disposition"] = (
            f"attachment; filename*=UTF-8''{encoded}"
        )

    def _iter_file(p: Path):
        with p.open("rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        _iter_file(target),
        media_type=mime or "application/octet-stream",
        headers=headers,
    )


class MkdirRequest(BaseModel):
    path: str


@router.post("/{session_id}/fs/mkdir", status_code=status.HTTP_204_NO_CONTENT)
def make_directory(
    session_id: str,
    body: MkdirRequest,
    user_id: CurrentUser = None,  # type: ignore[assignment]
) -> None:
    """Create a directory (and parents) inside the session workspace."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not body.path:
        raise HTTPException(status_code=400, detail="path required")
    target = _resolve(session.cwd, body.path)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/{session_id}/fs/upload", status_code=status.HTTP_204_NO_CONTENT)
async def upload_file(
    session_id: str,
    path: str = Form(default=""),
    file: UploadFile = File(...),
    user_id: CurrentUser = None,  # type: ignore[assignment]
) -> None:
    """Upload a file into the session workspace. path = target directory (relative to cwd)."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not file.filename:
        raise HTTPException(status_code=400, detail="filename required")

    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail=f"file too large (>{MAX_UPLOAD_SIZE // 1024 // 1024}MB)")

    target_dir = _resolve(session.cwd, path) if path else Path(session.cwd).resolve()
    if target_dir.exists() and not target_dir.is_dir():
        raise HTTPException(status_code=400, detail="target path is not a directory")

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / file.filename).write_bytes(content)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _sanitize_attachment_ext(filename: str) -> str:
    """Return the extension (with leading dot) we should store on disk.

    Mirrors the original suffix exactly when it is purely alphanumeric and
    ≤ 16 chars; otherwise we strip out non-alphanumeric chars and truncate.
    An empty/missing suffix returns "" so the stored filename is just the
    uuid hex — `@<path>` still works without an extension.
    """
    raw = Path(filename).suffix
    if not raw:
        return ""
    # Drop the leading dot for sanitization, then re-add.
    body = raw[1:].lower()
    body = _EXT_SANITIZE_RE.sub("", body)
    if not body:
        return ""
    return "." + body[:16]


@router.post("/{session_id}/upload-attachment")
async def upload_attachment(
    session_id: str,
    file: UploadFile = File(...),
    user_id: CurrentUser = None,  # type: ignore[assignment]
) -> dict:
    """Upload any file for AI analysis. Stored under
    <session.cwd>/.claude/uploads/<uuid>.<ext> with the original extension
    preserved; the absolute path is returned so the frontend can inject
    `@<path>` into the next prompt. Image extensions additionally get a
    `url` field for inline thumbnail rendering."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not file.filename:
        raise HTTPException(status_code=400, detail="filename required")

    ext = _sanitize_attachment_ext(file.filename)

    content = await file.read()
    if len(content) > MAX_ATTACHMENT_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"file too large (>{MAX_ATTACHMENT_SIZE // 1024 // 1024}MB)",
        )

    target_dir = Path(session.cwd).resolve() / ATTACHMENT_UPLOAD_SUBDIR
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        stored_name = f"{uuid.uuid4().hex}{ext}"
        target = target_dir / stored_name
        target.write_bytes(content)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    is_image = ext in IMAGE_EXTS
    result = {
        "path": str(target),
        "filename": file.filename,
        "stored_name": stored_name,
        "size": len(content),
        "is_image": is_image,
    }
    if is_image:
        # Browsers can't send Authorization headers on <img src>, so the
        # frontend appends ?token=<jwt> at render time. The path here is
        # session-scoped and validates filename format on the way out.
        result["url"] = f"/api/sessions/{session_id}/uploaded-image/{stored_name}"
    return result


# Legacy image-only endpoint. Kept so older frontend bundles (image-only)
# keep working; new code uses /upload-attachment.
@router.post("/{session_id}/upload-image")
async def upload_image(
    session_id: str,
    file: UploadFile = File(...),
    user_id: CurrentUser = None,  # type: ignore[assignment]
) -> dict:
    return await upload_attachment(session_id, file, user_id)


@router.get("/{session_id}/uploaded-image/{filename}")
def serve_uploaded_image(
    session_id: str,
    filename: str,
    token: str = Query(...),
) -> FileResponse:
    """Serve an uploaded image so the frontend can render it via <img src>.

    Auth is via query token (`<img>` can't send Authorization headers).
    Filename is restricted to the pattern upload_image writes — that's the
    primary path-traversal guard. The store ownership check is the
    secondary layer.
    """
    try:
        claims = _decode_token(token)
    except HTTPException:
        raise HTTPException(status_code=401, detail="unauthorized")
    user_id = claims.get("sub", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="unauthorized")

    if not _IMAGE_FILENAME_RE.match(filename):
        raise HTTPException(status_code=400, detail="invalid filename")

    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")

    target = Path(session.cwd).resolve() / IMAGE_UPLOAD_SUBDIR / filename
    if not target.is_file():
        raise HTTPException(status_code=404, detail="image not found")

    mime, _ = mimetypes.guess_type(str(target))
    return FileResponse(target, media_type=mime or "application/octet-stream")


@router.put("/{session_id}/fs/write")
def write_file(
    session_id: str,
    body: FileWriteRequest,
    user_id: CurrentUser = None,  # type: ignore[assignment]
) -> dict:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not body.path:
        raise HTTPException(status_code=400, detail="path required")

    target = _resolve(session.cwd, body.path)
    if target.is_dir():
        raise HTTPException(status_code=400, detail="path is a directory")
    if len(body.content.encode("utf-8")) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="content too large (>1MB)")

    if body.expected_mtime is not None and not body.force and target.exists():
        try:
            current_mtime = target.stat().st_mtime
        except OSError:
            current_mtime = None
        # 1 ms tolerance — some filesystems round mtime; floating-point equality
        # would false-positive a "conflict" that isn't one.
        if current_mtime is not None and abs(current_mtime - body.expected_mtime) > 0.001:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "file was modified externally since editing began",
                    "current_mtime": current_mtime,
                    "expected_mtime": body.expected_mtime,
                },
            )

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body.content, encoding="utf-8")
        new_mtime = target.stat().st_mtime
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"mtime": new_mtime}


SQLITE_ROW_LIMIT = 500


@router.get("/{session_id}/fs/sqlite")
def query_sqlite(
    session_id: str,
    path: str = Query(..., max_length=500),
    table: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=100, ge=1, le=SQLITE_ROW_LIMIT),
    offset: int = Query(default=0, ge=0),
    user_id: CurrentUser = None,  # type: ignore[assignment]
) -> SqliteResponse:
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")

    target = _resolve(session.cwd, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    if target.suffix.lower() not in SQLITE_EXTENSIONS:
        raise HTTPException(status_code=415, detail="not a sqlite file")

    rel = str(target.relative_to(Path(session.cwd).resolve()))

    try:
        uri = target.as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = None
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"cannot open sqlite: {exc}") from exc

    try:
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [r[0] for r in cur.fetchall()]

        if table is None:
            return SqliteResponse(tables=tables, path=rel)

        # Validate table name against known tables to prevent injection
        if table not in tables:
            raise HTTPException(status_code=404, detail="table not found")

        quoted = f'"{table}"'
        cur.execute(f"SELECT COUNT(*) FROM {quoted}")
        total = cur.fetchone()[0]

        cur.execute(f"SELECT * FROM {quoted} LIMIT ? OFFSET ?", (limit, offset))
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = [list(r) for r in cur.fetchall()]

        return SqliteResponse(tables=tables, columns=columns, rows=rows, total=total, path=rel)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        con.close()


@router.post("/{session_id}/fs/sqlite/exec")
def exec_sqlite(
    session_id: str,
    body: SqliteExecRequest,
    user_id: CurrentUser = None,  # type: ignore[assignment]
) -> SqliteExecResponse:
    """Execute arbitrary SQL against a SQLite file (read-write)."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")

    sql = body.sql.strip()
    if not sql:
        raise HTTPException(status_code=400, detail="sql required")

    target = _resolve(session.cwd, body.path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    if target.suffix.lower() not in SQLITE_EXTENSIONS:
        raise HTTPException(status_code=415, detail="not a sqlite file")

    try:
        con = sqlite3.connect(str(target))
        con.row_factory = None
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"cannot open sqlite: {exc}") from exc

    try:
        cur = con.cursor()
        cur.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = [list(r) for r in cur.fetchall()] if cur.description else []
        affected = cur.rowcount if cur.rowcount >= 0 else 0
        con.commit()
        if columns:
            message = f"{len(rows)} row(s) returned"
        else:
            message = f"{affected} row(s) affected"
        return SqliteExecResponse(columns=columns, rows=rows, affected=affected, message=message)
    except Exception as exc:
        con.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        con.close()


class MoveRequest(BaseModel):
    path: str           # relative path of the file/dir to move
    dest_dir: str       # relative path of the destination directory ("" = root)


@router.post("/{session_id}/fs/move", status_code=status.HTTP_204_NO_CONTENT)
def move_entry(
    session_id: str,
    body: MoveRequest,
    user_id: CurrentUser = None,  # type: ignore[assignment]
) -> None:
    """Move a file or directory into a different directory."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not body.path:
        raise HTTPException(status_code=400, detail="path required")

    base = Path(session.cwd).resolve()
    src = _resolve(session.cwd, body.path)
    if not src.exists():
        raise HTTPException(status_code=404, detail="source not found")

    # dest_dir="" means cwd root
    dst_dir = _resolve(session.cwd, body.dest_dir) if body.dest_dir else base
    if not dst_dir.is_dir():
        raise HTTPException(status_code=404, detail="destination directory not found")

    dst = dst_dir / src.name
    # Safety: both src and dst must be inside cwd
    try:
        src.resolve().relative_to(base)
        dst.resolve().relative_to(base)
    except ValueError:
        raise HTTPException(status_code=403, detail="path traversal not allowed")

    if dst == src:
        return  # no-op: same location
    if dst.exists():
        raise HTTPException(status_code=409, detail=f"'{src.name}' already exists in destination")
    # Prevent moving a directory into itself or a descendant
    if src.is_dir():
        try:
            dst.relative_to(src.resolve())
            raise HTTPException(status_code=400, detail="cannot move a directory into itself")
        except ValueError:
            pass
    try:
        shutil.move(str(src), str(dst))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class RenameRequest(BaseModel):
    path: str       # relative path of the file/dir to rename
    new_name: str   # new name only (not full path); rename in the same parent dir


@router.post("/{session_id}/fs/rename", status_code=status.HTTP_204_NO_CONTENT)
def rename_entry(
    session_id: str,
    body: RenameRequest,
    user_id: CurrentUser = None,  # type: ignore[assignment]
) -> None:
    """Rename a file or directory to a new name within the same parent directory."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not body.path or not body.new_name:
        raise HTTPException(status_code=400, detail="path and new_name required")
    if "/" in body.new_name or "\\" in body.new_name or body.new_name in (".", ".."):
        raise HTTPException(status_code=400, detail="new_name must be a plain name, not a path")

    src = _resolve(session.cwd, body.path)
    if not src.exists():
        raise HTTPException(status_code=404, detail="source not found")
    dst = src.parent / body.new_name
    if dst.exists():
        raise HTTPException(status_code=409, detail="destination already exists")
    # Ensure dst is still inside cwd
    base = Path(session.cwd).resolve()
    try:
        dst.resolve().relative_to(base)
    except ValueError:
        raise HTTPException(status_code=403, detail="path traversal not allowed")
    try:
        src.rename(dst)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class DeleteRequest(BaseModel):
    path: str               # relative path of the file or directory to delete
    recursive: bool = False  # if true, allow rmtree on non-empty directories


@router.post("/{session_id}/fs/delete", status_code=status.HTTP_204_NO_CONTENT)
def delete_entry(
    session_id: str,
    body: DeleteRequest,
    user_id: CurrentUser = None,  # type: ignore[assignment]
) -> None:
    """Delete a file or directory inside the session workspace.

    Safety checks:
      - path must resolve inside cwd (no traversal)
      - cannot delete the cwd root
      - non-empty directory requires recursive=true
    """
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not body.path:
        raise HTTPException(status_code=400, detail="path required")

    base = Path(session.cwd).resolve()
    target = _resolve(session.cwd, body.path)
    if not target.exists() and not target.is_symlink():
        raise HTTPException(status_code=404, detail="path not found")

    # Refuse to delete the workspace root itself.
    if target.resolve() == base:
        raise HTTPException(status_code=403, detail="cannot delete workspace root")

    if target.is_symlink() or target.is_file():
        try:
            target.unlink()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return

    if target.is_dir():
        try:
            has_entries = any(target.iterdir())
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if has_entries and not body.recursive:
            raise HTTPException(status_code=409, detail="directory not empty")
        try:
            if has_entries:
                shutil.rmtree(target)
            else:
                target.rmdir()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return

    raise HTTPException(status_code=400, detail="unsupported entry type")


class ArchiveListResponse(BaseModel):
    entries: list[str]
    total: int


@router.get("/{session_id}/fs/archive-list")
def list_archive_contents(
    session_id: str,
    path: str = Query(..., max_length=500),
    user_id: CurrentUser = None,  # type: ignore[assignment]
) -> ArchiveListResponse:
    """List files inside an archive without extracting it."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")

    target = _resolve(session.cwd, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    if not _is_archive_name(target.name):
        raise HTTPException(status_code=415, detail="not a supported archive format")
    if target.stat().st_size > ARCHIVE_MAX_SIZE:
        raise HTTPException(status_code=413, detail=f"archive too large (>{ARCHIVE_MAX_SIZE // 1024 // 1024}MB)")

    entries = _list_archive_entries(target)
    return ArchiveListResponse(entries=entries, total=len(entries))


class ExtractRequest(BaseModel):
    path: str   # relative path of the archive file


class ExtractResponse(BaseModel):
    output_dir: str    # relative path of the extracted directory


@router.post("/{session_id}/fs/extract")
def extract_archive_endpoint(
    session_id: str,
    body: ExtractRequest,
    user_id: CurrentUser = None,  # type: ignore[assignment]
) -> ExtractResponse:
    """Extract an archive file. Output directory: {stem}-{timestamp} next to the archive."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    if not body.path:
        raise HTTPException(status_code=400, detail="path required")

    target = _resolve(session.cwd, body.path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    if not _is_archive_name(target.name):
        raise HTTPException(status_code=415, detail="not a supported archive format")

    size = target.stat().st_size
    if size > ARCHIVE_MAX_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"archive too large ({size // 1024 // 1024}MB > {ARCHIVE_MAX_SIZE // 1024 // 1024}MB limit)"
        )

    # Build stem: strip all known archive suffixes
    stem = target.name
    for sfx in (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar", ".gz", ".bz2", ".xz", ".zip"):
        if stem.lower().endswith(sfx):
            stem = stem[: len(stem) - len(sfx)]
            break

    ts = int(time.time())
    dest_name = f"{stem}-{ts}"
    dest = target.parent / dest_name

    # Avoid name collision
    counter = 0
    while dest.exists():
        counter += 1
        dest = target.parent / f"{dest_name}-{counter}"
        dest_name = dest.name

    try:
        _extract_archive(target, dest)
    except Exception as exc:
        # Clean up partially created dir on failure
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    base = Path(session.cwd).resolve()
    rel = str(dest.relative_to(base))
    return ExtractResponse(output_dir=rel)


_DOWNLOAD_MAX_BYTES = 100 * 1024 * 1024  # 100 MB hard limit


def _dir_size(path: Path) -> int:
    """Recursively sum file sizes under path."""
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


class DirInfoItem(BaseModel):
    name: str
    path: str    # relative to session cwd
    type: str    # "file" | "dir"
    size: int    # bytes (for dirs: recursive sum)


class DirInfoResponse(BaseModel):
    total_size: int
    items: list[DirInfoItem]


@router.get("/{session_id}/fs/dir-info")
def get_dir_info(
    session_id: str,
    path: str = Query(default=""),
    user_id: CurrentUser = None,  # type: ignore[assignment]
) -> DirInfoResponse:
    """Return total size and top-level items with sizes for a directory."""
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")

    base = Path(session.cwd).resolve()
    target = _resolve(session.cwd, path) if path else base

    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail="directory not found")

    items: list[DirInfoItem] = []
    for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        try:
            rel = str(child.relative_to(base))
            if child.is_dir():
                sz = _dir_size(child)
                items.append(DirInfoItem(name=child.name, path=rel, type="dir", size=sz))
            elif child.is_file():
                sz = child.stat().st_size
                items.append(DirInfoItem(name=child.name, path=rel, type="file", size=sz))
        except OSError:
            pass

    total_size = sum(i.size for i in items)
    return DirInfoResponse(total_size=total_size, items=items)


def _zip_dir(src: Path, zip_name: str, compress: bool = True) -> io.BytesIO:
    """Zip directory src into an in-memory BytesIO buffer."""
    buf = io.BytesIO()
    compression = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    kwargs = {"compresslevel": 6} if compress else {}
    with zipfile.ZipFile(buf, "w", compression, **kwargs) as zf:
        for f in src.rglob("*"):
            if f.is_file():
                try:
                    zf.write(f, f.relative_to(src))
                except OSError:
                    pass
    buf.seek(0)
    return buf


class DownloadZipRequest(BaseModel):
    path: str = ""           # relative path of dir/file ("" = cwd root)
    exclude: list[str] = []  # paths to exclude (rsync --exclude)
    compress: bool = True    # False = ZIP_STORED (no compression, faster for small dirs)


@router.post("/{session_id}/fs/download-zip")
def download_zip_post(
    session_id: str,
    body: DownloadZipRequest,
    user_id: CurrentUser = None,  # type: ignore[assignment]
) -> StreamingResponse:
    """Zip a directory with optional exclusions (uses rsync to preserve attributes).

    Flow:
      1. rsync src → tmp-{uuid}/{dirname}/ (with --exclude patterns)
      2. Zip tmp-{uuid}/{dirname}/
      3. Stream zip, cleanup tmp dir
    """
    store = _get_store()
    session = store.get(session_id)
    if session is None or session.owner_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")

    base = Path(session.cwd).resolve()
    target = _resolve(session.cwd, body.path) if body.path else base

    if not target.exists():
        raise HTTPException(status_code=404, detail="path not found")

    # Single file: direct zip, no rsync needed
    if target.is_file():
        sz = target.stat().st_size
        if sz > _DOWNLOAD_MAX_BYTES:
            raise HTTPException(status_code=413, detail=f"file too large ({sz // 1024 // 1024}MB > 100MB)")
        buf = io.BytesIO()
        compression = zipfile.ZIP_DEFLATED if body.compress else zipfile.ZIP_STORED
        zf_kwargs = {"compresslevel": 6} if body.compress else {}
        with zipfile.ZipFile(buf, "w", compression, **zf_kwargs) as zf:
            try:
                zf.write(target, target.name)
            except OSError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{target.name}.zip"'},
        )

    # Directory: rsync to temp, zip, cleanup
    dirname = target.name or "workspace"
    tmp_root = Path(tempfile.gettempdir()) / f"tmp-{uuid.uuid4().hex}"
    tmp_dst = tmp_root / dirname
    tmp_root.mkdir(parents=True, exist_ok=True)

    try:
        # Build rsync command
        # Use -a (archive: preserves permissions, timestamps, symlinks, etc.)
        # Sanitize exclude paths: strip leading slash, reject traversal
        safe_excludes: list[str] = []
        for excl in body.exclude:
            parts = [p for p in excl.replace("\\", "/").split("/") if p and p != "."]
            if parts and ".." not in parts:
                safe_excludes.append("/".join(parts))

        cmd = ["rsync", "-a"]
        for excl in safe_excludes:
            cmd += [f"--exclude={excl}"]
        cmd += [str(target) + "/", str(tmp_dst) + "/"]

        try:
            result = subprocess.run(cmd, capture_output=True, timeout=120)
            # rsync exit code 24 = "some files vanished before transfer" — treat as OK
            if result.returncode not in (0, 24):
                raise RuntimeError(result.stderr.decode())
        except (FileNotFoundError, RuntimeError):
            # rsync not available or failed — fallback to shutil with deep-path support
            excl_set = set(safe_excludes)
            # Remove any partial rsync output before shutil copy
            if tmp_dst.exists():
                shutil.rmtree(tmp_dst, ignore_errors=True)

            def _ignore(directory: str, contents: list[str]) -> list[str]:
                rel_dir = os.path.relpath(directory, str(target))
                rel_dir = "" if rel_dir == "." else rel_dir
                ignored: list[str] = []
                for name in contents:
                    item_rel = f"{rel_dir}/{name}".lstrip("/") if rel_dir else name
                    if item_rel in excl_set:
                        ignored.append(name)
                return ignored

            shutil.copytree(str(target), str(tmp_dst), ignore=_ignore, symlinks=True)

        # Check size of the temp copy
        total_size = _dir_size(tmp_dst)
        if total_size > _DOWNLOAD_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"directory still too large after exclusions ({total_size // 1024 // 1024}MB > 100MB)"
            )

        buf = _zip_dir(tmp_dst, dirname, compress=body.compress)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{dirname}.zip"'},
    )


# ── File-system watch WebSocket ───────────────────────────────────────────────

_WATCH_BATCH_MS = 150   # collect changes for this long before sending
_CHANGE_TYPE = {Change.added: "add", Change.modified: "modify", Change.deleted: "delete"}


@router.websocket("/{session_id}/fs/watch")
async def fs_watch_ws(
    websocket: WebSocket,
    session_id: str,
    token: str = Query(...),
):
    # Auth: validate JWT token passed as query param
    try:
        claims = _decode_token(token)
        if not claims.get("sub"):
            await websocket.close(code=4001, reason="unauthorized")
            return
    except Exception:
        await websocket.close(code=4001, reason="unauthorized")
        return

    store = _get_store()
    session = store.get(session_id)
    if session is None:
        await websocket.close(code=4004, reason="session not found")
        return

    cwd = Path(session.cwd).resolve()
    if not cwd.is_dir():
        await websocket.close(code=4005, reason="session cwd not found")
        return

    await websocket.accept()

    pending: list[dict] = []
    flush_task: asyncio.Task | None = None

    async def flush():
        nonlocal pending, flush_task
        await asyncio.sleep(_WATCH_BATCH_MS / 1000)
        if pending:
            try:
                await websocket.send_text(json.dumps({"changes": pending}))
            except Exception:
                pass
        pending = []
        flush_task = None

    try:
        # step=500: poll the rust receiver every 500 ms instead of the default
        # 50 ms — cuts asyncio wake-ups 10× across N sessions × 3 frontend
        # hooks. inotify itself is push-based, so this only adds at most ~450
        # ms of detection latency.
        # debounce=2000: wait 2 s after the last event before flushing the
        # batch (default 1600 ms). A single "save" often triggers several
        # inotify events (modify, attrib, rename) — letting them coalesce
        # avoids fan-out into multiple WS messages.
        async for raw_changes in awatch(
            str(cwd),
            stop_event=asyncio.Event(),
            step=500,
            debounce=2000,
        ):
            for change, path_str in raw_changes:
                p = Path(path_str)
                try:
                    rel = p.relative_to(cwd)
                except ValueError:
                    continue
                parts = rel.parts
                # Skip ignored directory subtrees
                if any(part in SKIP_DIRS for part in parts):
                    continue
                parent = str(rel.parent) if len(parts) > 1 else ""
                pending.append({
                    "type": _CHANGE_TYPE.get(change, "modify"),
                    "path": str(rel),
                    "dir": parent,
                    "is_dir": p.is_dir(),
                })
            if pending and flush_task is None:
                flush_task = asyncio.create_task(flush())
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if flush_task:
            flush_task.cancel()
