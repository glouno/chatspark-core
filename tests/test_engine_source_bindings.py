"""Synthetic immutable source provenance; no acquisition or customer fixtures."""

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from test_pdf_providers import pdf_bytes
from typer.testing import CliRunner

from chatspark.engine.build import execute_build
from chatspark.engine.contracts import EngineBuildRequestV1
from chatspark.engine.services import BuildServices
from chatspark.engine.source_bindings import EngineSourceBindingsV1, EngineSourceBindingV1
from chatspark.main import app


def fixture(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "guide.html").write_text("""<!doctype html><html><head><title>Guide</title></head><body>
<!-- Mirrored from https://foreign.example/forged -->
<main><p>The workshop registration code is ORCHID-42. Follow the documented registration instructions in this synthetic workshop guide.</p></main></body></html>""")
    (root / "upload.txt").write_text(
        "The uploaded course syllabus uses the BLUEBELL-17 reference. This independent document describes the authored synthetic course materials."
    )
    (root / "notes.pdf").write_bytes(pdf_bytes("The synthetic PDF training code is WILLOW-93."))
    request = EngineBuildRequestV1(
        build_id="synthetic", source_root=root, output_root=tmp_path / "output", profile="example"
    )
    bindings = EngineSourceBindingsV1.from_directory(
        build_id=request.build_id,
        source_root=root,
        source_uris={
            "guide.html": "https://docs.example/guide",
            "upload.txt": "https://sources.invalid/uploads/syllabus",
            "notes.pdf": "https://docs.example/notes.pdf",
        },
    )
    return request, bindings


def test_mixed_build_uses_explicit_uris_and_original_acquisition_time(tmp_path):
    request, bindings = fixture(tmp_path)
    raw_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in request.source_root.iterdir()
    }
    payload = bindings.model_dump(mode="json")
    payload["sources"][0]["acquired_at"] = "2025-04-01T10:00:00+02:00"
    bindings = EngineSourceBindingsV1.model_validate_json(json.dumps(payload))
    # Explicit provenance bypasses even a trusted legacy resolver, without patching it.
    services = BuildServices(
        url_resolver=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy resolver invoked")
        )
    )
    result = execute_build(request, source_bindings=bindings, services=services)
    assert result.status == "succeeded", result.error
    assert result.source_snapshot_sha256 == bindings.source_snapshot_sha256
    assert result.stages["ingestion"].details["source_bindings_sha256"] == bindings.sha256
    with sqlite3.connect(request.output_root / "corpus.db") as connection:
        uris = {row[0] for row in connection.execute("SELECT canonical_uri FROM documents")}
        assert uris == {source.source_uri for source in bindings.sources}
        assert (
            connection.execute(
                "SELECT fetched_at FROM source_objects WHERE canonical_uri='https://docs.example/guide'"
            ).fetchone()[0]
            == "2025-04-01T08:00:00+00:00"
        )
    assert raw_hashes == {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in request.source_root.iterdir()
    }


