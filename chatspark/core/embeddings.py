from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class TextEmbedder(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def embed(self, text: str) -> list[float]: ...

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query_texts(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_document_texts(self, texts: Sequence[str]) -> list[list[float]]: ...


class ImageEmbedder(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def embed_image(self, path: str | Path) -> list[float]: ...

    def embed_images(self, paths: Sequence[str | Path]) -> list[list[float]]: ...


@dataclass(frozen=True)
class DeterministicEmbeddingConfig:
    model_name: str = "chatspark-deterministic-hash-v1"
    dimensions: int = 384
    normalize: bool = True
    seed: str = "chatspark"


class DeterministicHashEmbedder:
    """Small offline embedder for tests, local demos, and confidential dry-runs."""

    def __init__(self, config: DeterministicEmbeddingConfig | None = None):
        self._config = config or DeterministicEmbeddingConfig()
        if self._config.dimensions <= 0:
            raise ValueError("Deterministic embedding dimensions must be positive.")

    @property
    def model_name(self) -> str:
        return self._config.model_name

    @property
    def dimensions(self) -> int:
        return self._config.dimensions

    def embed(self, text: str) -> list[float]:
        return self._embed_one(text, role="text")

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed_texts(texts)

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]

    def embed_query_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text, role="query") for text in texts]

    def embed_document_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text, role="document") for text in texts]

    def _embed_one(self, text: str, *, role: str) -> list[float]:
        return _hash_embedding(
            f"{self._config.seed}\0{role}\0{text}".encode(),
            dimensions=self._config.dimensions,
            normalize=self._config.normalize,
        )


@dataclass(frozen=True)
class DeterministicImageEmbeddingConfig:
    model_name: str = "chatspark-deterministic-image-hash-v1"
    dimensions: int = 384
    normalize: bool = True
    seed: str = "chatspark-image"


class DeterministicImageEmbedder:
    """Offline image embedder for tests and no-network demos.

    This is a deterministic byte fingerprint, not a semantic vision model.
    """

    def __init__(self, config: DeterministicImageEmbeddingConfig | None = None):
        self._config = config or DeterministicImageEmbeddingConfig()
        if self._config.dimensions <= 0:
            raise ValueError("Deterministic image embedding dimensions must be positive.")

    @property
    def model_name(self) -> str:
        return self._config.model_name

    @property
    def dimensions(self) -> int:
        return self._config.dimensions

    def embed_image(self, path: str | Path) -> list[float]:
        image_path = Path(path)
        payload = self._config.seed.encode("utf-8") + b"\0image\0" + image_path.read_bytes()
        return _hash_embedding(
            payload,
            dimensions=self._config.dimensions,
            normalize=self._config.normalize,
        )

    def embed_images(self, paths: Sequence[str | Path]) -> list[list[float]]:
        return [self.embed_image(path) for path in paths]


def _hash_embedding(payload: bytes, *, dimensions: int, normalize: bool) -> list[float]:
    values: list[float] = []
    counter = 0
    while len(values) < dimensions:
        digest = hashlib.sha256(payload + b"\0" + str(counter).encode("ascii")).digest()
        for offset in range(0, len(digest), 4):
            if len(values) >= dimensions:
                break
            integer = struct.unpack(">I", digest[offset : offset + 4])[0]
            values.append((integer / 2**31) - 1.0)
        counter += 1
    if normalize:
        return _l2_normalize(values)
    return values


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]
