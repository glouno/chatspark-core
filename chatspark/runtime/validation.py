from pathlib import Path

from chatspark.runtime.config import settings
from chatspark.runtime.registry import get_registry


def validate_embedding_configuration():
    """Validate configuration without contacting services or loading models."""
    if settings.EMBEDDING_BACKEND not in {"deterministic", "openai", "sentence-transformers"}:
        raise ValueError(
            "Configure EMBEDDING_BACKEND (openai or sentence-transformers) and EMBEDDING_MODEL "
            "for semantic search; deterministic is an explicit test backend only"
        )
    if not settings.EMBEDDING_MODEL.strip():
        raise ValueError("Configure EMBEDDING_MODEL for the selected embedding backend")


def validate_runtime(profile, *, registry=None, require_dense=False):
    if require_dense and profile.retrieval.enable_dense:
        validate_embedding_configuration()
        from importlib.util import find_spec

        if find_spec("qdrant_client") is None:
            raise ValueError("Dense retrieval requires the chatspark-core[qdrant] extra")
        if (
            settings.EMBEDDING_BACKEND == "sentence-transformers"
            and find_spec("sentence_transformers") is None
        ):
            raise ValueError(
                "Local semantic embeddings require the chatspark-core[sentence-transformers] extra"
            )
    registry = registry or get_registry()
    selections = [
        (profile.extraction.html, "html"),
        (profile.extraction.pdf.provider, "pdf"),
        *[(p, "pdf") for p in profile.extraction.pdf.fallbacks],
        (profile.chunking, "chunker"),
        (profile.retrieval.strategy, "strategy"),
        (profile.retrieval.context, "context"),
        *[(p, "artifact") for p in profile.artifact_builders],
    ]
    if profile.policy:
        selections.append((profile.policy, "policy"))
    if profile.retrieval.strategy.provider == "hybrid":
        _, options = registry.descriptor(profile.retrieval.strategy, "strategy")
        if not (profile.retrieval.enable_dense or profile.retrieval.enable_sparse or options.lanes):
            raise ValueError("Hybrid strategy requires at least one lane")
        selections.extend((lane.selection(), "retrieval") for lane in options.lanes)
        for value, capability in [(options.planner, "planner"), (options.processor, "processor")]:
            if value:
                selections.append((value, capability))
    if profile.extraction.ocr:
        selections.append((profile.extraction.ocr, "ocr"))
        if profile.extraction.ocr.provider == "tesseract":
            if not profile.extraction.pdf.inspector:
                raise ValueError("Tesseract PDF OCR requires an explicit page inspector")
            selections.append((profile.extraction.pdf.inspector, "pdf-inspector"))
            registry.resolve(profile.extraction.ocr, "ocr").validate_setup()
    if profile.generation.remote_prompt:
        selections.append((profile.generation.remote_prompt, "prompt"))
    for selection, capability in selections:
        registry.descriptor(selection, capability)
    for name in ("system_prompt", "user_prompt"):
        filename = getattr(profile.generation, name)
        if filename and not Path(filename).is_file():
            raise ValueError("Missing explicit prompt override")
    from string import Template

    from chatspark.prompts import load_prompt

    for field, prompt_id, allowed in (
        ("system_prompt", "rag.classic_chat.system.v6", set()),
        ("user_prompt", "rag.classic_chat.user.v2", {"question", "context"}),
    ):
        override = getattr(profile.generation, field)
        template = Template(
            Path(override).read_text() if override else load_prompt(prompt_id).content
        )
        if not template.is_valid() or not set(template.get_identifiers()) <= allowed:
            raise ValueError("Malformed prompt template or unsupported variable")
    return registry
