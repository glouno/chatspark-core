from dataclasses import dataclass, field
from typing import Any


@dataclass
class RetrievedChunk:
    chunk_id: str
    content: str
    metadata: dict[str, Any]
    score: float
    source: str
    embedding: list[float] | None = None


@dataclass
class ChunkRetrievalResult:
    items: list[RetrievedChunk]
    diagnostics: dict[str, Any] = field(default_factory=dict)
    stages: dict[str, list[RetrievedChunk]] = field(default_factory=dict)
