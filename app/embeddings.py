"""Swappable embedding wrapper — same shape as skunkworks_ai_rag's
embeddings.py, just pointed at a code-specialized model. CPU by design:
this only ever runs as an on-demand batch job (indexing a repo when
asked about it), never on the interactive chat path, so there's no
reason to contend with Ollama/Iris for the homelab's one shared GPU."""

from functools import lru_cache
from typing import Sequence

import numpy as np

from app.config import EMBEDDING_MODEL


@lru_cache(maxsize=1)
def _model():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(EMBEDDING_MODEL, trust_remote_code=True, device="cpu")


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
