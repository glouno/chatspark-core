from __future__ import annotations

import os
import uuid
from collections.abc import Iterable
from importlib import import_module
from typing import Any

from chatspark.storage.vector_store import VectorStore, VectorStoreItem, VectorStoreResult


def _require_dependency(module_name: str) -> Any:
    try:
        return import_module(module_name)
    except ImportError as exc:
        raise RuntimeError(
            f"Qdrant vector store requires optional dependency {module_name!r}. "
            "Install it with `pip install chatspark-core[qdrant]` or `uv sync --extra qdrant`."
        ) from exc


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


_qdrant_client = _require_dependency("qdrant_client")
QdrantClient = _qdrant_client.QdrantClient
UnexpectedResponse = _require_dependency("qdrant_client.http.exceptions").UnexpectedResponse
qdrant = _require_dependency("qdrant_client.http.models")

_POINT_ID_NAMESPACE = uuid.UUID("d4e756f5-9e27-4c27-b7f2-35f7e1ebec8c")
_PAYLOAD_INDEXES: dict[str, Any] = {
    key: qdrant.PayloadSchemaType.KEYWORD
    for key in (
        "corpus_id",
        "chunk_set_id",
        "document_id",
        "doc_id",
        "chunk_id",
        "url",
        "doc_type",
        "content_type",
        "source_family",
        "site_host",
    )
}


def _to_qdrant_point_id(item_id: str) -> str | int:
    if item_id.isdigit():
        value = int(item_id)
        if value >= 0:
            return value
    try:
        return str(uuid.UUID(item_id))
    except ValueError:
        # Qdrant accepts UUIDs; derive a deterministic UUID from external IDs.
        return str(uuid.uuid5(_POINT_ID_NAMESPACE, item_id))


def _from_qdrant_result_id(raw_id: Any, payload: dict[str, Any]) -> str:
    chunk_id = payload.get("chunk_id")
    if isinstance(chunk_id, str) and chunk_id:
        return chunk_id
    return str(raw_id)


