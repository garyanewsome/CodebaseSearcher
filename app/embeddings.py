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


# Confirmed live on the homelab GPU: Jina's default max_seq_length is 8192
# (it's built for long-context code), and a single ~6500-char chunk — well
# under CHUNK_HARD_MAX_CHARS — tokenized to ~3000+ tokens and needed a
# 3.43 GiB attention tensor on its own (batch x heads x seq x seq scaling),
# OOMing a 12GB card even with nothing else resident on it. The char-based
# chunk cap in chunking.py bounds embedding *quality* (don't blend
# unrelated code into one vector) but is a poor proxy for the actual
# memory driver, which is token count. Truncating here is the real
# safety net: 1024 tokens keeps peak attention memory in the tens of MB
# regardless of how a chunk tokenizes, at the cost of silently dropping
# anything past that in a rare oversized chunk.
MAX_SEQ_LENGTH = 1024


@lru_cache(maxsize=1)
def _model():
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(EMBEDDING_MODEL, trust_remote_code=True, device=_resolve_device())
    model.max_seq_length = MAX_SEQ_LENGTH
    return model


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
