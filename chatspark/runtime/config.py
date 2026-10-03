import os
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        def operator_source():
            filename = os.getenv("CHATSPARK_RUNTIME_CONFIG")
            if not filename:
                return {}
            from chatspark.runtime.operator import load_operator_config

            return load_operator_config(filename).environment_values()

        return init_settings, env_settings, dotenv_settings, operator_source, file_secret_settings

    DEBUG: bool = False
    CHATSPARK_PROFILE: str = "hybrid"
    CHATSPARK_STATE_ROOT: Path = Field(
        default_factory=lambda: (
            Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local/share") / "chatspark"
        )
    )
    CHATSPARK_RUNTIME_ROOT: Path | None = None
    DATA_DIR: Path | None = None
    CHATSPARK_CACHE_ROOT: Path | None = None
    CHATSPARK_V3_DB_PATH: Path | None = None
    CHATSPARK_PLUGINS_ALLOWED: str = ""
    CHATSPARK_CHUNK_SET_ID: str = ""
    CHATSPARK_AUTHORIZED_FILTERS: str = "{}"
    CHATSPARK_RUNTIME_PROVIDER: str = ""
    CHATSPARK_RUNTIME_DISTRIBUTION: str = "chatspark-core"
    CHATSPARK_API_PROXY_SHARED_TOKEN: str = ""
    CHATSPARK_SERVE_PRELOAD: bool = False
    CHATSPARK_SERVE_BUILD_ID: str = ""
    CHATSPARK_SERVE_CORPUS_SHA256: str = ""
    RERANKER_MODEL: str | None = None
    RERANKER_REVISION: str = ""
    RERANKER_DEVICE: str | None = None
    RERANKER_BATCH_SIZE: int = Field(default=8, ge=1)
    QDRANT_URL: str = "http://127.0.0.1:6333"
    QDRANT_COLLECTION: str = "chatspark_chunks"
    QDRANT_API_KEY: str | None = None
    QDRANT_TIMEOUT_SECONDS: int = 30
    EMBEDDING_BACKEND: str = ""
    EMBEDDING_MODEL: str = ""
    EMBEDDING_REVISION: str = ""
    EMBEDDING_NORMALIZE: bool = True
    EMBEDDING_TEXT_FORMAT: str = "auto"
    EMBEDDING_DOCUMENT_COMPOSITION: Literal["text", "title_heading_text"] = "text"
    EMBEDDING_BATCH_SIZE: int = Field(default=64, ge=1, le=10000)
    EMBEDDING_DEVICE: str | None = None
    OPENAI_COMPAT_BASE_URL: str = "http://127.0.0.1:8000/v1"
    OPENAI_COMPAT_MODEL: str = ""
    OPENAI_COMPAT_API_KEY: str = ""
    OPENAI_COMPAT_TIMEOUT: float = 60
    OBS_TRACE_CONTENT_MODE: Literal["metadata", "summary", "full"] = "metadata"
    OBS_REQUEST_TRACE_ENABLED: bool = False
    OBS_CONVERSATION_STORE_ENABLED: bool = False
    LANGFUSE_ENABLED: bool = False
    CHATSPARK_IDENTITY_BASELINE_DATABASE: Path | None = None
    CRAWLER_USER_AGENT: str = "ChatSparkCrawler/1.0 (+https://github.com/glouno/chatspark-core)"

    def state_paths(self):
        runtime = self.CHATSPARK_RUNTIME_ROOT or self.CHATSPARK_STATE_ROOT / "runtime"
        return {
            "runtime": runtime,
            "data": self.DATA_DIR or runtime / "data",
            "cache": self.CHATSPARK_CACHE_ROOT or runtime / "cache",
        }

    def ensure_state(self):
        for path in self.state_paths().values():
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    @property
    def SQLITE_PATH(self):
        return str(self.CHATSPARK_V3_DB_PATH or self.state_paths()["data"] / "canonical-v3.db")


settings = Settings()
