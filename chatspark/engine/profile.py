"""Immutable profile/prompt composition for trusted, portable engine hosts."""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chatspark.profiles.loader import profile_digest
from chatspark.profiles.models import Profile
from chatspark.prompts.bindings import ServingPromptBindingsV1


class EngineProfileBundleV1(BaseModel):
    """Frozen values, not permission to read customer assets or reuse indexes.

    JSON text keeps nested profile values immutable. ``load_profile`` returns an
    independent validated object on every call. Original references are retained
    as identity; explicit prompt text replaces file reads without rewriting them.
    Hosts separately authorize ownership and retain the input bundle's digest.
    Provider file bindings are deliberately unsupported in this first interface.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["chatspark.engine-profile.v1"] = "chatspark.engine-profile.v1"
    profile_json: str = Field(min_length=1, max_length=1048576)
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompts: ServingPromptBindingsV1

    @model_validator(mode="after")
    def verified_profile(self):
        self.load_profile()
        return self

    def load_profile(self) -> Profile:
        profile = Profile.model_validate_json(self.profile_json)
        if profile_digest(profile) != self.profile_sha256:
            raise ValueError("Engine profile digest mismatch")
        self.prompts.validate_profile(profile)
        return profile

    @classmethod
    def from_profile(cls, profile: Profile, *, prompts: ServingPromptBindingsV1):
        return cls(
            profile_json=json.dumps(profile.model_dump(mode="json"), sort_keys=True),
            profile_sha256=profile_digest(profile),
            prompts=prompts,
        )

    def validate_registry(self, profile, registry):
        from chatspark.profiles.references import describe_profile_references

        if describe_profile_references(profile, registry).providers:
            raise ValueError("Engine profile bundle requires supported provider asset bindings")


def resolved_artifact(profile):
    """The existing resolved-profile wrapper, from exactly one loaded object."""
    return {
        "resolved_profile_version": 2,
        "profile": profile.model_dump(mode="json"),
        "profile_digest": profile_digest(profile),
    }


def load_bundle_file(bundle_path, *, result_path):
    if bundle_path is None:
        return None
    source, output = Path(bundle_path), Path(result_path)
    if source.resolve() == output.resolve() or (
        output.exists() and source.exists() and source.samefile(output)
    ):
        raise ValueError("Engine result must not overwrite profile bundle")
    return EngineProfileBundleV1.model_validate_json(source.read_bytes())
