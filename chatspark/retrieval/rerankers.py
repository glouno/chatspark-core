import re
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

from chatspark.runtime.config import settings


def tokens(text):
    return set(re.findall(r"\w+", text.casefold()))


def rerank(query, chunks, policy):
    if policy.backend == "token-overlap":
        wanted = tokens(query)
        scored = [
            replace(c, score=len(wanted & tokens(c.content)) / max(len(wanted), 1)) for c in chunks
        ]
    else:
        model = policy.model or settings.RERANKER_MODEL
        if not model:
            raise ValueError("Cross-encoder model is required")
        scores = _cross_encoder(
            model, settings.RERANKER_DEVICE, settings.RERANKER_REVISION
        ).predict([(query, c.content) for c in chunks], batch_size=settings.RERANKER_BATCH_SIZE)
        scored = [replace(c, score=float(score)) for c, score in zip(chunks, scores, strict=True)]
    return sorted(scored, key=lambda c: (-c.score, c.chunk_id))


def lexical_mmr(chunks, k, weight):
    remaining = list(chunks)
    selected = []
    denominator = max((abs(c.score) for c in chunks), default=1) or 1
    while remaining and len(selected) < k:

        def score(chunk):
            terms = tokens(chunk.content)
            redundancy = max(
                (
                    len(terms & tokens(c.content)) / max(len(terms | tokens(c.content)), 1)
                    for c in selected
                ),
                default=0,
            )
            return weight * chunk.score / denominator - (1 - weight) * redundancy

        chosen = min(remaining, key=lambda c: (-score(c), c.chunk_id))
        selected.append(chosen)
        remaining.remove(chosen)
    return selected


def embedding_mmr(chunks, k, weight, embedder):
    import math

    vectors = embedder.embed_document_texts([c.content for c in chunks])
    if len(vectors) != len(chunks):
        raise ValueError("MMR embedding count mismatch")
    normalized = [
        [v / (math.sqrt(sum(x * x for x in vector)) or 1) for v in vector] for vector in vectors
    ]
    remaining, selected = list(range(len(chunks))), []
    denominator = max((abs(c.score) for c in chunks), default=1) or 1
    while remaining and len(selected) < k:

        def score(index):
            redundancy = max(
                (
                    sum(a * b for a, b in zip(normalized[index], normalized[j], strict=True))
                    for j in selected
                ),
                default=0,
            )
            return weight * chunks[index].score / denominator - (1 - weight) * redundancy

        chosen = min(remaining, key=lambda i: (-score(i), chunks[i].chunk_id))
        selected.append(chosen)
        remaining.remove(chosen)
    return [chunks[i] for i in selected]


@lru_cache(maxsize=2)
def _cross_encoder(model, device, revision=""):
    if not Path(model).is_dir():
        raise ValueError("Prepare a local reranker model and configure its directory")
    from sentence_transformers import CrossEncoder

    return CrossEncoder(
        model,
        device=device,
        revision=revision or None,
        trust_remote_code=False,
        local_files_only=True,
    )
