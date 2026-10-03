from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any


@dataclass(frozen=True)
class RetrievalEvalCase:
    case_id: str
    query: str
    relevant_item_ids: frozenset[str] = frozenset()
    dataset_id: str | None = None
    dataset_role: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievalEvalResult:
    case_id: str
    retrieved_item_ids: tuple[str, ...]
    top_k: int
    latency_ms: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievalCaseMetrics:
    case_id: str
    evaluated: bool
    first_hit_rank: int | None
    recall_at_k: float | None
    mrr_at_k: float | None
    average_precision_at_k: float | None
    ndcg_at_k: float | None
    hit_at_1: float | None
    hit_at_3: float | None
    hit_at_5: float | None
    hit_at_10: float | None


def load_retrieval_eval_cases(path: str | Path) -> list[RetrievalEvalCase]:
    return [parse_retrieval_eval_case(row) for row in _load_eval_rows(path, key="cases")]


def load_retrieval_eval_results(path: str | Path) -> list[RetrievalEvalResult]:
    return [parse_retrieval_eval_result(row) for row in _load_eval_rows(path, key="results")]


def parse_retrieval_eval_case(row: dict[str, Any]) -> RetrievalEvalCase:
    case_id = str(row.get("case_id") or row.get("id") or "").strip()
    query = str(row.get("query") or row.get("query_text") or "").strip()
    if not case_id:
        raise ValueError("retrieval eval case must include case_id or id")
    if not query:
        raise ValueError(f"retrieval eval case {case_id!r} must include query")
    relevant_ids = (
        row.get("relevant_item_ids")
        or row.get("relevant_ids")
        or row.get("expected_item_ids")
        or row.get("expected_ids")
        or []
    )
    metadata = dict(row.get("metadata") or {})
    for key in ("expected_types", "expected_metadata", "non_match_ids", "visual_pair_ids"):
        if key in row and key not in metadata:
            metadata[key] = row[key]
    return RetrievalEvalCase(
        case_id=case_id,
        query=query,
        relevant_item_ids=frozenset(str(item_id) for item_id in relevant_ids),
        dataset_id=_optional_str(row.get("dataset_id")),
        dataset_role=_optional_str(row.get("dataset_role")),
        metadata=metadata,
    )


def parse_retrieval_eval_result(row: dict[str, Any]) -> RetrievalEvalResult:
    case_id = str(row.get("case_id") or row.get("id") or "").strip()
    if not case_id:
        raise ValueError("retrieval eval result must include case_id or id")
    retrieved_ids = (
        row.get("retrieved_item_ids") or row.get("retrieved_ids") or row.get("item_ids") or []
    )
    top_k = int(row.get("top_k") or len(retrieved_ids))
    latency = row.get("latency_ms")
    return RetrievalEvalResult(
        case_id=case_id,
        retrieved_item_ids=tuple(str(item_id) for item_id in retrieved_ids),
        top_k=max(top_k, 0),
        latency_ms=float(latency) if latency is not None else None,
        metadata=dict(row.get("metadata") or {}),
    )


def first_relevant_rank(
    retrieved_item_ids: Sequence[str], relevant_item_ids: set[str] | frozenset[str]
) -> int | None:
    if not relevant_item_ids:
        return None
    for rank, item_id in enumerate(retrieved_item_ids, start=1):
        if item_id in relevant_item_ids:
            return rank
    return None


def relevance_flags(
    retrieved_item_ids: Sequence[str],
    relevant_item_ids: set[str] | frozenset[str],
    *,
    top_k: int,
) -> list[bool]:
    if not relevant_item_ids:
        return []
    return [item_id in relevant_item_ids for item_id in retrieved_item_ids[: max(0, int(top_k))]]


def retrieval_case_metrics(
    case: RetrievalEvalCase, result: RetrievalEvalResult
) -> RetrievalCaseMetrics:
    evaluated = bool(case.relevant_item_ids)
    rank = first_relevant_rank(result.retrieved_item_ids[: result.top_k], case.relevant_item_ids)
    flags = relevance_flags(result.retrieved_item_ids, case.relevant_item_ids, top_k=result.top_k)
    return RetrievalCaseMetrics(
        case_id=case.case_id,
        evaluated=evaluated,
        first_hit_rank=rank,
        recall_at_k=(sum(flags) / len(case.relevant_item_ids)) if evaluated else None,
        mrr_at_k=(1.0 / rank) if rank is not None else (0.0 if evaluated else None),
        average_precision_at_k=average_precision_at_k(
            flags, len(case.relevant_item_ids), result.top_k
        ),
        ndcg_at_k=ndcg_at_k(flags, min(len(case.relevant_item_ids), result.top_k), result.top_k),
        hit_at_1=1.0 if rank == 1 else (0.0 if evaluated else None),
        hit_at_3=1.0 if rank is not None and rank <= 3 else (0.0 if evaluated else None),
        hit_at_5=1.0 if rank is not None and rank <= 5 else (0.0 if evaluated else None),
        hit_at_10=1.0 if rank is not None and rank <= 10 else (0.0 if evaluated else None),
    )


