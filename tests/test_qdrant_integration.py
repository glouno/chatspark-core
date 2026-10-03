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
