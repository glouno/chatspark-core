from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from chatspark.core.evaluation import (
    RetrievalEvalCase,
    RetrievalEvalResult,
    retrieval_case_metrics,
)
from chatspark.engine.contracts import (
    EngineBuildResultV1,
    EngineEvaluationRequestV1,
    EngineEvaluationResultV1,
    EvaluationMetricSetV1,
    EvaluationObservationV1,
    EvaluationRankedEvidenceV1,
)
from chatspark.retrieval.models import RetrievedChunk
from chatspark.runtime.factory import get_retriever

_STAGES = ("dense", "sparse", "graph", "fused", "score_adjusted", "reranked", "final")


def execute_evaluation(
    request: EngineEvaluationRequestV1, *, retriever_factory=None
) -> EngineEvaluationResultV1:
    """Run the configured retrieval pipeline and return content-free evidence rankings."""
    dataset_payload = request.dataset.read_bytes()
    dataset_sha256 = hashlib.sha256(dataset_payload).hexdigest()
    build = EngineBuildResultV1.model_validate_json(
        request.build_result.read_text(encoding="utf-8")
    )
    result = EngineEvaluationResultV1(
        build_id=build.build_id,
        status="failed",
        dataset_sha256=dataset_sha256,
        examples=0,
    )
    try:
        if build.status != "succeeded" or not build.chunk_set_id:
            raise ValueError("evaluation requires a successful build with a chunk set")
        _verify_database_artifact(build, request.database)
        rows = _load_dataset(dataset_payload)
        ground_truth = _resolve_ground_truth(request.database, build.chunk_set_id, rows)
        from chatspark.profiles import load_profile

        retriever = (
            retriever_factory(request, build)
            if retriever_factory
            else get_retriever(
                database=request.database,
                profile=load_profile(request.profile),
                chunk_set_id=build.chunk_set_id,
            )
        )
        observations: list[EvaluationObservationV1] = []
        for index, row in enumerate(rows, start=1):
            relevant, granularity = ground_truth[index - 1]
            if not relevant:
                continue
            top_k = int(row.get("top_k") or request.top_k)
            from time import perf_counter

            started = perf_counter()
            retrieval = retriever.retrieve_result(
                str(row["question"]),
                top_k,
                dict(row.get("filters") or {}),
            )
            stages = {
                name: [
                    _ranked_evidence(item, rank, granularity=granularity)
                    for rank, item in enumerate(
                        _deduplicate_evidence(items, granularity=granularity)[:top_k],
                        start=1,
                    )
                ]
                for name, items in {
                    **retrieval.stages,
                    "final": retrieval.items,
                }.items()
                if name in _STAGES
            }
            observations.append(
                EvaluationObservationV1(
                    question_id=str(row.get("question_id") or f"line-{index}"),
                    relevant_evidence_ids=sorted(relevant),
                    stages=stages,
                    slices={str(k): str(v) for k, v in dict(row.get("slices") or {}).items()},
                    latency_ms=(perf_counter() - started) * 1000,
                    temporal=row.get("temporal"),
                )
            )
        result.observations = observations
        result.examples = len(observations)
        result.stage_metrics = {
            stage: _summarize_stage(observations, stage, request.top_k)
            for stage in _STAGES
            if any(stage in row.stages for row in observations)
        }
        result.metrics = result.stage_metrics.get("final")
        result.status = "succeeded"
    except Exception as exc:
        from chatspark.runtime.errors import safe_error

        result.error = safe_error(exc, "Evaluation")
    return result


