from types import SimpleNamespace

import pytest

from chatspark.embeddings.openai_compatible import OpenAICompatibleEmbedder


def test_endpoint_dimension_change_is_rejected(monkeypatch):
    import chatspark.embeddings.openai_compatible as module

    responses = iter([[1.0, 2.0], [1.0, 2.0, 3.0]])

    def post(*_args, **_kwargs):
        vector = next(responses)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"data": [{"index": 0, "embedding": vector}]},
        )

    monkeypatch.setattr(module.requests, "post", post)
    embedder = OpenAICompatibleEmbedder(model_name="explicit-test", normalize=False)
    embedder.embed_document_texts(["document"])
    assert embedder.dimensions == 2
    with pytest.raises(ValueError, match="changed dimensions"):
        embedder.embed_query_texts(["query"])


def test_remote_index_records_dimensions_and_rejects_different_endpoint(candidate, monkeypatch):
    import json
    import sqlite3
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from chatspark.indexing import v3_indexer
    from chatspark.retrieval.pipeline import execution_context
    from chatspark.runtime.config import settings
    from chatspark.runtime.embedding_manifest import validate_dense_index

    dimensions = [3]
    inputs = []

    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            inputs.extend(request["input"])
            body = json.dumps(
                {
                    "data": [
                        {"index": i, "embedding": [1.0] * dimensions[0]}
                        for i in range(len(request["input"]))
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    indexed = []
    store = SimpleNamespace(count=lambda: len(indexed), upsert=lambda batch: indexed.extend(batch))
    database, build = candidate
    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "openai")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "synthetic-e5")
    monkeypatch.setattr(settings, "EMBEDDING_REVISION", "synthetic-revision")
    monkeypatch.setattr(settings, "EMBEDDING_TEXT_FORMAT", "e5")
    monkeypatch.setattr(settings, "EMBEDDING_NORMALIZE", True)
    monkeypatch.setattr(settings, "QDRANT_COLLECTION", "synthetic-dimension-check")

    def embedder():
        return OpenAICompatibleEmbedder(
            base_url=f"http://127.0.0.1:{server.server_port}/v1", api_key=""
        )

    monkeypatch.setattr(v3_indexer, "get_embedder", embedder)
    monkeypatch.setattr(v3_indexer, "get_vectorstore", lambda: store)
    try:
        report = v3_indexer.index_v3_database(database=database, chunk_set_id=build.chunk_set_id)
        assert report["ok"]
        assert indexed and all(value.startswith("passage: ") for value in inputs)
        with sqlite3.connect(database) as con:
            config = json.loads(
                con.execute(
                    "SELECT config_json FROM index_runs WHERE index_run_id=?",
                    (report["index_run_id"],),
                ).fetchone()[0]
            )
        assert config["embedding"]["dimensions"] == 3
        context = execution_context(database, chunk_set_id=build.chunk_set_id)
        validate_dense_index(context, embedder(), store)
        assert inputs[-1].startswith("query: ")
        monkeypatch.setattr(settings, "EMBEDDING_DOCUMENT_COMPOSITION", "title_heading_text")
        with pytest.raises(ValueError, match="fingerprint mismatch"):
            validate_dense_index(context, embedder(), store)
        monkeypatch.setattr(settings, "EMBEDDING_DOCUMENT_COMPOSITION", "text")
        dimensions[0] = 4
        with pytest.raises(ValueError, match="fingerprint mismatch"):
            validate_dense_index(context, embedder(), store)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_document_composition_changes_model_input_not_canonical_text(candidate, monkeypatch):
    import json
    import sqlite3

    from chatspark.indexing import v3_indexer
    from chatspark.runtime.config import settings

    database, build = candidate
    inputs, indexed = [], []
    embedder = SimpleNamespace(
        dimensions=2,
        model_name="synthetic",
        embed_document_texts=lambda texts: inputs.extend(texts) or [[1.0, 0.0] for _ in texts],
    )
    store = SimpleNamespace(count=lambda: len(indexed), upsert=lambda batch: indexed.extend(batch))
    monkeypatch.setattr(v3_indexer, "get_embedder", lambda: embedder)
    monkeypatch.setattr(v3_indexer, "get_vectorstore", lambda: store)
    monkeypatch.setattr(settings, "EMBEDDING_BATCH_SIZE", 1)
    monkeypatch.setattr(settings, "EMBEDDING_DOCUMENT_COMPOSITION", "title_heading_text")
    with sqlite3.connect(database) as con:
        con.execute("UPDATE document_revisions SET title='Synthetic title'")
        con.execute(
            "UPDATE chunks SET metadata_json=?",
            (json.dumps({"heading_path": ["Facts", "Details"]}),),
        )
    result = v3_indexer.index_v3_database(database=database, chunk_set_id=build.chunk_set_id)
    assert result["ok"]
    assert len(inputs) == len(indexed) > 1
    for text, item in zip(inputs, indexed, strict=True):
        assert text == "Synthetic title\nFacts > Details\n" + item.document.strip()
        assert not item.document.startswith("Synthetic title")


def test_document_composition_metadata_fallback_and_validation():
    from chatspark.embeddings.documents import document_embedding_text
    from chatspark.runtime.operator import OperatorConfiguration

    row = {
        "text": " repeated ",
        "title": "repeated",
        "metadata_json": '{"heading_text":"repeated"}',
    }
    assert document_embedding_text(row, "text") == " repeated "
    assert document_embedding_text(row, "title_heading_text") == "repeated"
    with pytest.raises(ValueError, match="Unsupported"):
        document_embedding_text(row, "unknown")
    config = OperatorConfiguration.model_validate(
        {"embedding": {"document_composition": "title_heading_text"}}
    )
    assert config.environment_values()["EMBEDDING_DOCUMENT_COMPOSITION"] == "title_heading_text"
    with pytest.raises(ValueError):
        OperatorConfiguration.model_validate({"embedding": {"document_composition": "unknown"}})
