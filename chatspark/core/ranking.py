from __future__ import annotations

import math
from collections.abc import Sequence

from chatspark.core.items import RetrievedItem


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def mmr_select_items(
    query_embedding: Sequence[float],
    candidates: Sequence[RetrievedItem],
    *,
    top_k: int,
    lambda_mult: float = 0.5,
) -> list[RetrievedItem]:
    if not candidates:
        return []
    if any(candidate.embedding is None for candidate in candidates):
        raise ValueError("Embedding MMR requires an embedding for every candidate")

    selected: list[RetrievedItem] = []
    remaining = list(candidates)

    while remaining and len(selected) < top_k:
        best: RetrievedItem | None = None
        best_score = float("-inf")
        for candidate in remaining:
            relevance = cosine_similarity(query_embedding, candidate.embedding or [])
            diversity = 0.0
            if selected:
                diversity = max(
                    cosine_similarity(candidate.embedding or [], item.embedding or [])
                    for item in selected
                )
            score = lambda_mult * relevance - (1.0 - lambda_mult) * diversity
            if score > best_score:
                best_score = score
                best = candidate
        if best is None:
            break
        selected.append(best)
        remaining = [candidate for candidate in remaining if candidate.item_id != best.item_id]
    return selected
