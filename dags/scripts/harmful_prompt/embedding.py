"""Optional sentence-transformers adapter, imported only when actually used.

Embeddings are the one part of this pipeline that needs a heavyweight optional
dependency. Importing it lazily keeps the base installation small and keeps the
test suite from ever downloading a model: tests inject their own embedder, and
nothing here is imported unless semantic deduplication actually runs.
"""
from __future__ import annotations

from typing import Sequence

INSTALL_HINT = (
    "sentence-transformers is not installed. Install the optional embedding "
    "dependencies with: pip install -r requirements-semantic.txt"
)


class SentenceTransformerEmbedder:
    """Encodes text with a sentence-transformers model."""

    provider = "sentence_transformers"

    def __init__(self, model: str, batch_size: int = 16) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as error:
            raise RuntimeError(INSTALL_HINT) from error
        self.model = model
        self.batch_size = batch_size
        self._encoder = SentenceTransformer(model)

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self._encoder.encode(
            list(texts),
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        return [[float(value) for value in vector] for vector in vectors]
