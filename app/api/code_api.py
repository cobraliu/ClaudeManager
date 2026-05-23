from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from app.config import (
    FILE_VIEWER_MODE_BYTES,
    FILE_VIEWER_MODE_LINES,
    get_file_viewer_max_bytes,
    get_file_viewer_max_lines,
    get_file_viewer_mode,
)
from app.security import CurrentUser
from app.services.session_store import SessionStore

router = APIRouter(prefix="/api/sessions", tags=["code"])

_store: SessionStore | None = None


def configure(store: SessionStore) -> None:
    global _store
    _store = store


def _get_store() -> SessionStore:
    assert _store is not None
    return _store


_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "env",
    "dist", "build", ".next", ".nuxt", ".cache", "coverage", ".tox",
    ".mypy_cache", ".pytest_cache", "target", "out", ".idea", ".vscode",
}
_SKIP_EXT = {".pyc", ".pyo", ".class", ".o", ".a", ".so", ".dll", ".exe", ".bin", ".wasm"}

_EXT_LANG: dict[str, str] = {
    ".ts": "typescript", ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript",
    ".py": "python", ".css": "css", ".scss": "scss", ".less": "css",
    ".html": "xml", ".htm": "xml", ".xml": "xml",
    ".json": "json", ".jsonc": "json",
    ".md": "markdown", ".mdx": "markdown",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".ini": "ini", ".cfg": "ini",
    ".rs": "rust", ".go": "go", ".java": "java",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".c": "c", ".h": "c", ".hpp": "cpp",
    ".sql": "sql", ".graphql": "graphql", ".gql": "graphql",
    ".env": "bash", ".txt": "plaintext",
}


def _safe_path(cwd: str, rel: str) -> Path:
    root = Path(cwd).resolve()
    target = (root / rel).resolve()
    if not str(target).startswith(str(root) + "/") and target != root:
        raise HTTPException(status_code=400, detail="Invalid path")
    return target


def _git(cwd: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True)
    return r.stdout


def _parse_diff_lines(diff: str) -> tuple[set[int], set[int]]:
    """Return (added_lines, removed_lines) as 1-based line numbers in the new file."""
    added: set[int] = set()
    removed: set[int] = set()
    new_line = 0
    old_line = 0

    for raw in diff.splitlines():
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)(?:,\d+)?", raw)
            if m:
                new_line = int(m.group(1)) - 1
            m2 = re.search(r"-(\d+)(?:,\d+)?", raw)
            if m2:
                old_line = int(m2.group(1)) - 1
        elif raw.startswith("+") and not raw.startswith("+++"):
            new_line += 1
            added.add(new_line)
        elif raw.startswith("-") and not raw.startswith("---"):
            old_line += 1
            removed.add(old_line)
        elif not raw.startswith("\\"):
            new_line += 1
            old_line += 1

    return added, removed


def _build_tree(path: Path, root: Path, depth: int, max_depth: int) -> dict:
    rel = str(path.relative_to(root))
    if path == root:
        rel = "."
    if path.is_dir():
        node: dict = {"name": path.name, "path": rel, "type": "dir", "children": []}
        if depth < max_depth:
            try:
                entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
                for child in entries:
                    if child.name in _SKIP_DIRS:
                        continue
                    if child.is_file() and child.suffix in _SKIP_EXT:
                        continue
                    node["children"].append(_build_tree(child, root, depth + 1, max_depth))
            except PermissionError:
                pass
        else:
            # At depth limit — check if directory actually has loadable content.
            # Return children=None to signal "has content, not yet loaded".
            try:
                has_content = any(
                    c for c in path.iterdir()
                    if c.name not in _SKIP_DIRS
                    and (c.is_dir() or c.suffix not in _SKIP_EXT)
                )
                if has_content:
                    node["children"] = None
            except PermissionError:
                pass
        return node
    else:
        return {"name": path.name, "path": rel, "type": "file"}


# ── Endpoints ──────────────────────────────────────────────────────────────

