from __future__ import annotations

from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DenseIndexRequestV1(_ContractModel):
    enabled: bool = False
    collection_name: str | None = None
    reset_vector_store: bool = False
    pre_index_policy: str = "off"

    @model_validator(mode="after")
    def require_collection_when_enabled(self) -> DenseIndexRequestV1:
        if self.enabled and not self.collection_name:
            raise ValueError("collection_name is required when dense indexing is enabled")
        return self


class EngineBuildRequestV1(_ContractModel):
    """Portable input contract for one immutable engine candidate build."""

    schema_version: Literal[1] = 1
    build_id: str = Field(min_length=1, max_length=200)
    source_root: Path
    output_root: Path
    profile: str
    source_base_url: str = "https://sources.invalid"
    chunk_strategy: str | None = None
    chunk_set_name: str = "active-v1"
    dense_index: DenseIndexRequestV1 = Field(default_factory=DenseIndexRequestV1)
    deadline_at: datetime | None = None
    labels: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def distinct_roots(self) -> EngineBuildRequestV1:
        source = self.source_root.expanduser().resolve()
        output = self.output_root.expanduser().resolve()
        if source == output or source in output.parents or output in source.parents:
            raise ValueError("source_root and output_root must be separate directory trees")
        return self


class BuildArtifactV1(_ContractModel):
    kind: Literal[
        "canonical_database",
        "profile_snapshot",
        "chunk_report",
        "dense_index_report",
        "knowledge_graph_index",
    ]
    relative_path: str = Field(min_length=1)
    sha256: str | None = None

    @model_validator(mode="after")
    def require_safe_relative_path(self) -> BuildArtifactV1:
        path = PurePosixPath(self.relative_path)
        if path.is_absolute() or ".." in path.parts or "\\" in self.relative_path:
            raise ValueError("relative_path must be a safe POSIX path below output_root")
        return self


class BuildStageResultV1(_ContractModel):
    status: Literal["succeeded", "skipped", "failed"]
    duration_ms: float = Field(ge=0)
    counts: dict[str, int] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)


class EngineBuildResultV1(_ContractModel):
    """Machine-readable build outcome; stdout is never part of the contract."""

    schema_version: Literal[1] = 1
    build_id: str
    status: Literal["succeeded", "failed"]
    started_at: datetime
    finished_at: datetime
    engine_name: Literal["chatspark"] = "chatspark"
    engine_version: str
    request_sha256: str
    source_snapshot_sha256: str | None = None
    profile_digest: str | None = None
    corpus_id: str | None = None
    ingestion_run_id: str | None = None
    chunk_set_id: str | None = None
    index_run_id: str | None = None
    stages: dict[str, BuildStageResultV1] = Field(default_factory=dict)
    artifacts: list[BuildArtifactV1] = Field(default_factory=list)
    error: str | None = None


class EngineEvaluationRequestV1(_ContractModel):
    """Evaluate one immutable build against a caller-owned JSONL dataset."""

    schema_version: Literal[1] = 1
    build_result: Path
    database: Path
    dataset: Path
    profile: str
    top_k: int = Field(default=10, ge=1, le=100)


class EvaluationMetricSetV1(_ContractModel):
    count: int = Field(ge=0)
    recall_at_k: float | None = Field(default=None, ge=0, le=1)
    hit_at_k: float | None = Field(default=None, ge=0, le=1)
    mrr_at_k: float | None = Field(default=None, ge=0, le=1)
    map_at_k: float | None = Field(default=None, ge=0, le=1)
    ndcg_at_k: float | None = Field(default=None, ge=0, le=1)
    latency_ms_p50: float | None = Field(default=None, ge=0)
    latency_ms_p95: float | None = Field(default=None, ge=0)
    latency_ms_p99: float | None = Field(default=None, ge=0)


class EvaluationRankedEvidenceV1(_ContractModel):
    evidence_id: str
    rank: int = Field(ge=1)
    published_at: datetime | None = None
    source_updated_at: datetime | None = None
    observed_at: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    supersedes: list[str] = Field(default_factory=list)


class EvaluationTemporalExpectationV1(_ContractModel):
    mode: Literal["latest", "current", "as_of", "historical"]
    as_of: datetime | None = None
    superseded_evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_as_of_timestamp(self) -> EvaluationTemporalExpectationV1:
        if self.mode == "as_of" and self.as_of is None:
            raise ValueError("as_of is required for an as_of temporal expectation")
        return self


class EvaluationObservationV1(_ContractModel):
    question_id: str
    relevant_evidence_ids: list[str]
    stages: dict[str, list[EvaluationRankedEvidenceV1]]
    slices: dict[str, str] = Field(default_factory=dict)
    latency_ms: float = Field(ge=0)
    temporal: EvaluationTemporalExpectationV1 | None = None


class EngineEvaluationResultV1(_ContractModel):
    schema_version: Literal[1] = 1
    build_id: str
    status: Literal["succeeded", "failed"]
    dataset_sha256: str
    examples: int = Field(ge=0)
    metrics: EvaluationMetricSetV1 | None = None
    stage_metrics: dict[str, EvaluationMetricSetV1] = Field(default_factory=dict)
    observations: list[EvaluationObservationV1] = Field(default_factory=list)
    error: str | None = None


def engine_contract_schema_v1() -> dict[str, Any]:
    """Return the engine-owned request/result schema bundle for consumers."""
    return {
        "schema_version": "chatspark.engine-contracts.v1",
        "request": EngineBuildRequestV1.model_json_schema(),
        "result": EngineBuildResultV1.model_json_schema(),
        "evaluation_request": EngineEvaluationRequestV1.model_json_schema(),
        "evaluation_result": EngineEvaluationResultV1.model_json_schema(),
    }