def summarize_retrieval_evaluation(
    cases: Sequence[RetrievalEvalCase],
    results: Sequence[RetrievalEvalResult],
) -> dict[str, Any]:
    case_by_id = {case.case_id: case for case in cases}
    matched_results = [result for result in results if result.case_id in case_by_id]
    per_case = [
        retrieval_case_metrics(case_by_id[result.case_id], result) for result in matched_results
    ]
    evaluated = [row for row in per_case if row.evaluated]
    latencies = [
        float(result.latency_ms) for result in matched_results if result.latency_ms is not None
    ]

    return {
        "count": len(matched_results),
        "relevance_eval_count": len(evaluated),
        "ground_truth_coverage_rate": safe_div(len(evaluated), len(matched_results)),
        "recall@k": mean_or_none([row.recall_at_k for row in evaluated]),
        "mrr@k": mean_or_none([row.mrr_at_k for row in evaluated]),
        "map@k": mean_or_none([row.average_precision_at_k for row in evaluated]),
        "ndcg@k": mean_or_none([row.ndcg_at_k for row in evaluated]),
        "hit@1": mean_or_none([row.hit_at_1 for row in evaluated]),
        "hit@3": mean_or_none([row.hit_at_3 for row in evaluated]),
        "hit@5": mean_or_none([row.hit_at_5 for row in evaluated]),
        "hit@10": mean_or_none([row.hit_at_10 for row in evaluated]),
        "error_rate": safe_div(
            sum(1 for result in matched_results if result.metadata.get("error")),
            len(matched_results),
        ),
        "latency_ms_avg": mean_or_none(latencies),
        "latency_ms_p50": percentile(latencies, 0.50),
        "latency_ms_p95": percentile(latencies, 0.95),
        "latency_ms_p99": percentile(latencies, 0.99),
    }


def dcg_at_k(relevant_flags: Sequence[bool], top_k: int) -> float:
    score = 0.0
    for rank, is_relevant in enumerate(relevant_flags[:top_k], start=1):
        if is_relevant:
            score += 1.0 / math.log2(rank + 1)
    return score


def ndcg_at_k(relevant_flags: Sequence[bool], relevant_count: int, top_k: int) -> float | None:
    if top_k <= 0 or relevant_count <= 0:
        return None
    dcg = dcg_at_k(relevant_flags, top_k)
    ideal_count = min(relevant_count, top_k)
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
    if idcg == 0.0:
        return None
    return dcg / idcg


def average_precision_at_k(
    relevant_flags: Sequence[bool], relevant_count: int, top_k: int
) -> float | None:
    if top_k <= 0 or relevant_count <= 0:
        return None
    hits = 0
    precision_sum = 0.0
    for rank, is_relevant in enumerate(relevant_flags[:top_k], start=1):
        if is_relevant:
            hits += 1
            precision_sum += hits / rank
    if hits == 0:
        return 0.0
    return precision_sum / min(relevant_count, top_k)


def safe_div(num: float, den: float) -> float | None:
    if den == 0:
        return None
    return num / den


def mean_or_none(values: Sequence[float | None]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return None
    return float(mean(clean))


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    if q <= 0:
        return float(min(values))
    if q >= 1:
        return float(max(values))
    sorted_values = sorted(float(value) for value in values)
    index = (len(sorted_values) - 1) * q
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return sorted_values[lower]
    fraction = index - lower
    return sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction


def _load_eval_rows(path: str | Path, *, key: str) -> list[dict[str, Any]]:
    eval_path = Path(path)
    text = eval_path.read_text(encoding="utf-8")
    if eval_path.suffix == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        payload = json.loads(text)
        if isinstance(payload, dict):
            rows = payload.get(key) or payload.get("items") or []
        else:
            rows = payload
    if not isinstance(rows, list):
        raise ValueError(
            f"{eval_path} must contain a JSON array, JSONL rows, or an object with {key!r}"
        )
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"{eval_path} eval rows must be JSON objects")
    return rows


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
