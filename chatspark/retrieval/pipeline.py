import itertools
import math
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime

from chatspark.plugins.contracts import (
    ExecutionContext,
    LaneRequest,
    ProviderSelection,
    RankedEvidence,
    RequestPolicyDecision,
    RetrievalServices,
    StrategyResult,
)
from chatspark.retrieval.models import ChunkRetrievalResult
from chatspark.storage.evidence import EvidenceError, EvidenceStore, narrow_filters, snapshot_digest


def _check_deadline(context):
    if context.deadline_at and datetime.now(UTC) >= context.deadline_at:
        raise TimeoutError("Retrieval deadline exceeded")


def _validate_request(request, lanes):
    if (
        not isinstance(request, LaneRequest)
        or not isinstance(request.lane, str)
        or request.lane not in lanes
        or type(request.limit) is not int
        or not 1 <= request.limit <= 1000
        or not isinstance(request.query, str)
        or not request.query.strip()
        or len(request.query) > 16000
        or type(request.weight) not in (int, float)
        or not 0 <= request.weight <= 100
        or not math.isfinite(request.weight)
    ):
        raise EvidenceError("Invalid planned retrieval request")


def execution_context(database, filters=None, *, chunk_set_id=None):
    store = EvidenceStore(database)
    with store._connect() as con:
        if chunk_set_id:
            row = con.execute(
                "SELECT corpus_id,chunk_set_id FROM chunk_sets WHERE chunk_set_id=? AND status='succeeded'",
                (chunk_set_id,),
            ).fetchone()
        else:
            rows = con.execute(
                "SELECT corpus_id,chunk_set_id FROM chunk_sets WHERE status='succeeded' ORDER BY created_at DESC,chunk_set_id"
            ).fetchall()
            if len(rows) != 1:
                raise EvidenceError("Select exactly one canonical chunk set")
            row = rows[0]
    if row is None:
        raise EvidenceError("Missing successful chunk set")
    return ExecutionContext(
        store.database,
        row["corpus_id"],
        row["chunk_set_id"],
        snapshot_digest(store.database),
        filters or {},
    )