# Limits for the cheap probe of collapsed untracked dirs in changed-files.
# An untracked directory whose subtree exceeds EITHER threshold is treated as
# "too large to expand" — we surface a warning + suggested .gitignore line
# instead of feeding it through `-uall` (which would unfold thousands of
# entries and trigger a per-file read loop below).
_UNTRACKED_DIR_MAX_FILES = 500
_UNTRACKED_DIR_MAX_BYTES = 50 * 1024 * 1024     # 50 MB

# Per-file cap for the untracked line-count helper. Files above this size get
# no line count reported (frontend shows "—"); this protects against a single
# huge binary (e.g. git pack files in a bare repo) blowing up RSS via
# full-file reads.
_UNTRACKED_LINECOUNT_MAX_BYTES = 1 * 1024 * 1024  # 1 MB


def _is_bare_git_repo(p: Path) -> bool:
    """A bare git repo looks like `HEAD` + `objects/` + `refs/` at top level."""
    try:
        return (p / "HEAD").is_file() and (p / "objects").is_dir() and (p / "refs").is_dir()
    except OSError:
        return False


def _probe_untracked_dir(root: Path, rel: str) -> dict:
    """Walk `rel` under `root` with early stop.

    Returns a dict {file_count, total_bytes, exceeded, is_bare_repo, truncated}.
    `exceeded` is True as soon as either limit is hit; we stop walking then.
    """
    target = root / rel
    is_bare = _is_bare_git_repo(target)
    file_count = 0
    total_bytes = 0
    exceeded = False
    stack: list[Path] = [target]
    while stack and not exceeded:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for ent in it:
                    try:
                        if ent.is_dir(follow_symlinks=False):
                            stack.append(Path(ent.path))
                        elif ent.is_file(follow_symlinks=False):
                            file_count += 1
                            try:
                                total_bytes += ent.stat(follow_symlinks=False).st_size
                            except OSError:
                                pass
                            if file_count >= _UNTRACKED_DIR_MAX_FILES or total_bytes >= _UNTRACKED_DIR_MAX_BYTES:
                                exceeded = True
                                break
                    except OSError:
                        continue
        except OSError:
            continue
    return {
        "file_count": file_count,
        "total_bytes": total_bytes,
        "exceeded": exceeded,
        "is_bare_repo": is_bare,
    }


def _count_lines_bounded(fp: Path) -> int | None:
    """Stream-count newlines in `fp`. Returns None if the file is over the
    size cap (we don't even open it)."""
    try:
        if fp.stat().st_size > _UNTRACKED_LINECOUNT_MAX_BYTES:
            return None
    except OSError:
        return None
    try:
        n = 0
        with fp.open("rb") as f:
            for _ in f:
                n += 1
        return n
    except OSError:
        return None


