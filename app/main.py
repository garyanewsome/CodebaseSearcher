import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app import embeddings, repo_manager, vault_writer, vector_store
from app.chunking import chunk_repo

logger = logging.getLogger("codebase-searcher")

app = FastAPI(title="CodebaseSearcher")


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
    if not hits:
        lines += ["No relevant snippets found for this question.", ""]
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

    hits = vector_store.search_repo(cache_key, request.question, top_k=8) if request.question else []
    display_name = _display_name(cache_key)
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
            "note_path": None,
            "note_error": str(exc),
        }

    return {"repo": display_name, "commit": commit, "hits": hits, "note_path": note_path}


@app.post("/forget")
def forget(request: ForgetRequest):
    repo = request.repo.strip()
    if not repo:
        raise HTTPException(status_code=400, detail="repo is required")
    cache_key = repo_manager.delete_repo(repo)
    vector_store.delete_repo_index(cache_key)
    return {"status": "ok"}
