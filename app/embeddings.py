"""Swappable embedding wrapper — same shape as skunkworks_ai_rag's
embeddings.py, just pointed at a code-specialized model. GPU, with an
explicit unload: a real CPU run took 552.7s to embed a mid-size repo
(and blew well past a normal tool-call timeout), so this runs bare-metal
on the homelab host and shares the RTX 3060 with Ollama/Iris via the same
handoff pattern already used for generate_image — Hermes unloads Ollama
before calling here, and calls unload_model() (below) right after, so the
embedding model doesn't sit resident contending with the next chat turn."""

from functools import lru_cache
from typing import Sequence

import numpy as np

from app.config import EMBEDDING_MODEL


def _resolve_device() -> str:
    # Auto-detect rather than hardcode "cuda" — the homelab host has a GPU,
    # but local dev (Mac, no CUDA) still needs this to fall back to CPU.
    import torch

    return "cuda" if torch.cuda.is_available() else "cpu"


@lru_cache(maxsize=1)
def _model():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(EMBEDDING_MODEL, trust_remote_code=True, device=_resolve_device())


def unload_model() -> None:
    """The other half of the handoff — called via /unload right after a
    /research call returns, mirroring Iris's own explicit unload route."""
    _model.cache_clear()
    import gc

    gc.collect()
    try:
        import torch

        torch.cuda.empty_cache()
    except Exception:
        pass


def embedding_dim() -> int:
    return int(_model().get_sentence_embedding_dimension())


def encode_texts(texts: Sequence[str], batch_size: int = 8) -> np.ndarray:
    if not texts:
        return np.zeros((0, embedding_dim()), dtype=np.float32)
    model = _model()
    emb = model.encode(
        list(texts),
        batch_size=batch_size,
        show_progress_bar=len(texts) > 20,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )
    return np.asarray(emb, dtype=np.float32)
