from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace

from chatspark.core.items import RetrievedItem


def rrf_fuse_items(
    ranked_lists: Sequence[Sequence[RetrievedItem]],
    *,
    k: int = 60,
    weights: Sequence[float] | None = None,
    source: str = "rrf",
) -> list[RetrievedItem]:
    scores: dict[str, float] = defaultdict(float)
    items: dict[str, RetrievedItem] = {}
    stream_weights = weights or [1.0] * len(ranked_lists)

    for weight, results in zip(stream_weights, ranked_lists, strict=False):
        for rank, item in enumerate(results):
            scores[item.item_id] += weight / (k + rank + 1)
            if item.item_id not in items:
                items[item.item_id] = item

    ranked_ids = sorted(items.keys(), key=lambda item_id: scores[item_id], reverse=True)
    return [replace(items[item_id], score=scores[item_id], source=source) for item_id in ranked_ids]
