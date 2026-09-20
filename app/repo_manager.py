"""Clone/pull a source repo to research, and track whether it's already
indexed at its current commit so re-asking about an unchanged repo
doesn't re-embed it from scratch every time."""

import shutil
import subprocess
from pathlib import Path

from app.config import DATA_DIR, GITHUB_SSH_KEY_PATH, GITHUB_USER


def _ssh_env() -> dict:
    import os

    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = f"ssh -i {GITHUB_SSH_KEY_PATH} -o StrictHostKeyChecking=accept-new"
    return env


def repos_dir() -> Path:
    d = Path(DATA_DIR) / "repos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def repo_path(repo: str) -> Path:
    return repos_dir() / repo


def _indexed_commit_file(repo: str) -> Path:
    return repo_path(repo).parent / f".{repo}.indexed_commit"


def clone_or_pull(repo: str) -> tuple[Path, str]:
    """Returns (local_path, current_head_commit)."""
    path = repo_path(repo)
    env = _ssh_env()
    if path.exists():
        subprocess.run(["git", "-C", str(path), "fetch", "--depth", "1", "origin"], env=env, check=True, capture_output=True)
        subprocess.run(["git", "-C", str(path), "reset", "--hard", "origin/HEAD"], check=True, capture_output=True)
    else:
        url = f"git@github.com:{GITHUB_USER}/{repo}.git"
        subprocess.run(
            ["git", "clone", "--depth", "1", url, str(path)],
            env=env, check=True, capture_output=True,
        )
    result = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    return path, result.stdout.strip()


def get_indexed_commit(repo: str) -> str | None:
    f = _indexed_commit_file(repo)
    return f.read_text().strip() if f.exists() else None


def set_indexed_commit(repo: str, commit: str) -> None:
    _indexed_commit_file(repo).write_text(commit)


def delete_repo(repo: str) -> None:
    path = repo_path(repo)
    if path.exists():
        shutil.rmtree(path)
    marker = _indexed_commit_file(repo)
    if marker.exists():
        marker.unlink()
