from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class VectorStoreItem:
    item_id: str
    embedding: list[float]
    document: str
    metadata: dict[str, Any]


@dataclass
class VectorStoreResult:
    item_id: str
    score: float
    document: str | None
    metadata: dict[str, Any]
    embedding: list[float] | None = None


class VectorStore(Protocol):
    def upsert(self, items: Iterable[VectorStoreItem]) -> None: ...

    def query(
        self,
        embedding: list[float],
        k: int,
        filters: dict[str, Any] | None = None,
        include_embeddings: bool = False,
    ) -> list[VectorStoreResult]: ...

    def get(self, item_ids: list[str]) -> list[VectorStoreResult]: ...

    def delete(self, item_ids: list[str]) -> None: ...

    def count(self) -> int: ...

    def list_ids(self, filters: dict[str, Any] | None = None) -> list[str]: ...

    def reset(self) -> None: ...
