from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from chatspark.plugins.contracts import RankedEvidence, StrategyResult
from chatspark.profiles import load_profile
from chatspark.profiles.models import Profile, StructuralSettings
from chatspark.profiles.schema import composed_profile_schema
from chatspark.runtime.factory import get_retriever
from chatspark.runtime.registry import get_registry
from chatspark.runtime.validation import validate_runtime
from chatspark.storage.evidence import EvidenceError


def test_explicit_version_and_removed_outer_fields():
    with pytest.raises(ValidationError):
        Profile.model_validate({"corpus": {"name": "local"}})
    for field in ["lanes", "planner", "processor", "enable_graph"]:
        with pytest.raises(ValidationError):
            Profile.model_validate(
                {"profile_version": 2, "corpus": {"name": "local"}, "retrieval": {field: []}}
            )


def test_unused_trust_declaration_is_rejected():
    with pytest.raises(ValidationError, match="trusted_path_roots"):
        Profile.model_validate(
            {
                "profile_version": 2,
                "corpus": {"name": "local"},
                "crawl": {"trusted_path_roots": ["/support/"]},
            }
        )


def test_selected_settings_are_strict():
    profile = load_profile("example")
    profile.chunking.settings = {"target_chunk_size_chars": 100, "overlap_chars": 100}
    with pytest.raises(ValueError):
        validate_runtime(profile)
    profile.chunking.settings = {"profile": {"profile_version": 1}}
    with pytest.raises(ValueError):
        validate_runtime(profile)


def test_schema_has_approved_provider_models():
    schema = composed_profile_schema(get_registry(allowed=[]))
    assert schema["x-plugin-api-version"] == 2
    assert "StructuralSettings" not in schema["$defs"]
    assert schema["$defs"]["structural_chunker"]["additionalProperties"] is False
    assert "private.retrieval" not in str(schema)
    assert schema["properties"]["artifact_builders"]["items"] == {"not": {}}


def test_schema_composes_nested_hybrid_lane_and_strategy_extensions():
    from chatspark.plugins.registry import ProviderDescriptor
    from chatspark.profiles.models import HtmlSettings

    registry = get_registry(allowed=[])
    registry.register(
        ProviderDescriptor(
            "test.planner", "1", frozenset({"planner"}), HtmlSettings, lambda options: None
        )
    )
    registry.register(
        ProviderDescriptor(
            "test.processor", "1", frozenset({"processor"}), HtmlSettings, lambda options: None
        )
    )
    schema = composed_profile_schema(registry)
    settings = schema["$defs"]["hybrid_strategy"]["properties"]
    lane = settings["lanes"]["items"]["oneOf"][0]
    assert lane["properties"]["provider"] == {"const": "sparse"}
    assert lane["properties"]["settings"] == {"$ref": "#/$defs/sparse_retrieval"}
    assert lane["properties"]["limit"]["maximum"] == 1000
    assert lane["properties"]["name"]["pattern"]
    for field in ("planner", "processor"):
        selection = settings[field]["anyOf"][0]["oneOf"][0]
        assert selection["properties"]["provider"] == {"const": "test." + field}
        assert selection["properties"]["settings"] == {
            "$ref": "#/$defs/test_" + field + "_" + field
        }


def test_strategy_is_not_ranked_twice(candidate):
    database, build = candidate
    retriever = get_retriever(
        database=database, profile=load_profile("example"), chunk_set_id=build.chunk_set_id
    )
    chunks = retriever.retrieve("registration", 3)
    record = RankedEvidence(chunks[0].chunk_id, 731)
    retriever.strategy = SimpleNamespace(
        retrieve=lambda *args: StrategyResult((record,), {"chosen": (record,)})
    )
    retriever.profile = retriever.profile.model_copy(
        update={"reranker": SimpleNamespace(backend="invalid")}
    )
    result = retriever.retrieve_result("registration", 3)
    assert result.items[0].score == 731
    assert result.stages["chosen"][0].score == 731


