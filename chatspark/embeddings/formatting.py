from __future__ import annotations

from collections.abc import Iterable


def resolve_embedding_text_format(model_name: str, configured_format: str) -> str:
    normalized = (configured_format or "auto").strip().lower()
    if normalized not in {"auto", "plain", "e5"}:
        raise ValueError("Embedding text format must be auto, plain or e5")
    if normalized == "auto":
        model_lower = (model_name or "").strip().lower()
        return "e5" if "e5" in model_lower else "plain"
    return normalized


def format_embedding_text(text: str, *, role: str, model_name: str, configured_format: str) -> str:
    effective_format = resolve_embedding_text_format(model_name, configured_format)
    normalized_role = "query" if role == "query" else "passage"
    if effective_format == "e5":
        prefix = "query: " if normalized_role == "query" else "passage: "
        if text.startswith("query: ") or text.startswith("passage: "):
            text = text.split(": ", 1)[1]
        return f"{prefix}{text}"
    return text


def format_embedding_texts(
    texts: Iterable[str],
    *,
    role: str,
    model_name: str,
    configured_format: str,
) -> list[str]:
    return [
        format_embedding_text(
            text, role=role, model_name=model_name, configured_format=configured_format
        )
        for text in texts
    ]
