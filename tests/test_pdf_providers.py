import importlib.util
import multiprocessing

import pytest

from chatspark.extraction.pdf import PdfExtractionError, extract_pdf_outcome
from chatspark.plugins import ProviderSelection
from chatspark.profiles.models import PdfProfile


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 0, 2.1])
def test_pdfium_rejects_unbounded_render_scale_before_processing(value):
    from pydantic import ValidationError

    from chatspark.extraction.pdf import PdfiumOptions

    with pytest.raises(ValidationError):
        PdfiumOptions(render_scale=value)


def pdf_bytes(text="The workshop code is ORCHID-42."):
    # Authored one-page PDF using standard built-in Helvetica, no binary assets.
    stream = f"BT /F1 12 Tf 30 750 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    result = b"%PDF-1.4\n"
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(result)
    result += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    result += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return (
        result
        + f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )


@pytest.mark.parametrize(
    "provider,module",
    [
        ("pdfminer", "pdfminer"),
        ("pypdf", "pypdf"),
        ("pdfplumber", "pdfplumber"),
        ("pdfium", "pypdfium2"),
    ],
)
def test_adapter_contract(provider, module):
    if importlib.util.find_spec(module) is None:
        pytest.skip("Optional adapter not installed")
    outcome = extract_pdf_outcome(
        pdf_bytes(), PdfProfile(provider=ProviderSelection(provider=provider))
    )
    assert outcome.status == "success"
    assert "ORCHID-42" in outcome.content.text
    assert outcome.diagnostics["provider_version"]
    assert len(outcome.diagnostics["input_sha256"]) == 64


def test_worker_timeout_is_terminated_and_reaped():
    before = {c.pid for c in multiprocessing.active_children()}
    with pytest.raises(PdfExtractionError) as error:
        extract_pdf_outcome(pdf_bytes(), PdfProfile(timeout_seconds=0.001))
    assert error.value.status == "timeout"
    assert {c.pid for c in multiprocessing.active_children()} == before


def test_empty_document_needs_ocr_without_downloading_models():
    assert extract_pdf_outcome(pdf_bytes("")).status == "needs_ocr"


@pytest.mark.parametrize("provider", ["pdfminer", "pypdf", "pdfplumber", "pdfium"])
def test_encrypted_classification(provider):
    modules = {
        "pdfminer": "pdfminer",
        "pypdf": "pypdf",
        "pdfplumber": "pdfplumber",
        "pdfium": "pypdfium2",
    }
    pytest.importorskip(modules[provider])
    pypdf = pytest.importorskip("pypdf")
    from io import BytesIO

    output = BytesIO()
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("owned-test-password")
    writer.write(output)
    with pytest.raises(PdfExtractionError) as error:
        extract_pdf_outcome(
            output.getvalue(), PdfProfile(provider=ProviderSelection(provider=provider))
        )
    assert error.value.status == "encrypted"


@pytest.mark.parametrize(
    "provider,module",
    [
        ("pdfminer", "pdfminer"),
        ("pypdf", "pypdf"),
        ("pdfplumber", "pdfplumber"),
        ("pdfium", "pypdfium2"),
    ],
)
def test_malformed_adapter_input_is_classified_invalid(provider, module):
    pytest.importorskip(module)
    with pytest.raises(PdfExtractionError) as error:
        extract_pdf_outcome(
            b"%PDF-malformed", PdfProfile(provider=ProviderSelection(provider=provider))
        )
    assert error.value.status == "invalid"


def test_failure_content_never_indexed(tmp_path):
    from chatspark.engine.build import execute_build
    from chatspark.engine.contracts import EngineBuildRequestV1

    source = tmp_path / "source"
    source.mkdir()
    (source / "malformed.pdf").write_bytes(b"%PDF-malformed")
    result = execute_build(
        EngineBuildRequestV1(
            build_id="invalid",
            source_root=source,
            output_root=tmp_path / "candidate",
            profile="example",
        )
    )
    assert result.status == "failed"
    import sqlite3

    with sqlite3.connect(tmp_path / "candidate/corpus.db") as con:
        assert con.execute("SELECT count(*) FROM document_revisions").fetchone()[0] == 0


def test_input_size_and_fallback_exhaustion():
    with pytest.raises(PdfExtractionError) as error:
        extract_pdf_outcome(pdf_bytes(), PdfProfile(max_bytes=10))
    assert error.value.status == "oversized"
    with pytest.raises(PdfExtractionError) as error:
        extract_pdf_outcome(
            b"%PDF-malformed", PdfProfile(fallbacks=[ProviderSelection(provider="pdfminer")])
        )
    assert len(error.value.attempts) == 2
    assert all(a["status"] != "success" for a in error.value.attempts)


def test_pdfium_page_inspection_is_bounded():
    pytest.importorskip("pypdfium2")
    from chatspark.extraction.pdf import inspect_pdf

    result = inspect_pdf(pdf_bytes(), ProviderSelection(provider="pdfium"))
    assert result["pages"][0]["width"] == 612
    assert result["pages"][0]["height"] == 792
    before = {c.pid for c in multiprocessing.active_children()}
    with pytest.raises(PdfExtractionError) as error:
        inspect_pdf(pdf_bytes(), ProviderSelection(provider="pdfium"), timeout_seconds=0.001)
    assert error.value.status == "timeout"
    assert {c.pid for c in multiprocessing.active_children()} == before


def test_pdfium_bounded_render_and_pdfium_encryption():
    pytest.importorskip("pypdfium2")
    pypdf = pytest.importorskip("pypdf")
    from io import BytesIO

    from chatspark.extraction.pdf import inspect_pdf

    result = inspect_pdf(
        pdf_bytes(),
        ProviderSelection(
            provider="pdfium", settings={"render_page_indices": [0], "render_scale": 0.5}
        ),
    )
    assert result["renders"][0]["png"].startswith(b"\x89PNG\r\n\x1a\n")
    assert result["renders"][0]["width"] == 306
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("synthetic-password")
    output = BytesIO()
    writer.write(output)
    with pytest.raises(PdfExtractionError) as error:
        extract_pdf_outcome(
            output.getvalue(), PdfProfile(provider=ProviderSelection(provider="pdfium"))
        )
    assert error.value.status == "encrypted"
