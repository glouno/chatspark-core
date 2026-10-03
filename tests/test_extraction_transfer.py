"""Deadline and size coverage for extractor result transport, not just parsing."""

import multiprocessing
import time
from pathlib import Path

import pytest

from chatspark.extraction import pdf
from chatspark.plugins import ProviderSelection
from chatspark.profiles.models import PdfProfile
from chatspark.runtime.registry import get_registry


def partial_writer(input_path, output_path, *args):
    # Reproduce a worker that starts transferring output then stalls indefinitely.
    Path(output_path).write_bytes(b'["success", {')
    time.sleep(30)


def oversized_writer(input_path, output_path, *args):
    Path(output_path).write_bytes(b"x" * 4096)


@pytest.mark.parametrize(
    "worker,status", [(partial_writer, "timeout"), (oversized_writer, "output_limit")]
)
def test_output_transport_is_bounded_and_reaped(monkeypatch, worker, status):
    monkeypatch.setattr(pdf, "_worker", worker)
    before = {p.pid for p in multiprocessing.active_children()}
    started = time.monotonic()
    outcome, content = pdf._run_provider(
        b"synthetic",
        ProviderSelection(provider="pdfminer"),
        get_registry(),
        "pdf",
        deadline=started + 1,
        max_output_bytes=1024,
    )
    assert (outcome, content) == (status, None)
    assert time.monotonic() - started < 3
    assert {p.pid for p in multiprocessing.active_children()} == before


def test_invalid_deadlines_cannot_disable_limits():
    from pydantic import ValidationError

    for duration in (float("inf"), float("nan"), 0, -1):
        with pytest.raises(ValidationError):
            PdfProfile(timeout_seconds=duration)
        with pytest.raises(pdf.PdfExtractionError):
            pdf.inspect_pdf(
                b"%PDF-synthetic", ProviderSelection(provider="pdfminer"), timeout_seconds=duration
            )
        with pytest.raises(pdf.PdfExtractionError):
            pdf.recognize_ocr(
                b"image", ProviderSelection(provider="unavailable"), timeout_seconds=duration
            )


def test_local_pdf_byte_limit_prevents_unbounded_read(tmp_path, monkeypatch):
    from chatspark.extraction.files import parse_file

    source = tmp_path / "large.pdf"
    source.write_bytes(b"%PDF-" + b"x" * 1024)

    def unexpected_read(self):
        raise AssertionError("Oversized file must not be read into memory")

    monkeypatch.setattr(Path, "read_bytes", unexpected_read)
    with pytest.raises(pdf.PdfExtractionError) as error:
        parse_file(source, pdf_config=PdfProfile(max_bytes=100))
    assert error.value.status == "oversized"
