"""Compose model inputs from canonical text and descriptive metadata.

This changes embedding inputs only. Canonical passage text and citations remain
owned by storage; model-specific prefixes are applied afterward by the embedder.
"""

import json


def document_embedding_text(row, composition):
    if composition == "text":
        return row["text"]
    if composition != "title_heading_text":
        raise ValueError("Unsupported embedding document composition")
    metadata = json.loads(row["metadata_json"] or "{}")
    heading = metadata.get("heading_path") or metadata.get("heading_text") or ""
    if isinstance(heading, (list, tuple)):
        heading = " > ".join(str(item).strip() for item in heading if str(item).strip())
    title = row["title"] or metadata.get("display_title") or metadata.get("title") or ""
    parts = []
    for value in (str(title).strip(), str(heading).strip(), str(row["text"] or "").strip()):
        if value and value not in parts:
            parts.append(value)
    return "\n".join(parts)
