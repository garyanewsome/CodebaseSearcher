import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app import embeddings, repo_manager, vault_policy, vault_writer, vector_store
from app.chunking import chunk_repo

logger = logging.getLogger("codebase-searcher")

app = FastAPI(title="CodebaseSearcher")

# When no question is given, search for this instead of skipping search
# entirely — a "first look" ask still needs *something* for Hermes's own
# model to actually read and summarize, not just a bare "indexed" message
# with zero repo content in it.
OVERVIEW_QUERY = "what this project does: its main purpose, architecture, and key components"


class ResearchRequest(BaseModel):
    repo: str
    question: str | None = None


class ForgetRequest(BaseModel):
    repo: str


class WriteNoteRequest(BaseModel):
    folder: str
    filename: str
    content: str


class ReadNoteRequest(BaseModel):
    path: str  # vault-relative path of a .md note


class SuggestEditRequest(BaseModel):
    path: str  # vault-relative path of the existing note the change is for
    proposed_content: str  # the full text the note should have after the change
    reason: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/unload")
def unload():
    # Hermes calls this right after /research returns, same handoff shape
    # as Iris's own /unload — frees the GPU for Ollama's next chat turn.
    embeddings.unload_model()
    return {"status": "ok"}


def _display_name(cache_key: str) -> str:
    # "owner__name" for a third-party repo -> just "name" for note titles;
    # the user's own repos are already a bare name as their cache_key.
    return cache_key.split("__", 1)[1] if "__" in cache_key else cache_key


def _build_findings_markdown(display_name: str, question: str | None, hits: list[dict]) -> str:
    lines = [f"# Codebase findings: {display_name}", ""]
    if question:
        lines += [f"**Question:** {question}", ""]
    else:
        lines += ["General overview — representative snippets, no specific question asked.", ""]
    if not hits:
        lines += ["No relevant snippets found.", ""]
    else:
        lines += ["## Relevant snippets", ""]
        for hit in hits:
            lines += [f"### `{hit['file_path']}`", "", "```", hit["content"], "```", ""]
    return "\n".join(lines)


@app.post("/research")
def research(request: ResearchRequest):
    repo = request.repo.strip()
    if not repo:
        raise HTTPException(status_code=400, detail="repo is required")

    try:
        path, commit, cache_key = repo_manager.clone_or_pull(repo)
    except Exception as exc:
        # A bare HTTPException here previously left the actual git error
        # (auth failure, network blip, bad ref) visible only in the HTTP
        # response, not in the service's own logs — undiagnosable from
        # journalctl alone. Log it too.
        logger.exception("Failed to clone/pull '%s'", repo)
        raise HTTPException(status_code=502, detail=f"Failed to clone/pull '{repo}': {exc}")

    if repo_manager.get_indexed_commit(cache_key) != commit:
        chunks = chunk_repo(path)
        count = vector_store.replace_repo_index(cache_key, chunks)
        repo_manager.set_indexed_commit(cache_key, commit)
        logger.info("Re-indexed %s at %s: %d chunks", cache_key, commit[:8], count)

    is_overview = not request.question
    search_query = request.question or OVERVIEW_QUERY
    hits = vector_store.search_repo(cache_key, search_query, top_k=8)
    display_name = _display_name(cache_key)

    # Reuse an existing general-overview note for this exact commit rather
    # than writing another one — the real failure mode this guards against:
    # a stalled/timed-out turn leaves nothing persisted for the model to
    # remember it already looked, so a manual retry (or the model just
    # deciding to look again) calls /research a second time for the same
    # unchanged repo. Confirmed live: that produced two byte-identical
    # "overview" notes a few seconds apart. A real question always gets
    # its own note, even for a repeat commit — a distinct question is a
    # distinct ask, not a redundant one.
    if is_overview:
        existing = repo_manager.get_overview_note(cache_key, commit)
        if existing:
            return {
                "repo": display_name,
                "commit": commit,
                "hits": hits,
                "is_overview": is_overview,
                "note_path": existing,
                "note_reused": True,
            }

    markdown = _build_findings_markdown(display_name, request.question, hits)

    try:
        note_path = vault_writer.write_finding(display_name, request.question, markdown)
    except Exception as exc:
        # The search itself succeeded even if the vault write failed —
        # still worth returning the snippets rather than a bare 500 for a
        # problem in an unrelated system (git push, deploy key, ...).
        logger.exception("Failed to write findings note for %s", cache_key)
        return {
            "repo": display_name,
            "commit": commit,
            "hits": hits,
            "is_overview": is_overview,
            "note_path": None,
            "note_error": str(exc),
        }

    if is_overview:
        repo_manager.set_overview_note(cache_key, commit, note_path)

    return {
        "repo": display_name,
        "commit": commit,
        "hits": hits,
        "is_overview": is_overview,
        "note_path": note_path,
    }


