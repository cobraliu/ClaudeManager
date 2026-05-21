"""Git operations for session auto-commit."""
from __future__ import annotations

import re
import subprocess
from datetime import datetime
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


def git_pull(cwd: str) -> dict:
    """Pull current branch. Uses tracking upstream if set; otherwise falls
    back to origin/<current-branch> so locally-created branches still pull."""
    current = git_current_branch(cwd)
    if not current:
        return {"ok": False, "output": "not on a branch"}
    has_upstream = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8",
    ).returncode == 0
    args = ["git", "pull", "--ff-only"]
    if not has_upstream:
        args += ["origin", current]
    result = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=60,
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
    """Return {current, local, remote_only} for the branch picker.

    remote_only: branches under refs/remotes/origin/ that don't have a local
    counterpart (excluding origin/HEAD). The picker uses these to offer
    "fetch + track + checkout" in one action.
    """
    current = git_current_branch(cwd)
    local_res = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8",
    )
    local = [b.strip() for b in (local_res.stdout if local_res.returncode == 0 else "").splitlines() if b.strip()]

    remote_res = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/"],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8",
    )
    remote_only: list[str] = []
    if remote_res.returncode == 0:
        local_set = set(local)
        for line in remote_res.stdout.splitlines():
            ref = line.strip()
            if not ref or ref == "origin/HEAD" or "/HEAD" in ref:
                continue
            # Strip "origin/" prefix to get the branch name.
            if not ref.startswith("origin/"):
                continue
            name = ref[len("origin/"):]
            if name and name not in local_set:
                remote_only.append(name)
    return {"current": current, "local": local, "remote_only": remote_only}


