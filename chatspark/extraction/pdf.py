"""Bounded PDF extraction through explicit, licensed providers."""

import base64
import hashlib
import json
import math
import multiprocessing
import os
import signal
import time
from dataclasses import dataclass, field
from io import BytesIO
from multiprocessing.connection import wait
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from chatspark.documents import DocContent
from chatspark.plugins.contracts import ExtractedPage, PageExtractionResult
from chatspark.profiles.models import PdfProfile
from chatspark.runtime.temporary import temporary_directory


class PdfOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PdfiumOptions(PdfOptions):
    render_page_indices: list[int] = Field(default_factory=list, max_length=10)
    render_scale: float = Field(default=1.0, ge=0.1, le=2.0, allow_inf_nan=False)


@dataclass
class PdfOutcome:
    status: str
    content: DocContent
    provider: str
    diagnostics: dict[str, Any] = field(default_factory=dict)
    pages: list[ExtractedPage] = field(default_factory=list)


class PdfExtractionError(ValueError):
    def __init__(self, status, attempts):
        super().__init__(f"PDF extraction {status}")
        self.status = status
        self.attempts = attempts


def _content(text, markdown=""):
    from chatspark.extraction.html import _sections_from_markdown

    text = text.replace("\x00", "").replace("\u00a0", " ").strip()
    return DocContent(
        text=text, markdown=markdown, sections=_sections_from_markdown(markdown) if markdown else []
    )


def page_result(texts, markdown=""):
    return PageExtractionResult(
        content=_content("\f".join(texts), markdown),
        pages=[ExtractedPage(page_index=i, text=text) for i, text in enumerate(texts)],
    )


class PdfminerExtractor:
    def extract(self, payload, options):
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTTextContainer
        from pdfminer.pdfdocument import PDFPasswordIncorrect

        try:
            texts = [
                "".join(
                    element.get_text() for element in page if isinstance(element, LTTextContainer)
                )
                for page in extract_pages(BytesIO(payload))
            ]
        except PDFPasswordIncorrect:
            raise PdfExtractionError("encrypted", []) from None
        return page_result(texts)


class PypdfExtractor:
    def extract(self, payload, options):
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError

        try:
            reader = PdfReader(BytesIO(payload))
        except PdfReadError:
            raise PdfExtractionError("invalid", []) from None
        if reader.is_encrypted:
            raise PdfExtractionError("encrypted", [])
        return page_result([p.extract_text() or "" for p in reader.pages])


class PdfplumberExtractor:
    def extract(self, payload, options):
        import pdfplumber
        from pdfminer.pdfdocument import PDFPasswordIncorrect
        from pdfminer.pdfparser import PDFSyntaxError
        from pdfplumber.utils.exceptions import PdfminerException

        texts, markdown = [], []
        try:
            document = pdfplumber.open(BytesIO(payload))
        except PDFPasswordIncorrect:
            raise PdfExtractionError("encrypted", []) from None
        except PDFSyntaxError:
            raise PdfExtractionError("invalid", []) from None
        except PdfminerException as error:
            underlying = error.__context__
            if isinstance(underlying, PDFPasswordIncorrect):
                raise PdfExtractionError("encrypted", []) from None
            if isinstance(underlying, PDFSyntaxError):
                raise PdfExtractionError("invalid", []) from None
            raise
        with document:
            for page in document.pages:
                text = page.extract_text() or ""
                texts.append(text)
                markdown.append(text)
                for table in page.extract_tables():
                    for row in table:
                        markdown.append(
                            "| "
                            + " | ".join(str(cell or "").replace("\n", " ") for cell in row)
                            + " |"
                        )
        return page_result(texts, "\n\n".join(markdown))


class PdfiumExtractor:
    def extract(self, payload, options):
        document = _open_pdfium(payload)
        texts = []
        try:
            for index in range(len(document)):
                page = document[index]
                try:
                    textpage = page.get_textpage()
                    try:
                        texts.append(textpage.get_text_range())
                    finally:
                        textpage.close()
                finally:
                    page.close()
        finally:
            document.close()
        return page_result(texts)

    def inspect(self, payload, options):
        document = _open_pdfium(payload)
        try:
            pages, renders = [], []
            for i in range(len(document)):
                page = document[i]
                try:
                    textpage = page.get_textpage()
                    try:
                        text_chars = len(textpage.get_text_range().strip())
                    finally:
                        textpage.close()
                    pages.append(
                        {
                            "page_index": i,
                            "width": page.get_width(),
                            "height": page.get_height(),
                            "text_chars": text_chars,
                        }
                    )
                    if i in options.get("render_page_indices", []):
                        scale = options.get("render_scale", 1.0)
                        if max(page.get_width(), page.get_height()) * scale > 4096:
                            raise PdfExtractionError("unsupported", [])
                        bitmap = page.render(scale=scale)
                        try:
                            image = bitmap.to_pil()
                            try:
                                output = BytesIO()
                                image.save(output, format="PNG")
                                renders.append(
                                    {
                                        "page_index": i,
                                        "width": image.width,
                                        "height": image.height,
                                        "png": output.getvalue(),
                                    }
                                )
                            finally:
                                image.close()
                        finally:
                            bitmap.close()
                finally:
                    page.close()
            return {"pages": pages, "renders": renders}
        finally:
            document.close()