@app.post("/write_note")
def write_note(request: WriteNoteRequest):
    # General-purpose: unlike /research's findings notes, this takes
    # whatever folder/filename/content Hermes's own chat model decides on
    # — e.g. saving a tech plan drafted in conversation, not something
    # this service itself researched. vault_writer validates the path
    # can't escape the vault clone.
    try:
        folder = vault_policy.check_folder(request.folder)
        filename = vault_policy.check_filename(request.filename)
        if not request.content.strip():
            raise vault_policy.PolicyViolation("content is required")
        content = vault_policy.stamp(request.content, "note")
        vault_policy.check_size(content)
        vault_policy.check_rate()
    except vault_policy.PolicyViolation as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    try:
        note_path = vault_writer.write_note(
            folder, filename, content, commit_summary=f"Hermes: add note {folder}/{filename}"
        )
    except vault_writer.InvalidVaultPath as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Failed to write note to %s/%s", folder, filename)
        raise HTTPException(status_code=502, detail=f"Failed to write note: {exc}")

    vault_policy.record("note", note_path, len(content.encode("utf-8")))
    return {"note_path": note_path}


@app.post("/read_note")
def read_note(request: ReadNoteRequest):
    # The whole text of one note, so an edit can be proposed against what is
    # really there rather than a search snippet. Capped at the write limit:
    # anything bigger could not be sent back as a suggestion anyway.
    try:
        path = vault_policy.check_readable(request.path)
        content = vault_writer.read_note(path)
    except vault_policy.PolicyViolation as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except vault_writer.InvalidVaultPath as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if content is None:
        raise HTTPException(status_code=404, detail=f"No note at {path!r}")
    size = len(content.encode("utf-8"))
    if size > vault_policy.VAULT_MAX_NOTE_BYTES:
        raise HTTPException(
            status_code=403,
            detail=f"{path!r} is {size} bytes, over the {vault_policy.VAULT_MAX_NOTE_BYTES}-byte limit for notes Hermes works on",
        )
    return {"path": path, "content": content}


@app.post("/suggest_edit")
def suggest_edit(request: SuggestEditRequest):
    # Hermes never edits an existing note. It files a suggestion note under
    # VAULT_SUGGESTIONS_FOLDER holding a unified diff against the current
    # text plus the full proposed text, and the user decides in Obsidian.
    try:
        vault_policy.check_rate()
        current = vault_writer.read_note(request.path)
        if current is None:
            raise vault_policy.PolicyViolation(
                f"No note at {request.path!r}. To create a new note use write_vault_note."
            )
        body = vault_policy.build_suggestion(request.path, current, request.proposed_content, request.reason)
        body = vault_policy.stamp(body, "suggestion")
        vault_policy.check_size(body)
        folder, filename = vault_policy.suggestion_location(request.path)
    except vault_policy.PolicyViolation as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except vault_writer.InvalidVaultPath as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        note_path = vault_writer.write_note(
            folder, filename, body, commit_summary=f"Hermes: suggest edit to {request.path}"
        )
    except Exception as exc:
        logger.exception("Failed to write suggestion for %s", request.path)
        raise HTTPException(status_code=502, detail=f"Failed to write suggestion: {exc}")

    vault_policy.record("suggestion", note_path, len(body.encode("utf-8")), reason=request.reason)
    return {"note_path": note_path}


@app.post("/forget")
def forget(request: ForgetRequest):
    repo = request.repo.strip()
    if not repo:
        raise HTTPException(status_code=400, detail="repo is required")
    cache_key = repo_manager.delete_repo(repo)
    vector_store.delete_repo_index(cache_key)
    return {"status": "ok"}
