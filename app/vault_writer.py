"""Writes notes into the Obsidian vault via its own git clone —
deliberately separate from Athenaeum's read-only vault clone (see this
project's README for why: Athenaeum's deploy key is read-only on
purpose, and this needed write access, so it gets its own key and its
own clone rather than upgrading a key that has no other reason to write
anything).

Every write is a brand-new, uniquely-named file — never an edit to an
existing note. That's what keeps a push conflict rare: git only has
something to reconcile when the same file changed on both sides, and a
fresh file never collides with whatever you're editing live in Obsidian.

write_note() is the general primitive (any folder, any filename, any
content — used directly by Hermes's write_vault_note tool for anything
from a tech plan to a meeting summary). write_finding() is CodebaseSearcher's
own specific caller, layering repo/topic/date naming on top of it.
"""

import os
import re
import subprocess
from datetime import date
from pathlib import Path

from app.config import DATA_DIR, VAULT_FINDINGS_FOLDER, VAULT_REPO_URL, VAULT_SSH_KEY_PATH


def _ssh_env() -> dict:
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = f"ssh -i {VAULT_SSH_KEY_PATH} -o StrictHostKeyChecking=accept-new"
    return env


def _vault_path() -> Path:
    return Path(DATA_DIR) / "vault"


def _ensure_clone() -> Path:
    path = _vault_path()
    env = _ssh_env()
    if not path.exists():
        subprocess.run(["git", "clone", VAULT_REPO_URL, str(path)], env=env, check=True, capture_output=True)
    return path


def _pull(path: Path) -> None:
    subprocess.run(["git", "-C", str(path), "pull", "--ff-only"], env=_ssh_env(), check=True, capture_output=True)


def _push(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(path), "push"], env=_ssh_env(), capture_output=True, text=True)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "note"


class InvalidVaultPath(ValueError):
    pass


def _resolve_target(vault_root: Path, folder: str, filename: str) -> Path:
    """Rejects anything that would escape the vault clone — write_note's
    folder/filename ultimately come from an LLM tool call, not a trusted
    caller, so this can't just trust them to stay put."""
    if not filename or "/" in filename or "\\" in filename or filename in (".", ".."):
        raise InvalidVaultPath(f"Invalid filename: {filename!r}")
    if ".." in Path(folder).parts:
        raise InvalidVaultPath(f"Invalid folder: {folder!r}")

    target = (vault_root / folder / filename).resolve()
    vault_root_resolved = vault_root.resolve()
    if vault_root_resolved not in target.parents:
        raise InvalidVaultPath(f"Resolved path {target} escapes the vault root")
    return target


def write_note(folder: str, filename: str, content: str, commit_summary: str) -> str:
    """The general primitive: write `content` to `folder/filename` in the
    vault, commit, push (one retry via pull --rebase on a rejected push).
    Auto-suffixes on a same-name collision rather than overwriting — see
    module docstring. Returns the note's path relative to the vault root."""
    path = _ensure_clone()
    _pull(path)

    target = _resolve_target(path, folder, filename)
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        stem, suffix = target.stem, target.suffix
        n = 2
        while target.exists():
            target = target.with_name(f"{stem}-{n}{suffix}")
            n += 1

    target.write_text(content)

    # path.resolve(), not path — target is already resolved (symlinks
    # followed), and on macOS /tmp -> /private/tmp makes the unresolved
    # and resolved forms mismatch for relative_to() (confirmed by a local
    # test against a real bare-repo vault before this was added).
    rel_path = str(target.relative_to(path.resolve()))
    subprocess.run(["git", "-C", str(path), "add", rel_path], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", commit_summary],
        check=True, capture_output=True,
    )

    result = _push(path)
    if result.returncode != 0:
        # Almost certainly someone else's commit landed in between our
        # pull and our push — rebase onto it and try once more. Our own
        # change is an isolated new file, so this should never actually
        # conflict in practice.
        subprocess.run(["git", "-C", str(path), "pull", "--rebase"], env=_ssh_env(), check=True, capture_output=True)
        retry = _push(path)
        if retry.returncode != 0:
            raise RuntimeError(f"Failed to push note after retry: {retry.stderr}")

    return rel_path


def write_finding(repo: str, question: str | None, markdown_body: str) -> str:
    """Writes a codebase-research finding — a folder per repo, filename
    from the question/topic and date."""
    repo_folder_name = _slugify(repo)
    topic = _slugify(question) if question else "overview"
    filename = f"{topic}-{date.today().isoformat()}.md"
    folder = f"{VAULT_FINDINGS_FOLDER}/{repo_folder_name}"
    return write_note(folder, filename, markdown_body, commit_summary=f"Codebase findings: {repo} — {topic}")
