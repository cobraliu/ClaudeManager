from __future__ import annotations

import gzip
import bz2
import lzma
import os
import shutil
import tarfile
import time
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, Form, Query, UploadFile
from pydantic import BaseModel

from app.security import CurrentUser

router = APIRouter(prefix="/api/fs", tags=["fs"])


@router.get("/dirs")
def list_dirs(
    path: str = Query(..., max_length=500),
    _user_id: CurrentUser = None,  # type: ignore[assignment]
) -> list[str]:
    """Return immediate subdirectories under the given path prefix.

    If `path` ends with '/', list all subdirs inside that directory.
    Otherwise, split into parent + partial name and return matches.
    """
    path = path.strip()
    if not path:
        return []

    # Resolve to absolute, reject relative traversal
    try:
        abs_path = str(Path(path).resolve())
    except Exception:
        return []

    if path.endswith("/"):
        parent = abs_path
        prefix = ""
    else:
        parent = str(Path(abs_path).parent)
        prefix = Path(abs_path).name.lower()

    try:
        parent_path = Path(parent)
        if not parent_path.is_dir():
            return []
        entries = sorted(
            str(entry)
            for entry in parent_path.iterdir()
            if entry.is_dir()
            and not entry.name.startswith(".")
            and entry.name.lower().startswith(prefix)
        )
        return entries[:30]
    except PermissionError:
        return []


INIT_ARCHIVE_MAX_SIZE = 100 * 1024 * 1024  # 100 MB

_ARCHIVE_SUFFIXES = (
    ".zip", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz",
    ".tar", ".gz", ".bz2", ".xz",
)


def _zip_strip_prefix(names: list[str]) -> str:
    """Return the common single top-level directory prefix to strip, or ''."""
    if not names:
        return ""
    tops = {n.split("/")[0] for n in names if n}
    if len(tops) != 1:
        return ""
    top = tops.pop()
    prefix = top + "/"
    # Every entry must either be the dir itself or start with prefix
    if all(n == top or n.startswith(prefix) for n in names):
        return prefix
    return ""


def _tar_strip_prefix(members: list[tarfile.TarInfo]) -> str:
    """Return the common single top-level directory prefix to strip, or ''."""
    names = [m.name for m in members if m.name]
    if not names:
        return ""
    tops = {n.split("/")[0] for n in names}
    if len(tops) != 1:
        return ""
    top = tops.pop()
    prefix = top + "/"
    if all(n == top or n.startswith(prefix) for n in names):
        return prefix
    return ""


def _safe_extract(data: bytes, filename: str, dest: Path) -> None:
    """Extract archive bytes directly into dest directory.

    If the archive has a single top-level directory, its contents are placed
    directly into dest (the top-level dir is stripped).
    """
    dest.mkdir(parents=True, exist_ok=True)
    n = filename.lower()
    import io
    buf = io.BytesIO(data)
    if n.endswith(".zip"):
        with zipfile.ZipFile(buf, "r") as zf:
            names = zf.namelist()
            prefix = _zip_strip_prefix(names)
            for member in zf.infolist():
                mname = member.filename
                if prefix:
                    if mname == prefix.rstrip("/") or mname == prefix:
                        continue  # skip the top dir entry itself
                    if not mname.startswith(prefix):
                        continue
                    mname = mname[len(prefix):]
                if not mname:
                    continue
                target = dest / mname
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(member.filename))
    elif any(n.endswith(s) for s in (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar")):
        with tarfile.open(fileobj=buf, mode="r:*") as tf:
            members = [m for m in tf.getmembers()
                       if not os.path.isabs(m.name) and ".." not in m.name]
            prefix = _tar_strip_prefix(members)
            if not prefix:
                tf.extractall(dest, members=members)
            else:
                for m in members:
                    if m.name == prefix.rstrip("/") or not m.name.startswith(prefix):
                        continue
                    rel = m.name[len(prefix):]
                    if not rel:
                        continue
                    target = dest / rel
                    if m.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                    elif m.isfile():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with tf.extractfile(m) as src_f:  # type: ignore[arg-type]
                            target.write_bytes(src_f.read())
                    elif m.issym():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        os.symlink(m.linkname, target)
    elif n.endswith(".gz"):
        out = dest / filename[:-3]
        with gzip.open(buf) as gf:
            out.write_bytes(gf.read())
    elif n.endswith(".bz2"):
        out = dest / filename[:-4]
        with bz2.open(buf) as bf:
            out.write_bytes(bf.read())
    elif n.endswith(".xz"):
        out = dest / filename[:-3]
        with lzma.open(buf) as xf:
            out.write_bytes(xf.read())
    else:
        raise ValueError(f"unsupported archive: {filename}")


class ExtractToResponse(BaseModel):
    path: str   # the absolute path that was created


@router.post("/extract-to")
async def extract_to_dir(
    target_dir: str = Form(...),
    file: UploadFile = File(...),
    _user_id: CurrentUser = None,  # type: ignore[assignment]
) -> ExtractToResponse:
    """Upload an archive and extract it into target_dir (must not already exist).
    Used during session creation to bootstrap a working directory from an archive."""
    if not file.filename:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="filename required")

    fname = file.filename.lower()
    if not any(fname.endswith(s) for s in _ARCHIVE_SUFFIXES):
        from fastapi import HTTPException
        raise HTTPException(status_code=415, detail="unsupported archive format")

    content = await file.read()
    if len(content) > INIT_ARCHIVE_MAX_SIZE:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=413,
            detail=f"archive too large ({len(content) // 1024 // 1024}MB > {INIT_ARCHIVE_MAX_SIZE // 1024 // 1024}MB limit)"
        )

    target = Path(target_dir.strip())
    if target.exists():
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail="target directory already exists")

    try:
        _safe_extract(content, file.filename, target)
    except Exception as exc:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return ExtractToResponse(path=str(target))
