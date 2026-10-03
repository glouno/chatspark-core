"""HTTP request/result v1. Canonical citation IDs belong to the engine."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=16000)
    filters: dict[str, object] | None = None
    top_k: int | None = Field(default=None, ge=1, le=100)
    corpus_scope: Literal["unified"] | None = None


class SourceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chunk_id: str
    doc_id: str | None = None
    url: str
    title: str
    heading_path: list[str]
    retrieval_corpus_id: str | None = None
    selected_corpus_scope: str | None = None


class ChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str
    sources: list[SourceItem]
