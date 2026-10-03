import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict
from typer.testing import CliRunner

from chatspark.engine.contracts import EngineEvaluationRequestV1
from chatspark.engine.evaluation import execute_evaluation
from chatspark.main import app
from chatspark.plugins import (
    PluginError,
    ProviderDescriptor,
    ProviderSelection,
    RankedEvidence,
    Registry,
)
from chatspark.profiles import load_profile
from chatspark.runtime.factory import get_retriever
from chatspark.storage.evidence import EvidenceError, EvidenceStore, narrow_filters


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid")


def test_build_retrieve_and_evaluate(candidate, tmp_path):
    database, build = candidate
    retriever = get_retriever(
        database=database, profile=load_profile("example"), chunk_set_id=build.chunk_set_id
    )
    chunks = retriever.retrieve("registration code", 3)
    assert "ORCHID-42" in chunks[0].content
    manifest = tmp_path / "build.json"
    manifest.write_text(build.model_dump_json())
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(
        json.dumps({"question": "registration code", "expected_url_substrings": ["workshop.md"]})
        + "\n"
    )
    result = execute_evaluation(
        EngineEvaluationRequestV1(
            build_result=manifest, database=database, dataset=dataset, profile="example", top_k=3
        )
    )
    assert result.status == "succeeded", result.error
    assert result.metrics.recall_at_k == 1


def test_registry_does_not_load_unapproved_code():
    def fail():
        raise AssertionError("Unapproved plugin imported")

    registry = Registry(
        entrypoint_reader=lambda **kw: [SimpleNamespace(name="unapproved", load=fail)]
    )
    registry.discover()
    with pytest.raises(PluginError):
        registry.resolve(ProviderSelection(provider="unapproved"), "retrieval")


def test_registry_validation():
    registry = Registry()
    descriptor = ProviderDescriptor(
        "sample", "1", frozenset({"retrieval"}), Options, lambda options: object()
    )
    registry.register(descriptor)
    with pytest.raises(PluginError):
        registry.register(descriptor)
    with pytest.raises(PluginError):
        registry.register(replace(descriptor, provider_id="other", api_version=1))
    with pytest.raises(PluginError):
        registry.register(replace(descriptor, provider_id="other", reference_fields=("../file",)))
    with pytest.raises(PluginError):
        registry.resolve(ProviderSelection(provider="sample", settings={"extra": 1}), "retrieval")
    with pytest.raises(PluginError):
        registry.resolve(ProviderSelection(provider="sample"), "pdf")


def test_filters_are_consistent_and_cannot_widen():
    assert narrow_filters({"scope": ["public", "internal"]}, {"scope": "public"}) == {
        "scope": "public"
    }
    with pytest.raises(EvidenceError):
        narrow_filters({"scope": "internal"}, {"scope": "public"})
    with pytest.raises(EvidenceError):
        narrow_filters({"scope": ["internal"]}, {"scope": ["internal", "public"]})


def test_unknown_and_stale_evidence_rejected(candidate):
    database, build = candidate
    retriever = get_retriever(
        database=database, profile=load_profile("example"), chunk_set_id=build.chunk_set_id
    )
    store = EvidenceStore(database)
    with pytest.raises(EvidenceError):
        store.resolve([RankedEvidence("invented", 1)], retriever.context)
    chunks = retriever.retrieve("registration", 3)
    with pytest.raises(EvidenceError):
        store.resolve(
            [RankedEvidence(chunks[0].chunk_id, 1)],
            replace(retriever.context, filters={"scope": "private"}),
        )
    with pytest.raises(EvidenceError):
        store.resolve(
            [{"evidence_id": chunks[0].chunk_id, "score": 1, "content": "injected"}],
            retriever.context,
        )
    with pytest.raises(EvidenceError):
        store.resolve(
            [RankedEvidence(chunks[0].chunk_id, 1)],
            replace(retriever.context, snapshot_sha256="0" * 64),
        )
    with pytest.raises(EvidenceError):
        store.resolve([RankedEvidence(chunks[0].chunk_id, 1)], retriever.context, allowed_ids=set())


def test_workspace_independent_and_overwrite_refused(tmp_path):
    result = CliRunner().invoke(
        app, ["workspace", "init", "--path", str(tmp_path / "workspace"), "--copy-prompts"]
    )
    assert result.exit_code == 0, result.output
    profile = load_profile(str(tmp_path / "workspace/profiles/local/profile.yaml"))
    assert Path(profile.generation.system_prompt).is_file()
    assert (
        CliRunner()
        .invoke(app, ["workspace", "init", "--path", str(tmp_path / "workspace")])
        .exit_code
        != 0
    )


def test_profile_rejects_active_private_features(tmp_path):
    p = tmp_path / "profile.yaml"
    p.write_text("profile_version: 1\ncorpus:\n  name: local\nretrieval:\n  enable_graph: true\n")
    with pytest.raises(ValueError, match="profile_version: 2"):
        load_profile(str(p))