def _open_pdfium(payload):
    import pypdfium2

    try:
        return pypdfium2.PdfDocument(payload)
    except pypdfium2.PdfiumError as error:
        if getattr(error, "err_code", None) == 4:
            raise PdfExtractionError("encrypted", []) from None
        raise PdfExtractionError("invalid", []) from None


def _json_bytes(value):
    if isinstance(value, bytes):
        return {"__chatspark_binary__": base64.b64encode(value).decode("ascii")}
    raise TypeError("Unsupported extraction result value")


def _restore_bytes(value):
    if set(value) == {"__chatspark_binary__"}:
        return base64.b64decode(value["__chatspark_binary__"], validate=True)
    return value


def _worker(
    input_path, output_path, selection, allowed, capability, max_output_bytes, call_options
):
    try:
        if os.name == "posix":
            os.setsid()
        from chatspark.runtime.registry import get_registry

        payload = Path(input_path).read_bytes()
        provider = get_registry(allowed=allowed).resolve(selection, capability)
        if capability == "pdf-inspector":
            status, result = "success", provider.inspect(payload, selection.settings)
        elif capability == "ocr":
            output = provider.recognize(payload, {**selection.settings, **(call_options or {})})
            if isinstance(output, PageExtractionResult):
                content = output.content
                status, result = "success" if content.text else "needs_ocr", output.model_dump()
            elif not isinstance(output, dict) or not isinstance(output.get("text"), str):
                raise PdfExtractionError("invalid", [])
            else:
                markdown = output.get("markdown", "")
                if not isinstance(markdown, str):
                    raise PdfExtractionError("invalid", [])
                content = _content(output["text"], markdown)
                status, result = (
                    "success" if content.text else "needs_ocr",
                    PageExtractionResult(content=content).model_dump(),
                )
        else:
            extraction = provider.extract(payload, selection.settings)
            if not isinstance(extraction, PageExtractionResult):
                raise PdfExtractionError("invalid", [])
            status, result = (
                "success" if extraction.content.text.strip() else "needs_ocr",
                extraction.model_dump(),
            )
        written = 0
        with Path(output_path).open("wb") as stream:
            for part in json.JSONEncoder(default=_json_bytes).iterencode([status, result]):
                encoded = part.encode("utf-8")
                written += len(encoded)
                if written > max_output_bytes:
                    raise PdfExtractionError("output_limit", [])
                stream.write(encoded)
        return
    except Exception as error:
        status = getattr(error, "status", None)
        if status is None:
            name = type(error).__name__.lower()
            status = (
                "encrypted"
                if any(s in name for s in ("password", "encrypt"))
                else "unsupported"
                if isinstance(error, ImportError)
                else "invalid"
                if any(s in name for s in ("syntax", "pdfread", "dataformat", "parse"))
                else "failed"
            )
        # No exception bodies or provider-controlled diagnostic strings cross the boundary.
        if status not in {
            "encrypted",
            "unsupported",
            "invalid",
            "timeout",
            "failed",
            "output_limit",
        }:
            status = "failed"
        serialized = json.dumps([status, None]).encode("utf-8")
    Path(output_path).write_bytes(serialized)


def _run_provider(
    payload, selection, registry, capability, *, deadline, max_output_bytes, call_options=None
):
    if (
        not math.isfinite(deadline)
        or not isinstance(max_output_bytes, int)
        or max_output_bytes < 1024
    ):
        raise ValueError(
            "Extraction requires finite deadlines and an output limit of at least 1024 bytes"
        )
    if time.monotonic() >= deadline:
        return "timeout", None
    with temporary_directory(prefix="chatspark-extraction-") as directory:
        source, result = Path(directory) / "input", Path(directory) / "result.json"
        source.write_bytes(payload)
        os.chmod(source, 0o600)
        context = multiprocessing.get_context("spawn")
        process = context.Process(
            target=_worker,
            args=(
                str(source),
                str(result),
                selection,
                tuple(registry.allowed),
                capability,
                max_output_bytes,
                call_options,
            ),
        )
        process.start()
        try:
            remaining = max(0, deadline - time.monotonic())
            if not wait([process.sentinel], timeout=remaining):
                return "timeout", None
            process.join(timeout=0)
            if process.exitcode != 0 or not result.is_file():
                return "failed", None
            if result.stat().st_size > max_output_bytes:
                return "output_limit", None
            with result.open("rb") as stream:
                serialized = stream.read(max_output_bytes + 1)
            if len(serialized) > max_output_bytes:
                return "output_limit", None
            try:
                status, output = json.loads(serialized, object_hook=_restore_bytes)
            except (TypeError, ValueError):
                return "invalid", None
            if time.monotonic() >= deadline:
                return "timeout", None
            if status not in {
                "success",
                "needs_ocr",
                "encrypted",
                "unsupported",
                "invalid",
                "timeout",
                "failed",
                "output_limit",
            }:
                return "invalid", None
            return status, output
        finally:
            _reap_worker(process)


