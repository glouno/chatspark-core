"""Synthetic installed OCR plugin: no models, subprocess tools or external calls."""

import multiprocessing
import time

import pytest
from test_pdf_providers import pdf_bytes

from chatspark.extraction.files import UnsupportedDocumentError, parse_bytes
from chatspark.extraction.pdf import PdfExtractionError, recognize_ocr
from chatspark.plugins import ProviderSelection
from chatspark.profiles.models import PdfProfile


@pytest.fixture
def ocr_plugin(tmp_path, monkeypatch):
    module = tmp_path / "synthetic_ocr.py"
    module.write_text("""
import time
from pydantic import BaseModel, ConfigDict
from chatspark.plugins import ProviderDescriptor
class Options(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delay: float = 0
    malformed: bool = False
    text_size: int = 0
    child_pid_file: str = ""
class Provider:
    def __init__(self, options): self.options = options
    def recognize(self, payload, options):
        if self.options.child_pid_file:
            import subprocess, sys
            from pathlib import Path
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            Path(self.options.child_pid_file).write_text(str(child.pid))
        time.sleep(self.options.delay)
        if self.options.malformed: return {"text": ["invalid"]}
        if self.options.text_size: return {"text": "x" * self.options.text_size}
        return {"text": "OCR registration code ORCHID-42.", "markdown": "OCR registration code ORCHID-42."}
def descriptor():
    return ProviderDescriptor("synthetic.ocr", "1", frozenset({"ocr"}), Options, Provider)
""")
    metadata = tmp_path / "synthetic_ocr-1.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: synthetic-ocr\nVersion: 1\n")
    (metadata / "entry_points.txt").write_text(
        "[chatspark.plugins]\nsynthetic.ocr = synthetic_ocr:descriptor\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    from chatspark.runtime.config import settings

    monkeypatch.setattr(settings, "CHATSPARK_PLUGINS_ALLOWED", "synthetic.ocr")
    return ProviderSelection(provider="synthetic.ocr")


def test_explicit_ocr_image_and_pdf_routing(ocr_plugin):
    image = parse_bytes(
        b"synthetic image",
        name="owned.png",
        source_url="https://example.invalid/owned.png",
        ocr_config=ocr_plugin,
    )
    assert "ORCHID-42" in image.content.text
    assert image.diagnostics["provider_version"] == "1"
    assert len(image.diagnostics["input_sha256"]) == 64
    empty_pdf = parse_bytes(
        pdf_bytes(""),
        name="owned.pdf",
        source_url="https://example.invalid/owned.pdf",
        ocr_config=ocr_plugin,
    )
    assert "ORCHID-42" in empty_pdf.content.text
    assert [a["provider"] for a in empty_pdf.diagnostics["attempts"]] == [
        "pdfminer",
        "synthetic.ocr",
    ]
    # A successful text extraction must not invoke the OCR plugin.
    text_pdf = parse_bytes(
        pdf_bytes("ordinary extraction"),
        name="owned.pdf",
        source_url="https://example.invalid/owned.pdf",
        ocr_config=ocr_plugin,
    )
    assert "ordinary extraction" in text_pdf.content.text
    assert text_pdf.diagnostics["provider_version"] != "1"


def test_ocr_absent_unapproved_and_invalid_output(ocr_plugin, monkeypatch):
    with pytest.raises(UnsupportedDocumentError):
        parse_bytes(b"image", name="owned.png", source_url="https://example.invalid/image")
    with pytest.raises(PdfExtractionError) as error:
        recognize_ocr(b"image", ocr_plugin.model_copy(update={"settings": {"malformed": True}}))
    assert error.value.status == "invalid"
    from chatspark.plugins import PluginError
    from chatspark.runtime.config import settings

    monkeypatch.setattr(settings, "CHATSPARK_PLUGINS_ALLOWED", "")
    with pytest.raises(PluginError):
        recognize_ocr(b"image", ocr_plugin)


def test_ocr_timeout_size_and_pdf_shared_deadline(ocr_plugin):
    before = {c.pid for c in multiprocessing.active_children()}
    with pytest.raises(PdfExtractionError) as error:
        recognize_ocr(
            b"image", ocr_plugin.model_copy(update={"settings": {"delay": 5}}), timeout_seconds=0.05
        )
    assert error.value.status == "timeout"
    assert {c.pid for c in multiprocessing.active_children()} == before

    with pytest.raises(PdfExtractionError) as error:
        recognize_ocr(b"image", ocr_plugin, max_bytes=1)
    assert error.value.status == "oversized"
    started = time.monotonic()
    with pytest.raises(PdfExtractionError) as error:
        parse_bytes(
            pdf_bytes(""),
            name="owned.pdf",
            source_url="https://example.invalid/pdf",
            pdf_config=PdfProfile(timeout_seconds=1),
            ocr_config=ocr_plugin.model_copy(update={"settings": {"delay": 5}}),
        )
    assert error.value.status == "timeout"
    assert time.monotonic() - started < 3
    assert {c.pid for c in multiprocessing.active_children()} == before


def test_build_passes_explicit_ocr_selection(ocr_plugin, tmp_path):
    import yaml

    from chatspark.engine.build import execute_build
    from chatspark.engine.contracts import EngineBuildRequestV1

    source = tmp_path / "source"
    source.mkdir()
    (source / "owned.pdf").write_bytes(pdf_bytes(""))
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        yaml.safe_dump(
            {
                "profile_version": 2,
                "corpus": {"name": "synthetic"},
                "retrieval": {"enable_dense": False},
                "extraction": {"ocr": ocr_plugin.model_dump()},
            }
        )
    )
    result = execute_build(
        EngineBuildRequestV1(
            build_id="explicit-ocr",
            source_root=source,
            output_root=tmp_path / "candidate",
            profile=str(profile),
        )
    )
    assert result.status == "succeeded", result.error
    from chatspark.profiles import load_profile
    from chatspark.runtime.factory import get_retriever

    chunks = get_retriever(
        database=tmp_path / "candidate/corpus.db",
        profile=load_profile(str(profile)),
        chunk_set_id=result.chunk_set_id,
    ).retrieve("registration", 3)
    assert "ORCHID-42" in chunks[0].content


def test_ocr_timeout_cleans_provider_subprocess(ocr_plugin, tmp_path):
    import os
    from pathlib import Path

    if os.name != "posix":
        pytest.skip("Process-group cleanup is POSIX-specific")
    pid_file = tmp_path / "provider-child.pid"
    selection = ocr_plugin.model_copy(
        update={
            "settings": {
                "delay": 30,
                "child_pid_file": str(pid_file),
            }
        }
    )
    with pytest.raises(PdfExtractionError) as error:
        recognize_ocr(b"image", selection, timeout_seconds=2)
    assert error.value.status == "timeout"
    assert pid_file.is_file(), "Provider subprocess must start before deadline"
    status = Path(f"/proc/{pid_file.read_text()}/stat")
    # An orphan may briefly remain as a zombie pending init reaping, never running.
    assert not status.exists() or status.read_text().split()[2] == "Z"


def test_ocr_worker_output_size_is_classified(ocr_plugin):
    selection = ocr_plugin.model_copy(update={"settings": {"text_size": 4096}})
    with pytest.raises(PdfExtractionError) as error:
        recognize_ocr(b"image", selection, max_output_bytes=1024)
    assert error.value.status == "output_limit"
