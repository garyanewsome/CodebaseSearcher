import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app import embeddings, jobs, repo_manager, vault_writer, vector_store
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


def _noop_progress(stage, detail=None, done=None, total=None):
    pass


def _do_research(repo: str, question: str | None, progress=_noop_progress) -> dict:
    """The whole research flow. `progress(stage, detail, done, total)` reports
    where it is (see jobs.py); a plain /research call passes a no-op."""
    repo = repo.strip()
    if not repo:
        raise HTTPException(status_code=400, detail="repo is required")

    try:
        _, cache_key = repo_manager.resolve_repo(repo)
    except Exception as exc:
        logger.exception("Couldn't resolve '%s'", repo)
        raise HTTPException(status_code=502, detail=f"Failed to clone/pull '{repo}': {exc}")
    display_name = _display_name(cache_key)

    # Held while the repo is fetched and indexed, so a retry or a second
    # question about a repo that is mid-index waits for that run and then
    # finds it indexed, instead of deleting and rebuilding the same index
    # underneath it.
    lock = repo_manager.repo_lock(cache_key)
    if lock.locked():
        progress("waiting", f"Waiting for another analysis of {display_name} to finish…")
    with lock:
        progress("cloning", f"Fetching {display_name}…")
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
            progress("chunking", f"Reading {display_name}'s source files…")
            chunks = chunk_repo(path)
            logger.info("Indexing %s at %s: %d chunks", cache_key, commit[:8], len(chunks))
            progress("embedding", f"Indexing {display_name}: embedding", 0, len(chunks))
            count = vector_store.replace_repo_index(
                cache_key, chunks, progress=lambda done, total: progress("embedding", None, done, total)
            )
            repo_manager.set_indexed_commit(cache_key, commit)
            logger.info("Re-indexed %s at %s: %d chunks", cache_key, commit[:8], count)

    is_overview = not question
    search_query = question or OVERVIEW_QUERY
    progress("searching", f"Searching {display_name} for relevant code…")
    hits = vector_store.search_repo(cache_key, search_query, top_k=8)

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

    markdown = _build_findings_markdown(display_name, question, hits)

    progress("writing", "Writing the findings note to the vault…")
    try:
        note_path = vault_writer.write_finding(display_name, question, markdown)
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


@app.post("/research")
def research(request: ResearchRequest):
    """Blocking version: returns when the whole thing is done. Fine for a
    small repo; for a big one use /research/start, which doesn't hold a
    request open for tens of minutes."""
    return _do_research(request.repo, request.question)


@app.post("/research/start")
def research_start(request: ResearchRequest):
    """Starts the research in the background (or joins one already running for
    the same repo and question) and returns a job to poll."""
    if not request.repo.strip():
        raise HTTPException(status_code=400, detail="repo is required")
    job = jobs.start(
        request.repo,
        request.question,
        lambda progress: _do_research(request.repo, request.question, progress),
    )
    return job.snapshot()


@app.get("/research/jobs/{job_id}")
def research_job(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job (the service may have restarted); start it again")
    return job.snapshot()


@app.post("/forget")
def forget(request: ForgetRequest):
    repo = request.repo.strip()
    if not repo:
        raise HTTPException(status_code=400, detail="repo is required")
    cache_key = repo_manager.delete_repo(repo)
    vector_store.delete_repo_index(cache_key)
    return {"status": "ok"}
