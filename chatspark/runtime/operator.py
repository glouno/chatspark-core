"""Strict secret-free operator configuration, separate from corpus behavior."""

import hashlib
import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import Field, field_validator

from chatspark.profiles.models import StrictModel


class EmbeddingBinding(StrictModel):
    backend: Literal["", "openai", "sentence-transformers", "deterministic"] = ""
    model: str = ""
    revision: str = ""
    text_format: Literal["auto", "plain", "e5"] = "auto"
    document_composition: Literal["text", "title_heading_text"] = "text"
    normalize: bool = True
    device: str | None = None
    batch_size: int = Field(default=64, ge=1, le=10000)


class ServiceBinding(StrictModel):
    url: str = "http://127.0.0.1:8000/v1"
    model: str = ""

    @field_validator("url")
    @classmethod
    def credential_free_url(cls, value):
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Service URL must be HTTP(S), without credentials, query or fragment")
        return value


class VectorBinding(ServiceBinding):
    url: str = "http://127.0.0.1:6333"
    collection: str = "chatspark_chunks"
    timeout_seconds: int = Field(default=30, ge=1)


class RerankerBinding(StrictModel):
    model: str | None = None
    revision: str = ""
    device: str | None = None
    batch_size: int = Field(default=8, ge=1, le=1000)


class StateBinding(StrictModel):
    root: Path | None = None
    database: Path | None = None
    chunk_set_id: str = ""


class OperatorConfiguration(StrictModel):
    runtime_version: Literal[1] = 1
    embedding: EmbeddingBinding = Field(default_factory=EmbeddingBinding)
    generation: ServiceBinding = Field(default_factory=ServiceBinding)
    vector: VectorBinding = Field(default_factory=VectorBinding)
    reranker: RerankerBinding = Field(default_factory=RerankerBinding)
    state: StateBinding = Field(default_factory=StateBinding)
    providers_allowed: list[str] = []
    runtime_provider: str = ""
    runtime_distribution: str = "chatspark-core"

    def environment_values(self):
        values = {
            "EMBEDDING_" + key.upper(): value
            for key, value in self.embedding.model_dump().items()
            if key != "normalize"
        }
        values["EMBEDDING_NORMALIZE"] = self.embedding.normalize
        values.update(
            OPENAI_COMPAT_BASE_URL=self.generation.url,
            OPENAI_COMPAT_MODEL=self.generation.model,
            QDRANT_URL=self.vector.url,
            QDRANT_COLLECTION=self.vector.collection,
            QDRANT_TIMEOUT_SECONDS=self.vector.timeout_seconds,
            CHATSPARK_PLUGINS_ALLOWED=",".join(self.providers_allowed),
            CHATSPARK_RUNTIME_PROVIDER=self.runtime_provider,
            CHATSPARK_RUNTIME_DISTRIBUTION=self.runtime_distribution,
            CHATSPARK_CHUNK_SET_ID=self.state.chunk_set_id,
            RERANKER_MODEL=self.reranker.model,
            RERANKER_REVISION=self.reranker.revision,
            RERANKER_DEVICE=self.reranker.device,
            RERANKER_BATCH_SIZE=self.reranker.batch_size,
        )
        if self.state.root:
            values["CHATSPARK_STATE_ROOT"] = self.state.root
        if self.state.database:
            values["CHATSPARK_V3_DB_PATH"] = self.state.database
        return values


def load_operator_config(filename):
    filename = Path(filename).expanduser().resolve()
    config = OperatorConfiguration.model_validate(yaml.safe_load(filename.read_text()))
    for field in ("root", "database"):
        value = getattr(config.state, field)
        if value and not value.is_absolute():
            setattr(config.state, field, (filename.parent / value).resolve())
    return config


def operator_digest(config):
    return hashlib.sha256(
        json.dumps(config.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
