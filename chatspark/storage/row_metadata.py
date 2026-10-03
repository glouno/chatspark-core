from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

SOURCE_DOCUMENT_METADATA_COLUMNS = {
    "url",
    "source_uri",
    "content_hash",
    "content_type",
    "source_family",
    "site_host",
    "path_prefix",
    "title",
    "last_modified",
    "etag",
    "http_status",
    "doc_type",
}

CHUNK_METADATA_COLUMNS = {
    "run_id",
    "section_id",
    "chunk_strategy",
    "chunk_index",
    "token_count",
    "char_count",
}


def hydrate_chunk_metadata_from_row(
    row: Any, base_metadata: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Merge normalized SQLite row fields into retrieval metadata."""
    row_map = _row_mapping(row)
    metadata: dict[str, Any] = dict(base_metadata or {})

    metadata.setdefault("chunk_id", row_map.get("chunk_id"))
    metadata.setdefault("doc_id", row_map.get("doc_id"))
    for key in ("corpus_id", "corpus_name", "retrieval_corpus_id"):
        value = row_map.get(key)
        if value is not None and value != "":
            metadata[key] = value

    used_columns = False
    for key in CHUNK_METADATA_COLUMNS:
        used_columns = _copy_if_present(metadata, row_map, key) or used_columns

    for key in SOURCE_DOCUMENT_METADATA_COLUMNS:
        value = row_map.get(f"source_{key}")
        if value is not None and value != "":
            metadata[key] = value
            used_columns = True

    heading_path = _json_list(row_map.get("section_heading_path"))
    if heading_path:
        metadata["heading_path"] = heading_path
        used_columns = True

    heading_text = row_map.get("section_heading_text")
    if heading_text:
        metadata["heading_text"] = heading_text
        used_columns = True

    section_index = row_map.get("section_index")
    if section_index is not None:
        metadata["section_index"] = section_index
        used_columns = True

    if used_columns:
        metadata["retrieval_metadata_source"] = "normalized_columns"
    else:
        metadata.setdefault("retrieval_metadata_source", "chunk_attributes")
    return metadata


def _row_mapping(row: Any) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return mapping
    if hasattr(row, "keys"):
        return dict(row)
    if isinstance(row, tuple) and len(row) >= 5:
        return {
            "chunk_id": row[0],
            "doc_id": row[1],
            "content": row[2],
            "attributes": row[3],
            "score": row[4],
        }
    return {}


def _copy_if_present(metadata: dict[str, Any], row: Mapping[str, Any], key: str) -> bool:
    value = row.get(key)
    if value is None or value == "":
        return False
    metadata[key] = value
    return True


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        if isinstance(decoded, list):
            return [str(item) for item in decoded]
        if isinstance(decoded, str):
            return [decoded]
    return []