def git_checkout_remote_branch(cwd: str, branch: str, stash: bool = False) -> dict:
    """Fetch origin, create a local tracking branch, then check it out.

    Used when the picker selects a branch that exists on origin but has no
    local counterpart. Conflict semantics match git_checkout_branch.
    """
    fetch_res = subprocess.run(
        ["git", "fetch", "origin", branch],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    if fetch_res.returncode != 0:
        return {"ok": False, "output": (fetch_res.stdout + fetch_res.stderr).strip(), "conflict": False}

    stashed = False
    if stash:
        current = git_current_branch(cwd) or "detached"
        from datetime import datetime
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"claudemanager: WIP on {current} (switching to {branch}) @ {ts}"
        stash_res = subprocess.run(
            ["git", "stash", "push", "-u", "-m", msg],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        )
        if stash_res.returncode != 0:
            return {"ok": False, "output": stash_res.stderr.strip(), "conflict": False}
        stashed = "No local changes" not in (stash_res.stdout + stash_res.stderr)

    result = subprocess.run(
        ["git", "checkout", "-b", branch, "--track", f"origin/{branch}"],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8",
    )
    out = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        return {"ok": True, "output": out, "conflict": False, "stashed": stashed}

    if "would be overwritten by checkout" in out or "would be overwritten by merge" in out:
        conflicting: list[str] = []
        for line in out.splitlines():
            stripped = line.strip()
            if line.startswith("\t") and stripped:
                conflicting.append(stripped)
        return {"ok": False, "output": out, "conflict": True, "conflicting_files": conflicting}

    return {"ok": False, "output": out, "conflict": False}


def git_checkout_branch(cwd: str, branch: str, stash: bool = False) -> dict:
    """Checkout an existing local branch.

    Tries plain `git checkout <branch>`. Git itself carries over uncommitted
    edits when they don't conflict with the target. If it refuses because local
    changes would be overwritten, returns conflict info so the caller can
    surface the conflicting files.

    If stash=True, runs `git stash push -u -m <auto msg>` first so the working
    tree is preserved (recoverable via `git stash pop`), then checks out.
    """
    stashed = False
    if stash:
        current = git_current_branch(cwd) or "detached"
        from datetime import datetime
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"claudemanager: WIP on {current} (switching to {branch}) @ {ts}"
        stash_res = subprocess.run(
            ["git", "stash", "push", "-u", "-m", msg],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        )
        if stash_res.returncode != 0:
            return {"ok": False, "output": stash_res.stderr.strip(), "conflict": False}
        # "No local changes to save" comes back with rc=0; only count an actual stash.
        stashed = "No local changes" not in (stash_res.stdout + stash_res.stderr)

    result = subprocess.run(
        ["git", "checkout", branch],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8",
    )
    out = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        return {"ok": True, "output": out, "conflict": False, "stashed": stashed}

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


# ── Merge (with VSCode-style conflict resolution) ──────────────────────────

def git_merge_status(cwd: str) -> dict:
    """Report whether a merge is in progress and which files are conflicted.

    Returns:
      {
        "in_progress": bool,           # .git/MERGE_HEAD exists
        "conflicted_files": [str,...], # paths from `git ls-files -u`
        "merge_head": str,             # short hash of being-merged commit
        "current_branch": str,
      }
    """
    merge_head_path = Path(cwd) / ".git" / "MERGE_HEAD"
    in_progress = merge_head_path.exists()
    merge_head = ""
    conflicted: list[str] = []
    if in_progress:
        try:
            merge_head = merge_head_path.read_text(encoding="utf-8").strip()[:8]
        except OSError:
            merge_head = ""
        ls = subprocess.run(
            ["git", "ls-files", "-u"],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        )
        if ls.returncode == 0:
            seen: set[str] = set()
            for line in ls.stdout.splitlines():
                # format: <mode> <hash> <stage>\t<path>
                if "\t" not in line:
                    continue
                path = line.split("\t", 1)[1]
                if path not in seen:
                    seen.add(path)
                    conflicted.append(path)
    return {
        "in_progress": in_progress,
        "conflicted_files": conflicted,
        "merge_head": merge_head,
        "current_branch": git_current_branch(cwd),
    }


def git_merge_preview(cwd: str, source: str, target: str) -> dict:
    """Read-only preview of merging `source` into `target`.

    Computes:
      - merge_kind: "up_to_date" | "fast_forward" | "clean" | "conflict" | "error"
      - ahead/behind: commit counts relative to the merge-base
      - commits: [{hash, short, subject, author, date}] for commits in source not on target
      - changed_files: [{path, status}] from `git diff --name-status target...source`
      - conflicting_files: paths git merge-tree predicts would conflict (only when
        merge_kind == "conflict")
      - error: human-readable error string when merge_kind == "error"

    Does not modify the working tree or index; safe to call repeatedly.
    """
    if not source or not target:
        return {"merge_kind": "error", "error": "source and target are required"}
    if source == target:
        return {"merge_kind": "error", "error": "source and target are the same"}

    def _run(args: list[str]) -> tuple[int, str, str]:
        r = subprocess.run(
            ["git", "-c", "core.quotepath=false", *args],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return r.returncode, r.stdout, r.stderr

    # Validate refs exist.
    for ref in (source, target):
        rc, _, _ = _run(["rev-parse", "--verify", f"{ref}^{{commit}}"])
        if rc != 0:
            return {"merge_kind": "error", "error": f"unknown ref: {ref}"}

    # Find merge-base. Without one, the branches share no history.
    rc, base_out, _ = _run(["merge-base", target, source])
    base = base_out.strip()
    if rc != 0 or not base:
        return {"merge_kind": "error", "error": f"no common ancestor between {target} and {source}"}

    # ahead = commits source has past base, behind = commits target has past base.
    def _count(rev_range: str) -> int:
        rc, out, _ = _run(["rev-list", "--count", rev_range])
        if rc != 0:
            return 0
        return int(out.strip() or "0")

    ahead = _count(f"{base}..{source}")
    behind = _count(f"{base}..{target}")

    # Changed files between target and source via the symmetric `target...source`
    # form — i.e., everything source has that's not on target, relative to base.
    rc, ns_out, _ = _run(["diff", "--name-status", f"{target}...{source}"])
    changed_files: list[dict] = []
    if rc == 0:
        for line in ns_out.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status = parts[0][:1]  # M / A / D / R / C — take first letter
            path = parts[-1]
            changed_files.append({"path": path, "status": status})

    # Commits in source not on target. Cap at 200 to keep payload bounded.
    commits: list[dict] = []
    # %x1f = unit separator — safe field delimiter that won't appear in commit metadata.
    fmt = "%H%x1f%h%x1f%an%x1f%ad%x1f%s"
    rc, log_out, _ = _run([
        "log", f"--pretty=format:{fmt}", "--date=iso-strict",
        "--max-count=200", f"{base}..{source}",
    ])
    if rc == 0:
        for line in log_out.splitlines():
            if not line.strip():
                continue
            parts = line.split("\x1f")
            if len(parts) < 5:
                continue
            commits.append({
                "hash": parts[0], "short": parts[1],
                "author": parts[2], "date": parts[3], "subject": parts[4],
            })

    if ahead == 0:
        return {
            "merge_kind": "up_to_date",
            "ahead": 0,
            "behind": behind,
            "commits": [],
            "changed_files": [],
            "conflicting_files": [],
        }
    if behind == 0:
        # target is an ancestor of source — fast-forward is possible.
        return {
            "merge_kind": "fast_forward",
            "ahead": ahead,
            "behind": 0,
            "commits": commits,
            "changed_files": changed_files,
            "conflicting_files": [],
        }

    # True three-way merge: probe for conflicts with `git merge-tree`.
    # Modern form (git >= 2.38): `git merge-tree --write-tree --name-only --merge-base=BASE TARGET SOURCE`.
    # Non-zero exit means conflicts; stdout's first line is the merged tree OID,
    # remaining lines are conflicted paths (with --name-only).
    rc, mt_out, mt_err = _run([
        "merge-tree", "--write-tree", "--name-only",
        f"--merge-base={base}", target, source,
    ])
    if rc == 0:
        return {
            "merge_kind": "clean",
            "ahead": ahead,
            "behind": behind,
            "commits": commits,
            "changed_files": changed_files,
            "conflicting_files": [],
        }
    # rc != 0 → conflicts. Lines after the first (tree OID) are paths.
    lines = [ln for ln in mt_out.splitlines() if ln.strip()]
    conflicting = lines[1:] if len(lines) > 1 else []
    if not conflicting and not mt_err:
        # Older git that doesn't support --write-tree falls through here.
        # Best-effort fall-back using the legacy `merge-tree BASE TARGET SOURCE`
        # form — search for conflict markers in its output.
        rc2, legacy_out, _ = _run(["merge-tree", base, target, source])
        if rc2 == 0 and "<<<<<<<" not in legacy_out:
            return {
                "merge_kind": "clean",
                "ahead": ahead,
                "behind": behind,
                "commits": commits,
                "changed_files": changed_files,
                "conflicting_files": [],
            }
    return {
        "merge_kind": "conflict",
        "ahead": ahead,
        "behind": behind,
        "commits": commits,
        "changed_files": changed_files,
        "conflicting_files": conflicting,
    }


def git_merge_file_diff(cwd: str, source: str, target: str, path: str) -> dict:
    """Return the unified diff of `path` between target and source.

    Uses `git diff target...source -- path` (symmetric form) so the diff shows
    changes source has that target doesn't, relative to the merge base.
    """
    if not source or not target or not path:
        return {"diff": "", "error": "source, target, and path are required"}
    r = subprocess.run(
        ["git", "-c", "core.quotepath=false", "diff", "--no-color",
         f"{target}...{source}", "--", path],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        return {"diff": "", "error": (r.stderr or "").strip() or "diff failed"}
    return {"diff": r.stdout}


def git_merge_start(cwd: str, source: str, target: str) -> dict:
    """Merge `source` into `target` (no-ff). If `target` is not the current branch,
    checks out `target` first.

    Before running `git merge`, creates `backup/before-merge-<timestamp>` pointing
    at the target branch's HEAD so users can always recover the pre-merge state
    via `git reset --hard <backup>`. The backup branch is preserved through both
    success and conflict paths; UI surfaces its name so it can be cleaned up
    manually when the user is confident the merge went well.

    Returns:
      - {"ok": True, "clean": True, "output": ..., "backup_branch": ...} when merge completed (no conflicts)
      - {"ok": True, "clean": False, "conflicted_files": [...], "backup_branch": ...} when conflicts surfaced
      - {"ok": True, "up_to_date": True, "output": ...} when already up to date (no backup created)
      - {"ok": False, "output": ...} on hard failure (dirty tree, unknown branch, ...)
    """
    if source == target:
        return {"ok": False, "output": "source and target branches are the same"}
    # Refuse if a merge is already in progress.
    if (Path(cwd) / ".git" / "MERGE_HEAD").exists():
        return {"ok": False, "output": "a merge is already in progress; resolve or abort it first"}
    # Refuse if working tree is dirty — git itself will refuse, but we surface a friendlier error.
    if git_is_dirty(cwd):
        return {"ok": False, "output": "working tree has uncommitted changes; commit or stash before merging"}

    # Switch to target branch if not already there.
    current = git_current_branch(cwd)
    if current != target:
        co = subprocess.run(
            ["git", "checkout", target],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        )
        if co.returncode != 0:
            return {"ok": False, "output": f"failed to checkout target branch '{target}': {(co.stdout + co.stderr).strip()}"}

    # Create a backup pointer at target's current HEAD before touching anything.
    # The %Y%m%d-%H%M%S stamp makes collisions effectively impossible across
    # repeat attempts; if the user merges 4 times in the same minute, we fall
    # back to appending a short HEAD sha.
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_branch = f"backup/before-merge-{ts}"
    bk = subprocess.run(
        ["git", "branch", backup_branch, "HEAD"],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8",
    )
    if bk.returncode != 0:
        # Likely cause: rare same-second collision. Disambiguate with HEAD short sha.
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        )
        suffix = sha.stdout.strip() if sha.returncode == 0 else "x"
        backup_branch = f"backup/before-merge-{ts}-{suffix}"
        bk2 = subprocess.run(
            ["git", "branch", backup_branch, "HEAD"],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        )
        if bk2.returncode != 0:
            # Don't block the merge if backup creation fails; just warn via output.
            return {"ok": False, "output": f"failed to create backup branch: {(bk2.stdout + bk2.stderr).strip()}"}

    result = subprocess.run(
        ["git", "merge", "--no-ff", "--no-edit", source],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    out = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        if "Already up to date" in out or "Already up-to-date" in out:
            # Nothing actually happened — delete the unused backup so we don't
            # accumulate empty pointers on repeat "already up-to-date" calls.
            subprocess.run(
                ["git", "branch", "-D", backup_branch],
                cwd=cwd, capture_output=True, text=True, encoding="utf-8",
            )
            return {"ok": True, "up_to_date": True, "output": out}
        return {"ok": True, "clean": True, "output": out, "backup_branch": backup_branch}

    # Non-zero exit: either a conflict (merge in progress, files unmerged) or a real error.
    status = git_merge_status(cwd)
    if status["in_progress"] and status["conflicted_files"]:
        return {
            "ok": True,
            "clean": False,
            "conflicted_files": status["conflicted_files"],
            "output": out,
            "backup_branch": backup_branch,
        }
    # Hard failure with no merge in progress — the backup is now dead weight, drop it.
    subprocess.run(
        ["git", "branch", "-D", backup_branch],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8",
    )
    return {"ok": False, "output": out}


def git_conflict_file_versions(cwd: str, path: str) -> dict:
    """Return base/ours/theirs/working for a conflicted file.

    Uses `git show :<stage>:<path>` where stage is 1=base, 2=ours, 3=theirs.
    A missing stage (e.g., file added on only one side) returns empty string.
    Working is the on-disk content with conflict markers.
    """
    def _show(stage: int) -> str:
        r = subprocess.run(
            ["git", "show", f":{stage}:{path}"],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return r.stdout if r.returncode == 0 else ""

    working = ""
    try:
        working = (Path(cwd) / path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        working = ""

    return {
        "path": path,
        "base": _show(1),
        "ours": _show(2),
        "theirs": _show(3),
        "working": working,
    }


def git_resolve_file(cwd: str, path: str, content: str) -> dict:
    """Write resolved content to disk, then `git add <path>` to mark resolved."""
    target = Path(cwd) / path
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as e:
        return {"ok": False, "output": f"write failed: {e}"}
    add = subprocess.run(
        ["git", "add", "--", path],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8",
    )
    if add.returncode != 0:
        return {"ok": False, "output": (add.stdout + add.stderr).strip()}
    return {"ok": True, "output": ""}


def git_merge_continue(cwd: str, message: str | None = None) -> dict:
    """Finalize an in-progress merge. Refuses if any files are still unmerged."""
    if not (Path(cwd) / ".git" / "MERGE_HEAD").exists():
        return {"ok": False, "output": "no merge in progress"}
    status = git_merge_status(cwd)
    if status["conflicted_files"]:
        return {
            "ok": False,
            "output": f"still unresolved: {', '.join(status['conflicted_files'])}",
        }
    cmd = ["git", "commit", "--no-edit"]
    if message:
        cmd = ["git", "commit", "-m", message]
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
    )
    out = (result.stdout + result.stderr).strip()
    return {"ok": result.returncode == 0, "output": out}


def git_merge_abort(cwd: str) -> dict:
    """Abort the in-progress merge (restores working tree to pre-merge state)."""
    result = subprocess.run(
        ["git", "merge", "--abort"],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8",
    )
    out = (result.stdout + result.stderr).strip()
    return {"ok": result.returncode == 0, "output": out}
