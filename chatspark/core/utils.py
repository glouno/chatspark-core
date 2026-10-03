from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from pathlib import Path
from statistics import mean
from typing import Any

from chatspark.core.evaluation import average_precision_at_k as _average_precision_at_k
from chatspark.core.evaluation import dcg_at_k as _dcg_at_k
from chatspark.core.evaluation import ndcg_at_k as _ndcg_at_k


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{lineno}: {exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"Expected JSON object at {path}:{lineno}")
            rows.append(payload)
    return rows


load_golden = load_jsonl


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def safe_div(numerator: float, denominator: float) -> float | None:
    return None if denominator == 0 else numerator / denominator


def tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", (text or "").lower())


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    if q <= 0:
        return float(min(values))
    if q >= 1:
        return float(max(values))
    ordered = sorted(float(value) for value in values)
    index = (len(ordered) - 1) * q
    lower, upper = math.floor(index), math.ceil(index)
    if lower == upper:
        return ordered[lower]
    fraction = index - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def mean_or_none(values: Sequence[float]) -> float | None:
    return float(mean(values)) if values else None


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def token_f1(prediction: str, reference: str) -> float | None:
    predicted, expected = set(tokenize(prediction)), set(tokenize(reference))
    if not predicted or not expected:
        return None
    overlap = len(predicted & expected)
    if not overlap:
        return 0.0
    precision, recall = overlap / len(predicted), overlap / len(expected)
    return 2 * precision * recall / (precision + recall)


def dcg_at_k(relevant_flags: Sequence[bool], k: int) -> float:
    return _dcg_at_k(relevant_flags, k)


def ndcg_at_k(relevant_flags: Sequence[bool], k: int) -> float | None:
    return _ndcg_at_k(relevant_flags, sum(relevant_flags), k)


def average_precision_at_k(relevant_flags: Sequence[bool], k: int) -> float | None:
    return _average_precision_at_k(relevant_flags, sum(relevant_flags), k)
