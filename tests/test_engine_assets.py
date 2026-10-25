"""Synthetic installed-provider composition over the actual canonical engine."""

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError
from typer.testing import CliRunner

from chatspark.application import AnswerRequestV1, ServingSnapshotV1, create_answer_runtime
from chatspark.engine.assets import (
    AssetBoundRegistry,
    EngineBuildAssetsV1,
    EngineOutputAssociationsV1,
    EngineServingAssetsV1,
    ProviderInputAssetV1,
    ProviderOutputTargetV1,
    ProviderPublishedAssetV1,
    file_digest,
    load_assets_file,
)
from chatspark.engine.build import execute_build
from chatspark.engine.contracts import EngineBuildRequestV1, EngineEvaluationRequestV1
from chatspark.engine.evaluation import execute_evaluation
from chatspark.engine.profile import EngineProfileBundleV1
from chatspark.engine.services import BuildServices
from chatspark.ingestion.v3_rechunk import StructuralChunker
from chatspark.main import app
from chatspark.plugins import ProviderDescriptor, ProviderSelection, RankedEvidence
from chatspark.plugins.contracts import StrategyResult
from chatspark.profiles import load_profile, profile_digest
from chatspark.profiles.models import StructuralSettings
from chatspark.profiles.references import ProviderFileReferenceV1
from chatspark.prompts.bindings import PromptAssetV1, ServingPromptBindingsV1
from chatspark.retrieval.pipeline import execution_context
from chatspark.runtime.registry import get_registry
from chatspark.storage.evidence import EvidenceStore


class IndexOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact: str
    label: str


class ChunkOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auxiliary: str
    size: int = 200


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def setup_candidate(tmp_path, name="first", behavior="valid"):
    root = tmp_path / name
    root.mkdir()
    source = root / "source"
    source.mkdir()
    (source / "workshop.md").write_text(f"# Workshop\nThe registration code is {name}-42.\n")
    auxiliary = root / "materialized-auxiliary.txt"
    auxiliary.write_text("Synthetic chunk configuration")
    auxiliary.chmod(0o444)
    profile = load_profile("example")
    profile.corpus.name = name
    profile.generation.system_prompt = "/absent-host/system.txt"
    profile.chunking = ProviderSelection(
        provider="synthetic-chunker",
        settings={
            "auxiliary": "/absent-host/auxiliary.txt",
            "size": 200 if name == "first" else 300,
        },
    )
    selection = ProviderSelection(
        provider="synthetic-index",
        settings={
            "artifact": "/absent-host/index.json",
            "label": name,
        },
    )
    profile.artifact_builders = [selection]
    profile.retrieval.strategy = selection.model_copy(deep=True)
    prompt = f"Synthetic instructions for {name}."
    bundle = EngineProfileBundleV1.from_profile(
        profile,
        prompts=ServingPromptBindingsV1(
            profile_sha256=profile_digest(profile),
            system=PromptAssetV1(
                reference=profile.generation.system_prompt,
                text=prompt,
                sha256=digest(prompt),
            ),
        ),
    )
    registry = get_registry(allowed=[])
    calls = []

    class Index:
        def __init__(self, options):
            self.options = options

        def build(self, database, output_root, context):
            assert context.corpus_id and context.chunk_set_id
            with EvidenceStore(database)._connect() as con:
                records = [
                    row["chunk_id"]
                    for row in con.execute(
                        "SELECT chunk_id FROM chunks WHERE chunk_set_id=?",
                        (context.chunk_set_id,),
                    ).fetchall()
                ]
            path = Path(self.options.artifact)
            assert path == output_root / "indexes/synthetic.json"
            assert not path.exists()
            if behavior == "wrong-path":
                path = output_root / "unexpected.json"
            elif behavior == "symlink":
                path.symlink_to(auxiliary)
            elif behavior == "hardlink":
                path.hardlink_to(auxiliary)
            if behavior not in ("symlink", "hardlink"):
                path.write_text(json.dumps({"label": self.options.label, "records": records}))
            calls.append(("build", self.options.label, str(path)))
            return {
                "kind": "knowledge_graph_index",
                "relative_path": path.relative_to(output_root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }

        def retrieve(self, query, limit, context, policy, services):
            payload = json.loads(Path(self.options.artifact).read_text())
            assert payload["label"] == self.options.label == name
            calls.append(("retrieve", self.options.label, str(self.options.artifact)))
            records = [RankedEvidence(record, 1.0) for record in payload["records"][:limit]]
            return StrategyResult(records, {"graph": records})

    registry.register(
        ProviderDescriptor(
            "synthetic-index",
            "1",
            frozenset({"artifact", "strategy"}),
            IndexOptions,
            Index,
            reference_fields=("artifact",),
            build_output_fields=("artifact",),
        )
    )

    def chunker(options):
        assert Path(options.auxiliary).read_text() == "Synthetic chunk configuration"
        calls.append(("chunker", name, options.auxiliary))
        return StructuralChunker(
            StructuralSettings(
                target_chunk_size_chars=options.size,
                overlap_chars=10,
            )
        )

    registry.register(
        ProviderDescriptor(
            "synthetic-chunker",
            "1",
            frozenset({"chunker"}),
            ChunkOptions,
            chunker,
            reference_fields=("auxiliary",),
            immutable_input_fields=("auxiliary",),
        )
    )
    assets = EngineBuildAssetsV1(
        profile_sha256=bundle.profile_sha256,
        inputs=(
            ProviderInputAssetV1(
                reference=ProviderFileReferenceV1(
                    provider="synthetic-chunker",
                    reference_field="auxiliary",
                    reference=profile.chunking.settings["auxiliary"],
                ),
                path=auxiliary,
                sha256=hashlib.sha256(auxiliary.read_bytes()).hexdigest(),
                size_bytes=auxiliary.stat().st_size,
            ),
        ),
        outputs=(
            ProviderOutputTargetV1(
                producer="synthetic-index",
                relative_path="indexes/synthetic.json",
                references=(
                    ProviderFileReferenceV1(
                        provider="synthetic-index",
                        reference_field="artifact",
                        reference=selection.settings["artifact"],
                    ),
                ),
            ),
        ),
    )
    request = EngineBuildRequestV1(
        build_id=name,
        source_root=source,
        output_root=root / "attempt",
        profile="/absent-host/profile.yaml",
        chunk_strategy="structural",
    )
    return request, bundle, registry, assets, calls


def build_candidate(candidate):
    request, bundle, registry, assets, calls = candidate
    result = execute_build(
        request,
        profile_bundle=bundle,
        provider_assets=assets,
        services=BuildServices(registry=registry),
    )
    assert result.status == "succeeded", result.error
    associations = EngineOutputAssociationsV1.model_validate_json(
        (request.output_root / "provider-output-associations.json").read_bytes(),
    )
    serving = EngineServingAssetsV1(
        profile_sha256=bundle.profile_sha256,
        inputs=assets.inputs,
        outputs=tuple(
            ProviderPublishedAssetV1(
                association=association,
                path=request.output_root / association.target.relative_path,
            )
            for association in associations.outputs
        ),
    )
    return result, serving


def evaluation_request(request, build):
    manifest = request.output_root / "build-result.json"
    manifest.write_text(build.model_dump_json())
    dataset = request.output_root.parent / "cases.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "question": "registration code",
                "expected_url_substrings": ["workshop.md"],
            }
        )
        + "\n"
    )
    return EngineEvaluationRequestV1(
        build_result=manifest,
        database=request.output_root / "corpus.db",
        dataset=dataset,
        profile=request.profile,
    )


