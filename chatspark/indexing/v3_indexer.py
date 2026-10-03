"""Candidate-only indexing with canonical index-run provenance."""

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from chatspark.embeddings.documents import document_embedding_text
from chatspark.runtime.config import settings
from chatspark.runtime.factory import get_embedder, get_vectorstore
from chatspark.storage.evidence import EvidenceStore
from chatspark.storage.vector_store import VectorStoreItem


def index_v3_database(
    *,
    database,
    chunk_set_id,
    report_path=None,
    reset_vector_store=False,
    pre_index_policy="off",
    **kwargs,
):
    if pre_index_policy != "off" or reset_vector_store:
        raise ValueError("Baseline indexing requires a new collection with pre_index_policy=off")
    store = EvidenceStore(database)
    with store._connect() as con:
        row = con.execute(
            "SELECT corpus_id,profile_digest FROM chunk_sets WHERE chunk_set_id=? AND status='succeeded'",
            (chunk_set_id,),
        ).fetchone()
    if row is None:
        raise ValueError("Successful canonical chunk set required")
    rows = store.rows(corpus_id=row["corpus_id"], chunk_set_id=chunk_set_id)
    embedder, vectorstore = get_embedder(), get_vectorstore()
    if vectorstore.count():
        raise ValueError("Dense candidate target must be an empty collection")
    if not rows:
        raise ValueError("Dense indexing requires nonempty canonical chunks")
    from chatspark.runtime.embedding_manifest import embedding_fingerprint

    first_batch = rows[: settings.EMBEDDING_BATCH_SIZE]

    def texts(batch):
        return [document_embedding_text(r, settings.EMBEDDING_DOCUMENT_COMPOSITION) for r in batch]

    first_vectors = embedder.embed_document_texts(texts(first_batch))
    if len(first_vectors) != len(first_batch):
        raise ValueError("Embedding count mismatch")
    fingerprint = embedding_fingerprint(embedder)
    if fingerprint["dimensions"] is None:
        fingerprint["dimensions"] = len(first_vectors[0])
        import hashlib

        fingerprint.pop("sha256")
        fingerprint["sha256"] = hashlib.sha256(
            json.dumps(fingerprint, sort_keys=True).encode()
        ).hexdigest()
    index_id = "rf3_index_run_" + uuid.uuid4().hex

    def now():
        return datetime.now(UTC).isoformat()

    with sqlite3.connect(database) as con:
        con.execute(
            "INSERT INTO index_runs(index_run_id,chunk_set_id,profile_digest,index_kind,model_id,target_uri,config_json,status,started_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                index_id,
                chunk_set_id,
                row["profile_digest"],
                "dense",
                embedder.model_name,
                settings.QDRANT_COLLECTION,
                json.dumps({"batch_size": settings.EMBEDDING_BATCH_SIZE, "embedding": fingerprint}),
                "running",
                now(),
            ),
        )
    report = {
        "report_version": 1,
        "index_run_id": index_id,
        "indexed": 0,
        "status": "failed",
        "ok": False,
        "chunks_total": len(rows),
        "processed_chunks": 0,
    }
    try:
        for start in range(0, len(rows), settings.EMBEDDING_BATCH_SIZE):
            batch = rows[start : start + settings.EMBEDDING_BATCH_SIZE]
            vectors = first_vectors if start == 0 else embedder.embed_document_texts(texts(batch))
            vectorstore.upsert(
                [
                    VectorStoreItem(r["chunk_id"], v, r["text"], store.chunk(r).metadata)
                    for r, v in zip(batch, vectors, strict=True)
                ]
            )
            report["indexed"] += len(batch)
        report.update(status="succeeded", ok=True, processed_chunks=report["indexed"])
    finally:
        with sqlite3.connect(database) as con:
            con.execute(
                "UPDATE index_runs SET status=?,stats_json=?,finished_at=? WHERE index_run_id=?",
                (report["status"], json.dumps(report), now(), index_id),
            )
        if report_path:
            Path(report_path).write_text(json.dumps(report, indent=2) + "\n")
    return report