class HybridRetriever:
    def __init__(self, *, context, profile, registry, dense=None, options=None, embedder=None):
        self.context = context
        self.profile = profile
        self.registry = registry
        from chatspark.profiles.models import HybridSettings

        self.options = options or HybridSettings()
        self.embedder = embedder
        self.dense = dense
        self.lanes = {}
        if profile.enable_sparse:
            self.lanes["sparse"] = registry.resolve(
                ProviderSelection(
                    provider="sparse", settings={"fts_bm25_weights": self.options.fts_bm25_weights}
                ),
                "retrieval",
            )
        if profile.enable_dense:
            if dense is None:
                raise ValueError("Dense retrieval requires a configured vector provider")
            self.lanes["dense"] = dense
        for lane in self.options.lanes:
            self.lanes[lane.name] = registry.resolve(lane.selection(), "retrieval")
        self.planner = (
            registry.resolve(self.options.planner, "planner") if self.options.planner else None
        )
        self.processor = (
            registry.resolve(self.options.processor, "processor")
            if self.options.processor
            else None
        )
        self.context_builder = None

    def retrieve(self, query_text, k, filters=None):
        return self.retrieve_result(query_text, k, filters).items

    def retrieve_result(self, query_text, k, filters=None):
        if type(k) is not int or not 1 <= k <= 100:
            raise EvidenceError("Result limit must be between 1 and 100")
        if not isinstance(query_text, str) or not query_text.strip() or len(query_text) > 16000:
            raise EvidenceError("Invalid retrieval query")
        context = replace(
            self.context,
            filters=narrow_filters(self.context.filters, {} if filters is None else filters),
            requested_limit=k,
        )
        _check_deadline(context)
        store = EvidenceStore(context.database)
        stages = {}
        streams = []
        weights = []
        if self.planner:
            requests = list(itertools.islice(self.planner.plan(query_text, context), 17))
            if len(requests) > 16:
                raise EvidenceError("Planner exceeded request budget")
            _check_deadline(context)
        else:
            requests = [
                LaneRequest(
                    name,
                    query_text,
                    self.profile.dense_k
                    if name == "dense"
                    else self.profile.sparse_k
                    if name == "sparse"
                    else next(lane.limit for lane in self.options.lanes if lane.name == name),
                    weight=self.profile.rrf_dense_weight
                    if name == "dense"
                    else self.profile.rrf_sparse_weight
                    if name == "sparse"
                    else next(lane.weight for lane in self.options.lanes if lane.name == name),
                )
                for name in self.lanes
            ]
        for request in requests:
            _validate_request(request, self.lanes)
            _check_deadline(context)
            scoped = replace(
                context,
                filters=narrow_filters(context.filters, request.filters),
                requested_limit=request.limit,
            )
            candidates = list(
                itertools.islice(
                    self.lanes[request.lane].retrieve(request.query, request.limit, scoped),
                    request.limit + 1,
                )
            )
            _check_deadline(context)
            chunks = store.resolve(candidates, scoped, maximum=request.limit)
            stages.setdefault(request.lane, []).extend(chunks)
            streams.append(chunks)
            weights.append(request.weight)
        scores = defaultdict(float)
        for stream, weight in zip(streams, weights, strict=True):
            for rank, chunk in enumerate(stream, 1):
                scores[chunk.chunk_id] += weight / (self.profile.rrf_k + rank)
        ranked = [
            RankedEvidence(cid, score)
            for cid, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        ]
        stages["fused"] = store.resolve(ranked, context)
        if self.processor:
            _check_deadline(context)
            allowed = {c.evidence_id for c in ranked}
            ranked = list(
                itertools.islice(self.processor.process(query_text, ranked, context), 1001)
            )
            _check_deadline(context)
            store.resolve(ranked, context, allowed_ids=allowed)
        chunks = store.resolve(ranked[: self.profile.rerank_input_k], context)
        if self.profile.reranker.backend != "none":
            _check_deadline(context)
            from chatspark.retrieval.rerankers import rerank

            chunks = rerank(query_text, chunks, self.profile.reranker)
            _check_deadline(context)
            ranked = [RankedEvidence(c.chunk_id, c.score) for c in chunks]
            chunks = store.resolve(
                ranked, context, allowed_ids={c.chunk_id for c in stages["fused"]}
            )
        stages["reranked"] = chunks
        if self.profile.final_selector == "mmr":
            from chatspark.retrieval.rerankers import lexical_mmr

            if self.options.mmr_mode == "embedding":
                from chatspark.retrieval.rerankers import embedding_mmr

                if self.embedder is None:
                    raise ValueError("Embedding MMR requires an explicitly configured embedder")
                chunks = embedding_mmr(chunks, k, self.profile.mmr_lambda, self.embedder)
            else:
                chunks = lexical_mmr(chunks, k, self.profile.mmr_lambda)
        selected = []
        counts = defaultdict(int)
        for chunk in chunks:
            doc = chunk.metadata["doc_id"]
            if self.profile.max_chunks_per_doc and counts[doc] >= self.profile.max_chunks_per_doc:
                continue
            selected.append(chunk)
            counts[doc] += 1
            if len(selected) >= min(k, self.profile.final_k):
                break
        if self.context_builder:
            _check_deadline(context)
            selected = store.resolve(
                list(
                    itertools.islice(
                        self.context_builder.build(
                            query_text,
                            [RankedEvidence(c.chunk_id, c.score) for c in selected],
                            replace(context, requested_limit=min(k, self.profile.final_k)),
                        ),
                        min(k, self.profile.final_k) + 1,
                    )
                ),
                context,
                maximum=min(k, self.profile.final_k),
            )
            _check_deadline(context)
        # Revalidate even if a provider modified state after its first return.
        selected = store.resolve([RankedEvidence(c.chunk_id, c.score) for c in selected], context)
        _check_deadline(context)
        return ChunkRetrievalResult(
            selected, {"providers": self.registry.manifest()}, {**stages, "final": selected}
        )


