from importlib.metadata import version

from chatspark.plugins.registry import ProviderDescriptor, Registry
from chatspark.runtime.config import settings


def get_registry(*, allowed=None):
    from chatspark.extraction.pdf import (
        PdfiumExtractor,
        PdfiumOptions,
        PdfminerExtractor,
        PdfOptions,
        PdfplumberExtractor,
        PypdfExtractor,
    )
    from chatspark.extraction.tesseract import TesseractProvider, TesseractSettings
    from chatspark.retrieval.sparse_sqlite import SparseOptions, V3SQLiteSparseRetriever

    registry = Registry(
        allowed=allowed
        if allowed is not None
        else [s.strip() for s in settings.CHATSPARK_PLUGINS_ALLOWED.split(",") if s.strip()]
    )
    for provider_id, package, cls in [
        ("pdfminer", "pdfminer.six", PdfminerExtractor),
        ("pypdf", "pypdf", PypdfExtractor),
        ("pdfplumber", "pdfplumber", PdfplumberExtractor),
        ("pdfium", "pypdfium2", PdfiumExtractor),
    ]:
        try:
            installed_version = version(package)
        except Exception:
            continue
        capabilities = frozenset({"pdf", "pdf-inspector"} if provider_id == "pdfium" else {"pdf"})
        registry.register(
            ProviderDescriptor(
                provider_id,
                installed_version,
                capabilities,
                PdfiumOptions if provider_id == "pdfium" else PdfOptions,
                lambda options, implementation=cls: implementation(),
            )
        )
    registry.register(
        ProviderDescriptor(
            "tesseract", "0.3.0rc2", frozenset({"ocr"}), TesseractSettings, TesseractProvider
        )
    )
    registry.register(
        ProviderDescriptor(
            "sparse",
            "1",
            frozenset({"retrieval"}),
            SparseOptions,
            lambda options: V3SQLiteSparseRetriever(options),
        )
    )
    from pydantic import BaseModel, ConfigDict

    from chatspark.extraction.html import HtmlExtractor
    from chatspark.ingestion.v3_rechunk import StructuralChunker
    from chatspark.profiles.models import HtmlSettings, HybridSettings, StructuralSettings
    from chatspark.retrieval.pipeline import CanonicalContext, HybridStrategy

    class EmptySettings(BaseModel):
        model_config = ConfigDict(extra="forbid")

    for name, capability, model, factory in [
        ("hybrid", "strategy", HybridSettings, HybridStrategy),
        ("canonical", "context", EmptySettings, lambda options: CanonicalContext()),
        ("html", "html", HtmlSettings, HtmlExtractor),
        ("structural", "chunker", StructuralSettings, StructuralChunker),
    ]:
        registry.register(
            ProviderDescriptor(name, "0.3.0rc2", frozenset({capability}), model, factory)
        )
    registry.discover()
    return registry
