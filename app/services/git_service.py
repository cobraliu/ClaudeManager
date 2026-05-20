"""Git operations for session auto-commit."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

_DEFAULT_GITIGNORE = """\
# Dependencies
**/node_modules/
vendor/
.pnp/
.pnp.js

# Python
__pycache__/
*.py[cod]
*.pyo
.venv/
venv/
env/
ENV/
*.egg-info/
dist/
build/
.eggs/

# Build outputs
dist/
build/
.next/
.nuxt/
out/
target/
*.class

# Large model / data files
*.bin
*.weights
*.ckpt
*.pt
*.pth
*.onnx
*.h5
*.hdf5
*.pkl
*.pickle
*.npy
*.npz
*.parquet
*.arrow
*.safetensors

# Archives
*.zip
*.tar
*.tar.gz
*.tgz
*.tar.bz2
*.rar
*.7z

# Media
*.mp4
*.avi
*.mov
*.mkv
*.mp3
*.wav
*.flac

# Logs & temp
*.log
*.tmp
*.swp
*.swo
.cache/
tmp/
temp/

# Secrets / env
.env
.env.*
*.pem
*.key
secrets.*

# OS
.DS_Store
Thumbs.db
desktop.ini

# IDE
.idea/
.vscode/
*.suo
*.user
"""


def is_git_repo(cwd: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def git_init(cwd: str) -> dict:
    result = subprocess.run(
        ["git", "init"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {"ok": False, "output": (result.stdout + result.stderr).strip()}

    # Create .gitignore only if one doesn't already exist
    gitignore_path = Path(cwd) / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text(_DEFAULT_GITIGNORE, encoding="utf-8")

    return {
        "ok": True,
        "output": (result.stdout + result.stderr).strip(),
    }


def _strip_code_blocks(text: str) -> str:
    """Remove fenced code blocks (``` ... ```) and inline code (`...`) from text."""
    # Remove fenced code blocks (``` or ~~~, with optional language tag)
    text = re.sub(r"```[\w]*\n.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"~~~[\w]*\n.*?~~~", "", text, flags=re.DOTALL)
    # Remove inline code
    text = re.sub(r"`[^`\n]+`", "", text)
    return text


def _strip_markdown_formatting(text: str) -> str:
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.MULTILINE)
    return text.strip()


def make_commit_summary(last_assistant_text: str, max_len: int = 72) -> str:
    """
    Extract a commit subject from the non-code parts of the last assistant reply.
    Returns the first meaningful non-code sentence/line, capped at max_len chars.
    """
    no_code = _strip_code_blocks(last_assistant_text)
    no_fmt = _strip_markdown_formatting(no_code)
    for line in no_fmt.splitlines():
        line = line.strip()
        if len(line) >= 8:  # skip very short fragments
            return line[:max_len]
    return "Claude auto-commit"


def make_commit_message(prompts: list[dict] | str, assistant_text: str) -> str:
    """
    Build a full git commit message.

    prompts: list of {"text": str, "ts": float, "time_str": str}  — all user prompts
             since the last commit, OR a plain str (legacy / manual commit path).
    assistant_text: the last assistant reply.

    Truncation rules:
      - single prompt  → keep up to 512 chars
      - multiple prompts → keep up to 256 chars each
    """
    subject = make_commit_summary(assistant_text)

    no_code = _strip_code_blocks(assistant_text)
    no_fmt = _strip_markdown_formatting(no_code)
    body_response = "\n".join(line for line in no_fmt.splitlines() if line.strip())

    # Normalise prompts to list[dict]
    if isinstance(prompts, str):
        prompt_list = [{"text": prompts, "ts": 0.0, "time_str": ""}] if prompts.strip() else []
    else:
        prompt_list = [p for p in prompts if p.get("text", "").strip()]

    parts = [subject]

    if prompt_list:
        max_len = 512 if len(prompt_list) == 1 else 256
        lines = []
        for p in prompt_list:
            text = p["text"].strip()
            truncated = text[:max_len] + ("…" if len(text) > max_len else "")
            prefix = f"[{p['time_str']}] " if p.get("time_str") else ""
            lines.append(f"{prefix}{truncated}")
        label = "Prompt" if len(prompt_list) == 1 else "Prompts"
        parts.append(f"{label}:\n" + "\n\n".join(lines))

    if body_response:
        parts.append(f"Response:\n{body_response}")

    return "\n\n".join(parts)


def _get_staged_files(cwd: str) -> list[str]:
    """Return list of files staged for commit (after git add -A)."""
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "diff", "--cached", "--name-only"],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        return []
    return [f for f in result.stdout.splitlines() if f.strip()]


def git_is_dirty(cwd: str) -> bool:
    """Return True if there are any uncommitted changes (staged or unstaged)."""
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "status", "--porcelain"],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def git_add_commit(cwd: str, message: str, author: str = "claude") -> dict:
    """
    Run git add -A then git commit.
    The commit message body lists the changed files; message is the subject line.
    Returns {"ok": bool, "committed": bool, "output": str}.
    'committed' is False when there is nothing to commit.
    author: used as both git user.name and the local part of user.email.
    """
    add = subprocess.run(
        ["git", "add", "-A"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if add.returncode != 0:
        return {"ok": False, "committed": False, "output": add.stderr.strip()}

    # Build full commit message: subject + changed-files body
    staged = _get_staged_files(cwd)
    if staged:
        files_body = "Changed files:\n" + "\n".join(f"  {f}" for f in staged)
        full_message = f"{message}\n\n{files_body}"
    else:
        full_message = message

    # Pass message via stdin (-F -) to avoid "Argument list too long" (E2BIG)
    # when the commit message contains a very long assistant response.
    commit = subprocess.run(
        [
            "git",
            "-c", "core.quotepath=false",
            "-c", f"user.email={author}@auto",
            "-c", f"user.name={author}",
            "commit",
            "-F", "-",
        ],
        input=full_message,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    out = (commit.stdout + commit.stderr).strip()
    if commit.returncode == 0:
        return {"ok": True, "committed": True, "output": out}
    if "nothing to commit" in out or "nothing added to commit" in out:
        return {"ok": True, "committed": False, "output": out}
    return {"ok": False, "committed": False, "output": out}


def git_search_commits(cwd: str, query: str, n: int = 500) -> list[dict]:
    """Search commits whose full message contains query (case-insensitive).
    Returns list of {hash, short_hash, subject, author, date, context}
    where context is the first matching line from the full message body.
    """
    result = subprocess.run(
        [
            "git", "-c", "core.quotepath=false", "log", f"-{n}",
            f"--grep={query}", "--regexp-ignore-case",
            "--format=\x1e%H\x1f%h\x1f%s\x1f%an\x1f%ai\x1f%B",
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        return []

    q_lower = query.lower()
    entries = []
    for record in result.stdout.split("\x1e"):
        record = record.strip()
        if not record:
            continue
        parts = record.split("\x1f", 5)
        if len(parts) < 5:
            continue
        commit_hash = parts[0].strip()
        short_hash = parts[1].strip()
        subject = parts[2].strip()
        author = parts[3].strip()
        date = parts[4].strip()
        body = parts[5] if len(parts) > 5 else ""

        # Find first matching line in the full message for context
        context = ""
        for line in body.splitlines():
            if q_lower in line.lower():
                context = line.strip()[:300]
                break

        if not commit_hash:
            continue
        entries.append({
            "hash": commit_hash,
            "short_hash": short_hash,
            "subject": subject,
            "author": author,
            "date": date,
            "context": context,
        })
    return entries


def git_log(cwd: str, n: int = 20) -> list[dict]:
    """Return last n commits as list of {hash, short_hash, subject, author, date}."""
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "log", f"-{n}", "--pretty=format:%H\x1f%h\x1f%s\x1f%an\x1f%ai"],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        return []
    entries = []
    for line in result.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 5:
            entries.append({
                "hash": parts[0],
                "short_hash": parts[1],
                "subject": parts[2],
                "author": parts[3],
                "date": parts[4],
            })
    return entries


def git_file_log(cwd: str, rel_path: str, n: int = 50) -> list[dict]:
    """Return git log for a specific file (follows renames). Returns list of {hash, short_hash, subject, author, date}."""
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "log", f"-{n}", "--follow",
         "--pretty=format:%H\x1f%h\x1f%s\x1f%an\x1f%ai", "--", rel_path],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        return []
    entries = []
    for line in result.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 5:
            entries.append({
                "hash": parts[0],
                "short_hash": parts[1],
                "subject": parts[2],
                "author": parts[3],
                "date": parts[4],
            })
    return entries


def git_file_show(cwd: str, rel_path: str, commit_hash: str) -> str:
    """Return file content at a specific commit. Returns empty string if not found."""
    result = subprocess.run(
        ["git", "show", f"{commit_hash}:{rel_path}"],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def git_file_diff(cwd: str, rel_path: str, commit_hash: str) -> str:
    """Return the unified diff for a specific file in a specific commit."""
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "show", "--patch", "--no-color",
         commit_hash, "--", rel_path],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def git_commit_full_message(cwd: str, commit_hash: str) -> str:
    """Return the full commit message (subject + body)."""
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "log", "-1", "--format=%B", commit_hash],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8",
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def git_show_commit(cwd: str, commit_hash: str) -> dict:
    """
    Return full commit details: message + per-file old/new content.
    For the initial commit (no parent) old_content is always ''.
    """
    message = git_commit_full_message(cwd, commit_hash)
    # Try diff against parent; fall back to empty tree for first commit
    parent = f"{commit_hash}^"
    changed = git_diff_files(cwd, parent, commit_hash)
    if not changed:
        # Possibly first commit — diff against empty tree
        empty_tree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        changed = git_diff_files(cwd, empty_tree, commit_hash)

    files = []
    for path in changed[:50]:
        old_content = git_file_at_commit(cwd, parent, path)
        new_content = git_file_at_commit(cwd, commit_hash, path)
        files.append({"path": path, "old_content": old_content, "new_content": new_content})
    return {"message": message, "files": files}


def git_diff_files(cwd: str, old_hash: str, new_hash: str) -> list[str]:
    """Return list of files changed between two commits."""
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "diff", "--name-only", old_hash, new_hash],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        return []
    return [f for f in result.stdout.splitlines() if f.strip()]


def git_file_at_commit(cwd: str, commit_hash: str, filepath: str) -> str:
    """Return the content of a file at a specific commit, or '' if not found.
    Binary files are returned as empty string."""
    result = subprocess.run(
        ["git", "show", f"{commit_hash}:{filepath}"],
        cwd=cwd,
        capture_output=True,
    )
    if result.returncode != 0:
        return ""
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return "(binary file)"


def git_clone(url: str, target_dir: str) -> dict:
    """Clone url into target_dir (must not exist). Returns {"ok": bool, "output": str}."""
    import os
    from app.config import get_proxy_env
    parent = str(Path(target_dir).parent)
    os.makedirs(parent, exist_ok=True)
    env = os.environ.copy()
    env.update(get_proxy_env())
    result = subprocess.run(
        ["git", "clone", url, target_dir],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )
    out = (result.stdout + result.stderr).strip()
    return {"ok": result.returncode == 0, "output": out}


def git_get_remote(cwd: str) -> str:
    """Return the URL of the 'origin' remote, or '' if not set."""
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return ""


def git_set_remote(cwd: str, url: str) -> dict:
    """Set or update the 'origin' remote URL."""
    # Remove existing origin if present
    subprocess.run(["git", "remote", "remove", "origin"], cwd=cwd, capture_output=True)
    if not url:
        return {"ok": True, "output": "Remote removed."}
    result = subprocess.run(
        ["git", "remote", "add", "origin", url],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return {
        "ok": result.returncode == 0,
        "output": (result.stdout + result.stderr).strip(),
    }


def git_push(cwd: str) -> dict:
    """Push HEAD to origin. Returns {"ok": bool, "output": str}."""
    # Get current branch name
    branch_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=cwd, capture_output=True, text=True,
    )
    branch = branch_result.stdout.strip() or "main"
    result = subprocess.run(
        ["git", "push", "-u", "origin", branch],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = (result.stdout + result.stderr).strip()
    return {"ok": result.returncode == 0, "output": out}


def git_current_branch(cwd: str) -> str:
    """Return current branch name, or '' if detached / failure."""
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        return ""
    name = result.stdout.strip()
    return "" if name == "HEAD" else name


def git_list_branches(cwd: str) -> dict:
    """Return {current: str, local: [str, ...]} for local branches only."""
    current = git_current_branch(cwd)
    result = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        return {"current": current, "local": []}
    local = [b.strip() for b in result.stdout.splitlines() if b.strip()]
    return {"current": current, "local": local}


def git_checkout_branch(cwd: str, branch: str, force_discard: bool = False) -> dict:
    """Checkout an existing local branch.

    Strategy:
      - Try plain `git checkout <branch>` first. Git carries over uncommitted
        edits when they don't conflict with the target.
      - If git refuses because local changes would be overwritten, return
        {ok: False, conflict: True, conflicting_files: [...]} so the caller
        can decide whether to discard and retry.
      - If force_discard=True, reset --hard + clean -fd first, then checkout.
    """
    if force_discard:
        reset = subprocess.run(
            ["git", "reset", "--hard", "HEAD"],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        )
        if reset.returncode != 0:
            return {"ok": False, "output": reset.stderr.strip(), "conflict": False}
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        )

    result = subprocess.run(
        ["git", "checkout", branch],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8",
    )
    out = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        return {"ok": True, "output": out, "conflict": False}

    # Parse conflicting file list from git's error output.
    if "would be overwritten by checkout" in out or "would be overwritten by merge" in out:
        conflicting: list[str] = []
        for line in out.splitlines():
            stripped = line.strip()
            # Git lists conflicting files indented with a tab on lines between
            # "error: Your local changes..." and "Please commit your changes...".
            if line.startswith("\t") and stripped:
                conflicting.append(stripped)
        return {
            "ok": False,
            "output": out,
            "conflict": True,
            "conflicting_files": conflicting,
        }

    return {"ok": False, "output": out, "conflict": False}


def git_graph_log(cwd: str, scope: str = "current", n: int = 500) -> list[dict]:
    """Return commits suitable for client-side graph rendering.

    scope: 'current' (HEAD), 'all' (--all), or a specific branch name.
    Each commit: {hash, short_hash, parents: [hash,...], subject, author, date, refs: [str,...]}.
    """
    args = ["git", "-c", "core.quotepath=false", "log", f"-{n}",
            "--pretty=format:%H\x1f%h\x1f%P\x1f%s\x1f%an\x1f%ai\x1f%D"]
    if scope == "all":
        args.append("--all")
    elif scope and scope != "current":
        args.append(scope)
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        return []
    entries = []
    for line in result.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) < 6:
            continue
        full, short, parents, subject, author, date = parts[:6]
        refs_raw = parts[6] if len(parts) > 6 else ""
        refs = [r.strip() for r in refs_raw.split(",") if r.strip()] if refs_raw else []
        entries.append({
            "hash": full,
            "short_hash": short,
            "parents": [p for p in parents.split(" ") if p],
            "subject": subject,
            "author": author,
            "date": date,
            "refs": refs,
        })
    return entries


def git_rollback(cwd: str, commit_hash: str, author: str = "claude") -> dict:
    """
    Rollback to commit_hash by checking out its tree, then committing.
    Intermediate commits are preserved in history.
    Returns {"ok": bool, "output": str}.
    """
    checkout = subprocess.run(
        ["git", "checkout", commit_hash, "--", "."],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if checkout.returncode != 0:
        return {"ok": False, "output": checkout.stderr.strip()}

    commit = subprocess.run(
        [
            "git",
            "-c", f"user.email={author}@auto",
            "-c", f"user.name={author}",
            "commit",
            "-m", f"rollback to {commit_hash[:8]}",
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    out = (commit.stdout + commit.stderr).strip()
    if commit.returncode == 0:
        return {"ok": True, "output": out}
    if "nothing to commit" in out:
        return {"ok": True, "output": "Already at that version, nothing to rollback"}
    return {"ok": False, "output": out}
