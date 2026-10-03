"""Build identity must describe the exact bytes that were extracted."""

import hashlib
import sqlite3

import pytest

from chatspark.engine import build as module
from chatspark.engine.contracts import EngineBuildRequestV1
from chatspark.engine.services import BuildServices
from chatspark.extraction.files import parse_bytes
from chatspark.runtime.manifest import write_runtime_manifest


def request(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "workshop.md").write_text("# Workshop\nThe registration code is ORCHID-42.")
    return EngineBuildRequestV1(
        build_id="snapshot", source_root=source, output_root=tmp_path / "output", profile="example"
    )


def test_extraction_receives_verified_bytes_without_another_source_read(tmp_path, monkeypatch):
    build_request = request(tmp_path)
    calls = []

    def parser(raw, **kwargs):
        calls.append(raw)
        return parse_bytes(raw, **kwargs)

    original = module.read_input

    def read_once(path, **kwargs):
        assert not calls
        return original(path, **kwargs)

    monkeypatch.setattr(module, "read_input", read_once)
    result = module.execute_build(build_request, services=BuildServices(byte_parser=parser))
    assert result.status == "succeeded", result.error
    assert len(calls) == 1
    with sqlite3.connect(build_request.output_root / "corpus.db") as con:
        actual = con.execute("SELECT raw_sha256 FROM source_observations").fetchone()[0]
        assert actual == hashlib.sha256(calls[0]).hexdigest()


@pytest.mark.parametrize("stage", ["before_read", "during_parser", "before_success", "add_file"])
def test_changing_source_cannot_produce_successful_snapshot(tmp_path, monkeypatch, stage):
    build_request = request(tmp_path)
    path = build_request.source_root / "workshop.md"
    sentinel = "CHANGED-SOURCE https://private.invalid/secret"
    original_read = module.read_input

    def read(path, **kwargs):
        if stage == "before_read":
            path.write_text(sentinel)
        return original_read(path, **kwargs)

    def parser(raw, **kwargs):
        if stage == "during_parser":
            path.write_text(sentinel)
        return parse_bytes(raw, **kwargs)

    def manifest(destination, **kwargs):
        write_runtime_manifest(destination, **kwargs)
        if stage == "before_success":
            path.write_text(sentinel)
        if stage == "add_file":
            (path.parent / "later.txt").write_text(sentinel)

    monkeypatch.setattr(module, "read_input", read)
    result = module.execute_build(
        build_request, services=BuildServices(byte_parser=parser, manifest_writer=manifest)
    )
    assert result.status == "failed"
    assert sentinel not in result.model_dump_json()


def test_path_based_composition_rejects_mutation_during_parse(tmp_path):
    build_request = request(tmp_path)

    def old_parser(path, **kwargs):
        raw = path.read_bytes()
        path.write_text("Changed while the integration was parsing")
        return parse_bytes(raw, name=path.name, local_path=path, **kwargs)

    result = module.execute_build(build_request, services=BuildServices(parser=old_parser))
    assert result.status == "failed"


def test_source_symlink_cannot_escape_build_root(tmp_path):
    build_request = request(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("Private outside-root input")
    (build_request.source_root / "escape.md").symlink_to(outside)
    result = module.execute_build(build_request)
    assert result.status == "failed"
    assert "Private outside-root input" not in result.model_dump_json()


def test_snapshot_digest_preserves_existing_identity_algorithm(tmp_path):
    build_request = request(tmp_path)
    path = build_request.source_root / "workshop.md"
    digest = hashlib.sha256()
    digest.update(b"workshop.md\0")
    digest.update(hashlib.sha256(path.read_bytes()).digest())
    digest.update(b"\0")
    recorded = {}
    assert (
        module._source_snapshot_digest(build_request.source_root, [path], file_hashes=recorded)
        == digest.hexdigest()
    )
    assert recorded == {"workshop.md": hashlib.sha256(path.read_bytes()).hexdigest()}
