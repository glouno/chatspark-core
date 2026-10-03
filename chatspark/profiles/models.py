from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from chatspark.plugins.contracts import ProviderSelection


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CorpusProfile(StrictModel):
    name: str = Field(min_length=1)
    display_name: str = Field(default="", description="Display metadata saved with the corpus")
    description: str = Field(default="", description="Descriptive metadata saved with the corpus")
    default_scope: str = Field(
        default="public", description="Descriptive metadata; does not grant authorization"
    )
    language_hints: list[str] = Field(
        default_factory=list,
        description="Descriptive language metadata; does not select a model or OCR language",
    )


class CanonicalURLRewrite(StrictModel):
    from_host: str = ""
    to_host: str = ""
    from_path: str = ""
    to_path: str = ""
    from_path_prefix: str = ""
    to_path_prefix: str = ""


class CrawlProfile(StrictModel):
    canonical_url_rewrites: list[CanonicalURLRewrite] = []
    start_urls: list[str] = []
    allowed_domains: list[str] = []
    deny_domains: list[str] = []
    allow_patterns: list[str] = []
    deny_patterns: list[str] = []
    html_ignored_extensions: list[str] = []
    files_ignored_extensions: list[str] = []
    file_link_allow_patterns: list[str] = []
    blocked_content_types: list[str] = []
    concurrent_requests_per_domain: int = Field(default=2, ge=1, le=32)
    autothrottle_target_concurrency: float = Field(default=1, gt=0, le=32)
    max_pages: int = Field(default=100, ge=1, le=10000)
    user_agent: str = "ChatSparkCrawler/1.0 (+https://github.com/glouno/chatspark-core)"
    download_delay_seconds: float = Field(default=1, ge=0, allow_inf_nan=False)


class AttributePatternRule(StrictModel):
    attribute: Literal["id", "class", "role", "aria-label"]
    pattern: str
    tags: list[str] = []

    @field_validator("pattern")
    @classmethod
    def valid_pattern(cls, value):
        import re

        re.compile(value)
        return value


class HtmlSettings(StrictModel):
    include_tables: bool = True
    remove_selectors: list[str] = Field(
        default_factory=lambda: ["script", "style", "noscript", "nav", "footer"]
    )
    attribute_patterns: list[AttributePatternRule] = []

    @field_validator("remove_selectors")
    @classmethod
    def valid_selectors(cls, values):
        import soupsieve

        for value in values:
            soupsieve.compile(value)
        return values


class StructuralSettings(StrictModel):
    target_chunk_size_chars: int = Field(default=1000, ge=100)
    overlap_chars: int = Field(default=100, ge=0)
    pdf_target_chunk_size_chars: int = Field(default=1000, ge=100)
    pdf_overlap_chars: int = Field(default=100, ge=0)
    min_chunk_chars: int = Field(default=0, ge=0)
    tiny_chunk_policy: Literal["keep", "merge", "drop"] = "keep"
    table_aware: bool = True

    @model_validator(mode="after")
    def check_overlap(self):
        if (
            self.overlap_chars >= self.target_chunk_size_chars
            or self.pdf_overlap_chars >= self.pdf_target_chunk_size_chars
        ):
            raise ValueError("Chunk overlap must be smaller than chunk size")
        if self.min_chunk_chars > min(
            self.target_chunk_size_chars, self.pdf_target_chunk_size_chars
        ):
            raise ValueError("Minimum chunk size exceeds the configured target")
        return self


class PdfProfile(StrictModel):
    provider: ProviderSelection = Field(
        default_factory=lambda: ProviderSelection(provider="pdfminer")
    )
    fallbacks: list[ProviderSelection] = []
    inspector: ProviderSelection | None = Field(
        default_factory=lambda: ProviderSelection(provider="pdfium")
    )
    fallback_on_needs_ocr: bool = True
    max_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    max_output_bytes: int = Field(default=64 * 1024 * 1024, ge=1024)
    timeout_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)


class ExtractionProfile(StrictModel):
    html: ProviderSelection = Field(default_factory=lambda: ProviderSelection(provider="html"))
    content_scope: Literal["all", "html-only"] = "all"
    pdf: PdfProfile = Field(default_factory=PdfProfile)
    ocr: ProviderSelection | None = None
    min_document_text_chars: int = Field(default=0, ge=0)


