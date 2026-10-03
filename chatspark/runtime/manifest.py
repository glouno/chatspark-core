"""Separate composition metadata; never extend strict engine contract v1."""

import hashlib
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from chatspark.profiles import profile_digest
from chatspark.runtime.config import settings
from chatspark.runtime.registry import get_registry


def runtime_manifest(
    *,
    profile,
    artifacts=(),
    selected_distribution=None,
    registry=None,
    prompt_metadata=None,
    embedding=None,
):
    selected_distribution = selected_distribution or settings.CHATSPARK_RUNTIME_DISTRIBUTION
    registry = registry or get_registry()
    packages = {}
    for name in dict.fromkeys(("chatspark-core", selected_distribution)):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            raise RuntimeError("Selected runtime distribution is not installed") from None
    prompts = {}
    from chatspark.prompts import load_prompt

    for field, prompt_id in (
        ("system_prompt", "rag.classic_chat.system.v6"),
        ("user_prompt", "rag.classic_chat.user.v2"),
    ):
        override = getattr(profile.generation, field)
        body = Path(override).read_bytes() if override else load_prompt(prompt_id).content.encode()
        prompts[field] = hashlib.sha256(body).hexdigest()
    from chatspark.runtime.operator import OperatorConfiguration, operator_digest

    operator = OperatorConfiguration.model_validate(
        {
            "embedding": {
                "backend": settings.EMBEDDING_BACKEND,
                "model": settings.EMBEDDING_MODEL,
                "revision": settings.EMBEDDING_REVISION,
                "text_format": settings.EMBEDDING_TEXT_FORMAT,
                "document_composition": settings.EMBEDDING_DOCUMENT_COMPOSITION,
                "normalize": settings.EMBEDDING_NORMALIZE,
                "device": settings.EMBEDDING_DEVICE,
                "batch_size": settings.EMBEDDING_BATCH_SIZE,
            },
            "generation": {
                "url": settings.OPENAI_COMPAT_BASE_URL,
                "model": settings.OPENAI_COMPAT_MODEL,
            },
            "vector": {
                "url": settings.QDRANT_URL,
                "collection": settings.QDRANT_COLLECTION,
                "timeout_seconds": settings.QDRANT_TIMEOUT_SECONDS,
            },
            "reranker": {
                "model": settings.RERANKER_MODEL,
                "revision": settings.RERANKER_REVISION,
                "device": settings.RERANKER_DEVICE,
                "batch_size": settings.RERANKER_BATCH_SIZE,
            },
            "state": {
                "root": settings.CHATSPARK_STATE_ROOT,
                "database": settings.CHATSPARK_V3_DB_PATH,
                "chunk_set_id": settings.CHATSPARK_CHUNK_SET_ID,
            },
            "providers_allowed": sorted(registry.allowed),
            "runtime_provider": settings.CHATSPARK_RUNTIME_PROVIDER,
            "runtime_distribution": selected_distribution,
        }
    )
    provenance = {
        "status": "not_resolved",
        "remote_selected": bool(profile.generation.remote_prompt),
    }
    if prompt_metadata is not None:
        provenance = {
            key: prompt_metadata[key]
            for key in (
                "system_sha256",
                "user_sha256",
                "system_provider",
                "user_provider",
                "system_revision",
                "system_implementation_version",
            )
            if key in prompt_metadata
        }
        for key, value in provenance.items():
            if not isinstance(value, str) or len(value) > 128:
                raise ValueError("Invalid content-free prompt provenance")
            if key.endswith("sha256") and (
                len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
            ):
                raise ValueError("Invalid prompt hash")
    return {
        "runtime_manifest_version": 1,
        "distributions": packages,
        "selected_distribution": selected_distribution,
        "plugin_api_version": 2,
        "providers": registry.manifest(),
        "configuration_sha256": profile_digest(profile),
        "operator_configuration_sha256": operator_digest(operator),
        "embedding": embedding,
        "reranker": operator.reranker.model_dump(mode="json"),
        "prompt_hashes": prompts,
        "prompt_provenance": provenance,
        "artifacts": [a.model_dump(mode="json") for a in artifacts],
    }


def write_runtime_manifest(destination, **kwargs):
    Path(destination).write_text(
        json.dumps(runtime_manifest(**kwargs), indent=2, sort_keys=True) + "\n"
    )
