"""Secret-free embedding identity shared by index construction and querying."""

import hashlib
import json
from pathlib import Path

from chatspark.embeddings.formatting import resolve_embedding_text_format
from chatspark.runtime.config import settings


def embedding_fingerprint(embedder):
    model = Path(settings.EMBEDDING_MODEL)
    artifacts = {}
    if model.is_dir():
        for pattern in ("*.json", "*.safetensors", "*.bin", "*.model", "*.txt"):
            for file in sorted(model.rglob(pattern)):
                if file.is_file():
                    with file.open("rb") as stream:
                        artifacts[file.relative_to(model).as_posix()] = hashlib.file_digest(
                            stream, "sha256"
                        ).hexdigest()
    binding = {
        "backend": settings.EMBEDDING_BACKEND,
        "model": settings.EMBEDDING_MODEL,
        "revision": settings.EMBEDDING_REVISION,
        "text_format": resolve_embedding_text_format(
            settings.EMBEDDING_MODEL, settings.EMBEDDING_TEXT_FORMAT
        ),
        "normalize": settings.EMBEDDING_NORMALIZE,
        "document_composition": settings.EMBEDDING_DOCUMENT_COMPOSITION,
        "dimensions": getattr(embedder, "dimensions", None),
        "artifacts": artifacts,
    }
    binding["sha256"] = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    return binding


def validate_dense_index(context, embedder, vectorstore):
    from chatspark.storage.evidence import EvidenceStore

    with EvidenceStore(context.database)._connect() as con:
        rows = con.execute(
            "SELECT config_json FROM index_runs WHERE chunk_set_id=? AND index_kind='dense' AND status='succeeded' AND target_uri=? ORDER BY finished_at DESC",
            (context.chunk_set_id, settings.QDRANT_COLLECTION),
        ).fetchall()
    if not rows:
        raise ValueError("Dense retrieval requires a matching successful index; run corpus build")
    expected = json.loads(rows[0]["config_json"]).get("embedding")
    if getattr(embedder, "dimensions", None) is None:
        # Explicit dense serving preflight contacts the selected embedding service;
        # profile validation alone never does. No corpus/customer text is sent.
        vectors = embedder.embed_query_texts(["ChatSpark embedding dimension validation"])
        if len(vectors) != 1 or not vectors[0]:
            raise ValueError("Embedding service returned invalid preflight dimensions")
    actual = embedding_fingerprint(embedder)
    if actual["dimensions"] is None:
        actual["dimensions"] = len(vectors[0])
        actual.pop("sha256")
        actual["sha256"] = hashlib.sha256(json.dumps(actual, sort_keys=True).encode()).hexdigest()
    if expected != actual:
        raise ValueError("Embedding model/index fingerprint mismatch; build a new candidate index")
    if vectorstore.count() == 0:
        raise ValueError("Dense retrieval requires the configured populated collection")
