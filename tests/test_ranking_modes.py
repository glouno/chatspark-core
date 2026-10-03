from types import SimpleNamespace

import pytest

from chatspark.core.items import RetrievedItem
from chatspark.core.ranking import mmr_select_items
from chatspark.retrieval.models import RetrievedChunk
from chatspark.retrieval.rerankers import embedding_mmr, lexical_mmr, rerank


def chunks():
    return [
        RetrievedChunk("a", "orchid workshop", {}, 1, "canonical"),
        RetrievedChunk("b", "orchid workshop", {}, 0.9, "canonical"),
        RetrievedChunk("c", "violet registration", {}, 0.8, "canonical"),
    ]


def test_standard_reranker_changes_rank_using_source_passage():
    selected = rerank("violet", chunks(), SimpleNamespace(backend="token-overlap"))
    assert [c.chunk_id for c in selected] == ["c", "a", "b"]
    assert selected[0].score == 1
    assert chunks()[0].score == 1


def test_explicit_lexical_and_embedding_diversity_select_nonduplicate_passage():
    assert [c.chunk_id for c in lexical_mmr(chunks(), 2, 0.1)] == ["a", "c"]
    calls = []

    class Embedder:
        def embed_document_texts(self, texts):
            calls.append(texts)
            return [[1, 0], [1, 0], [0, 1]]

    assert [c.chunk_id for c in embedding_mmr(chunks(), 2, 0.1, Embedder())] == ["a", "c"]
    assert calls == [[c.content for c in chunks()]]
    with pytest.raises(ValueError, match="embedding count mismatch"):
        embedding_mmr(chunks(), 2, 0.1, SimpleNamespace(embed_document_texts=lambda _: []))


def test_generic_embedding_mmr_cannot_silently_become_score_selection():
    candidates = [RetrievedItem(item_id="a", text="canonical", score=1)]
    with pytest.raises(ValueError, match="every candidate"):
        mmr_select_items([1, 0], candidates, top_k=1)
