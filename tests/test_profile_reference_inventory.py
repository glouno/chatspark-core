"""Generic descriptor metadata with synthetic absent files; no provider execution."""

from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError
from typer.testing import CliRunner

from chatspark.main import app
from chatspark.profiles import (
    Profile,
    ProfileReferenceInventoryV1,
    describe_profile_references,
    profile_digest,
)


class FileOptions(BaseModel):
    graph: dict[str, str]


def example():
    selection = {
        "provider": "synthetic.index",
        "settings": {"graph": {"artifact": "/old-host/absent-index.json"}},
    }
    profile = Profile.model_validate(
        {
            "profile_version": 2,
            "corpus": {"name": "synthetic"},
            "retrieval": {"strategy": selection, "enable_dense": False},
            "artifact_builders": [selection],
            "generation": {
                "system_prompt": "/old-host/absent-system.md",
                "user_prompt": "/old-host/absent-user.md",
            },
        }
    )
    descriptor = SimpleNamespace(
        provider_id="synthetic.index",
        settings_model=FileOptions,
        reference_fields=("graph.artifact",),
        factory=lambda *args: pytest.fail("must not instantiate provider"),
    )
    return profile, SimpleNamespace(providers={"synthetic.index": descriptor})


def test_metadata_is_unique_profile_bound_and_never_opens_absent_files():
    profile, registry = example()
    original = profile.model_dump_json()
    metadata = describe_profile_references(profile, registry)
    assert metadata.profile_sha256 == profile_digest(profile)
    assert metadata.model_dump(mode="json")["providers"] == [
        {
            "provider": "synthetic.index",
            "reference_field": "graph.artifact",
            "reference": "/old-host/absent-index.json",
        }
    ]
    assert [(p.role, p.reference) for p in metadata.prompts] == [
        ("system", "/old-host/absent-system.md"),
        ("user", "/old-host/absent-user.md"),
    ]
    assert profile.model_dump_json() == original


@pytest.mark.parametrize("bad", ["digest", "field", "duplicate", "prompt_role", "schema", "extra"])
def test_forged_reference_metadata_fails_closed(bad):
    profile, registry = example()
    value = describe_profile_references(profile, registry).model_dump(mode="json")
    if bad == "digest":
        value["profile_sha256"] = "invalid"
    elif bad == "field":
        value["providers"][0]["reference_field"] = "graph..artifact"
    elif bad == "duplicate":
        value["providers"] *= 2
    elif bad == "prompt_role":
        value["prompts"] = [value["prompts"][0]] * 2
    elif bad == "schema":
        value["schema_version"] = "unknown"
    else:
        value["authority"] = "unverified"
    with pytest.raises(ValidationError):
        ProfileReferenceInventoryV1.model_validate(value)


def test_cli_preserves_resolved_v2_and_emits_separate_metadata(tmp_path):
    import json

    output, references = tmp_path / "profile.json", tmp_path / "references.json"
    result = CliRunner().invoke(
        app,
        [
            "profiles",
            "resolve",
            "example",
            "--output",
            str(output),
            "--references-output",
            str(references),
        ],
    )
    assert result.exit_code == 0, result.output
    profile = json.loads(output.read_text())
    metadata = ProfileReferenceInventoryV1.model_validate_json(references.read_bytes())
    assert set(profile) == {"resolved_profile_version", "profile", "profile_digest"}
    assert profile["resolved_profile_version"] == 2
    assert profile["profile_digest"] == metadata.profile_sha256
    assert profile_digest(Profile.model_validate(profile["profile"])) == metadata.profile_sha256
    assert not metadata.providers and not metadata.prompts


def test_cli_rejects_alias_output_paths_without_overwriting(tmp_path):
    output = tmp_path / "preserved.json"
    output.write_text("preserved")
    result = CliRunner().invoke(
        app,
        [
            "profiles",
            "resolve",
            "example",
            "--output",
            str(output),
            "--references-output",
            str(output),
        ],
    )
    assert result.exit_code != 0
    assert output.read_text() == "preserved"
