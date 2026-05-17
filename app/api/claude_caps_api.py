from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.security import CurrentUser

router = APIRouter(prefix="/api/claude-caps", tags=["claude-caps"])

_GLOBAL_ROOT = Path.home() / ".claude"
_HISTORY_DIR = ".cm_history"
_MAX_VERSIONS = 30


# ── Models ─────────────────────────────────────────────────────────────────────

class CapItem(BaseModel):
    relpath: str
    name: str
    description: str
    exists: bool
    size: int = 0


class CapSection(BaseModel):
    id: str
    title: str
    items: list[CapItem]
    new_template: str | None = None
    new_dir: str | None = None


class CapListResponse(BaseModel):
    scope_root: str
    sections: list[CapSection]


class WriteRequest(BaseModel):
    scope: str
    relpath: str
    content: str
    cwd: str | None = None


class CapVersion(BaseModel):
    version_id: str    # sortable timestamp string used as filename key
    saved_at: str      # ISO timestamp
    size: int
    preview: str       # first ~120 chars of content


class CapVersionsResponse(BaseModel):
    versions: list[CapVersion]


class RollbackRequest(BaseModel):
    scope: str
    relpath: str
    version_id: str
    cwd: str | None = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_frontmatter_desc(text: str) -> str | None:
    """Pull description from YAML frontmatter (handles inline and >- folded scalar)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    desc_lines: list[str] = []
    capturing = False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if capturing:
            if line.startswith("  ") or line.startswith("\t"):
                desc_lines.append(line.strip())
            else:
                capturing = False
                break
        elif re.match(r"\s*description\s*:", line):
            rest = re.split(r"description\s*:", line, 1)[1].strip().strip("\"'")
            if rest in (">-", ">", "|", "|-"):
                capturing = True
            elif rest:
                return rest[:120]
    if desc_lines:
        return " ".join(desc_lines)[:120]
    return None


def _extract_desc(p: Path) -> str:
    if not p.exists():
        return ""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        if p.suffix == ".json":
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    keys = list(data.keys())
                    preview = ", ".join(keys[:5])
                    return f"{len(keys)} keys: {preview}"
            except Exception:
                pass
            return text[:80].replace("\n", " ")
        # Try YAML frontmatter description first
        fm = _extract_frontmatter_desc(text)
        if fm:
            return fm
        # Fall back to first non-empty non-heading line
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith("#") and not s.startswith("---"):
                return s[:120]
    except Exception:
        pass
    return ""


def _scope_root(scope: str, cwd: str | None) -> Path:
    if scope == "global":
        return _GLOBAL_ROOT
    if scope == "project":
        if not cwd:
            raise HTTPException(400, "cwd required for project scope")
        return Path(cwd)
    raise HTTPException(400, f"Unknown scope: {scope}")


def _resolve(scope: str, relpath: str, cwd: str | None) -> Path:
    root = _scope_root(scope, cwd)
    rel = Path(relpath)
    if rel.is_absolute():
        raise HTTPException(400, "relpath must be relative")
    resolved = (root / rel).resolve()
    root_resolved = root.resolve()
    if not str(resolved).startswith(str(root_resolved) + "/") and resolved != root_resolved:
        raise HTTPException(403, "Path traversal detected")
    return resolved


def _history_dir(scope: str, cwd: str | None, relpath: str) -> Path:
    """Return the directory that stores versions for a given file."""
    root = _scope_root(scope, cwd)
    # Store history inside the scope root's .cm_history subdirectory,
    # mirroring the relpath structure: .cm_history/skills/init.md/
    safe = relpath.replace("/", "__")
    return root / _HISTORY_DIR / safe


def _save_version(scope: str, cwd: str | None, relpath: str, file_path: Path) -> None:
    """Back up current content of file_path before overwriting."""
    if not file_path.exists():
        return
    hist = _history_dir(scope, cwd, relpath)
    hist.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
    backup = hist / f"{ts}.bak"
    shutil.copy2(file_path, backup)
    # Trim oldest versions
    versions = sorted(hist.glob("*.bak"))
    for old in versions[:-_MAX_VERSIONS]:
        old.unlink(missing_ok=True)


def _cap_item(root: Path, relpath: str, name: str) -> CapItem:
    p = root / relpath
    exists = p.exists()
    return CapItem(
        relpath=relpath, name=name,
        description=_extract_desc(p) if exists else "",
        exists=exists,
        size=p.stat().st_size if exists else 0,
    )


_PKG_PRIMARIES = ("SKILL.md", "AGENT.md", "COMMAND.md", "README.md")


def _primary_file(pkg_dir: Path) -> Path | None:
    """Find the primary editable file in a skill/agent package directory."""
    for name in _PKG_PRIMARIES:
        p = pkg_dir / name
        if p.exists():
            return p
    mds = sorted(f for f in pkg_dir.iterdir() if f.is_file() and f.suffix == ".md")
    return mds[0] if mds else None


def _dir_items(root: Path, dir_name: str) -> list[CapItem]:
    """List items in a capability directory, supporting both flat files and package subdirs."""
    d = root / dir_name
    if not d.is_dir():
        return []
    items = []
    for entry in sorted(d.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            # Package directory (e.g. skills/init/ containing SKILL.md)
            primary = _primary_file(entry)
            if primary:
                items.append(CapItem(
                    relpath=f"{dir_name}/{entry.name}/{primary.name}",
                    name=entry.name,
                    description=_extract_desc(primary),
                    exists=True,
                    size=primary.stat().st_size,
                ))
            else:
                items.append(CapItem(
                    relpath=f"{dir_name}/{entry.name}",
                    name=entry.name,
                    description="(directory, no .md file)",
                    exists=True,
                    size=0,
                ))
        elif entry.is_file():
            items.append(CapItem(
                relpath=f"{dir_name}/{entry.name}",
                name=entry.stem if entry.suffix in (".md", ".json") else entry.name,
                description=_extract_desc(entry),
                exists=True,
                size=entry.stat().st_size,
            ))
    return items


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/list")
def list_caps(
    scope: str = Query(...),
    cwd: str | None = Query(default=None),
    _user: CurrentUser = None,  # type: ignore[assignment]
) -> CapListResponse:
    if scope == "global":
        root = _GLOBAL_ROOT
        sections = [
            CapSection(
                id="instructions", title="Instructions",
                items=[_cap_item(root, "CLAUDE.md", "CLAUDE.md")],
                new_template="md",
            ),
            CapSection(
                id="settings", title="Settings",
                items=[_cap_item(root, "settings.json", "settings.json")],
            ),
            CapSection(
                id="commands", title="Commands",
                items=_dir_items(root, "commands"),
                new_template="md", new_dir="commands",
            ),
            CapSection(
                id="skills", title="Skills",
                items=_dir_items(root, "skills"),
                new_template="md", new_dir="skills",
            ),
            CapSection(
                id="agents", title="Agents",
                items=_dir_items(root, "agents"),
                new_template="md", new_dir="agents",
            ),
        ]
        return CapListResponse(scope_root=str(root), sections=sections)

    elif scope == "project":
        if not cwd:
            raise HTTPException(400, "cwd required for project scope")
        proj_root = Path(cwd)
        mcp_path = proj_root / ".mcp.json"
        mcp_cl_path = proj_root / ".claude" / ".mcp.json"

        def _pitem(relpath: str, name: str) -> CapItem:
            p = proj_root / relpath
            return CapItem(relpath=relpath, name=name, description=_extract_desc(p),
                           exists=p.exists(), size=p.stat().st_size if p.exists() else 0)

        def _pdir(dir_prefix: str) -> list[CapItem]:
            d = proj_root / dir_prefix
            if not d.is_dir():
                return []
            items = []
            for f in sorted(d.iterdir()):
                if f.is_file() and not f.name.startswith("."):
                    items.append(CapItem(
                        relpath=f"{dir_prefix}/{f.name}",
                        name=f.stem if f.suffix in (".md", ".json") else f.name,
                        description=_extract_desc(f),
                        exists=True, size=f.stat().st_size,
                    ))
            return items

        mcp_src = mcp_path if mcp_path.exists() else mcp_cl_path
        mcp_item = CapItem(
            relpath=".mcp.json", name=".mcp.json",
            description=_extract_desc(mcp_src),
            exists=mcp_src.exists(),
            size=mcp_src.stat().st_size if mcp_src.exists() else 0,
        )

        sections = [
            CapSection(id="instructions", title="Instructions",
                       items=[_pitem(".claude/CLAUDE.md", "CLAUDE.md")], new_template="md"),
            CapSection(id="settings", title="Settings",
                       items=[_pitem(".claude/settings.json", "settings.json"),
                              _pitem(".claude/settings.local.json", "settings.local.json")]),
            CapSection(id="mcp", title="MCP Servers", items=[mcp_item]),
            CapSection(id="commands", title="Commands", items=_pdir(".claude/commands"),
                       new_template="md", new_dir=".claude/commands"),
            CapSection(id="skills", title="Skills", items=_pdir(".claude/skills"),
                       new_template="md", new_dir=".claude/skills"),
            CapSection(id="agents", title="Agents", items=_pdir(".claude/agents"),
                       new_template="md", new_dir=".claude/agents"),
        ]
        return CapListResponse(scope_root=str(proj_root), sections=sections)

    raise HTTPException(400, f"Unknown scope: {scope}")


@router.get("/file")
def read_file(
    scope: str = Query(...),
    relpath: str = Query(...),
    cwd: str | None = Query(default=None),
    _user: CurrentUser = None,  # type: ignore[assignment]
) -> dict:
    p = _resolve(scope, relpath, cwd)
    if not p.exists():
        return {"content": "", "exists": False}
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"content": content, "exists": True}


@router.put("/file", status_code=204)
def write_file(body: WriteRequest, _user: CurrentUser = None) -> None:  # type: ignore[assignment]
    p = _resolve(body.scope, body.relpath, body.cwd)
    _save_version(body.scope, body.cwd, body.relpath, p)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(body.content, encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, str(e))


@router.delete("/file", status_code=204)
def delete_file(
    scope: str = Query(...),
    relpath: str = Query(...),
    cwd: str | None = Query(default=None),
    _user: CurrentUser = None,  # type: ignore[assignment]
) -> None:
    p = _resolve(scope, relpath, cwd)
    if not p.exists():
        raise HTTPException(404, "File not found")
    _save_version(scope, cwd, relpath, p)
    try:
        p.unlink()
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/versions")
def list_versions(
    scope: str = Query(...),
    relpath: str = Query(...),
    cwd: str | None = Query(default=None),
    _user: CurrentUser = None,  # type: ignore[assignment]
) -> CapVersionsResponse:
    hist = _history_dir(scope, cwd, relpath)
    if not hist.is_dir():
        return CapVersionsResponse(versions=[])

    versions: list[CapVersion] = []
    for bak in sorted(hist.glob("*.bak"), reverse=True):
        try:
            content = bak.read_text(encoding="utf-8", errors="replace")
            size = bak.stat().st_size
            # Parse timestamp from filename: YYYYMMDDTHHMMSS_ffffff
            stem = bak.stem  # e.g. 20240115T103000_123456
            try:
                dt = datetime.strptime(stem, "%Y%m%dT%H%M%S_%f").replace(tzinfo=timezone.utc)
                saved_at = dt.isoformat()
            except ValueError:
                saved_at = stem
            versions.append(CapVersion(
                version_id=stem,
                saved_at=saved_at,
                size=size,
                preview=content[:120].replace("\n", " "),
            ))
        except Exception:
            continue
    return CapVersionsResponse(versions=versions)


@router.get("/version-content")
def get_version_content(
    scope: str = Query(...),
    relpath: str = Query(...),
    version_id: str = Query(...),
    cwd: str | None = Query(default=None),
    _user: CurrentUser = None,  # type: ignore[assignment]
) -> dict:
    hist = _history_dir(scope, cwd, relpath)
    bak = hist / f"{version_id}.bak"
    if not bak.exists():
        raise HTTPException(404, "Version not found")
    try:
        content = bak.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"content": content}


@router.post("/rollback", status_code=204)
def rollback_version(body: RollbackRequest, _user: CurrentUser = None) -> None:  # type: ignore[assignment]
    hist = _history_dir(body.scope, body.cwd, body.relpath)
    bak = hist / f"{body.version_id}.bak"
    if not bak.exists():
        raise HTTPException(404, "Version not found")

    target = _resolve(body.scope, body.relpath, body.cwd)
    # Back up the current file before restoring
    _save_version(body.scope, body.cwd, body.relpath, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(bak, target)
    except Exception as e:
        raise HTTPException(500, str(e))
