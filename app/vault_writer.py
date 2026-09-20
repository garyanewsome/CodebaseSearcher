"""Writes findings notes into the Obsidian vault via its own git clone —
deliberately separate from Athenaeum's read-only vault clone (see this
project's README for why: Athenaeum's deploy key is read-only on
purpose, and this needed write access, so it gets its own key and its
own clone rather than upgrading a key that has no other reason to write
anything).

Every write is a brand-new, uniquely-named file — never an edit to an
existing note. That's what keeps a push conflict rare: git only has
something to reconcile when the same file changed on both sides, and a
fresh file never collides with whatever you're editing live in Obsidian.
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


def write_finding(repo: str, question: str | None, markdown_body: str) -> str:
    """Writes a new note, commits, pushes (one retry via pull --rebase on
    a rejected push). Returns the note's path relative to the vault root."""
    path = _ensure_clone()
    _pull(path)

    # A folder per repo, not one flat pile — findings for the same repo
    # stay together as you research it over multiple sessions.
    repo_folder_name = _slugify(repo)
    folder = path / VAULT_FINDINGS_FOLDER / repo_folder_name
    folder.mkdir(parents=True, exist_ok=True)

    topic = _slugify(question) if question else "overview"
    filename = f"{topic}-{date.today().isoformat()}.md"
    # A second research call on the same repo/topic/day appends a suffix
    # rather than silently overwriting the earlier note — both stay.
    n = 2
    while (folder / filename).exists():
        filename = f"{topic}-{date.today().isoformat()}-{n}.md"
        n += 1

    file_path = folder / filename
    file_path.write_text(markdown_body)

    rel_path = str(Path(VAULT_FINDINGS_FOLDER) / repo_folder_name / filename)
    subprocess.run(["git", "-C", str(path), "add", rel_path], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", f"Codebase findings: {repo} — {topic}"],
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
            raise RuntimeError(f"Failed to push findings note after retry: {retry.stderr}")

    return rel_path
