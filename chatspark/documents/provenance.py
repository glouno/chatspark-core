from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from chatspark.documents.models import (
    ContentScope,
    ContentType,
    Document,
    DocumentProvenance,
    ExtractorProfile,
    SourceFamily,
    SourceMetadata,
)


def infer_source_family(content_type: ContentType) -> SourceFamily:
    if content_type == ContentType.HTML:
        return SourceFamily.HTML
    if content_type == ContentType.PDF:
        return SourceFamily.PDF
    if content_type in {ContentType.MARKDOWN, ContentType.TEXT, ContentType.WORD}:
        return SourceFamily.DOCUMENT
    if content_type == ContentType.PRESENTATION:
        return SourceFamily.PRESENTATION
    if content_type == ContentType.SPREADSHEET:
        return SourceFamily.SPREADSHEET
    if content_type == ContentType.IMAGE:
        return SourceFamily.IMAGE
    return SourceFamily.UNKNOWN


def infer_site_host(url: str) -> str | None:
    host = urlparse(url).netloc.strip().lower()
    return host or None


def infer_path_prefix(url: str) -> str:
    path = (urlparse(url).path or "").strip("/")
    if not path:
        return "/"
    return f"/{path.split('/', 1)[0]}"


def build_source_metadata(
    *,
    url: str,
    content_type: ContentType,
    http_status: int,
    title: str | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
) -> SourceMetadata:
    return SourceMetadata(
        url=url,
        content_type=content_type,
        source_family=infer_source_family(content_type),
        site_host=infer_site_host(url),
        path_prefix=infer_path_prefix(url),
        http_status=http_status,
        title=title,
        etag=etag,
        last_modified=last_modified,
    )


def build_document_provenance(
    *,
    corpus_scope: str | None = None,
    extractor_profile: ExtractorProfile | None = None,
    content_scope: ContentScope | None = None,
    canonical_variant_label: str | None = None,
    profile_name: str | None = None,
    profile_digest: str | None = None,
    extraction_fingerprint: str | None = None,
) -> DocumentProvenance:
    return DocumentProvenance(
        corpus_scope=_enum_or_str(corpus_scope),
        extractor_profile=extractor_profile,
        content_scope=content_scope,
        canonical_variant_label=(canonical_variant_label or "").strip() or None,
        profile_name=(profile_name or "").strip() or None,
        profile_digest=(profile_digest or "").strip() or None,
        extraction_fingerprint=(extraction_fingerprint or "").strip() or None,
    )


def chunk_metadata_provenance(doc: Document, *, chunk_strategy: str) -> dict[str, Any]:
    provenance = doc.provenance
    source = doc.source
    site_host = source.site_host or infer_site_host(source.url)
    path_prefix = source.path_prefix or infer_path_prefix(source.url)
    source_family = source.source_family
    if source_family == SourceFamily.UNKNOWN:
        source_family = infer_source_family(source.content_type)

    metadata: dict[str, Any] = {
        "source_family": source_family.value
        if isinstance(source_family, SourceFamily)
        else str(source_family),
        "site_host": site_host,
        "path_prefix": path_prefix,
        "chunk_strategy": chunk_strategy,
    }

    if provenance.corpus_scope is not None:
        metadata["corpus_scope"] = _enum_or_str(provenance.corpus_scope)
    if provenance.extractor_profile is not None:
        metadata["extractor_profile"] = provenance.extractor_profile.value
    if provenance.content_scope is not None:
        metadata["content_scope"] = provenance.content_scope.value
    if provenance.canonical_variant_label:
        metadata["canonical_variant_label"] = provenance.canonical_variant_label
    if provenance.profile_name:
        metadata["profile_name"] = provenance.profile_name
    if provenance.profile_digest:
        metadata["profile_digest"] = provenance.profile_digest
    if provenance.extraction_fingerprint:
        metadata["extraction_fingerprint"] = provenance.extraction_fingerprint

    return metadata


def _enum_or_str(value: Any) -> str | None:
    if value is None:
        return None
    enum_value = getattr(value, "value", None)
    return str(enum_value if enum_value is not None else value)