def test_real_build_evaluation_and_streams_bind_separate_corpus_outputs(tmp_path):
    runtimes = []
    for name in ("first", "second"):
        candidate = setup_candidate(tmp_path, name)
        request, bundle, registry, assets, calls = candidate
        build, serving = build_candidate(candidate)
        assert build.profile_digest == bundle.profile_sha256
        assert not Path(request.profile).exists()
        assert calls[0][0] == "chunker" and calls[1][0] == "build"
        association = serving.outputs[0].association
        assert association.database_sha256 == file_digest(request.output_root / "corpus.db")[0]
        assert association.source_sha256 == build.source_snapshot_sha256
        assert serving.outputs[0].path.stat().st_mode & 0o222 == 0
        result = execute_evaluation(
            evaluation_request(request, build),
            profile_bundle=bundle,
            provider_assets=serving,
            registry=registry,
        )
        assert result.status == "succeeded", result.error
        assert result.metrics.recall_at_k == 1
        context = execution_context(request.output_root / "corpus.db")

        class Model:
            def __init__(self, corpus):
                self.corpus = corpus
                self.inputs = []

            def stream_chat(self, messages, *, max_tokens):
                self.inputs.append(messages)
                assert f"{self.corpus}-42" in messages[1]["content"]
                yield f"The registration code is {self.corpus}-42. [C1]"

        model = Model(name)
        runtime = create_answer_runtime(
            ServingSnapshotV1(
                build_id=name,
                database=context.database,
                corpus_id=context.corpus_id,
                chunk_set_id=context.chunk_set_id,
                corpus_sha256=context.snapshot_sha256,
                profile_sha256=bundle.profile_sha256,
            ),
            profile=bundle.load_profile(),
            registry=registry,
            llm=model,
            prompt_bindings=bundle.prompts,
            provider_assets=serving,
            selected_build=build,
        )
        runtimes.append((name, runtime, model))

    async def run():
        async def answer(name, runtime, model):
            await runtime.warmup()
            events = [
                event
                async for event in runtime.stream_answer(
                    AnswerRequestV1(question="registration code"),
                )
            ]
            assert events[-1].event == "done"
            assert name + "-42" in events[-1].data["answer"]
            assert events[-1].data["sources"]
            assert model.inputs[0][0]["content"].startswith(f"Synthetic instructions for {name}.")
            assert all(
                other + "-42" not in json.dumps(model.inputs)
                for other in ("first", "second")
                if other != name
            )

        await asyncio.gather(*(answer(*item) for item in runtimes))

    asyncio.run(run())
    from fastapi.testclient import TestClient

    from chatspark.serve.application import create_http_app

    for name, runtime, model in runtimes:
        with TestClient(create_http_app(runtime, authorize=lambda _: None)) as client:
            response = client.post("/chat", json={"question": "registration code"})
            assert response.status_code == 200
            assert name + "-42" in response.json()["answer"]
            assert response.json()["sources"]
        assert len(model.inputs) == 2


@pytest.mark.parametrize("behavior", ["wrong-path", "symlink", "hardlink"])
def test_invalid_provider_output_fails_build(tmp_path, behavior):
    request, bundle, registry, assets, calls = setup_candidate(tmp_path, behavior=behavior)
    result = execute_build(
        request,
        profile_bundle=bundle,
        provider_assets=assets,
        services=BuildServices(registry=registry),
    )
    assert result.status == "failed"
    assert not (request.output_root / "provider-output-associations.json").exists()


def test_stale_output_cannot_be_candidate_input_or_read_before_build(tmp_path):
    request, bundle, registry, assets, calls = setup_candidate(tmp_path)
    sealed = assets.inputs[0]
    stale = EngineBuildAssetsV1(
        profile_sha256=assets.profile_sha256,
        inputs=assets.inputs
        + (sealed.model_copy(update={"reference": assets.outputs[0].references[0]}),),
    )
    with pytest.raises(ValueError, match="cannot be supplied as build input"):
        AssetBoundRegistry(registry, bundle.load_profile(), stale, output_root=request.output_root)
    scoped = AssetBoundRegistry(
        registry, bundle.load_profile(), assets, output_root=request.output_root
    )
    with pytest.raises(ValueError, match="cannot be read during candidate build"):
        scoped.resolve(bundle.load_profile().retrieval.strategy, "strategy")
    output = request.output_root / assets.outputs[0].relative_path
    output.parent.mkdir(parents=True)
    output.write_text("previous sealed index")
    with pytest.raises(ValueError, match="must be absent"):
        scoped.resolve(bundle.load_profile().artifact_builders[0], "artifact")
    assert not calls


