import hashlib
import json
from dataclasses import replace

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from chatspark.engine.build import execute_build
from chatspark.engine.contracts import EngineBuildRequestV1, EngineEvaluationRequestV1
from chatspark.engine.evaluation import execute_evaluation
from chatspark.engine.profile import EngineProfileBundleV1, resolved_artifact
from chatspark.engine.services import BuildServices
from chatspark.main import app
from chatspark.profiles import load_profile, profile_digest
from chatspark.prompts.bindings import PromptAssetV1, ServingPromptBindingsV1
from chatspark.runtime.registry import get_registry
from chatspark.runtime.validation import validate_runtime


def bundle_for(profile, system=None, user=None):
    def asset(role, text):
        if text is None:
            return None
        return PromptAssetV1(
            reference=getattr(profile.generation, role + "_prompt"),
            text=text,
            sha256=hashlib.sha256(text.encode()).hexdigest(),
        )

    return EngineProfileBundleV1.from_profile(
        profile,
        prompts=ServingPromptBindingsV1(
            profile_sha256=profile_digest(profile),
            system=asset("system", system),
            user=asset("user", user),
        ),
    )


def request_for(tmp_path, profile="example"):
    source = tmp_path / "source"
    source.mkdir()
    (source / "workshop.md").write_text("# Workshop\n\nThe registration code is ORCHID-42.\n")
    return EngineBuildRequestV1(
        build_id="synthetic",
        source_root=source,
        output_root=tmp_path / "output",
        profile=profile,
    )


def test_bundle_is_independent_preserves_values_and_rechecks_digest():
    profile = load_profile("example")
    bundle = bundle_for(profile)
    expected = profile.model_dump(mode="json")
    profile.retrieval.final_k += 1
    copy = bundle.load_profile()
    assert copy.model_dump(mode="json") == expected
    copy.retrieval.final_k += 2
    assert bundle.load_profile().model_dump(mode="json") == expected
    with pytest.raises(ValidationError):
        bundle.profile_json = "{}"
    with pytest.raises(ValidationError, match="digest mismatch"):
        EngineProfileBundleV1.model_validate({**bundle.model_dump(), "profile_sha256": "0" * 64})
    with pytest.raises(ValueError, match="digest mismatch"):
        bundle.model_copy(update={"profile_json": profile.model_dump_json()}).load_profile()


@pytest.mark.parametrize("role,text", [("system", "$foreign"), ("user", "${invalid")])
def test_bound_prompt_validation_preserves_template_rules(role, text):
    profile = load_profile("example")
    setattr(profile.generation, role + "_prompt", "/absent-host/prompt.txt")
    bundle = bundle_for(profile, **{role: text})
    with pytest.raises(ValueError, match="Malformed prompt"):
        validate_runtime(bundle.load_profile(), prompt_bindings=bundle.prompts)


def test_missing_and_foreign_prompt_bindings_fail():
    profile = load_profile("example")
    profile.generation.system_prompt = "/absent-host/system.txt"
    with pytest.raises(ValueError, match="reference mismatch"):
        bundle_for(profile)
    bound = bundle_for(profile, system="Synthetic project instructions.")
    changed = profile.model_copy(deep=True)
    changed.corpus.name = "foreign"
    with pytest.raises(ValueError, match="profile mismatch"):
        EngineProfileBundleV1.from_profile(changed, prompts=bound.prompts)


def test_build_evaluation_after_original_profile_and_prompts_deleted(tmp_path):
    profile = load_profile("example")
    profile.corpus.name = "synthetic-portable"
    profile.retrieval.final_k = 2
    profile.chunking.settings["target_chunk_size_chars"] = 170
    original = tmp_path / "original.json"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Synthetic project-specific instructions.")
    profile.generation.system_prompt = str(prompt)
    original.write_text(profile.model_dump_json())
    bundle = bundle_for(profile, system=prompt.read_text())
    request = request_for(tmp_path, str(original))
    original.unlink()
    prompt.unlink()
    result = execute_build(request, profile_bundle=bundle)
    assert result.status == "succeeded", result.error
    assert result.profile_digest == bundle.profile_sha256
    snapshot = json.loads((request.output_root / "resolved-profile.json").read_text())
    assert snapshot == resolved_artifact(profile)
    runtime_manifest = json.loads((request.output_root / "runtime-manifest.json").read_text())
    assert runtime_manifest["prompt_hashes"]["system_prompt"] == bundle.prompts.system.sha256
    manifest = tmp_path / "build.json"
    manifest.write_text(result.model_dump_json())
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "question": "registration code",
                "expected_url_substrings": ["workshop.md"],
            }
        )
        + "\n"
    )
    evaluation_request = EngineEvaluationRequestV1(
        build_result=manifest,
        database=request.output_root / "corpus.db",
        dataset=dataset,
        profile=str(original),
    )
    evaluation = execute_evaluation(evaluation_request, profile_bundle=bundle)
    assert evaluation.status == "succeeded", evaluation.error
    assert evaluation.metrics.recall_at_k == 1
    other = bundle.load_profile()
    other.corpus.name = "other-project"
    other.generation.system_prompt = None
    rejected = execute_evaluation(evaluation_request, profile_bundle=bundle_for(other))
    assert rejected.status == "failed"
    assert rejected.observations == []


