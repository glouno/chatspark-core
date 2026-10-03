from __future__ import annotations

from collections.abc import Sequence

from chatspark.core.embeddings import DeterministicEmbeddingConfig, DeterministicHashEmbedder
from chatspark.embeddings.base import Embedder


class DeterministicEmbedder(Embedder):
    def __init__(
        self,
        model_name: str = "chatspark-deterministic-hash-v1",
        dimensions: int = 384,
        normalize_embeddings: bool = True,
        seed: str = "chatspark",
    ):
        self._base = DeterministicHashEmbedder(
            DeterministicEmbeddingConfig(
                model_name=model_name,
                dimensions=dimensions,
                normalize=normalize_embeddings,
                seed=seed,
            )
        )

    @property
    def model_name(self) -> str:
        return self._base.model_name

    @property
    def dimensions(self):
        return self._base.dimensions

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return self._base.embed_texts(texts)

    def embed_query_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return self._base.embed_query_texts(texts)

    def embed_document_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return self._base.embed_document_texts(texts)
