from types import SimpleNamespace

import pytest

from chatspark.generation.prompts import resolve_messages
from chatspark.plugins.contracts import ProviderSelection, ResolvedPrompt
from chatspark.profiles import load_profile
from chatspark.runtime.manifest import runtime_manifest


def test_actual_remote_revision_recorded_without_body(monkeypatch):
    import chatspark.runtime.registry as registry

    profile = load_profile("example")
    profile.generation.remote_prompt = ProviderSelection(provider="synthetic-prompt")
    response = ResolvedPrompt(
        text="Generic evidence instructions", revision="7", implementation_version="2.1"
    )
    monkeypatch.setattr(
        registry,
        "get_registry",
        lambda: SimpleNamespace(
            resolve=lambda *_args: SimpleNamespace(get_system_prompt=lambda: response)
        ),
    )
    messages, metadata = resolve_messages("synthetic question", [], profile)
    assert messages[0]["content"].startswith(response.text)
    assert metadata["system_revision"] == "7"
    manifest = runtime_manifest(profile=profile, prompt_metadata=metadata)
    assert manifest["prompt_provenance"]["system_implementation_version"] == "2.1"
    assert response.text not in str(manifest)
    assert "synthetic question" not in str(manifest)


def test_untyped_remote_prompt_is_rejected(monkeypatch):
    import chatspark.runtime.registry as registry

    profile = load_profile("example")
    profile.generation.remote_prompt = ProviderSelection(provider="synthetic-prompt")
    monkeypatch.setattr(
        registry,
        "get_registry",
        lambda: SimpleNamespace(
            resolve=lambda *_args: SimpleNamespace(get_system_prompt=lambda: "unversioned")
        ),
    )
    with pytest.raises(RuntimeError, match="Remote prompt provider failed"):
        resolve_messages("synthetic question", [], profile)
    profile.generation.allow_remote_fallback = True
    _, metadata = resolve_messages("synthetic question", [], profile)
    assert metadata["system_provider"] == "bundled_fallback"
    assert "system_revision" not in metadata
