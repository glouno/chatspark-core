import json
from functools import lru_cache

from chatspark.profiles import load_profile
from chatspark.retrieval.pipeline import StrategyRetriever, execution_context
from chatspark.runtime.config import settings
from chatspark.runtime.registry import get_registry


def get_embedder():
    from chatspark.runtime.validation import validate_embedding_configuration

    validate_embedding_configuration()
    return _cached_embedder(
        settings.EMBEDDING_BACKEND,
        settings.EMBEDDING_MODEL,
        settings.EMBEDDING_DEVICE,
        settings.EMBEDDING_NORMALIZE,
        settings.EMBEDDING_TEXT_FORMAT,
        settings.EMBEDDING_BATCH_SIZE,
        settings.OPENAI_COMPAT_BASE_URL,
        settings.OPENAI_COMPAT_API_KEY,
    )


@lru_cache(maxsize=4)
def _cached_embedder(backend, model, device, normalize, text_format, batch_size, url, key):
    if backend == "deterministic":
        from chatspark.embeddings.deterministic import DeterministicEmbedder

        return DeterministicEmbedder(model_name=model, normalize_embeddings=normalize)
    if backend == "openai":
        from chatspark.embeddings.openai_compatible import OpenAICompatibleEmbedder

        return OpenAICompatibleEmbedder(
            base_url=url,
            model_name=model,
            api_key=key,
            text_format=text_format,
            normalize=normalize,
        )
    if backend == "sentence-transformers":
        from chatspark.embeddings.local import LocalEmbedder

        return LocalEmbedder(
            model,
            device=device,
            normalize=normalize,
            text_format=text_format,
            batch_size=batch_size,
        )
    raise ValueError("Unsupported embedding backend")


def get_vectorstore():
    from chatspark.storage.qdrant import QdrantVectorStore

    return QdrantVectorStore(
        collection_name=settings.QDRANT_COLLECTION,
        url=settings.QDRANT_URL,
        api_key=settings.QDRANT_API_KEY,
        timeout=settings.QDRANT_TIMEOUT_SECONDS,
    )


class DenseLane:
    def __init__(self):
        self.embedder = get_embedder()
        self.store = get_vectorstore()

    def retrieve(self, query, limit, context):
        from chatspark.plugins.contracts import RankedEvidence

        embedding = self.embedder.embed_query_texts([query])[0]
        filters = {
            **context.filters,
            "corpus_id": context.corpus_id,
            "chunk_set_id": context.chunk_set_id,
        }
        return [
            RankedEvidence(r.item_id, float(r.score))
            for r in self.store.query(embedding, limit, filters)
        ]


def get_retriever(*, database=None, profile=None, chunk_set_id=None, filters=None):
    profile = load_profile(settings.CHATSPARK_PROFILE) if profile is None else profile
    from chatspark.runtime.validation import validate_runtime
    from chatspark.storage.evidence import narrow_filters

    validate_runtime(profile, require_dense=True)

    authorized = json.loads(settings.CHATSPARK_AUTHORIZED_FILTERS)
    if not isinstance(authorized, dict):
        raise ValueError("Authorized filters must be a JSON mapping")
    context = execution_context(
        database or settings.CHATSPARK_V3_DB_PATH,
        narrow_filters(authorized, filters or {}),
        chunk_set_id=chunk_set_id or settings.CHATSPARK_CHUNK_SET_ID or None,
    )
    dense = DenseLane() if profile.retrieval.enable_dense else None
    if dense:
        from chatspark.runtime.embedding_manifest import validate_dense_index

        validate_dense_index(context, dense.embedder, dense.store)
    return StrategyRetriever(
        context=context,
        profile=profile.retrieval,
        registry=get_registry(),
        dense=dense,
        embedder=dense.embedder if dense else None,
        policy=profile.policy,
    )
