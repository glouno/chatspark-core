"""API-2 trusted extension contracts. These do not sandbox installed code."""

import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from chatspark.documents import DocContent


class ProviderSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    settings: dict[str, Any] = {}


@dataclass(frozen=True)
class ExecutionContext:
    database: Path
    corpus_id: str
    chunk_set_id: str
    snapshot_sha256: str
    filters: Mapping[str, Any] = field(default_factory=dict)
    deadline_at: datetime | None = None
    requested_limit: int | None = None

    def __post_init__(self):
        object.__setattr__(
            self,
            "filters",
            MappingProxyType(
                {k: tuple(v) if isinstance(v, list) else v for k, v in self.filters.items()}
            ),
        )


@dataclass(frozen=True)
class RankedEvidence:
    evidence_id: str
    score: float

    def __post_init__(self):
        if not self.evidence_id or not math.isfinite(self.score):
            raise ValueError("Evidence ID and finite score are required")


@dataclass(frozen=True)
class LaneRequest:
    lane: str
    query: str
    limit: int
    filters: Mapping[str, Any] = field(default_factory=dict)
    weight: float = 1.0


@dataclass(frozen=True)
class StrategyResult:
    evidence: Sequence[RankedEvidence]
    stages: Mapping[str, Sequence[RankedEvidence]] = field(default_factory=dict)
    diagnostics: Mapping[str, int | float | bool] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievalServices:
    registry: Any
    dense: Any = None
    embedder: Any = None
    reranker: Any = None


class RequestPolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["allow", "block_retrieval"] = "allow"


class RequestPolicy(Protocol):
    def evaluate(self, query: str, context: ExecutionContext) -> RequestPolicyDecision: ...


class ResolvedPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str = Field(min_length=1, max_length=100000)
    revision: str = Field(min_length=1, max_length=128)
    implementation_version: str = Field(min_length=1, max_length=128)


class PromptProvider(Protocol):
    def get_system_prompt(self) -> ResolvedPrompt: ...


class RetrievalStrategy(Protocol):
    def retrieve(
        self,
        query: str,
        limit: int,
        context: ExecutionContext,
        policy: Any,
        services: RetrievalServices,
    ) -> StrategyResult: ...


class RetrievalLane(Protocol):
    def retrieve(
        self, query: str, limit: int, context: ExecutionContext
    ) -> Sequence[RankedEvidence]: ...


class RetrievalPlanner(Protocol):
    def plan(self, query: str, context: ExecutionContext) -> Sequence[LaneRequest]: ...


class CandidateProcessor(Protocol):
    def process(
        self, query: str, candidates: Sequence[RankedEvidence], context: ExecutionContext
    ) -> Sequence[RankedEvidence]: ...


class ContextBuilder(Protocol):
    def build(
        self, query: str, candidates: Sequence[RankedEvidence], context: ExecutionContext
    ) -> Sequence[RankedEvidence]: ...


class ArtifactBuilder(Protocol):
    def build(
        self, database: Path, output_root: Path, context: ExecutionContext
    ) -> Mapping[str, Any]: ...


class ExtractedPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    page_index: int = Field(ge=0, le=9999)
    text: str
    markdown: str = ""
    method: Literal["native", "ocr"] = "native"


class PageExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    content: DocContent
    pages: list[ExtractedPage] = Field(default_factory=list, max_length=10000)

    @field_validator("pages")
    @classmethod
    def ordered_pages(cls, pages):
        if [page.page_index for page in pages] != list(range(len(pages))):
            raise ValueError("Extraction pages must be unique, contiguous and in source order")
        return pages


class PdfExtractor(Protocol):
    def extract(self, payload: bytes, options: Mapping[str, Any]) -> PageExtractionResult: ...


class PdfInspector(Protocol):
    def inspect(self, payload: bytes, options: Mapping[str, Any]) -> Mapping[str, Any]: ...


class OcrProvider(Protocol):
    def recognize(self, payload: bytes, options: Mapping[str, Any]) -> Any: ...
