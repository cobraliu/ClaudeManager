from __future__ import annotations

import re
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

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

@router.get("/{session_id}/code/changed-files")
def get_changed_files(session_id: str, _user: CurrentUser) -> list[dict]:
    """Return files changed relative to HEAD (or untracked)."""
    store = _get_store()
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404)

    cwd = session.cwd
    out = _git(cwd, "status", "--porcelain")
    if not out:
        return []

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

    files = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        xy = line[:2]
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ")[-1]
        path = path.strip().strip('"')

        status_map = {
            "M": "modified", "A": "added", "D": "deleted",
            "R": "renamed", "C": "copied", "U": "conflict",
            "?": "untracked",
        }
        status_char = xy.strip()[0] if xy.strip() else "?"
        status = status_map.get(status_char, "modified")

        entry: dict = {"path": path, "status": status}
        if path in numstat:
            a, r = numstat[path]
            entry["added"] = a
            entry["removed"] = r
        elif status == "untracked":
            try:
                fp = Path(cwd) / path
                if fp.is_file():
                    line_count = len(fp.read_text(errors="replace").splitlines())
                    entry["added"] = line_count
                    entry["removed"] = 0
            except OSError:
                pass
        files.append(entry)

    return files


@router.get("/{session_id}/code/file")
def get_file(
    session_id: str,
    path: str = Query(...),
    _user: CurrentUser = None,
) -> dict:
    """Return file content, detected language, and diff line info."""
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

    try:
        raw_head = target.read_bytes()[:8192]
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Detect binary by null bytes in the first 8 KiB
    if b"\x00" in raw_head:
        return {
            "path": path,
            "is_binary": True,
            "size": target.stat().st_size,
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

    # Limit to 3000 lines to avoid huge payloads
    lines = content.splitlines()
    truncated = len(lines) > 3000
    if truncated:
        lines = lines[:3000]
        content = "\n".join(lines)

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
        "diff_raw": diff_out,
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