def execute_evaluation_file(
    request_path: str | Path, result_path: str | Path
) -> EngineEvaluationResultV1:
    request = EngineEvaluationRequestV1.model_validate_json(
        Path(request_path).read_text(encoding="utf-8")
    )
    result = execute_evaluation(request)
    path = Path(result_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return result


def _verify_database_artifact(build: EngineBuildResultV1, database: Path) -> None:
    expected = next(
        (item.sha256 for item in build.artifacts if item.kind == "canonical_database"),
        None,
    )
    if expected is None:
        raise ValueError("build result has no canonical database digest")
    actual = hashlib.sha256(database.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError("canonical database digest does not match the build result")


def _load_dataset(payload: bytes) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line_number, line in enumerate(payload.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or not str(row.get("question") or "").strip():
            raise ValueError(f"evaluation line {line_number} has no question")
        rows.append(row)
    if not rows:
        raise ValueError("evaluation dataset is empty")
    return rows


def _resolve_ground_truth(
    database: Path, chunk_set_id: str, rows: list[dict[str, object]]
) -> list[tuple[set[str], str]]:
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as connection:
        corpus_rows = connection.execute(
            """SELECT c.chunk_id, d.document_id, d.canonical_uri
               FROM chunks AS c
               JOIN document_revisions AS r ON r.revision_id = c.revision_id
               JOIN documents AS d ON d.document_id = r.document_id
               WHERE c.chunk_set_id = ?""",
            (chunk_set_id,),
        ).fetchall()
    resolved: list[tuple[set[str], str]] = []
    for row in rows:
        chunk_ids = {str(value) for value in row.get("expected_chunk_ids", [])}
        doc_ids = {str(value) for value in row.get("expected_doc_ids", [])}
        exact_urls = {str(value) for value in row.get("expected_urls", [])}
        url_fragments = [
            str(value).casefold()
            for field in ("expected_url_substrings", "source_hint_patterns")
            for value in row.get(field, [])
        ]
        has_document_signals = bool(doc_ids or exact_urls or url_fragments)
        if chunk_ids and has_document_signals:
            raise ValueError("evaluation rows cannot mix chunk and document/URL relevance")
        for _chunk_id, document_id, canonical_uri in corpus_rows:
            uri = str(canonical_uri)
            if (
                str(document_id) in doc_ids
                or uri in exact_urls
                or any(fragment in uri.casefold() for fragment in url_fragments)
            ):
                doc_ids.add(str(document_id))
        resolved.append((doc_ids, "document") if has_document_signals else (chunk_ids, "chunk"))
    return resolved


def _summarize_stage(
    observations: list[EvaluationObservationV1], stage: str, default_top_k: int
) -> EvaluationMetricSetV1:
    metrics: list[tuple[object, float]] = []
    for row in observations:
        ids = [item.evidence_id for item in row.stages.get(stage, [])]
        case = RetrievalEvalCase(
            case_id=row.question_id,
            query="redacted",
            relevant_item_ids=frozenset(row.relevant_evidence_ids),
        )
        evaluated = retrieval_case_metrics(
            case,
            RetrievalEvalResult(
                case_id=row.question_id,
                retrieved_item_ids=tuple(ids),
                top_k=default_top_k,
                latency_ms=row.latency_ms,
            ),
        )
        metrics.append((evaluated, row.latency_ms))
    if not metrics:
        return EvaluationMetricSetV1(count=0)
    values = [item for item, _ in metrics]
    latencies = sorted(latency for _, latency in metrics)
    return EvaluationMetricSetV1(
        count=len(values),
        recall_at_k=sum(float(item.recall_at_k or 0) for item in values) / len(values),
        hit_at_k=sum(float(item.first_hit_rank is not None) for item in values) / len(values),
        mrr_at_k=sum(float(item.mrr_at_k or 0) for item in values) / len(values),
        map_at_k=sum(float(item.average_precision_at_k or 0) for item in values) / len(values),
        ndcg_at_k=sum(float(item.ndcg_at_k or 0) for item in values) / len(values),
        latency_ms_p50=_percentile(latencies, 0.50),
        latency_ms_p95=_percentile(latencies, 0.95),
        latency_ms_p99=_percentile(latencies, 0.99),
    )


def _percentile(values: list[float], quantile: float) -> float:
    index = (len(values) - 1) * quantile
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def _deduplicate_evidence(items: list[RetrievedChunk], *, granularity: str) -> list[RetrievedChunk]:
    selected: list[RetrievedChunk] = []
    seen: set[str] = set()
    for item in items:
        identity = _evidence_id(item, granularity)
        if identity in seen:
            continue
        seen.add(identity)
        selected.append(item)
    return selected


def _evidence_id(item: RetrievedChunk, granularity: str) -> str:
    if granularity == "document":
        return str(item.metadata.get("doc_id") or item.chunk_id)
    return str(item.chunk_id)


def _ranked_evidence(
    item: RetrievedChunk, rank: int, *, granularity: str
) -> EvaluationRankedEvidenceV1:
    metadata = dict(item.metadata or {})
    return EvaluationRankedEvidenceV1(
        evidence_id=_evidence_id(item, granularity),
        rank=rank,
        published_at=metadata.get("published_at"),
        source_updated_at=metadata.get("source_updated_at") or metadata.get("last_modified"),
        observed_at=metadata.get("observed_at") or metadata.get("fetched_at"),
        valid_from=metadata.get("valid_from"),
        valid_to=metadata.get("valid_to"),
        supersedes=[str(value) for value in metadata.get("supersedes", [])],
    )
