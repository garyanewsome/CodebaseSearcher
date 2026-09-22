"""Clone/pull a source repo to research, and track whether it's already
indexed at its current commit so re-asking about an unchanged repo
doesn't re-embed it from scratch every time.

`repo` accepts several shapes, since a chat request might name it any
of these ways:
  - a bare name ("Hermes") — assumed to be the account's own repo,
    cloned over SSH with the account-level key (needed for private repos)
  - "owner/repo" ("michaelwillis/dragonfly-reverb") — someone else's repo
  - a full URL (https://github.com/... or git@github.com:...)
Anything not under the account's own GitHub user clones over plain HTTPS,
no auth needed — that's the common case for "hey, look at this public
repo," and it means a typo'd or someone-else's repo fails with a normal
git error instead of a confusing auth failure against the wrong owner.
"""

import re
import shutil
import subprocess
from pathlib import Path

from app.config import DATA_DIR, GITHUB_SSH_KEY_PATH, GITHUB_USER


def _ssh_env() -> dict:
    import os

    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = f"ssh -i {GITHUB_SSH_KEY_PATH} -o StrictHostKeyChecking=accept-new"
    return env


def resolve_repo(repo: str) -> tuple[str, str]:
    """Returns (clone_url, cache_key). cache_key is filesystem/collection
    safe and disambiguates same-named repos from different owners."""
    value = repo.strip().rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]

    if value.startswith("https://github.com/"):
        owner_repo = value[len("https://github.com/"):]
    elif value.startswith("http://github.com/"):
        owner_repo = value[len("http://github.com/"):]
    elif value.startswith("git@github.com:"):
        owner_repo = value[len("git@github.com:"):]
    elif "/" in value:
        owner_repo = value
    else:
        owner_repo = f"{GITHUB_USER}/{value}"

    owner_repo = owner_repo.strip("/")
    if "/" not in owner_repo:
        raise ValueError(f"Couldn't parse a GitHub owner/repo from {repo!r}")
    owner, name = owner_repo.split("/", 1)
    name = re.sub(r"[^a-zA-Z0-9_.-]", "", name)
    owner = re.sub(r"[^a-zA-Z0-9_.-]", "", owner)

    if owner.lower() == GITHUB_USER.lower():
        clone_url = f"git@github.com:{owner}/{name}.git"
        cache_key = name
    else:
        clone_url = f"https://github.com/{owner}/{name}.git"
        cache_key = f"{owner}__{name}"
    return clone_url, cache_key


def repos_dir() -> Path:
    d = Path(DATA_DIR) / "repos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def repo_path(cache_key: str) -> Path:
    return repos_dir() / cache_key


def _indexed_commit_file(cache_key: str) -> Path:
    return repo_path(cache_key).parent / f".{cache_key}.indexed_commit"


def _overview_note_file(cache_key: str) -> Path:
    return repo_path(cache_key).parent / f".{cache_key}.overview_note"


def clone_or_pull(repo: str) -> tuple[Path, str, str]:
    """Returns (local_path, current_head_commit, cache_key)."""
    clone_url, cache_key = resolve_repo(repo)
    path = repo_path(cache_key)

    if clone_url.startswith("git@github.com:"):
        env = _ssh_env()
    else:
        import os

        env = os.environ.copy()  # plain HTTPS, no special SSH env needed

    if path.exists():
        subprocess.run(["git", "-C", str(path), "fetch", "--depth", "1", "origin"], env=env, check=True, capture_output=True)
        subprocess.run(["git", "-C", str(path), "reset", "--hard", "origin/HEAD"], check=True, capture_output=True)
    else:
        subprocess.run(
            ["git", "clone", "--depth", "1", clone_url, str(path)],
            env=env, check=True, capture_output=True,
        )
    result = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    return path, result.stdout.strip(), cache_key


def get_indexed_commit(cache_key: str) -> str | None:
    f = _indexed_commit_file(cache_key)
    return f.read_text().strip() if f.exists() else None


def set_indexed_commit(cache_key: str, commit: str) -> None:
    _indexed_commit_file(cache_key).write_text(commit)


def get_overview_note(cache_key: str, commit: str) -> str | None:
    """Returns the existing general-overview note's vault path for this
    exact commit, if one was already written — None if the repo changed
    since, or no overview has been written yet. Guards against a real
    failure mode: a retry after a stalled/timed-out turn (the model's
    reply for the first attempt never got far enough to be persisted, so
    it has no memory it already researched this) re-calls research_repo
    for the same unchanged repo, and without this, that silently writes a
    second byte-identical overview note — confirmed live."""
    f = _overview_note_file(cache_key)
    if not f.exists():
        return None
    stored_commit, _, note_path = f.read_text().partition("\n")
    return note_path if stored_commit == commit and note_path else None


def set_overview_note(cache_key: str, commit: str, note_path: str) -> None:
    _overview_note_file(cache_key).write_text(f"{commit}\n{note_path}")


def delete_repo(repo: str) -> str:
    """Returns the cache_key that was deleted, so the caller can also
    drop the matching vector index."""
    _, cache_key = resolve_repo(repo)
    path = repo_path(cache_key)
    if path.exists():
        shutil.rmtree(path)
    marker = _indexed_commit_file(cache_key)
    if marker.exists():
        marker.unlink()
    overview_marker = _overview_note_file(cache_key)
    if overview_marker.exists():
        overview_marker.unlink()
    return cache_key
