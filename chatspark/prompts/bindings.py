"""Verified local prompt texts for portable, immutable serving composition."""

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chatspark.profiles.loader import profile_digest


class PromptAssetV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reference: str = Field(min_length=1, max_length=4096)
    text: str = Field(max_length=262144)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def verified_text(self):
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.sha256:
            raise ValueError("Prompt asset digest mismatch")
        return self


class ServingPromptBindingsV1(BaseModel):
    """The host verifies artifact ownership before constructing these bindings.

    References identify the original frozen profile's settings; they are never
    opened. Text hashes attest integrity, not customer authorization. Bundled or
    remote prompt selection retains its existing semantics.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["chatspark.serving-prompts.v1"] = "chatspark.serving-prompts.v1"
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    system: PromptAssetV1 | None = None
    user: PromptAssetV1 | None = None

    def validate_profile(self, profile):
        if profile_digest(profile) != self.profile_sha256:
            raise ValueError("Prompt binding profile mismatch")
        for role in ("system", "user"):
            reference = getattr(profile.generation, role + "_prompt")
            asset = getattr(self, role)
            if (asset.reference if asset is not None else None) != (reference or None):
                raise ValueError("Prompt binding reference mismatch")