class HybridStrategy:
    """Conventional retrieval, completing fusion/reranking/selection exactly once."""

    def __init__(self, options):
        self.options = options

    def retrieve(self, query, limit, context, policy, services):
        if not (policy.enable_dense or policy.enable_sparse or self.options.lanes):
            raise ValueError("Hybrid strategy requires at least one lane")
        result = HybridRetriever(
            context=context,
            profile=policy,
            registry=services.registry,
            dense=services.dense,
            options=self.options,
            embedder=services.embedder,
        ).retrieve_result(query, limit)
        return StrategyResult(
            tuple(RankedEvidence(c.chunk_id, c.score) for c in result.items),
            {
                name: tuple(RankedEvidence(c.chunk_id, c.score) for c in chunks)
                for name, chunks in result.stages.items()
            },
        )


class CanonicalContext:
    def build(self, query, candidates, context):
        return candidates


class StrategyRetriever:
    """Resolve all provider IDs through canonical storage; no duplicate ranking stages."""

    def __init__(self, *, context, profile, registry, dense=None, embedder=None, policy=None):
        self.context = context
        self.profile = profile
        self.registry = registry
        self.strategy = registry.resolve(profile.strategy, "strategy")
        self.context_builder = registry.resolve(profile.context, "context")
        self.services = RetrievalServices(registry=registry, dense=dense, embedder=embedder)
        self.request_policy = registry.resolve(policy, "policy") if policy else None

    def warmup(self):
        """Exercise the selected strategy using a generic query and canonical IDs."""
        self.retrieve_result("Service readiness check", 1)
        warm = getattr(self.strategy, "warmup", None)
        if callable(warm):
            warm(self.context, self.profile, self.services)

    def retrieve(self, query_text, k, filters=None):
        return self.retrieve_result(query_text, k, filters).items

    def retrieve_result(self, query_text, k, filters=None):
        if type(k) is not int or not 1 <= k <= 100:
            raise EvidenceError("Result limit must be between 1 and 100")
        if not isinstance(query_text, str) or not query_text.strip() or len(query_text) > 16000:
            raise EvidenceError("Invalid retrieval query")
        limit = min(k, self.profile.final_k)
        context = replace(
            self.context,
            requested_limit=limit,
            filters=narrow_filters(self.context.filters, {} if filters is None else filters),
        )
        _check_deadline(context)
        if self.request_policy:
            decision = self.request_policy.evaluate(query_text, context)
            if not isinstance(decision, RequestPolicyDecision):
                raise EvidenceError("Invalid request policy decision")
            _check_deadline(context)
            if decision.action == "block_retrieval":
                return ChunkRetrievalResult([], {"request_policy_blocked": True}, {"final": []})
        result = self.strategy.retrieve(query_text, limit, context, self.profile, self.services)
        _check_deadline(context)
        if not isinstance(result, StrategyResult) or not isinstance(result.stages, dict):
            raise EvidenceError("Invalid strategy result")
        if len(result.stages) > 32 or len(result.diagnostics) > 32:
            raise EvidenceError("Strategy diagnostics exceeded budget")
        if any(
            not isinstance(key, str)
            or not 1 <= len(key) <= 64
            or type(value) not in (int, float, bool)
            or not math.isfinite(value)
            for key, value in result.diagnostics.items()
        ):
            raise EvidenceError("Strategy diagnostics must be bounded and content-free")
        store = EvidenceStore(context.database)
        stages = {}
        for name, records in result.stages.items():
            if not isinstance(name, str) or not 1 <= len(name) <= 64:
                raise EvidenceError("Invalid ranking stage name")
            bounded = list(itertools.islice(records, 1001))
            stages[name] = bounded
        stages = store.resolve_groups(stages, context)
        selected = store.resolve(
            list(itertools.islice(result.evidence, limit + 1)), context, maximum=limit
        )
        expanded = list(
            itertools.islice(
                self.context_builder.build(
                    query_text,
                    tuple(RankedEvidence(c.chunk_id, c.score) for c in selected),
                    context,
                ),
                limit + 1,
            )
        )
        _check_deadline(context)
        selected = store.resolve(expanded, context, maximum=limit)
        _check_deadline(context)
        return ChunkRetrievalResult(
            selected,
            {
                "providers": self.registry.manifest(),
                "strategy": self.profile.strategy.provider,
                "diagnostics": dict(result.diagnostics),
            },
            {**stages, "final": selected},
        )
