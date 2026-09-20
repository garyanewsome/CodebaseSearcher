import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app import repo_manager, vault_writer, vector_store
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


def _build_findings_markdown(repo: str, question: str | None, hits: list[dict]) -> str:
    lines = [f"# Codebase findings: {repo}", ""]
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
        path, commit = repo_manager.clone_or_pull(repo)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to clone/pull '{repo}': {exc}")

    if repo_manager.get_indexed_commit(repo) != commit:
        chunks = chunk_repo(path)
        count = vector_store.replace_repo_index(repo, chunks)
        repo_manager.set_indexed_commit(repo, commit)
        logger.info("Re-indexed %s at %s: %d chunks", repo, commit[:8], count)

    hits = vector_store.search_repo(repo, request.question, top_k=8) if request.question else []
    markdown = _build_findings_markdown(repo, request.question, hits)

    try:
        note_path = vault_writer.write_finding(repo, request.question, markdown)
    except Exception as exc:
        # The search itself succeeded even if the vault write failed —
        # still worth returning the snippets rather than a bare 500 for a
        # problem in an unrelated system (git push, deploy key, ...).
        logger.exception("Failed to write findings note for %s", repo)
        return {
            "repo": repo,
            "commit": commit,
            "hits": hits,
            "note_path": None,
            "note_error": str(exc),
        }

    return {"repo": repo, "commit": commit, "hits": hits, "note_path": note_path}


@app.post("/forget")
def forget(request: ForgetRequest):
    repo = request.repo.strip()
    if not repo:
        raise HTTPException(status_code=400, detail="repo is required")
    repo_manager.delete_repo(repo)
    vector_store.delete_repo_index(repo)
    return {"status": "ok"}
