from pathlib import Path

import pytest

from chatspark.profiles import load_profile
from chatspark.profiles.loader import ProfileError


def test_inherited_and_external_prompts_use_declaring_directory(tmp_path):
    parent = tmp_path / "parent"
    child = tmp_path / "child"
    section = tmp_path / "sections"
    for directory in (parent, child, section):
        directory.mkdir()
    (parent / "system.md").write_text("Generic parent prompt")
    (section / "user.md").write_text("$question $context")
    (parent / "profile.yaml").write_text(
        "profile_version: 2\ncorpus: {name: references}\ngeneration: {system_prompt: system.md}\n"
    )
    (section / "generation.yaml").write_text("generation: {user_prompt: user.md}\n")
    path = child / "profile.yaml"
    path.write_text(
        "extends: ../parent/profile.yaml\nsections: {generation: ../sections/generation.yaml}\n"
    )
    profile = load_profile(str(path))
    assert Path(profile.generation.system_prompt) == parent / "system.md"
    assert Path(profile.generation.user_prompt) == section / "user.md"
    (child / "system.md").write_text("Generic child prompt")
    path.write_text(path.read_text() + "generation: {system_prompt: system.md}\n")
    assert Path(load_profile(str(path)).generation.system_prompt) == child / "system.md"


@pytest.mark.parametrize("section", ["extends: 1", "sections: []"])
def test_invalid_reference_shapes_have_clear_errors(tmp_path, section):
    path = tmp_path / "profile.yaml"
    path.write_text("profile_version: 2\ncorpus: {name: references}\n" + section)
    with pytest.raises(ProfileError):
        load_profile(str(path))


def test_provider_artifact_references_keep_declaring_directory(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from chatspark.profiles.loader import resolve_profile_data

    descriptor = SimpleNamespace(reference_fields=("graph.artifact",))
    monkeypatch.setattr(
        "chatspark.runtime.registry.get_registry",
        lambda: SimpleNamespace(providers={"test.strategy": descriptor}),
    )
    parent = tmp_path / "parent"
    child = tmp_path / "child"
    sections = tmp_path / "sections"
    for directory in (parent, child, sections):
        directory.mkdir()
    (parent / "profile.yaml").write_text(
        "profile_version: 2\ncorpus: {name: references}\nretrieval:\n"
        "  strategy: {provider: test.strategy, settings: {graph: {artifact: graph.json}}}\n"
    )
    path = child / "profile.yaml"
    path.write_text("extends: ../parent/profile.yaml\n")
    data, _ = resolve_profile_data(path)
    assert data["retrieval"]["strategy"]["settings"]["graph"]["artifact"] == str(
        parent / "graph.json"
    )
    (sections / "retrieval.yaml").write_text(
        "strategy: {provider: test.strategy, settings: {graph: {artifact: other.json}}}\n"
    )
    path.write_text(path.read_text() + "sections: {retrieval: ../sections/retrieval.yaml}\n")
    data, _ = resolve_profile_data(path)
    assert data["retrieval"]["strategy"]["settings"]["graph"]["artifact"] == str(
        sections / "other.json"
    )


def test_undeclared_provider_values_are_not_treated_as_paths(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from chatspark.profiles.loader import resolve_profile_data

    monkeypatch.setattr(
        "chatspark.runtime.registry.get_registry", lambda: SimpleNamespace(providers={})
    )
    path = tmp_path / "profile.yaml"
    path.write_text(
        "profile_version: 2\ncorpus: {name: references}\nretrieval:\n"
        "  strategy: {provider: unapproved, settings: {artifact: keep-verbatim}}\n"
    )
    data, _ = resolve_profile_data(path)
    assert data["retrieval"]["strategy"]["settings"]["artifact"] == "keep-verbatim"