@pytest.mark.parametrize(
    "field",
    ["build_id", "corpus_id", "chunk_set_id", "source_sha256", "database_sha256", "profile_sha256"],
)
def test_foreign_build_association_fails_before_retrieval(tmp_path, field):
    candidate = setup_candidate(tmp_path)
    request, bundle, registry, assets, calls = candidate
    build, serving = build_candidate(candidate)
    output = serving.outputs[0]
    association = output.association.model_copy(
        update={field: "foreign" if field.endswith("_id") else "0" * 64}
    )
    foreign = serving.model_copy(
        update={"outputs": (output.model_copy(update={"association": association}),)}
    )
    with pytest.raises(ValueError, match="does not belong"):
        AssetBoundRegistry(registry, bundle.load_profile(), foreign, build=build)
    assert all(call[0] != "retrieve" for call in calls)


def test_output_and_input_mutation_and_foreign_settings_fail(tmp_path):
    candidate = setup_candidate(tmp_path)
    request, bundle, registry, assets, calls = candidate
    build, serving = build_candidate(candidate)
    changed = serving.outputs[0].association.model_copy(
        update={"producer_settings_sha256": "0" * 64}
    )
    invalid = serving.model_copy(
        update={"outputs": (serving.outputs[0].model_copy(update={"association": changed}),)}
    )
    with pytest.raises(ValueError, match="implementation mismatch"):
        AssetBoundRegistry(registry, bundle.load_profile(), invalid, build=build)
    path = serving.outputs[0].path
    path.chmod(0o644)
    path.write_text("x" * serving.outputs[0].association.size_bytes)
    path.chmod(0o444)
    with pytest.raises(ValueError, match="digest mismatch"):
        AssetBoundRegistry(registry, bundle.load_profile(), serving, build=build)
    auxiliary = assets.inputs[0].path
    auxiliary.chmod(0o644)
    with pytest.raises(ValueError, match="sealed file"):
        assets.inputs[0].verify()


def test_missing_extra_bindings_and_descriptor_output_roles_fail(tmp_path):
    request, bundle, registry, assets, calls = setup_candidate(tmp_path)
    with pytest.raises(ValueError, match="exactly match"):
        AssetBoundRegistry(
            registry,
            bundle.load_profile(),
            assets.model_copy(update={"inputs": ()}),
            output_root=request.output_root,
        )
    extra_reference = assets.inputs[0].reference.model_copy(
        update={"reference": "/absent-host/foreign.txt"}
    )
    extra = assets.inputs[0].model_copy(update={"reference": extra_reference})
    extra_assets = assets.model_copy(update={"inputs": assets.inputs + (extra,)})
    with pytest.raises(ValueError, match="exactly match"):
        AssetBoundRegistry(
            registry, bundle.load_profile(), extra_assets, output_root=request.output_root
        )
    registry.providers["synthetic-index"] = replace(
        registry.providers["synthetic-index"], build_output_fields=()
    )
    with pytest.raises(ValueError, match="not declared as a fresh build output"):
        AssetBoundRegistry(registry, bundle.load_profile(), assets, output_root=request.output_root)
    with pytest.raises(ValueError, match="build-output field"):
        registry.register(
            replace(
                registry.providers["synthetic-index"],
                provider_id="invalid",
                build_output_fields=("foreign",),
            )
        )
    with pytest.raises(ValidationError, match="canonical relative path"):
        ProviderOutputTargetV1(
            producer="synthetic-index",
            relative_path="../escape",
            references=assets.outputs[0].references,
        )


