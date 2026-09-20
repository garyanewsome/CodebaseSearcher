"""Chroma, embedded in-process — same pattern Athenaeum already uses for
the vault, not a new database to stand up and operate. One collection
per repo, named after it, so re-indexing a repo is just "drop and
recreate its own collection," never touching any other repo's data."""

import re
from pathlib import Path

import chromadb

from app.config import DATA_DIR
from app.embeddings import encode_texts

_client = None


def _get_client():
    global _client
    if _client is None:
        chroma_dir = Path(DATA_DIR) / "chroma"
        chroma_dir.mkdir(parents=True, exist_ok=True)
        _client = chromadb.PersistentClient(path=str(chroma_dir))
    return _client


def _collection_name(repo: str) -> str:
    # Chroma collection names are restricted (alnum, -, _, 3-63 chars) —
    # a repo name is already close to that shape, this just guarantees it.
    safe = re.sub(r"[^a-zA-Z0-9_-]", "-", repo).strip("-")[:55]
    return f"repo-{safe or 'unnamed'}"


def replace_repo_index(repo: str, chunks: list[dict]) -> int:
    """Drops and rebuilds this repo's collection from scratch — simpler
    and safer than diffing which chunks changed, and cheap enough given
    this only runs when the repo's commit hash actually moved."""
    client = _get_client()
    name = _collection_name(repo)
    try:
        client.delete_collection(name)
    except Exception:
        pass
    if not chunks:
        return 0

    collection = client.create_collection(name)
    contents = [c["content"] for c in chunks]
    embeddings = encode_texts(contents)
    ids = [f"{repo}::{i}" for i in range(len(chunks))]
    metadatas = [{"file_path": c["file_path"]} for c in chunks]
    collection.add(
        ids=ids,
        embeddings=embeddings.tolist(),
        documents=contents,
        metadatas=metadatas,
    )
    return len(chunks)


def search_repo(repo: str, query: str, top_k: int = 8) -> list[dict]:
    client = _get_client()
    name = _collection_name(repo)
    try:
        collection = client.get_collection(name)
    except Exception:
        return []
    query_embedding = encode_texts([query])[0].tolist()
    result = collection.query(query_embeddings=[query_embedding], n_results=top_k)
    hits = []
    docs = result.get("documents") or [[]]
    metas = result.get("metadatas") or [[]]
    dists = result.get("distances") or [[]]
    for doc, meta, dist in zip(docs[0], metas[0], dists[0]):
        hits.append({"content": doc, "file_path": meta.get("file_path", ""), "distance": dist})
    return hits


def delete_repo_index(repo: str) -> None:
    client = _get_client()
    try:
        client.delete_collection(_collection_name(repo))
    except Exception:
        pass