@router.get("/{session_id}/code/changed-files")
def get_changed_files(session_id: str, _user: CurrentUser) -> dict:
    """Return files changed relative to HEAD (or untracked).

    Response shape:
        {
          "files":    [{path, status, added?, removed?}, ...],
          "warnings": [{kind, path, file_count?, approx_size_bytes?,
                        is_bare_repo?, suggested_ignore}, ...],
        }

    Untracked DIRECTORIES are not expanded blindly. We first run
    `git status --porcelain` (no `-uall`, so untracked dirs stay collapsed as
    `?? path/`). For each collapsed dir we do a cheap early-stopping size
    probe; if it exceeds the file-count or byte threshold we emit a warning
    with a suggested `.gitignore` line and skip expansion. Small dirs get
    re-queried with `-uall -- path` so their individual files still appear.
    """
    store = _get_store()
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404)

    cwd = session.cwd
    cwd_path = Path(cwd)

    # Cheap probe: -unormal collapses untracked dirs to a single `?? dir/` line.
    out = _git(cwd, "status", "--porcelain")
    if not out:
        return {"files": [], "warnings": []}

    # Collect diff stats (tracked modified/added/deleted files only)
    numstat: dict[str, tuple[int, int]] = {}
    for ns_args in (
        ["diff", "--numstat", "HEAD"],
        ["diff", "--numstat", "--cached", "HEAD"],
    ):
        ns_out = _git(cwd, *ns_args) or ""
        for line in ns_out.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                try:
                    a = int(parts[0]) if parts[0] != "-" else 0
                    r = int(parts[1]) if parts[1] != "-" else 0
                    p = parts[2]
                    prev = numstat.get(p, (0, 0))
                    numstat[p] = (prev[0] + a, prev[1] + r)
                except ValueError:
                    pass

    status_map = {
        "M": "modified", "A": "added", "D": "deleted",
        "R": "renamed", "C": "copied", "U": "conflict",
        "?": "untracked",
    }

    files: list[dict] = []
    warnings: list[dict] = []
    # Defer expansion of safe untracked dirs to a second pass so we can issue
    # one `git status` per dir instead of one per file.
    safe_dirs_to_expand: list[str] = []

    for line in out.splitlines():
        if len(line) < 4:
            continue
        xy = line[:2]
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ")[-1]
        path = path.strip().strip('"')

        status_char = xy.strip()[0] if xy.strip() else "?"
        status = status_map.get(status_char, "modified")

        # Collapsed untracked directory entry: `?? codes/`
        if status == "untracked" and path.endswith("/"):
            rel = path.rstrip("/")
            probe = _probe_untracked_dir(cwd_path, rel)
            if probe["exceeded"] or probe["is_bare_repo"]:
                kind = "bare_git_repo" if probe["is_bare_repo"] else "large_untracked_dir"
                warnings.append({
                    "kind": kind,
                    "path": rel,
                    "file_count": probe["file_count"],
                    "approx_size_bytes": probe["total_bytes"],
                    "is_bare_repo": probe["is_bare_repo"],
                    "suggested_ignore": rel + "/",
                })
                # Surface a single placeholder entry so the user sees that the
                # dir has changes, even though we won't enumerate them.
                files.append({
                    "path": path,
                    "status": "untracked",
                    "is_skipped_dir": True,
                })
                continue
            safe_dirs_to_expand.append(rel)
            continue

        entry: dict = {"path": path, "status": status}
        if path in numstat:
            a, r = numstat[path]
            entry["added"] = a
            entry["removed"] = r
        elif status == "untracked":
            try:
                fp = cwd_path / path
                if fp.is_file():
                    lc = _count_lines_bounded(fp)
                    if lc is not None:
                        entry["added"] = lc
                        entry["removed"] = 0
            except OSError:
                pass
        files.append(entry)

    # Second pass: expand safe untracked dirs one at a time. Each call is
    # scoped via `-- path`, so the cost is bounded by that dir's contents
    # (already probed under the threshold).
    for rel in safe_dirs_to_expand:
        sub = _git(cwd, "status", "--porcelain", "--untracked-files=all", "--", rel) or ""
        for line in sub.splitlines():
            if len(line) < 4 or not line.startswith("?? "):
                continue
            p = line[3:].strip().strip('"')
            if p.endswith("/"):
                # Shouldn't happen under -uall, but skip defensively.
                continue
            entry: dict = {"path": p, "status": "untracked"}
            try:
                fp = cwd_path / p
                if fp.is_file():
                    lc = _count_lines_bounded(fp)
                    if lc is not None:
                        entry["added"] = lc
                        entry["removed"] = 0
            except OSError:
                pass
            files.append(entry)

    return {"files": files, "warnings": warnings}