class ExtraLane(StrictModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    provider: str
    settings: dict = {}
    limit: int = Field(default=20, ge=1, le=1000)
    weight: float = Field(default=1, ge=0, le=100, allow_inf_nan=False)

    def selection(self):
        return ProviderSelection(provider=self.provider, settings=self.settings)


class HybridSettings(StrictModel):
    lanes: list[ExtraLane] = Field(default_factory=list, max_length=14)
    planner: ProviderSelection | None = None
    processor: ProviderSelection | None = None
    fts_bm25_weights: list[float] = Field(
        default_factory=lambda: [1, 5, 3, 2.5], min_length=4, max_length=4
    )
    mmr_mode: Literal["lexical", "embedding"] = "lexical"

    @field_validator("fts_bm25_weights")
    @classmethod
    def weights(cls, values):
        import math

        if any(not math.isfinite(v) or v < 0 for v in values) or not any(values):
            raise ValueError("FTS weights must be finite, nonnegative and not all zero")
        return values

    @model_validator(mode="after")
    def names(self):
        names = [lane.name for lane in self.lanes]
        if len(names) != len(set(names)) or set(names) & {"dense", "sparse"}:
            raise ValueError("Extra lanes need unique names distinct from dense and sparse")
        return self


class RerankerProfile(StrictModel):
    backend: Literal["none", "token-overlap", "cross-encoder"] = "none"
    model: str | None = None


class RetrievalProfile(StrictModel):
    strategy: ProviderSelection = Field(
        default_factory=lambda: ProviderSelection(provider="hybrid")
    )
    enable_dense: bool = True
    enable_sparse: bool = True
    dense_k: int = Field(default=20, ge=1, le=1000)
    sparse_k: int = Field(default=20, ge=1, le=1000)
    rrf_k: int = Field(default=60, ge=1)
    rrf_dense_weight: float = Field(default=1, ge=0, allow_inf_nan=False)
    rrf_sparse_weight: float = Field(default=1, ge=0, allow_inf_nan=False)
    final_k: int = Field(default=8, ge=1, le=100)
    rerank_input_k: int = Field(default=50, ge=1, le=1000)
    max_chunks_per_doc: int = Field(default=2, ge=0)
    reranker: RerankerProfile = Field(default_factory=RerankerProfile)
    final_selector: Literal["score", "mmr"] = "score"
    mmr_lambda: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    context: ProviderSelection = Field(
        default_factory=lambda: ProviderSelection(provider="canonical")
    )


class GenerationProfile(StrictModel):
    timezone: str = "UTC"
    system_prompt: str | None = None
    user_prompt: str | None = None
    remote_prompt: ProviderSelection | None = None
    allow_remote_fallback: bool = False
    max_context_chars: int = Field(default=16000, ge=1)
    max_tokens: int = Field(default=1024, ge=1)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        ZoneInfo(value)
        return value


class EvaluationProfile(StrictModel):
    smoke_questions: list[str] = Field(
        default_factory=list, description="Evaluation planning metadata; not executed at runtime"
    )
    dataset_paths: list[str] = Field(
        default_factory=list,
        description="Owner dataset references; evaluation requires an explicit request",
    )
    required_source_urls: list[str] = Field(
        default_factory=list,
        description="Owner acceptance planning metadata; not retrieval routing",
    )


class PresentationProfile(StrictModel):
    example_questions: list[str] = Field(
        default_factory=list, description="Example question metadata for consuming interfaces"
    )


class Profile(StrictModel):
    profile_version: Literal[2]
    corpus: CorpusProfile
    crawl: CrawlProfile = Field(default_factory=CrawlProfile)
    extraction: ExtractionProfile = Field(default_factory=ExtractionProfile)
    chunking: ProviderSelection = Field(
        default_factory=lambda: ProviderSelection(provider="structural")
    )
    retrieval: RetrievalProfile = Field(default_factory=RetrievalProfile)
    generation: GenerationProfile = Field(default_factory=GenerationProfile)
    policy: ProviderSelection | None = None
    evaluation: EvaluationProfile = Field(default_factory=EvaluationProfile)
    presentation: PresentationProfile = Field(default_factory=PresentationProfile)
    artifact_builders: list[ProviderSelection] = []
