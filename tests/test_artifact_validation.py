import hashlib
from dataclasses import replace

import pytest

from chatspark.engine.build import execute_build
from chatspark.engine.contracts import BuildArtifactV1, EngineBuildRequestV1
from chatspark.engine.services import BuildServices
from chatspark.plugins import ProviderDescriptor, ProviderSelection
from chatspark.profiles import load_profile
from chatspark.runtime.registry import get_registry


@pytest.mark.parametrize("behavior", ["valid", "mutate", "duplicate", "symlink", "escape"])
def test_provider_artifact_integrity_checked_at_completion(tmp_path, monkeypatch, behavior):
    from chatspark.retrieval.sparse_sqlite import SparseOptions
    from chatspark.runtime import registry as module
    from chatspark.runtime import validation

    source = tmp_path / "source"
    source.mkdir()
    (source / "workshop.md").write_text("# Workshop\nThe registration code is ORCHID-42.")
    output = tmp_path / "build"
    outside = tmp_path / "outside.json"
    outside.write_text("private")
    registry = get_registry()

    class Provider:
        def build(self, database, output_root, context):
            assert context.corpus_id and context.chunk_set_id
            path = output_root / "extension.json"
            if behavior in {"symlink", "escape"}:
                if behavior == "escape":
                    (output_root / "subdir").symlink_to(tmp_path, target_is_directory=True)
                    path = output_root / "subdir/outside.json"
                else:
                    path.symlink_to(outside)
            else:
                path.write_text("immutable output")
            return {
                "kind": "knowledge_graph_index",
                "relative_path": path.relative_to(output_root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }

    descriptor = ProviderDescriptor(
        "synthetic-artifact", "1", frozenset({"artifact"}), SparseOptions, lambda _: Provider()
    )
    registry.register(descriptor)
    if behavior == "mutate":

        class Mutating:
            def build(self, database, output_root, context):
                (output_root / "extension.json").write_text("changed after declaration")
                path = output_root / "second.json"
                path.write_text("second")
                return BuildArtifactV1(
                    kind="knowledge_graph_index",
                    relative_path=path.name,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                ).model_dump()

        registry.register(replace(descriptor, provider_id="mutating", factory=lambda _: Mutating()))
    profile = load_profile("example")
    profile.artifact_builders = [ProviderSelection(provider="synthetic-artifact")]
    if behavior in {"duplicate", "mutate"}:
        profile.artifact_builders.append(
            ProviderSelection(provider="mutating" if behavior == "mutate" else "synthetic-artifact")
        )
    monkeypatch.setattr(module, "get_registry", lambda: registry)
    monkeypatch.setattr(validation, "get_registry", lambda: registry)
    result = execute_build(
        EngineBuildRequestV1(
            build_id="artifact", source_root=source, output_root=output, profile="example"
        ),
        services=BuildServices(profile_reader=lambda _: profile),
    )
    assert result.status == ("succeeded" if behavior == "valid" else "failed"), result.error
    if behavior != "valid":
        assert result.error == "Build failed; inspect inputs and operator configuration"
    assert "private" not in result.model_dump_json()
