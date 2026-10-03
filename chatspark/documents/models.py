"""Canonical document and ingestion models owned by ChatSpark."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ContentType(StrEnum):
    HTML = "html"
    PDF = "pdf"
    MARKDOWN = "markdown"
    TEXT = "text"
    WORD = "word"
    PRESENTATION = "presentation"
    SPREADSHEET = "spreadsheet"
    IMAGE = "image"
    UNKNOWN = "unknown"


class SourceFamily(StrEnum):
    HTML = "html"
    PDF = "pdf"
    DOCUMENT = "document"
    PRESENTATION = "presentation"
    SPREADSHEET = "spreadsheet"
    IMAGE = "image"
    UNKNOWN = "unknown"


class DocType(StrEnum):
    PROCEDURE = "procedure"
    FORM = "form"
    FAQ = "faq"
    CONTACT = "contact"
    EVENT = "event"
    NEWS = "news"
    ABOUT = "about"
    OTHER = "other"


class CrawlEngine(StrEnum):
    SCRAPY = "scrapy"


class ExtractorProfile(StrEnum):
    # Reserved legacy wire values; not provider selection or shipped implementations.
    CURRENT = "current"
    COLLEAGUE = "colleague"
    COLLEAGUE_V3 = "colleague_v3"
    HYBRID = "hybrid"


class ContentScope(StrEnum):
    ALL = "all"
    HTML_ONLY = "html-only"


class CorpusScope(StrEnum):
    WWW = "www"
    WWW_PERSO = "www-perso"


class SourceMetadata(BaseModel):
    url: str
    content_type: ContentType
    source_family: SourceFamily = SourceFamily.UNKNOWN
    site_host: str | None = None
    path_prefix: str | None = None
    fetched_at: datetime = Field(default_factory=datetime.utcnow)
    title: str | None = None
    last_modified: str | None = None
    etag: str | None = None
    http_status: int = 200


class Section(BaseModel):
    heading_path: list[str]
    content: str
    start_char_idx: int
    end_char_idx: int


class DocContent(BaseModel):
    markdown: str
    text: str  # Normalized plain text
    sections: list[Section] = Field(default_factory=list)


class SoftTriage(BaseModel):
    doc_type: DocType = DocType.OTHER
    confidence: float = 0.0
    scores: dict[str, float] = Field(default_factory=dict)
    topics: list[str] = Field(default_factory=list)


class DocumentProvenance(BaseModel):
    corpus_scope: str | None = None
    extractor_profile: ExtractorProfile | None = None
    content_scope: ContentScope | None = None
    canonical_variant_label: str | None = None
    profile_name: str | None = None
    profile_digest: str | None = None
    extraction_fingerprint: str | None = None


class Document(BaseModel):
    doc_id: str
    source: SourceMetadata
    content: DocContent
    annotations: SoftTriage = Field(default_factory=SoftTriage)
    provenance: DocumentProvenance = Field(default_factory=DocumentProvenance)
    content_hash: str
    is_deleted: bool = False


class Chunk(BaseModel):
    chunk_id: str
    doc_id: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    embedding: list[float] | None = None


class PipelineModules(BaseModel):
    crawl: bool = True
    normalize: bool = True
    chunk: bool = False
    index: bool = False


class IngestionPipelineConfig(BaseModel):
    crawl_engine: CrawlEngine = CrawlEngine.SCRAPY
    seed_url: str
    max_depth: int = 2
    profile_name: str | None = None
    scrapy_reset_state: bool = False
    reset_canonical: bool = False
    chunk_strategy: str = "structure"
    corpus_scope: str | None = None
    extractor_profile: ExtractorProfile = ExtractorProfile.CURRENT
    content_scope: ContentScope = ContentScope.ALL
    canonical_variant_label: str | None = None
    resume_existing: bool = False
    parallel_normalize_workers: int = 0
    normalize_commit_batch_size: int = 100
    modules: PipelineModules = Field(default_factory=PipelineModules)


class PipelineRunReport(BaseModel):
    crawl_engine: CrawlEngine
    ingestion_run_id: str | None = None
    crawled: bool = False
    normalized_docs: int = 0
    generated_chunks: int = 0
    indexed_chunks: int = 0
    stage_timings: dict[str, float] = Field(default_factory=dict)
    normalize_stats: dict[str, Any] = Field(default_factory=dict)
