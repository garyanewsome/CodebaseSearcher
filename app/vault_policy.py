"""Limits on what Hermes may write into the vault.

vault_writer.write_note() already only ever creates new files and refuses
paths that escape the clone, but it will happily put a file anywhere
inside it -- including `.obsidian/plugins/` (Obsidian runs that JS) or
`.git/hooks/`. And its caller is an LLM tool call, so nothing else bounds
how much, how often or where it writes.

These checks sit in front of the two endpoints Hermes's chat model can
reach (/write_note and /suggest_edit), not inside write_note() itself, so
CodebaseSearcher's own findings notes (write_finding) are unaffected.
Enforcement lives here, next to the deploy key, rather than in Hermes: a
prompt telling the model to behave is not a limit.

Every accepted write lands in an append-only JSONL audit log, which also
backs the daily cap.
"""

import difflib
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from app.config import (
    DATA_DIR,
    VAULT_MAX_NOTE_BYTES,
    VAULT_MAX_WRITES_PER_DAY,
    VAULT_SUGGESTIONS_FOLDER,
    VAULT_WRITE_ROOTS,
)


class PolicyViolation(ValueError):
    """The write is refused. The message is shown to the model, so it says
    what is allowed, not just what was wrong."""


def _audit_path() -> Path:
    return Path(DATA_DIR) / "vault-audit.jsonl"


def check_folder(folder: str) -> str:
    """Returns the normalised folder, or raises. No path component may start
    with '.', and if VAULT_WRITE_ROOTS is set the folder must be under one."""
    parts = [p for p in PurePosixPath(folder.strip().strip("/")).parts if p not in ("", ".")]
    if any(p.startswith(".") for p in parts):
        raise PolicyViolation(f"Folders starting with '.' are off limits: {folder!r}")
    normalised = "/".join(parts)
    if not VAULT_WRITE_ROOTS:
        return normalised
    for root in VAULT_WRITE_ROOTS:
        if normalised == root or normalised.startswith(root + "/"):
            return normalised
    allowed = ", ".join(repr(r) for r in VAULT_WRITE_ROOTS)
    raise PolicyViolation(
        f"Hermes may only create notes under {allowed}, not {folder!r}. "
        f"To change an existing note, use suggest_vault_edit; the user reviews it."
    )


def check_readable(path: str) -> str:
    """Reads get the same dot-path rule as writes (no .git, no .obsidian),
    but no folder scope: the whole vault is readable."""
    parts = [p for p in PurePosixPath(path.strip().strip("/")).parts if p not in ("", ".")]
    if not parts or any(p.startswith(".") for p in parts):
        raise PolicyViolation(f"Hidden paths can't be read: {path!r}")
    if PurePosixPath(parts[-1]).suffix.lower() != ".md":
        raise PolicyViolation(f"Only .md notes can be read, not {path!r}")
    return "/".join(parts)


def check_filename(filename: str) -> str:
    """Notes are markdown. No extension means .md; any other extension is refused."""
    filename = filename.strip()
    if filename.startswith("."):
        raise PolicyViolation(f"Filenames starting with '.' are off limits: {filename!r}")
    suffix = Path(filename).suffix
    if not suffix:
        return filename + ".md"
    if suffix.lower() != ".md":
        raise PolicyViolation(f"Only .md notes may be written, not {filename!r}")
    return filename


def check_size(content: str) -> None:
    size = len(content.encode("utf-8"))
    if size > VAULT_MAX_NOTE_BYTES:
        raise PolicyViolation(
            f"Note is {size} bytes; the limit is {VAULT_MAX_NOTE_BYTES}. Split it or shorten it."
        )


def _read_audit() -> list[dict]:
    path = _audit_path()
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def check_rate() -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    used = sum(1 for e in _read_audit() if e.get("at", "").startswith(today))
    if used >= VAULT_MAX_WRITES_PER_DAY:
        raise PolicyViolation(
            f"Daily write limit reached ({VAULT_MAX_WRITES_PER_DAY} notes today, UTC). Try again tomorrow."
        )


def record(kind: str, path: str, size: int, reason: str | None = None) -> None:
    entry = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": kind,
        "path": path,
        "bytes": size,
        "reason": reason,
    }
    log = _audit_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def stamp(content: str, kind: str) -> str:
    """Marks the note as machine-written so it is easy to find in Obsidian.
    Adds to existing frontmatter if there is some, rather than a second block."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fields = f"source: hermes\nhermes_kind: {kind}\nhermes_created: {now}\n"
    if content.startswith("---\n"):
        return "---\n" + fields + content[4:]
    return f"---\n{fields}---\n\n{content}"


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "note"


def suggestion_location(target_path: str) -> tuple[str, str]:
    """Folder and filename for a suggestion about `target_path`."""
    stem = _slugify(PurePosixPath(target_path).stem)
    day = datetime.now(timezone.utc).date().isoformat()
    return VAULT_SUGGESTIONS_FOLDER, f"{stem}-{day}.md"


def build_suggestion(target_path: str, current: str, proposed: str, reason: str) -> str:
    """A suggestion note: why, a unified diff to review quickly, and the full
    proposed text to copy from. The diff is the review tool, the full text is
    the apply tool; either alone is awkward."""
    diff = "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            proposed.splitlines(keepends=True),
            fromfile=f"a/{target_path}",
            tofile=f"b/{target_path}",
        )
    )
    if not diff:
        raise PolicyViolation("The proposed text is identical to the note; nothing to suggest.")
    # A fence longer than any run of backticks inside, so quoted code cannot close it early.
    fence = "`" * max(3, max((len(m) for m in re.findall(r"`+", current + proposed)), default=0) + 1)
    return (
        f"---\n"
        f"target: \"[[{PurePosixPath(target_path).with_suffix('')}]]\"\n"
        f"status: pending\n"
        f"---\n\n"
        f"# Suggested edit to {target_path}\n\n"
        f"**Why:** {reason.strip()}\n\n"
        f"To accept, replace the note's text with the proposal below, then delete this file. "
        f"To reject, delete this file. Nothing was changed in the original.\n\n"
        f"## Diff\n\n{fence}diff\n{diff}{fence}\n\n"
        f"## Full proposed text\n\n{fence}markdown\n{proposed.rstrip(chr(10))}\n{fence}\n"
    )
