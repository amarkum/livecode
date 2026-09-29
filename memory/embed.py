from __future__ import annotations

import logging
import struct
import threading
from typing import Callable, Sequence

import numpy as np
from sklearn.feature_extraction.text import HashingVectorizer
from sklearn.preprocessing import normalize

logger = logging.getLogger(__name__)

EmbedFn = Callable[[list[str]], list[list[float]]]

EMBED_DIMS = 384
_vectorizer: HashingVectorizer | None = None
_lock = threading.Lock()

def pack_embedding(vec: Sequence[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *[float(x) for x in vec])

def unpack_embedding(blob: bytes) -> np.ndarray:
    n = len(blob) // 4
    return np.array(struct.unpack(f"{n}f", blob), dtype=np.float32)

def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na <= 0 or nb <= 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))

def _get_vectorizer() -> HashingVectorizer:
    global _vectorizer
    if _vectorizer is None:
        with _lock:
            if _vectorizer is None:
                _vectorizer = HashingVectorizer(
                    n_features=EMBED_DIMS,
                    alternate_sign=False,
                    norm=None,
                    lowercase=True,
                    ngram_range=(1, 2),
                    analyzer="word",
                )
    return _vectorizer

def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    mat = _get_vectorizer().transform(texts)
    dense = mat.astype(np.float32).toarray()
    dense = normalize(dense, norm="l2", axis=1)
    return [row.tolist() for row in dense]

def default_embedder() -> EmbedFn:
    return embed_texts