def test_legacy_build_loads_profile_once_and_rejects_drift(tmp_path):
    profile = load_profile("example")
    calls = []
    services = BuildServices(profile_reader=lambda name: calls.append(name) or profile)
    request = request_for(tmp_path)
    result = execute_build(request, services=services)
    assert result.status == "succeeded", result.error
    assert calls == ["example"]
    changed = profile.model_copy(deep=True)
    changed.retrieval.final_k += 1
    drifted = execute_build(
        request.model_copy(update={"output_root": tmp_path / "drifted"}),
        services=replace(services, profile_snapshot=lambda _: resolved_artifact(changed)),
    )
    assert drifted.status == "failed" and not drifted.artifacts
    rejected = execute_build(
        request.model_copy(update={"output_root": tmp_path / "rejected"}),
        profile_bundle=bundle_for(profile),
        services=services,
    )
    assert rejected.status == "failed" and not rejected.artifacts


def test_provider_references_fail_closed_before_old_index_is_used(monkeypatch):
    from pydantic import BaseModel

    from chatspark.plugins import ProviderDescriptor
    from chatspark.plugins.contracts import ProviderSelection

    class Options(BaseModel):
        artifact: str

    registry = get_registry(allowed=[])
    registry.register(
        ProviderDescriptor(
            "synthetic-artifact",
            "1",
            frozenset({"artifact"}),
            Options,
            lambda _: pytest.fail("old index factory invoked"),
            reference_fields=("artifact",),
        )
    )
    profile = load_profile("example")
    profile.artifact_builders = [
        ProviderSelection(
            provider="synthetic-artifact",
            settings={"artifact": "/old-build/sealed-index.json"},
        )
    ]
    bundle = bundle_for(profile)
    with pytest.raises(ValueError, match="provider asset bindings"):
        bundle.validate_registry(bundle.load_profile(), registry)


def test_cli_bundle_is_additive_and_old_host_paths_are_not_opened(tmp_path):
    profile = load_profile("example")
    profile.generation.user_prompt = "/absent-host/user.txt"
    bundle = bundle_for(profile, user="Question: $question\nEvidence: $context")
    request = request_for(tmp_path, "/absent-host/profile.yaml")
    request_file = tmp_path / "request.json"
    request_file.write_text(request.model_dump_json())
    bundle_file = tmp_path / "bundle.json"
    bundle_file.write_text(bundle.model_dump_json())
    result_file = tmp_path / "result.json"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "engine",
            "build",
            "--request",
            str(request_file),
            "--result",
            str(result_file),
            "--profile-bundle",
            str(bundle_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result_file.read_text())["status"] == "succeeded"
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "question": "registration code",
                "expected_url_substrings": ["workshop.md"],
            }
        )
        + "\n"
    )
    evaluation = EngineEvaluationRequestV1(
        build_result=result_file,
        database=request.output_root / "corpus.db",
        dataset=dataset,
        profile=request.profile,
    )
    evaluation_file = tmp_path / "evaluate.json"
    evaluation_file.write_text(evaluation.model_dump_json())
    evaluated = runner.invoke(
        app,
        [
            "engine",
            "evaluate",
            "--request",
            str(evaluation_file),
            "--result",
            str(tmp_path / "evaluated.json"),
            "--profile-bundle",
            str(bundle_file),
        ],
    )
    assert evaluated.exit_code == 0, evaluated.output


@pytest.mark.parametrize("alias", ["same", "symlink", "hardlink"])
def test_result_cannot_replace_immutable_bundle(tmp_path, alias):
    from chatspark.engine.profile import load_bundle_file

    source = tmp_path / "bundle.json"
    source.write_text(bundle_for(load_profile("example")).model_dump_json())
    expected = source.read_bytes()
    target = tmp_path / "result.json"
    if alias == "same":
        target = source
    elif alias == "symlink":
        target.symlink_to(source)
    else:
        target.hardlink_to(source)
    with pytest.raises(ValueError, match="must not overwrite"):
        load_bundle_file(source, result_path=target)
    assert source.read_bytes() == expected


def test_separate_profile_bundles_keep_chunking_and_retrieval_settings(tmp_path):
    from chatspark.runtime.factory import get_retriever
    from chatspark.storage.evidence import EvidenceStore

    bundles = []
    for name, size, limit in [("first", 100, 1), ("second", 300, 3)]:
        root = tmp_path / name
        root.mkdir()
        profile = load_profile("example")
        profile.corpus.name = name
        profile.chunking.settings["target_chunk_size_chars"] = size
        profile.chunking.settings["overlap_chars"] = 10
        profile.retrieval.final_k = limit
        profile.retrieval.max_chunks_per_doc = 5
        bundle = bundle_for(profile)
        request = request_for(root, "/absent-host/profile.yaml")
        (request.source_root / "workshop.md").write_text(
            "\n\n".join(
                f"Registration code ORCHID-42 applies to workshop number {index}."
                for index in range(30)
            )
            + "\n"
        )
        build = execute_build(request, profile_bundle=bundle)
        assert build.status == "succeeded", build.error
        with EvidenceStore(request.output_root / "corpus.db")._connect() as con:
            settings = json.loads(
                con.execute(
                    "SELECT config_json FROM chunk_sets WHERE chunk_set_id=?",
                    (build.chunk_set_id,),
                ).fetchone()["config_json"]
            )
        assert settings["size"] == size
        retriever = get_retriever(
            database=request.output_root / "corpus.db",
            profile=bundle.load_profile(),
            chunk_set_id=build.chunk_set_id,
            prompt_bindings=bundle.prompts,
        )
        assert len(retriever.retrieve("registration code", 10)) == limit
        assert build.profile_digest == bundle.profile_sha256
        bundles.append(bundle)
    assert bundles[0].profile_sha256 != bundles[1].profile_sha256
