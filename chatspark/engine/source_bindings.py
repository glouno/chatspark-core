"""Immutable per-file source provenance for offline, corpus-agnostic builds."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EngineSourceBindingV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    relative_path: str = Field(min_length=1, max_length=1000)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_uri: str = Field(min_length=1, max_length=4096)
    acquired_at: datetime | None = None

    @field_validator("acquired_at")
    @classmethod
    def acquisition_time(cls, value):
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Source acquisition timestamp requires a timezone")
            return value.astimezone(UTC)
        return value

    @field_validator("relative_path")
    @classmethod
    def safe_path(cls, value):
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or "\\" in value
            or ":" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or path.as_posix() != value
        ):
            raise ValueError("Source binding requires a safe relative POSIX path")
        return value

    @field_validator("source_uri")
    @classmethod
    def stable_uri(cls, value):
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Source URI contains whitespace or control characters")
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError
            parsed.port
        except ValueError:
            raise ValueError(
                "Source binding requires an absolute HTTP(S) URI without userinfo"
            ) from None
        return value


class EngineSourceBindingsV1(BaseModel):
    """Caller-owned provenance; neither network acquisition nor authorization."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["chatspark.engine-source-bindings.v1"] = (
        "chatspark.engine-source-bindings.v1"
    )
    build_id: str = Field(min_length=1, max_length=200)
    source_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sources: tuple[EngineSourceBindingV1, ...] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def distinct_paths(self):
        paths = [source.relative_path for source in self.sources]
        if len(paths) != len(set(paths)):
            raise ValueError("Duplicate source binding path")
        uris = [source.source_uri for source in self.sources]
        if len(uris) != len(set(uris)):
            raise ValueError("Duplicate source binding URI")
        return self

    @property
    def sha256(self):
        # Entry order is not provenance. Stable hashes do not include host paths.
        value = self.model_dump(mode="json")
        value["sources"].sort(key=lambda source: source["relative_path"])
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    def validate_sources(self, *, build_id, source_root, source_hashes, source_digest):
        if self.build_id != build_id or self.source_snapshot_sha256 != source_digest:
            raise ValueError("Source binding build or snapshot identity mismatch")
        by_path = {source.relative_path: source for source in self.sources}
        if set(by_path) != set(source_hashes):
            raise ValueError("Source binding inventory mismatch")
        root = Path(source_root).resolve()
        for relative, digest in source_hashes.items():
            source = by_path[relative]
            if source.content_sha256 != digest:
                raise ValueError("Source binding content digest mismatch")
            path = root
            for part in PurePosixPath(relative).parts:
                path = path / part
                if path.is_symlink():
                    raise ValueError("Source binding may not traverse symlinks")
            path.resolve().relative_to(root)
            if not path.is_file() or path.stat().st_nlink != 1:
                raise ValueError("Source binding requires an unaliased regular file")
        return by_path

    @classmethod
    def from_directory(cls, *, build_id, source_root, source_uris, acquisition_times=None):
        """Fingerprint supported source files using the engine's existing contract.

        Every supported file needs an explicit URI. No source text is inspected
        for provenance and no downloaded bytes are rewritten.
        """
        from chatspark.engine.build import _source_snapshot_digest
        from chatspark.ingestion.mirror_files import iter_local_mirror_files

        root = Path(source_root).expanduser().resolve()
        files = list(iter_local_mirror_files(root))
        hashes = {}
        digest = _source_snapshot_digest(root, files, file_hashes=hashes)
        if set(source_uris) != set(hashes):
            raise ValueError("Source binding inventory mismatch")
        acquisition_times = acquisition_times or {}
        if not set(acquisition_times) <= set(hashes):
            raise ValueError("Acquisition timestamps reference an unknown source")
        binding = cls(
            build_id=build_id,
            source_snapshot_sha256=digest,
            sources=tuple(
                EngineSourceBindingV1(
                    relative_path=relative,
                    content_sha256=sha,
                    source_uri=source_uris[relative],
                    acquired_at=acquisition_times.get(relative),
                )
                for relative, sha in sorted(hashes.items())
            ),
        )
        binding.validate_sources(
            build_id=build_id, source_root=root, source_hashes=hashes, source_digest=digest
        )
        return binding


def load_source_bindings_file(path, *, result_path):
    if path is None:
        return None
    path = Path(path)
    if path.resolve() == Path(result_path).resolve():
        raise ValueError("Source binding input and build result must be distinct files")
    with path.open("rb") as stream:
        payload = stream.read(8 * 1024 * 1024 + 1)
    if len(payload) > 8 * 1024 * 1024:
        raise ValueError("Source binding manifest exceeds size limit")
    return EngineSourceBindingsV1.model_validate_json(payload)
