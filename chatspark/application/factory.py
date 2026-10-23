"""Explicit dependency composition; never changes process environment or settings."""

from functools import partial
from typing import Mapping

from chatspark.generation.prompts import resolve_messages
from chatspark.retrieval.pipeline import StrategyRetriever, execution_context
from chatspark.runtime.factory import DenseLane
from chatspark.storage.evidence import EvidenceStore, narrow_filters

from .v1 import ServingSnapshotV1, SnapshotAnswerRuntime


def validate_index_binding(snapshot, context, embedder, vectorstore, fingerprint):
    """Verify the actual collection and complete index identity, before retrieval."""
    import hashlib
    import json

    if vectorstore.collection_name != snapshot.vector_collection:
        raise ValueError("Vector collection binding mismatch")
    actual = dict(fingerprint)
    digest = actual.pop("sha256", None)
    if (
        digest != snapshot.embedding_sha256
        or digest != hashlib.sha256(json.dumps(actual, sort_keys=True).encode()).hexdigest()
        or actual.get("dimensions") != getattr(embedder, "dimensions", None)
        or not actual.get("dimensions")
    ):
        raise ValueError("Embedding binding mismatch")
    with EvidenceStore(context.database)._connect() as con:
        row = con.execute(
            "SELECT config_json FROM index_runs WHERE chunk_set_id=? "
            "AND index_kind='dense' AND status='succeeded' AND target_uri=? "
            "ORDER BY finished_at DESC LIMIT 1",
            (context.chunk_set_id, snapshot.vector_collection),
        ).fetchone()
    if not row or json.loads(row["config_json"]).get("embedding") != dict(fingerprint):
        raise ValueError("Snapshot has no matching dense index")
    if vectorstore.count() == 0:
        raise ValueError("Dense index is empty")


def create_answer_runtime(
    snapshot: ServingSnapshotV1,
    *,
    profile,
    registry,
    llm,
    authorized_filters: Mapping[str, object] | None = None,
    embedder=None,
    vectorstore=None,
    embedding_identity: Mapping[str, object] | None = None,
    **runtime_options,
) -> SnapshotAnswerRuntime:
    """Compose installed allowlisted providers for exactly one immutable snapshot.

    The host owns authorization, model construction and embedding fingerprint
    attestation. No caller-supplied corpus/collection selector enters this factory.
    Endpoint-dependent embedders must establish dimensions before composition.
    """
    context = execution_context(
        snapshot.database,
        narrow_filters({}, authorized_filters or {}),
        chunk_set_id=snapshot.chunk_set_id,
    )
    validator = None
    dense = None
    if profile.retrieval.enable_dense:
        if embedder is None or vectorstore is None or embedding_identity is None:
            raise ValueError("Dense serving requires explicit model, store and fingerprint")
        # Freeze the attestation independently from the host's mutable mapping.
        import json

        fingerprint = json.loads(json.dumps(dict(embedding_identity)))
        validator = partial(
            validate_index_binding, snapshot, context, embedder, vectorstore, fingerprint
        )
        validator()
        dense = DenseLane(embedder=embedder, store=vectorstore)
    retriever = StrategyRetriever(
        context=context,
        profile=profile.retrieval,
        registry=registry,
        dense=dense,
        embedder=embedder,
        policy=profile.policy,
    )
    return SnapshotAnswerRuntime(
        snapshot,
        profile,
        retriever,
        llm,
        resolver=partial(resolve_messages, registry=registry),
        index_validator=validator,
        **runtime_options,
    )