def test_cli_empty_asset_manifest_uses_same_portable_contract(tmp_path):
    profile = load_profile("example")
    bundle = EngineProfileBundleV1.from_profile(
        profile, prompts=ServingPromptBindingsV1(profile_sha256=profile_digest(profile))
    )
    source = tmp_path / "source"
    source.mkdir()
    (source / "workshop.md").write_text("The registration code is ORCHID-42.")
    request = EngineBuildRequestV1(
        build_id="cli",
        source_root=source,
        output_root=tmp_path / "build",
        profile="/absent-host/profile.yaml",
    )
    request_path, bundle_path, assets_path = (
        tmp_path / name for name in ("request.json", "bundle.json", "assets.json")
    )
    request_path.write_text(request.model_dump_json())
    bundle_path.write_text(bundle.model_dump_json())
    assets_path.write_text(
        EngineBuildAssetsV1(profile_sha256=bundle.profile_sha256).model_dump_json()
    )
    result = CliRunner().invoke(
        app,
        [
            "engine",
            "build",
            "--request",
            str(request_path),
            "--result",
            str(tmp_path / "result.json"),
            "--profile-bundle",
            str(bundle_path),
            "--provider-assets",
            str(assets_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (
        EngineOutputAssociationsV1.model_validate_json(
            (request.output_root / "provider-output-associations.json").read_bytes()
        ).outputs
        == ()
    )
    with pytest.raises(ValueError, match="must not overwrite"):
        load_assets_file(assets_path, result_path=assets_path, model=EngineBuildAssetsV1)


def test_role_inventory_preserves_profile_and_unclassified_references_fail(tmp_path):
    from chatspark.profiles import describe_profile_assets

    request, bundle, registry, assets, calls = setup_candidate(tmp_path)
    profile = bundle.load_profile()
    inventory = describe_profile_assets(profile, registry)
    assert inventory.profile_sha256 == bundle.profile_sha256
    assert {item.reference.provider: item.role for item in inventory.providers} == {
        "synthetic-chunker": "immutable_input",
        "synthetic-index": "build_output",
    }
    assert not calls
    registry.providers["synthetic-chunker"] = replace(
        registry.providers["synthetic-chunker"],
        immutable_input_fields=(),
    )
    assert describe_profile_assets(profile, registry).providers[0].role == "unclassified"
    with pytest.raises(ValueError, match="trusted immutable-input role"):
        AssetBoundRegistry(registry, profile, assets, output_root=request.output_root)
    assert profile_digest(profile) == bundle.profile_sha256


def test_modified_admitted_file_and_foreign_selection_are_rejected(tmp_path):
    candidate = setup_candidate(tmp_path)
    request, bundle, registry, assets, calls = candidate
    build, serving = build_candidate(candidate)
    scoped = AssetBoundRegistry(registry, bundle.load_profile(), serving, build=build)
    selection = bundle.load_profile().retrieval.strategy
    foreign = selection.model_copy(update={"settings": {**selection.settings, "label": "foreign"}})
    with pytest.raises(ValueError, match="differs from frozen profile"):
        scoped.resolve(foreign, "strategy")
    scoped.verify_files()
    path = serving.outputs[0].path
    path.chmod(0o644)
    path.write_text("mutated after admission")
    path.chmod(0o444)
    with pytest.raises(ValueError, match="changed after admission"):
        scoped.verify_files()


def test_cli_role_metadata_and_output_aliases(tmp_path):
    profile_output, reference_output, asset_output = (
        tmp_path / name for name in ("profile.json", "refs.json", "assets.json")
    )
    runner = CliRunner()
    args = [
        "profiles",
        "resolve",
        "example",
        "--output",
        str(profile_output),
        "--references-output",
        str(reference_output),
        "--assets-output",
        str(asset_output),
    ]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    resolved = json.loads(profile_output.read_text())
    metadata = json.loads(asset_output.read_text())
    assert metadata["schema_version"] == "chatspark.profile-assets.v1"
    assert metadata["profile_sha256"] == resolved["profile_digest"]
    assert metadata["providers"] == []
    original = reference_output.read_bytes()
    asset_output.unlink()
    asset_output.hardlink_to(reference_output)
    rejected = runner.invoke(app, args)
    assert rejected.exit_code != 0
    assert reference_output.read_bytes() == original


def test_runtime_rechecks_admitted_assets_before_model_input(tmp_path):
    candidate = setup_candidate(tmp_path)
    request, bundle, registry, assets, calls = candidate
    build, serving = build_candidate(candidate)
    context = execution_context(request.output_root / "corpus.db")

    class Model:
        def stream_chat(self, *args, **kwargs):
            pytest.fail("mutated provider output reached generation")

    runtime = create_answer_runtime(
        ServingSnapshotV1(
            build_id=build.build_id,
            database=context.database,
            corpus_id=context.corpus_id,
            chunk_set_id=context.chunk_set_id,
            corpus_sha256=context.snapshot_sha256,
            profile_sha256=bundle.profile_sha256,
        ),
        profile=bundle.load_profile(),
        registry=registry,
        llm=Model(),
        prompt_bindings=bundle.prompts,
        provider_assets=serving,
        selected_build=build,
    )

    async def run():
        await runtime.warmup()
        path = serving.outputs[0].path
        path.chmod(0o644)
        path.write_text("foreign canary after warmup")
        path.chmod(0o444)
        events = [
            event
            async for event in runtime.stream_answer(AnswerRequestV1(question="registration code"))
        ]
        assert [event.event for event in events] == ["error"]
        assert "foreign canary" not in json.dumps([event.model_dump() for event in events])
        await runtime.shutdown()

    asyncio.run(run())


def test_binding_preserves_trusted_factory_composition(tmp_path):
    candidate = setup_candidate(tmp_path)
    request, bundle, registry, assets, calls = candidate
    build, serving = build_candidate(candidate)
    resolved = []

    class Adapter:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.providers = wrapped.providers
            self.allowed = wrapped.allowed

        def descriptor(self, selection, capability):
            return self.wrapped.descriptor(selection, capability)

        def resolve(self, selection, capability):
            resolved.append(selection)
            return self.wrapped.resolve(selection, capability)

        def manifest(self):
            return self.wrapped.manifest()

    scoped = AssetBoundRegistry(Adapter(registry), bundle.load_profile(), serving, build=build)
    selection = bundle.load_profile().retrieval.strategy
    _, options = scoped.descriptor(selection, "strategy")
    assert options.artifact == str(serving.outputs[0].path)
    scoped.resolve(selection, "strategy")
    assert resolved[0].settings["artifact"] == str(serving.outputs[0].path)
    assert selection.settings["artifact"] == "/absent-host/index.json"


def test_size_mismatch_rejected_before_opening_file(tmp_path, monkeypatch):
    path = tmp_path / "sealed.txt"
    path.write_text("owned synthetic file")
    path.chmod(0o444)
    monkeypatch.setattr(
        "chatspark.engine.assets.os.open",
        lambda *a, **kw: pytest.fail("size mismatch scanned the file"),
    )
    with pytest.raises(ValueError, match="size mismatch"):
        file_digest(path, sealed=True, expected_size=1)


def test_replaced_open_file_is_rejected_before_read_and_descriptor_closed(tmp_path, monkeypatch):
    import os

    original, foreign = tmp_path / "original.txt", tmp_path / "foreign.txt"
    original.write_text("owned")
    foreign.write_text("foreign content that must not be read")
    original.chmod(0o444)
    foreign.chmod(0o444)
    open_file, opened = os.open, []

    def replaced(*args, **kwargs):
        descriptor = open_file(foreign, os.O_RDONLY)
        opened.append(descriptor)
        return descriptor

    class ForbiddenRead:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, *args):
            pytest.fail("foreign file read before inode validation")

    monkeypatch.setattr("chatspark.engine.assets.os.open", replaced)
    monkeypatch.setattr("chatspark.engine.assets.os.fdopen", lambda *a, **kw: ForbiddenRead())
    for _ in range(3):
        with pytest.raises(ValueError, match="changed during verification"):
            file_digest(original, sealed=True, expected_size=5)
        with pytest.raises(OSError):
            os.fstat(opened[-1])


def test_parent_alias_cannot_redirect_fresh_output_even_inside_attempt(tmp_path):
    request, bundle, registry, assets, calls = setup_candidate(tmp_path)
    scoped = AssetBoundRegistry(
        registry, bundle.load_profile(), assets, output_root=request.output_root
    )
    directory = request.output_root / "different-indexes"
    directory.mkdir(parents=True)
    (request.output_root / "indexes").symlink_to(directory, target_is_directory=True)
    with pytest.raises(ValueError, match="canonical path without symlinks"):
        scoped.resolve(bundle.load_profile().artifact_builders[0], "artifact")
    assert not calls
    assert not list(directory.iterdir())
