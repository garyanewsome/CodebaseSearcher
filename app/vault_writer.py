"""Writes CodebaseSearcher's findings notes into the Obsidian vault.

The vault's git clone, deploy key and write limits belong to the
vault-keeper service (see its README); this only formats a finding's
location and asks vault-keeper to write it. Keeping the clone in one place
is the point: this service used to own it and Hermes borrowed it.
"""

import re
from datetime import date

import httpx

from app.config import VAULT_FINDINGS_FOLDER, VAULT_KEEPER_URL


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "note"


def write_finding(repo: str, question: str | None, markdown_body: str) -> str:
    """Writes a codebase-research finding -- a folder per repo, filename
    from the question/topic and date. Returns the note's vault path."""
    repo_folder_name = _slugify(repo)
    topic = _slugify(question) if question else "overview"
    response = httpx.post(
        f"{VAULT_KEEPER_URL}/write_note",
        json={
            "folder": f"{VAULT_FINDINGS_FOLDER}/{repo_folder_name}",
            "filename": f"{topic}-{date.today().isoformat()}.md",
            "content": markdown_body,
            "source": "codebase-searcher",
            "kind": "finding",
        },
        timeout=60.0,
    )
    if response.status_code != 200:
        # vault-keeper's detail says why (policy refusal, git trouble).
        raise RuntimeError(f"vault-keeper refused or failed ({response.status_code}): {response.text[:300]}")
    return response.json()["note_path"]
