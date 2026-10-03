"""Explicit local OCR. Binaries and language data are installed by the operator."""

import shutil
import subprocess
from io import BytesIO
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from chatspark.plugins.contracts import ExtractedPage, PageExtractionResult, ProviderSelection
from chatspark.runtime.temporary import temporary_directory


class TesseractSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    languages: list[str] = Field(default_factory=lambda: ["eng"], min_length=1, max_length=8)
    min_native_text_chars: int = Field(default=40, ge=0, le=100000)
    render_scale: float = Field(default=2, ge=0.5, le=2, allow_inf_nan=False)
    max_render_dimension: int = Field(default=4096, ge=100, le=4096)
    max_render_pixels: int = Field(default=16777216, ge=10000, le=67108864)
    max_pages: int = Field(default=200, ge=1, le=10000)
    page_segmentation_mode: int = Field(default=3, ge=0, le=13)

    @field_validator("languages")
    @classmethod
    def language_ids(cls, values):
        import re

        if any(not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", value) for value in values):
            raise ValueError("Use installed Tesseract language IDs such as eng or fra")
        return values


class TesseractProvider:
    def __init__(self, options):
        self.options = options

    def validate_setup(self):
        binary = shutil.which("tesseract")
        if not binary:
            raise ValueError("Tesseract OCR requires an explicitly installed tesseract binary")
        result = subprocess.run(
            [binary, "--list-langs"], capture_output=True, timeout=10, check=True, text=True
        )
        available = set((result.stdout + result.stderr).splitlines())
        if not set(self.options.languages) <= available:
            raise ValueError("Install every selected Tesseract language before processing")
        return binary

    def _image_text(self, payload, binary):
        from PIL import Image

        from chatspark.extraction.pdf import PdfExtractionError

        with Image.open(BytesIO(payload)) as image:
            if (
                max(image.size) > self.options.max_render_dimension
                or image.width * image.height > self.options.max_render_pixels
                or getattr(image, "n_frames", 1) != 1
            ):
                raise PdfExtractionError("unsupported", [])
            with temporary_directory(prefix="chatspark-tesseract-") as directory:
                source = Path(directory) / "input.png"
                target = Path(directory) / "output"
                with image.convert("RGB") as converted:
                    converted.save(source)
                subprocess.run(
                    [
                        binary,
                        str(source),
                        str(target),
                        "-l",
                        "+".join(self.options.languages),
                        "--psm",
                        str(self.options.page_segmentation_mode),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True,
                    timeout=120,
                )
                with target.with_suffix(".txt").open("rb") as stream:
                    output = stream.read(1024 * 1024 + 1)
                if len(output) > 1024 * 1024:
                    raise PdfExtractionError("output_limit", [])
                return output.decode("utf-8").strip()

    def recognize(self, payload, options):
        from chatspark.extraction.pdf import PdfExtractionError, _content

        binary = self.validate_setup()
        if not payload.startswith(b"%PDF-"):
            text = self._image_text(payload, binary)
            return PageExtractionResult(
                content=_content(text), pages=[ExtractedPage(page_index=0, text=text, method="ocr")]
            )
        from chatspark.runtime.registry import get_registry

        selection = ProviderSelection.model_validate(
            options.get("inspector") or {"provider": "pdfium"}
        )
        inspector = get_registry().resolve(selection, "pdf-inspector")
        inspection = inspector.inspect(payload, {**selection.settings, "render_page_indices": []})
        if len(inspection["pages"]) > self.options.max_pages:
            raise PdfExtractionError("unsupported", [])
        native = {
            page.page_index: page
            for value in options.get("native_pages", [])
            if (page := ExtractedPage.model_validate(value))
        }
        pages = []
        for info in inspection["pages"]:
            index = info["page_index"]
            page = native.get(index)
            if page and len(page.text.strip()) >= self.options.min_native_text_chars:
                pages.append(page)
                continue
            scale = min(
                self.options.render_scale,
                self.options.max_render_dimension / max(info["width"], info["height"]),
            )
            if info["width"] * info["height"] * scale * scale > self.options.max_render_pixels:
                raise PdfExtractionError("unsupported", [])
            rendered = inspector.inspect(
                payload,
                {**selection.settings, "render_page_indices": [index], "render_scale": scale},
            )
            text = self._image_text(rendered["renders"][0]["png"], binary)
            pages.append(ExtractedPage(page_index=index, text=text, method="ocr"))
        return PageExtractionResult(
            content=_content("\f".join(page.text for page in pages)), pages=pages
        )