class QdrantVectorStore(VectorStore):
    def __init__(
        self,
        collection_name: str | None = None,
        *,
        url: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
        create_payload_indexes: bool | None = None,
    ):
        self._collection_name = collection_name or os.getenv(
            "QDRANT_COLLECTION", "chatspark_chunks"
        )
        self._client = QdrantClient(
            url=url or os.getenv("QDRANT_URL", "http://127.0.0.1:16333"),
            api_key=api_key if api_key is not None else os.getenv("QDRANT_API_KEY"),
            timeout=timeout or int(os.getenv("QDRANT_TIMEOUT_SECONDS", "30")),
        )
        self._create_payload_indexes = (
            create_payload_indexes
            if create_payload_indexes is not None
            else _env_bool("QDRANT_CREATE_PAYLOAD_INDEXES", True)
        )
        self._payload_indexes_ensured = False
        self._vector_size: int | None = self._discover_vector_size()

    def _collection_exists(self) -> bool:
        try:
            self._client.get_collection(self._collection_name)
            return True
        except UnexpectedResponse as exc:
            if exc.status_code == 404:
                return False
            raise
        except Exception:
            # Keep transport/runtime failures visible to the caller.
            raise

    def _collection_missing(self) -> bool:
        try:
            return not self._collection_exists()
        except UnexpectedResponse as exc:
            if exc.status_code == 404:
                return True
            raise

    def _discover_vector_size(self) -> int | None:
        if self._collection_missing():
            return None
        collection = self._client.get_collection(self._collection_name)
        vectors_config = collection.config.params.vectors
        if isinstance(vectors_config, dict):
            first = next(iter(vectors_config.values()), None)
            return int(first.size) if first is not None else None
        return int(vectors_config.size)

    def _ensure_collection(self, vector_size: int) -> None:
        if self._collection_exists():
            if self._vector_size is None:
                self._vector_size = self._discover_vector_size()
            self._ensure_payload_indexes()
            return

        self._client.create_collection(
            collection_name=self._collection_name,
            vectors_config=qdrant.VectorParams(size=vector_size, distance=qdrant.Distance.COSINE),
        )
        self._ensure_payload_indexes()
        self._vector_size = vector_size

    def _ensure_payload_indexes(self) -> None:
        if getattr(self, "_payload_indexes_ensured", False):
            return
        if not getattr(
            self, "_create_payload_indexes", _env_bool("QDRANT_CREATE_PAYLOAD_INDEXES", True)
        ):
            return
        create_payload_index = getattr(self._client, "create_payload_index", None)
        if not callable(create_payload_index):
            return
        for field_name, field_schema in _PAYLOAD_INDEXES.items():
            create_payload_index(
                collection_name=self._collection_name,
                field_name=field_name,
                field_schema=field_schema,
                wait=True,
            )
        self._payload_indexes_ensured = True

    def _to_filter(self, filters: dict[str, Any] | None) -> qdrant.Filter | None:
        if not filters:
            return None
        must: list[qdrant.Condition] = []
        for key, value in filters.items():
            if isinstance(value, (list, tuple, set)):
                must.append(qdrant.FieldCondition(key=key, match=qdrant.MatchAny(any=list(value))))
                continue
            must.append(qdrant.FieldCondition(key=key, match=qdrant.MatchValue(value=value)))
        return qdrant.Filter(must=must)

    @staticmethod
    def _extract_vector(raw_vector: Any) -> list[float] | None:
        if raw_vector is None:
            return None
        if isinstance(raw_vector, dict):
            return next(iter(raw_vector.values()), None)
        return raw_vector

    def upsert(self, items: Iterable[VectorStoreItem]) -> None:
        points = list(items)
        if not points:
            return
        self._ensure_collection(len(points[0].embedding))
        qdrant_points: list[qdrant.PointStruct] = []
        for item in points:
            payload: dict[str, Any] = {"document": item.document}
            payload.update(item.metadata or {})
            payload.setdefault("chunk_id", item.item_id)
            qdrant_points.append(
                qdrant.PointStruct(
                    id=_to_qdrant_point_id(item.item_id),
                    vector=item.embedding,
                    payload=payload,
                )
            )
        self._client.upsert(
            collection_name=self._collection_name,
            points=qdrant_points,
            wait=True,
        )

    def query(
        self,
        embedding: list[float],
        k: int,
        filters: dict[str, Any] | None = None,
        include_embeddings: bool = False,
    ) -> list[VectorStoreResult]:
        if self._collection_missing():
            return []
        if hasattr(self._client, "search"):
            hits = self._client.search(
                collection_name=self._collection_name,
                query_vector=embedding,
                query_filter=self._to_filter(filters),
                limit=k,
                with_payload=True,
                with_vectors=include_embeddings,
            )
        else:
            response = self._client.query_points(
                collection_name=self._collection_name,
                query=embedding,
                query_filter=self._to_filter(filters),
                limit=k,
                with_payload=True,
                with_vectors=include_embeddings,
            )
            hits = response.points
        results: list[VectorStoreResult] = []
        for hit in hits:
            payload = dict(hit.payload or {})
            document = payload.pop("document", None)
            results.append(
                VectorStoreResult(
                    item_id=_from_qdrant_result_id(hit.id, payload),
                    score=float(hit.score),
                    document=document,
                    metadata=payload,
                    embedding=self._extract_vector(hit.vector) if include_embeddings else None,
                )
            )
        return results

    def get(self, item_ids: list[str]) -> list[VectorStoreResult]:
        if not item_ids or self._collection_missing():
            return []
        qdrant_ids = [_to_qdrant_point_id(item_id) for item_id in item_ids]
        points = self._client.retrieve(
            collection_name=self._collection_name,
            ids=qdrant_ids,
            with_payload=True,
            with_vectors=True,
        )
        out: list[VectorStoreResult] = []
        for point in points:
            payload = dict(point.payload or {})
            document = payload.pop("document", None)
            out.append(
                VectorStoreResult(
                    item_id=_from_qdrant_result_id(point.id, payload),
                    score=0.0,
                    document=document,
                    metadata=payload,
                    embedding=self._extract_vector(point.vector),
                )
            )
        return out

    def delete(self, item_ids: list[str]) -> None:
        if not item_ids or self._collection_missing():
            return
        qdrant_ids = [_to_qdrant_point_id(item_id) for item_id in item_ids]
        self._client.delete(
            collection_name=self._collection_name,
            points_selector=qdrant.PointIdsList(points=qdrant_ids),
            wait=True,
        )

    def count(self) -> int:
        if self._collection_missing():
            return 0
        return int(self._client.count(collection_name=self._collection_name, exact=True).count)

    def list_ids(self, filters: dict[str, Any] | None = None) -> list[str]:
        """Return logical item IDs, optionally restricted by indexed payload."""
        if self._collection_missing():
            return []
        out: list[str] = []
        offset = None
        while True:
            points, offset = self._client.scroll(
                collection_name=self._collection_name,
                scroll_filter=self._to_filter(filters),
                limit=256,
                offset=offset,
                with_payload=["chunk_id"],
                with_vectors=False,
            )
            for point in points:
                out.append(_from_qdrant_result_id(point.id, dict(point.payload or {})))
            if offset is None:
                break
        return sorted(set(out))

    def reset(self) -> None:
        if not self._collection_missing():
            self._client.delete_collection(collection_name=self._collection_name)
        self._vector_size = None
        self._payload_indexes_ensured = False