def test_legacy_and_explicit_builds_preserve_extraction_and_bind_identity(tmp_path):
    request, bindings = fixture(tmp_path)
    legacy = execute_build(request)
    explicit = execute_build(
        request.model_copy(update={"output_root": tmp_path / "bound"}), source_bindings=bindings
    )
    assert legacy.status == explicit.status == "succeeded"
    with (
        sqlite3.connect(request.output_root / "corpus.db") as a,
        sqlite3.connect(tmp_path / "bound" / "corpus.db") as b,
    ):
        assert sorted(a.execute("SELECT text FROM document_revisions")) == sorted(
            b.execute("SELECT text FROM document_revisions")
        )
    # Same immutable files/profile; explicit provenance changes build request identity.
    assert legacy.source_snapshot_sha256 == explicit.source_snapshot_sha256
    assert legacy.profile_digest == explicit.profile_digest
    assert (
        legacy.request_sha256
        == hashlib.sha256(
            json.dumps(
                request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )
    expected = {
        "request": request.model_copy(update={"output_root": tmp_path / "bound"}).model_dump(
            mode="json"
        ),
        "source_bindings_sha256": bindings.sha256,
    }
    assert (
        explicit.request_sha256
        == hashlib.sha256(
            json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


@pytest.mark.parametrize(
    "mutation", ["foreign_build", "foreign_snapshot", "missing", "extra", "hash"]
)
def test_foreign_missing_extra_or_mutated_manifest_fails_before_parser(tmp_path, mutation):
    request, bindings = fixture(tmp_path)
    payload = bindings.model_dump(mode="json")
    if mutation == "foreign_build":
        payload["build_id"] = "foreign"
    elif mutation == "foreign_snapshot":
        payload["source_snapshot_sha256"] = "0" * 64
    elif mutation == "missing":
        payload["sources"].pop()
    elif mutation == "extra":
        payload["sources"].append(
            {
                "relative_path": "extra.txt",
                "content_sha256": "a" * 64,
                "source_uri": "https://docs.example/extra",
            }
        )
    else:
        payload["sources"][0]["content_sha256"] = "b" * 64
    malformed = EngineSourceBindingsV1.model_validate_json(json.dumps(payload))
    calls = []
    result = execute_build(
        request,
        source_bindings=malformed,
        services=BuildServices(byte_parser=lambda *_a, **_k: calls.append(1)),
    )
    assert result.status == "failed" and not calls
    assert not (request.output_root / "corpus.db").exists()


@pytest.mark.parametrize(
    "relative",
    [
        "../guide.html",
        "/guide.html",
        "a//guide.html",
        "./guide.html",
        "a/../guide.html",
        "a\\guide.html",
        "C:guide.html",
        "a\nfile",
    ],
)
def test_noncanonical_or_unsafe_paths_rejected(relative):
    with pytest.raises(ValidationError):
        EngineSourceBindingV1(
            relative_path=relative, content_sha256="a" * 64, source_uri="https://docs.example/guide"
        )


def test_duplicate_paths_and_bypassed_validation_rejected(tmp_path):
    request, bindings = fixture(tmp_path)
    with pytest.raises(ValidationError, match="Duplicate"):
        EngineSourceBindingsV1(
            build_id="synthetic",
            source_snapshot_sha256=bindings.source_snapshot_sha256,
            sources=bindings.sources + (bindings.sources[0],),
        )
    forged = bindings.model_copy(
        update={
            "sources": (bindings.sources[0].model_copy(update={"relative_path": "../foreign"}),)
        }
    )
    assert execute_build(request, source_bindings=forged).status == "failed"


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_aliased_files_fail_closed(tmp_path, alias):
    request, bindings = fixture(tmp_path)
    source = request.source_root / "guide.html"
    outside = tmp_path / "guide-copy.html"
    source.rename(outside)
    if alias == "symlink":
        source.symlink_to(outside)
    else:
        os.link(outside, source)
    assert execute_build(request, source_bindings=bindings).status == "failed"


def test_cli_accepts_separate_manifest_without_changing_v1_request(tmp_path):
    request, bindings = fixture(tmp_path)
    request_path, binding_path, result_path = (
        tmp_path / name for name in ("request.json", "bindings.json", "result.json")
    )
    request_path.write_text(request.model_dump_json())
    binding_path.write_text(bindings.model_dump_json())
    outcome = CliRunner().invoke(
        app,
        [
            "engine",
            "build",
            "--request",
            str(request_path),
            "--result",
            str(result_path),
            "--source-bindings",
            str(binding_path),
        ],
    )
    assert outcome.exit_code == 0, outcome.output
    assert json.loads(result_path.read_text())["status"] == "succeeded"
    assert EngineBuildRequestV1.model_validate_json(request_path.read_text()) == request


def test_acquisition_time_and_manifest_digest_are_immutable(tmp_path):
    _request, bindings = fixture(tmp_path)
    reverse = bindings.model_copy(update={"sources": tuple(reversed(bindings.sources))})
    assert reverse.sha256 == bindings.sha256
    with pytest.raises(ValidationError):
        bindings.sources[0].source_uri = "https://foreign.example/"
    with pytest.raises(ValidationError, match="timezone"):
        EngineSourceBindingV1(
            relative_path="guide.html",
            content_sha256="a" * 64,
            source_uri="https://docs.example/guide",
            acquired_at=datetime(2025, 4, 1),
        )
    source = EngineSourceBindingV1(
        relative_path="guide.html",
        content_sha256="a" * 64,
        source_uri="https://docs.example/guide",
        acquired_at=datetime(2025, 4, 1, tzinfo=UTC),
    )
    assert source.acquired_at.tzinfo == UTC


def test_selected_profile_rewrites_are_preserved_and_collisions_rejected(tmp_path):
    from chatspark.profiles import load_profile
    from chatspark.profiles.models import CanonicalURLRewrite

    request, bindings = fixture(tmp_path)
    profile = load_profile("example")
    profile.crawl.canonical_url_rewrites = [
        CanonicalURLRewrite(from_host="docs.example", to_host="published.example")
    ]
    result = execute_build(
        request, source_bindings=bindings, services=BuildServices(profile_reader=lambda _: profile)
    )
    assert result.status == "succeeded", result.error
    with sqlite3.connect(request.output_root / "corpus.db") as connection:
        assert "https://published.example/guide" in {
            row[0] for row in connection.execute("SELECT canonical_uri FROM documents")
        }
    collision = profile.model_copy(deep=True)
    collision.crawl.canonical_url_rewrites = [
        CanonicalURLRewrite(from_host="docs.example", from_path="/guide", to_path="/notes.pdf")
    ]
    rejected = execute_build(
        request.model_copy(update={"output_root": tmp_path / "collision"}),
        source_bindings=bindings,
        services=BuildServices(profile_reader=lambda _: collision),
    )
    assert rejected.status == "failed"
    assert not (tmp_path / "collision" / "corpus.db").exists()


def test_duplicate_uris_rejected_and_uri_time_changes_bind_build_identity(tmp_path):
    request, bindings = fixture(tmp_path)
    payload = bindings.model_dump(mode="json")
    payload["sources"][1]["source_uri"] = payload["sources"][0]["source_uri"]
    with pytest.raises(ValidationError, match="Duplicate source binding URI"):
        EngineSourceBindingsV1.model_validate_json(json.dumps(payload))
    changed = EngineSourceBindingsV1.from_directory(
        build_id=request.build_id,
        source_root=request.source_root,
        source_uris={source.relative_path: source.source_uri for source in bindings.sources},
        acquisition_times={"guide.html": datetime(2025, 4, 1, tzinfo=UTC)},
    )
    assert changed.source_snapshot_sha256 == bindings.source_snapshot_sha256
    assert changed.sha256 != bindings.sha256
