"""Profile-bound reference metadata; installed hosts own authorization and files."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chatspark.profiles.loader import profile_digest


class ProviderFileReferenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: str = Field(min_length=1, max_length=200)
    reference_field: str = Field(min_length=1, max_length=200)
    reference: str = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def valid_field(self):
        if not all(part.isidentifier() for part in self.reference_field.split(".")):
            raise ValueError("Invalid provider reference field")
        return self


class PromptFileReferenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role: Literal["system", "user"]
    reference: str = Field(min_length=1, max_length=4096)


class ProfileReferenceInventoryV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["chatspark.profile-references.v1"] = "chatspark.profile-references.v1"
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    providers: tuple[ProviderFileReferenceV1, ...] = Field(default=(), max_length=128)
    prompts: tuple[PromptFileReferenceV1, ...] = Field(default=(), max_length=2)

    @model_validator(mode="after")
    def unique_references(self):
        if len({(p.provider, p.reference_field, p.reference) for p in self.providers}) != len(
            self.providers
        ):
            raise ValueError("Duplicate provider reference")
        if len({p.role for p in self.prompts}) != len(self.prompts):
            raise ValueError("Duplicate prompt role")
        return self


class ProviderAssetReferenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reference: ProviderFileReferenceV1
    role: Literal["immutable_input", "build_output", "unclassified"]


class ProfileAssetInventoryV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["chatspark.profile-assets.v1"] = "chatspark.profile-assets.v1"
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    providers: tuple[ProviderAssetReferenceV1, ...] = Field(default=(), max_length=128)
    prompts: tuple[PromptFileReferenceV1, ...] = Field(default=(), max_length=2)

    @model_validator(mode="after")
    def unique_references(self):
        ProfileReferenceInventoryV1(
            profile_sha256=self.profile_sha256,
            providers=tuple(value.reference for value in self.providers),
            prompts=self.prompts,
        )
        return self


def describe_profile_assets(profile, registry):
    """Trusted descriptor roles, not ownership, file content or build freshness."""
    inventory = describe_profile_references(profile, registry)
    return ProfileAssetInventoryV1(
        profile_sha256=inventory.profile_sha256,
        prompts=inventory.prompts,
        providers=tuple(
            ProviderAssetReferenceV1(
                reference=reference,
                role=(
                    "build_output"
                    if reference.reference_field
                    in registry.providers[reference.provider].build_output_fields
                    else "immutable_input"
                    if reference.reference_field
                    in registry.providers[reference.provider].immutable_input_fields
                    else "unclassified"
                ),
            )
            for reference in inventory.providers
        ),
    )


def describe_profile_references(profile, registry):
    """Inspect approved descriptor settings without reading files or factories.

    References preserve resolved profile values; they do not attest file content,
    owner, freshness or availability. The caller supplies an approved registry.
    """
    references = set()

    def visit(value):
        if isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, dict):
            provider = value.get("provider")
            descriptor = registry.providers.get(provider) if isinstance(provider, str) else None
            if descriptor is not None:
                options = descriptor.settings_model.model_validate(value.get("settings", {}))
                for field in descriptor.reference_fields:
                    selected = options.model_dump(mode="json")
                    for part in field.split("."):
                        selected = selected.get(part) if isinstance(selected, dict) else None
                    if selected is not None and selected != "":
                        reference = ProviderFileReferenceV1(
                            provider=descriptor.provider_id,
                            reference_field=field,
                            reference=selected,
                        )
                        references.add(
                            (reference.provider, reference.reference_field, reference.reference)
                        )
            for child in value.values():
                visit(child)

    visit(profile.model_dump(mode="json"))
    return ProfileReferenceInventoryV1(
        profile_sha256=profile_digest(profile),
        providers=tuple(
            ProviderFileReferenceV1(provider=provider, reference_field=field, reference=reference)
            for provider, field, reference in sorted(references)
        ),
        prompts=tuple(
            PromptFileReferenceV1(role=role, reference=reference)
            for role in ("system", "user")
            if (reference := getattr(profile.generation, role + "_prompt"))
        ),
    )