def test_selected_strategy_warmup_uses_canonical_validation(candidate):
    retriever = get_retriever(database=candidate[0], profile=load_profile("example"))
    calls = []
    retriever.strategy = SimpleNamespace(
        retrieve=lambda query, *args: calls.append(query) or StrategyResult(()),
        warmup=lambda *args: calls.append(args),
    )
    retriever.warmup()
    assert calls[0] == "Service readiness check"
    assert calls[1] == (retriever.context, retriever.profile, retriever.services)
    retriever.strategy = SimpleNamespace(
        retrieve=lambda *args: StrategyResult((RankedEvidence("unknown", 1),))
    )
    with pytest.raises(EvidenceError):
        retriever.warmup()


def test_request_policy_runs_before_strategy_and_context(candidate):
    from chatspark.plugins.contracts import RequestPolicyDecision

    retriever = get_retriever(database=candidate[0], profile=load_profile("example"))

    def forbidden(*args):
        pytest.fail("A blocked request reached retrieval or context expansion")

    retriever.strategy = SimpleNamespace(retrieve=forbidden)
    retriever.context_builder = SimpleNamespace(build=forbidden)
    retriever.request_policy = SimpleNamespace(
        evaluate=lambda query, scope: RequestPolicyDecision(action="block_retrieval")
    )
    assert retriever.retrieve_result("registration", 3).items == []
    retriever.request_policy = SimpleNamespace(evaluate=lambda *args: {"action": "allow"})
    with pytest.raises(EvidenceError, match="policy decision"):
        retriever.retrieve_result("registration", 3)


@pytest.mark.parametrize(
    "payload",
    [
        StrategyResult((RankedEvidence("unknown", 1),)),
        StrategyResult((), {"injected": (RankedEvidence("unknown", 1),)}),
        StrategyResult((), diagnostics={"text": "private content"}),
        StrategyResult(({"evidence_id": "fake", "score": 1, "content": "injected"},)),
    ],
)
def test_strategy_invalid_evidence_and_diagnostics(candidate, payload):
    database, build = candidate
    retriever = get_retriever(
        database=database, profile=load_profile("example"), chunk_set_id=build.chunk_set_id
    )
    retriever.strategy = SimpleNamespace(retrieve=lambda *args: payload)
    with pytest.raises(EvidenceError):
        retriever.retrieve("registration", 3)


def test_strategy_context_cannot_widen_authorization(candidate):
    database, build = candidate
    retriever = get_retriever(
        database=database, profile=load_profile("example"), chunk_set_id=build.chunk_set_id
    )
    record = RankedEvidence(retriever.retrieve("registration", 3)[0].chunk_id, 1)
    retriever.context = replace(retriever.context, filters={"doc_id": "other"})
    retriever.strategy = SimpleNamespace(retrieve=lambda *args: StrategyResult(()))
    retriever.context_builder = SimpleNamespace(build=lambda *args: (record,))
    with pytest.raises(EvidenceError):
        retriever.retrieve("registration", 3)


def test_table_rows_and_repeated_headers():
    from chatspark.ingestion.v3_rechunk import structural_chunks

    table = "| Code | Detail |\n| --- | --- |\n" + "\n".join(
        f"| ITEM-{i} | {'value ' * 7} |" for i in range(8)
    )
    chunks = structural_chunks(
        table, 150, 0, StructuralSettings(target_chunk_size_chars=150, overlap_chars=0)
    )
    assert len(chunks) > 2
    assert all(len(c) <= 150 for c in chunks)
    assert all(c.startswith("| Code | Detail |") for c in chunks)
    assert all(any(f"| ITEM-{i} |" in c for c in chunks) for i in range(8))


def test_tiny_chunks():
    from chatspark.ingestion.v3_rechunk import structural_chunks

    settings = StructuralSettings(min_chunk_chars=20, tiny_chunk_policy="drop")
    assert structural_chunks("short", 1000, 100, settings) == []
    settings.tiny_chunk_policy = "keep"
    assert structural_chunks("short", 1000, 100, settings) == ["short"]