def test_missing_prompt_override_fails(tmp_path):
    p = tmp_path / "profile.yaml"
    p.write_text(
        "profile_version: 2\ncorpus:\n  name: local\ngeneration:\n  system_prompt: missing.md\n"
    )
    with pytest.raises(ValueError, match="Missing"):
        load_profile(str(p))


def test_empty_invalid_oversized_pdf():
    from chatspark.extraction.pdf import PdfExtractionError, extract_pdf_outcome
    from chatspark.profiles.models import PdfProfile

    assert extract_pdf_outcome(b"").status == "empty"
    with pytest.raises(PdfExtractionError) as error:
        extract_pdf_outcome(b"not a pdf")
    assert error.value.status == "invalid"
    with pytest.raises(PdfExtractionError) as error:
        extract_pdf_outcome(b"%PDF-1234", PdfProfile(max_bytes=5))
    assert error.value.status == "oversized"


def test_default_privacy_and_no_downloads():
    from chatspark.runtime.config import Settings

    config = Settings(_env_file=None)
    assert not config.OBS_REQUEST_TRACE_ENABLED
    assert not config.OBS_CONVERSATION_STORE_ENABLED
    assert config.OBS_TRACE_CONTENT_MODE == "metadata"
    assert config.EMBEDDING_BACKEND == ""
    assert config.EMBEDDING_MODEL == ""
    assert not config.LANGFUSE_ENABLED
    assert not load_profile("example").extraction.ocr


def test_migration_refuses_existing_comparison_report(tmp_path):
    target = tmp_path / "profile.yaml"
    report = target.with_suffix(".migration.json")
    report.write_text("retained comparison")
    response = CliRunner().invoke(app, ["profiles", "migrate", "example", "--output", str(target)])
    assert response.exit_code != 0
    assert report.read_text() == "retained comparison"
    assert not target.exists()


def test_rechunk_stays_in_selected_corpus_and_honors_pdf_limits(candidate):
    from chatspark.ingestion.v3_rechunk import rechunk_v3_database
    from chatspark.storage.v3_repository import V3Repository, stable_id, utc_now

    database, build = candidate
    profile = load_profile("example")
    profile.chunking.settings["pdf_target_chunk_size_chars"] = 100
    profile.chunking.settings["pdf_overlap_chars"] = 0
    with V3Repository(database) as repo, repo.transaction():
        other = repo.ensure_corpus(name="separate-corpus")
        source = repo.ensure_source_system(
            corpus_id=other, kind="local", root_uri="https://other.invalid"
        )
        repo.upsert_content_document_revision(
            corpus_id=other,
            source_system_id=source,
            ingestion_run_id=build.ingestion_run_id,
            document_id=stable_id("other"),
            canonical_uri="https://other.invalid/document",
            content_hash="other-hash",
            text="Separate authorized corpus text.",
            markdown="",
            media_type="text/plain",
            fetched_at=utc_now(),
            title="Other",
        )
        repo.connection.execute(
            "UPDATE document_revisions SET media_type='application/pdf' WHERE revision_id IN (SELECT current_revision_id FROM documents WHERE corpus_id=?)",
            (build.corpus_id,),
        )
        repo.connection.execute(
            "UPDATE sections SET text=? WHERE revision_id IN (SELECT current_revision_id FROM documents WHERE corpus_id=?)",
            ("workshop fact " * 30, build.corpus_id),
        )
    report = rechunk_v3_database(
        database=database, profile=profile, chunk_set_name="pdf-limit-check"
    )
    rows = EvidenceStore(database).rows(
        corpus_id=build.corpus_id, chunk_set_id=report["chunk_set_id"]
    )
    assert rows and all(len(row["text"]) <= 100 for row in rows)
    with EvidenceStore(database)._connect() as con:
        assert (
            con.execute(
                "SELECT count(*) FROM chunks c JOIN document_revisions r USING(revision_id) JOIN documents d USING(document_id) WHERE c.chunk_set_id=? AND d.corpus_id!=?",
                (report["chunk_set_id"], build.corpus_id),
            ).fetchone()[0]
            == 0
        )


def test_result_errors_do_not_disclose_provider_content(tmp_path):
    from chatspark.engine.build import execute_build
    from chatspark.engine.contracts import EngineBuildRequestV1
    from chatspark.engine.services import BuildServices

    source = tmp_path / "source"
    source.mkdir()
    (source / "guide.txt").write_text("Owned synthetic document")
    sentinel = "PRIVATE-SOURCE-CONTENT https://secret.invalid token-example"

    def failing_parser(*args, **kwargs):
        raise ValueError(sentinel)

    result = execute_build(
        EngineBuildRequestV1(
            build_id="redaction",
            source_root=source,
            output_root=tmp_path / "output",
            profile="example",
        ),
        services=BuildServices(parser=failing_parser),
    )
    assert result.status == "failed"
    assert sentinel not in result.model_dump_json()
    assert "secret.invalid" not in result.error
