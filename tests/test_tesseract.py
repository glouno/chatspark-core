import shutil
from io import BytesIO

import pytest

from chatspark.extraction.files import parse_bytes
from chatspark.extraction.tesseract import TesseractProvider, TesseractSettings
from chatspark.plugins.contracts import ProviderSelection


def test_missing_ocr_setup_fails_without_installing(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="explicitly installed"):
        TesseractProvider(TesseractSettings()).validate_setup()


def scan_bytes():
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1600, 300), "white")
    # Operator-installed system font; no font or raster asset is distributed.
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 48)
    except OSError:
        font = ImageFont.load_default(size=48)
    ImageDraw.Draw(image).text((40, 70), "ORCHID-42 registration code", fill="black", font=font)
    output = BytesIO()
    image.save(output, format="PDF", resolution=150)
    image.close()
    return output.getvalue()


def test_real_mixed_pdf_preserves_native_pages_and_recognizes_scan():
    if not shutil.which("tesseract"):
        pytest.skip("Optional Tesseract binary is not installed")
    pypdf = pytest.importorskip("pypdf")
    pytest.importorskip("pypdfium2")
    from test_pdf_providers import pdf_bytes

    writer = pypdf.PdfWriter()
    native = "Native document fact LAVENDER-17 must remain available before the scanned page."
    for payload in (pdf_bytes(native), scan_bytes()):
        writer.append(pypdf.PdfReader(BytesIO(payload)))
    output = BytesIO()
    writer.write(output)
    result = parse_bytes(
        output.getvalue(),
        name="mixed.pdf",
        source_url="https://example.invalid/mixed.pdf",
        ocr_config=ProviderSelection(provider="tesseract"),
    )
    assert native in result.content.text
    assert "ORCHID-42" in result.content.text
    assert result.content.text.index("LAVENDER-17") < result.content.text.index("ORCHID-42")
    assert result.diagnostics["pages"] == [
        {"page_index": 0, "method": "native"},
        {"page_index": 1, "method": "ocr"},
    ]


def test_ocr_workspace_cleanup_preserves_external_symlink_target(tmp_path):
    from pathlib import Path

    from chatspark.runtime.temporary import temporary_directory

    external = tmp_path / "external"
    external.mkdir()
    protected = external / "document.txt"
    protected.write_text("must survive cleanup")
    with temporary_directory(prefix="chatspark-test-") as directory:
        workspace = Path(directory)
        (workspace / "external-link").symlink_to(external, target_is_directory=True)
    assert not workspace.exists()
    assert protected.read_text() == "must survive cleanup"