def test_html_selectors_attribute_rules_and_table_selection():
    from chatspark.extraction.html import extract_html
    from chatspark.profiles.models import HtmlSettings

    html = '<main><h1>Guide</h1><p>Useful document fact.</p><p class="notice">Remove me.</p><table><tr><th>Code</th><th>Value</th></tr><tr><td>LOT-42</td><td>18</td></tr></table></main>'
    options = HtmlSettings(remove_selectors=[".notice"])
    parsed = extract_html(html, "https://example.invalid", options)
    assert "Remove me" not in parsed.text
    assert any("| LOT-42 | 18 |" in s.content for s in parsed.sections)
    options.include_tables = False
    assert "LOT-42" not in extract_html(html, "https://example.invalid", options).text


def test_e5_prefixes_once():
    from chatspark.embeddings.formatting import format_embedding_text

    for text in ["details", "passage: details", "query: details"]:
        assert (
            format_embedding_text(text, role="query", model_name="e5", configured_format="e5")
            == "query: details"
        )


def test_operator_file_and_environment_precedence(tmp_path, monkeypatch):
    import yaml

    from chatspark.runtime.config import Settings

    path = tmp_path / "runtime.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "embedding": {"backend": "deterministic", "model": "from-file"},
                "state": {"database": "corpus.db"},
            }
        )
    )
    monkeypatch.setenv("CHATSPARK_RUNTIME_CONFIG", str(path))
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.EMBEDDING_MODEL == "from-file"
    assert settings.CHATSPARK_V3_DB_PATH == tmp_path / "corpus.db"
    monkeypatch.setenv("EMBEDDING_MODEL", "environment")
    assert Settings(_env_file=None).EMBEDDING_MODEL == "environment"
    assert Settings(_env_file=None, EMBEDDING_MODEL="explicit").EMBEDDING_MODEL == "explicit"


def test_operator_rejects_credentials_and_unknown_fields():
    from chatspark.runtime.operator import OperatorConfiguration

    for data in [
        {"api_key": "placeholder"},
        {"generation": {"url": "https://user:password@example.invalid"}},
        {"vector": {"url": "https://example.invalid?token=placeholder"}},
    ]:
        with pytest.raises(ValidationError):
            OperatorConfiguration.model_validate(data)


def test_reranker_revision_is_passed_without_remote_download(tmp_path, monkeypatch):
    import sys

    from chatspark.retrieval.rerankers import _cross_encoder
    from chatspark.runtime.operator import OperatorConfiguration

    calls = []
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(
            CrossEncoder=lambda *args, **kwargs: calls.append((args, kwargs)) or object()
        ),
    )
    _cross_encoder.cache_clear()
    first = _cross_encoder(str(tmp_path), "cpu", "revision-a")
    assert _cross_encoder(str(tmp_path), "cpu", "revision-a") is first
    assert _cross_encoder(str(tmp_path), "cpu", "revision-b") is not first
    assert [call[1]["revision"] for call in calls] == ["revision-a", "revision-b"]
    assert all(call[1]["local_files_only"] and not call[1]["trust_remote_code"] for call in calls)
    config = OperatorConfiguration.model_validate({"reranker": {"revision": "revision-a"}})
    assert config.environment_values()["RERANKER_REVISION"] == "revision-a"
    _cross_encoder.cache_clear()


def test_manifest_excludes_credentials_and_prompt_bodies(monkeypatch):
    from chatspark.runtime.config import settings
    from chatspark.runtime.manifest import runtime_manifest

    monkeypatch.setattr(settings, "OPENAI_COMPAT_API_KEY", "credential-placeholder")
    result = runtime_manifest(
        profile=load_profile("example"),
        prompt_metadata={
            "system_sha256": "a" * 64,
            "system_provider": "bundled",
            "prompt_body": "private body",
        },
    )
    assert len(result["operator_configuration_sha256"]) == 64
    assert result["prompt_provenance"]["system_sha256"] == "a" * 64
    assert "private body" not in str(result)
    assert "credential-placeholder" not in str(result)


def test_index_fingerprint_and_missing_index(candidate):
    from chatspark.embeddings.deterministic import DeterministicEmbedder
    from chatspark.retrieval.pipeline import execution_context
    from chatspark.runtime.embedding_manifest import validate_dense_index

    database, build = candidate
    context = execution_context(database, chunk_set_id=build.chunk_set_id)
    with pytest.raises(ValueError, match="matching successful index"):
        validate_dense_index(context, DeterministicEmbedder(), SimpleNamespace(count=lambda: 3))