@router.get("/{session_id}/code/file")
def get_file(
    session_id: str,
    path: str = Query(...),
    meta_only: bool = Query(default=False),
    _user: CurrentUser = None,
) -> dict:
    """Return file content, detected language, and diff line info.

    meta_only=true skips the content read entirely and just returns size+mtime.
    Used by the FileViewer header for sqlite/archive/pdf/image where content
    is rendered by a specialized viewer but size still needs to be displayed.
    """
    store = _get_store()
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404)

    cwd = session.cwd
    target = _safe_path(cwd, path)

    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="Not a file")

    if meta_only:
        try:
            st = target.stat()
        except OSError as e:
            raise HTTPException(status_code=500, detail=str(e))
        return {
            "path": path,
            "is_binary": True,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "content": "",
            "language": "binary",
            "added_lines": [],
            "removed_lines": [],
            "truncated": False,
        }

    try:
        st = target.stat()
        raw_head = target.read_bytes()[:8192]
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Detect binary by null bytes in the first 8 KiB
    if b"\x00" in raw_head:
        return {
            "path": path,
            "is_binary": True,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "content": "",
            "language": "binary",
            "added_lines": [],
            "removed_lines": [],
            "truncated": False,
        }

    try:
        content = target.read_text(errors="replace")
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Admin-configurable truncation: limit by lines, by bytes, or unlimited.
    # We always compute total_lines from the FULL file so the header can show
    # "showing N of M".
    mode = get_file_viewer_mode()
    all_lines = content.splitlines()
    total_lines = len(all_lines)
    truncated = False
    truncated_by: str | None = None
    if mode == FILE_VIEWER_MODE_LINES:
        max_lines = get_file_viewer_max_lines()
        if total_lines > max_lines:
            truncated = True
            truncated_by = "lines"
            all_lines = all_lines[:max_lines]
            content = "\n".join(all_lines)
    elif mode == FILE_VIEWER_MODE_BYTES:
        max_bytes = get_file_viewer_max_bytes()
        encoded = content.encode("utf-8", errors="replace")
        if len(encoded) > max_bytes:
            truncated = True
            truncated_by = "bytes"
            content = encoded[:max_bytes].decode("utf-8", errors="replace")
            all_lines = content.splitlines()
    # `lines` is the slice we actually render. The untracked-file branch below
    # references it unconditionally, so we must assign in both cases.
    lines = all_lines
    displayed_lines = len(lines)

    language = _EXT_LANG.get(target.suffix.lower(), "plaintext")

    # Get diff relative to HEAD (falls back to empty if not a git repo)
    diff_out = _git(cwd, "diff", "HEAD", "--", path)
    if not diff_out:
        # Try against index (newly staged files)
        diff_out = _git(cwd, "diff", "--cached", "--", path)

    if not diff_out:
        # Check if file is untracked (new) — ls-files prints the path if tracked, empty if not
        tracked = _git(cwd, "ls-files", "--error-unmatch", path)
        if not tracked.strip() and lines:
            n = len(lines)
            diff_out = (
                f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{n} @@\n"
                + "\n".join(f"+{line}" for line in lines)
            )

    added, removed = _parse_diff_lines(diff_out)

    return {
        "path": path,
        "content": content,
        "language": language,
        "added_lines": sorted(added),
        "removed_lines": sorted(removed),
        "truncated": truncated,
        "truncated_by": truncated_by,
        "displayed_lines": displayed_lines,
        "diff_raw": diff_out,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "total_lines": total_lines,
    }


@router.get("/{session_id}/code/tree")
def get_tree(
    session_id: str,
    depth: int = Query(default=2, ge=1, le=6),
    path: str = Query(default=""),
    _user: CurrentUser = None,
) -> dict:
    """Return directory tree. Use `path` to lazy-load a subdirectory."""
    store = _get_store()
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404)

    root = Path(session.cwd).resolve()
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="cwd not found")

    if path and path != ".":
        start = _safe_path(session.cwd, path)
        if not start.is_dir():
            raise HTTPException(status_code=404, detail="path not found")
    else:
        start = root

    return _build_tree(start, root, 0, depth)


@router.get("/{session_id}/code/dirs")
def list_subdirs(
    session_id: str,
    path: str = Query(default=""),
    _user: CurrentUser = None,
) -> dict:
    """Return immediate subdirectory names of `path` (relative to session cwd).

    Used by the FileViewer Save-As picker for the cascading directory chooser:
    each chosen segment triggers another call to load the next level's options.
    """
    store = _get_store()
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404)

    root = Path(session.cwd).resolve()
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="cwd not found")

    if path and path != ".":
        target = _safe_path(session.cwd, path)
        if not target.is_dir():
            raise HTTPException(status_code=404, detail="path not found")
    else:
        target = root

    dirs: list[str] = []
    try:
        for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue
            if child.name in _SKIP_DIRS:
                continue
            dirs.append(child.name)
    except PermissionError:
        pass
    return {"path": path or ".", "dirs": dirs}


@router.get("/{session_id}/code/exists")
def check_exists(
    session_id: str,
    path: str = Query(...),
    _user: CurrentUser = None,
) -> dict:
    """Cheap stat check used by Save-As to detect overwrites before writing."""
    store = _get_store()
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404)
    target = _safe_path(session.cwd, path)
    return {"exists": target.exists(), "is_file": target.is_file()}