def _reap_worker(process):
    process.join(timeout=0.2)
    if os.name == "posix":
        # Clean provider descendants even if their direct parent already exited.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if process.is_alive():
        process.terminate()
    process.join(timeout=1)
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.is_alive():
        process.kill()
    process.join()


def extract_pdf_outcome(payload, config=None, *, registry=None):
    config = config or PdfProfile()
    if len(payload) > config.max_bytes:
        raise PdfExtractionError("oversized", [])
    if not payload:
        return PdfOutcome("empty", _content(""), config.provider.provider)
    if not payload.startswith(b"%PDF-"):
        raise PdfExtractionError("invalid", [])
    from chatspark.runtime.registry import get_registry

    registry = registry or get_registry()
    selections = [config.provider, *config.fallbacks]
    for selection in selections:
        registry.descriptor(selection, "pdf")
    deadline = time.monotonic() + config.timeout_seconds
    attempts = []
    for selection in selections:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PdfExtractionError("timeout", attempts)
        status, serialized = _run_provider(
            payload,
            selection,
            registry,
            "pdf",
            deadline=deadline,
            max_output_bytes=config.max_output_bytes,
        )
        attempts.append({"provider": selection.provider, "status": status})
        if serialized is not None and (
            status == "success"
            or selection == selections[-1]
            or (status == "needs_ocr" and not config.fallback_on_needs_ocr)
        ):
            descriptor, _ = registry.descriptor(selection, "pdf")
            extraction = PageExtractionResult.model_validate(serialized)
            return PdfOutcome(
                status,
                extraction.content,
                selection.provider,
                {
                    "input_sha256": hashlib.sha256(payload).hexdigest(),
                    "provider_version": descriptor.implementation_version,
                    "settings": selection.settings,
                    "attempts": attempts,
                },
                extraction.pages,
            )
        if status == "timeout":
            raise PdfExtractionError(status, attempts)
    raise PdfExtractionError(attempts[-1]["status"], attempts)


def extract_pdf(payload, config=None):
    return extract_pdf_outcome(payload, config).content


def recognize_ocr(
    payload,
    selection,
    *,
    registry=None,
    max_bytes=50 * 1024 * 1024,
    timeout_seconds=120,
    max_output_bytes=64 * 1024 * 1024,
    native_pages=(),
    inspector=None,
):
    """Invoke explicit OCR only; provider output never includes exception text."""
    from chatspark.runtime.registry import get_registry

    if len(payload) > max_bytes:
        raise PdfExtractionError("oversized", [])
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise PdfExtractionError("timeout", [])
    registry = registry or get_registry()
    descriptor, _ = registry.descriptor(selection, "ocr")
    status, serialized = _run_provider(
        payload,
        selection,
        registry,
        "ocr",
        deadline=time.monotonic() + timeout_seconds,
        max_output_bytes=max_output_bytes,
        call_options={
            "native_pages": [page.model_dump() for page in native_pages],
            "inspector": inspector.model_dump() if inspector else None,
        },
    )
    if status != "success":
        raise PdfExtractionError(status, [{"provider": selection.provider, "status": status}])
    extraction = PageExtractionResult.model_validate(serialized)
    return PdfOutcome(
        status,
        extraction.content,
        selection.provider,
        {
            "input_sha256": hashlib.sha256(payload).hexdigest(),
            "provider_version": descriptor.implementation_version,
            "settings": selection.settings,
            "attempts": [{"provider": selection.provider, "status": status}],
            "pages": [
                {"page_index": page.page_index, "method": page.method} for page in extraction.pages
            ],
        },
        extraction.pages,
    )


def inspect_pdf(
    payload,
    selection,
    *,
    registry=None,
    max_bytes=50 * 1024 * 1024,
    timeout_seconds=120,
    max_output_bytes=64 * 1024 * 1024,
):
    """Run a selected page inspector in a bounded, reaped subprocess."""
    from chatspark.runtime.registry import get_registry

    if len(payload) > max_bytes:
        raise PdfExtractionError("oversized", [])
    if not payload.startswith(b"%PDF-"):
        raise PdfExtractionError("invalid", [])
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise PdfExtractionError("timeout", [])
    registry = registry or get_registry()
    registry.descriptor(selection, "pdf-inspector")
    status, result = _run_provider(
        payload,
        selection,
        registry,
        "pdf-inspector",
        deadline=time.monotonic() + timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
    if (
        status != "success"
        or not isinstance(result, dict)
        or not isinstance(result.get("pages"), list)
    ):
        raise PdfExtractionError(status if status != "success" else "invalid", [])
    return result
