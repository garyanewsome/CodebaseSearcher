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


# Texts embedded (and stored) per step. Small enough that progress moves
# every few seconds and a crash loses little, big enough to keep the GPU busy.
EMBED_SLICE = 128


def _drop(client, name: str) -> None:
    try:
        client.delete_collection(name)
    except Exception:
        pass


def replace_repo_index(repo: str, chunks: list[dict], progress=None) -> int:
    """Rebuilds this repo's collection from scratch, only ever touching its
    own data.

    The new index is built under a temporary name, slice by slice, and only
    swapped in once complete. Two things that went wrong before: Chroma
    refuses a single add() larger than its max batch size (5,461 here), so a
    repo with more chunks than that embedded for 20+ minutes and then failed
    at the very last step; and the old index was dropped before the new one
    existed, so a failure left the repo with no index at all.

    `progress(done, total)` is called after every slice."""
    client = _get_client()
    name = _collection_name(repo)
    building = f"{name}-building"
    _drop(client, building)  # a leftover from a run that was killed midway

    total = len(chunks)
    if not chunks:
        _drop(client, name)
        return 0

    collection = client.create_collection(building)
    step = max(1, min(EMBED_SLICE, client.get_max_batch_size()))
    try:
        for start in range(0, total, step):
            part = chunks[start : start + step]
            contents = [c["content"] for c in part]
            embeddings = encode_texts(contents, show_progress_bar=False)
            collection.add(
                ids=[f"{repo}::{start + i}" for i in range(len(part))],
                embeddings=embeddings.tolist(),
                documents=contents,
                metadatas=[{"file_path": c["file_path"]} for c in part],
            )
            if progress:
                progress(start + len(part), total)
    except Exception:
        _drop(client, building)
        raise

    _drop(client, name)
    collection.modify(name=name)
    return total


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
