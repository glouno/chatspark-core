import os
import uuid

import pytest

from chatspark.engine.build import execute_build
from chatspark.engine.contracts import DenseIndexRequestV1, EngineBuildRequestV1
from chatspark.profiles import load_profile
from chatspark.runtime.config import settings
from chatspark.runtime.factory import get_retriever


@pytest.mark.skipif(
    not os.getenv("CHATSPARK_TEST_QDRANT_URL"),
    reason="Explicit isolated Qdrant test endpoint required",
)
def test_candidate_dense_index_and_canonical_filtering(tmp_path, monkeypatch):
    collection = "core_test_" + uuid.uuid4().hex
    monkeypatch.setattr(settings, "QDRANT_URL", os.environ["CHATSPARK_TEST_QDRANT_URL"])
    monkeypatch.setattr(settings, "QDRANT_COLLECTION", collection)
    source = tmp_path / "source"
    source.mkdir()
    (source / "registration.md").write_text("# Workshop\n\nRegistration uses ORCHID-42.\n")
    (source / "return.md").write_text("# Returns\n\nReturn the violet kit at 16:30.\n")
    from qdrant_client import QdrantClient

    client = QdrantClient(url=settings.QDRANT_URL)
    try:
        result = execute_build(
            EngineBuildRequestV1(
                build_id="dense",
                source_root=source,
                output_root=tmp_path / "candidate",
                profile="example",
                dense_index=DenseIndexRequestV1(enabled=True, collection_name=collection),
            )
        )
        assert result.status == "succeeded", result.error
        profile = load_profile("example")
        profile.retrieval.enable_dense = True
        retriever = get_retriever(
            database=tmp_path / "candidate/corpus.db",
            profile=profile,
            chunk_set_id=result.chunk_set_id,
        )
        chunks = retriever.retrieve("registration", 3)
        assert chunks
        assert all(c.metadata["chunk_set_id"] == result.chunk_set_id for c in chunks)
        assert not retriever.retrieve("registration", 3, {"doc_id": "unavailable"})
        import asyncio

        from chatspark.application import (
            AnswerRequestV1,
            ServingSnapshotV1,
            create_answer_runtime,
            profile_digest,
        )
        from chatspark.runtime.embedding_manifest import embedding_fingerprint
        from chatspark.storage.qdrant import QdrantVectorStore

        class Model:
            def stream_chat(self, messages, *, max_tokens):
                assert "ORCHID-42" in messages[1]["content"]
                yield "Registration uses ORCHID-42. [C1]"

        fingerprint = embedding_fingerprint(retriever.services.embedder)
        snapshot = ServingSnapshotV1(
            build_id="dense",
            database=retriever.context.database,
            corpus_id=retriever.context.corpus_id,
            chunk_set_id=result.chunk_set_id,
            corpus_sha256=retriever.context.snapshot_sha256,
            profile_sha256=profile_digest(profile),
            vector_collection=collection,
            embedding_sha256=fingerprint["sha256"],
        )
        # An unrelated process-level collection must not affect explicit composition.
        monkeypatch.setattr(settings, "QDRANT_COLLECTION", "unselected_foreign_collection")
        kwargs = dict(
            profile=profile,
            registry=retriever.registry,
            llm=Model(),
            embedder=retriever.services.embedder,
            vectorstore=retriever.services.dense.store,
            embedding_identity=fingerprint,
        )
        runtime = create_answer_runtime(snapshot, **kwargs)

        async def answer():
            await runtime.warmup()
            events = [
                e async for e in runtime.stream_answer(AnswerRequestV1(question="registration"))
            ]
            assert events[-1].event == "done"
            await runtime.shutdown()

        asyncio.run(answer())
        foreign = QdrantVectorStore(
            collection_name=collection + "_foreign", url=settings.QDRANT_URL
        )
        try:
            with pytest.raises(ValueError, match="collection binding mismatch"):
                create_answer_runtime(snapshot, **(kwargs | {"vectorstore": foreign}))
            with pytest.raises(ValueError, match="Embedding binding mismatch"):
                create_answer_runtime(
                    snapshot,
                    **(kwargs | {"embedding_identity": fingerprint | {"sha256": "0" * 64}}),
                )
        finally:
            foreign.close()
        import sqlite3

        with sqlite3.connect(tmp_path / "candidate/corpus.db") as con:
            assert (
                con.execute(
                    "SELECT status FROM index_runs WHERE index_run_id=?", (result.index_run_id,)
                ).fetchone()[0]
                == "succeeded"
            )
    finally:
        if client.collection_exists(collection):
            client.delete_collection(collection)
        client.close()
