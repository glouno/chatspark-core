"""Scoped portable provider files; installed hosts own authorization and admission."""

import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chatspark.plugins.contracts import ProviderSelection
from chatspark.profiles.loader import profile_digest
from chatspark.profiles.references import ProviderFileReferenceV1, describe_profile_references


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def reference_key(reference):
    return reference.provider, reference.reference_field, reference.reference


def selection_key(selection):
    return json.dumps(selection.model_dump(mode="json"), sort_keys=True, allow_nan=False)


def _safe_relative(value):
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or path.as_posix() != value
        or value in ("", ".")
    ):
        raise ValueError("Provider output requires a canonical relative path")
    return value


def _canonical_path(path):
    if (
        not path.is_absolute()
        or path.resolve() != path
        or any(parent.is_symlink() for parent in (path, *path.parents))
    ):
        raise ValueError("Provider asset requires a canonical path without symlinks")


def _stat_identity(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def file_digest(path, *, sealed=False, expected_size=None):
    """Verify a regular single-link file through one descriptor, including mutation."""
    _canonical_path(path)
    before = path.stat()
    if expected_size is not None and before.st_size != expected_size:
        raise ValueError("Provider asset size mismatch")
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or (sealed and before.st_mode & 0o222)
    ):
        raise ValueError("Provider asset requires a regular single-link sealed file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            opened = os.fstat(descriptor)
            if _stat_identity(opened) != _stat_identity(before):
                raise ValueError("Provider asset changed during verification")
            remaining = opened.st_size
            hasher = hashlib.sha256()
            while remaining:
                block = stream.read(min(remaining, 1024 * 1024))
                if not block:
                    raise ValueError("Provider asset changed during verification")
                hasher.update(block)
                remaining -= len(block)
            if stream.read(1):
                raise ValueError("Provider asset changed during verification")
            digest = hasher.hexdigest()
            after = os.fstat(descriptor)

        if _stat_identity(before) != _stat_identity(opened) or _stat_identity(
            opened
        ) != _stat_identity(after):
            raise ValueError("Provider asset changed during verification")
        if _stat_identity(after) != _stat_identity(path.stat()):
            raise ValueError("Provider asset changed during verification")
        return digest, after.st_size
    finally:
        os.close(descriptor)


class ProviderInputAssetV1(_Frozen):
    reference: ProviderFileReferenceV1
    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)

    def verify(self):
        if file_digest(self.path, sealed=True, expected_size=self.size_bytes) != (
            self.sha256,
            self.size_bytes,
        ):
            raise ValueError("Provider input digest mismatch")


class _SealedReceipt:
    """Registry-local attestation; changing an admitted file invalidates it."""

    def __init__(self, path, digest, size):
        self.path = path
        self.signature = path.stat()
        if file_digest(path, sealed=True, expected_size=size) != (digest, size):
            raise ValueError("Sealed provider asset digest mismatch")
        self.verify()

    def verify(self):
        _canonical_path(self.path)
        current = self.path.stat()
        for field in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        ):
            if getattr(current, field) != getattr(self.signature, field):
                raise ValueError("Sealed provider asset changed after admission")


class ProviderOutputTargetV1(_Frozen):
    producer: str = Field(min_length=1, max_length=200)
    relative_path: str = Field(min_length=1, max_length=4096)
    references: tuple[ProviderFileReferenceV1, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def valid_target(self):
        _safe_relative(self.relative_path)
        if len({reference_key(ref) for ref in self.references}) != len(self.references):
            raise ValueError("Duplicate provider output reference")
        return self


class EngineBuildAssetsV1(_Frozen):
    schema_version: Literal["chatspark.engine-build-assets.v1"] = "chatspark.engine-build-assets.v1"
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inputs: tuple[ProviderInputAssetV1, ...] = Field(default=(), max_length=128)
    outputs: tuple[ProviderOutputTargetV1, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def unique_assets(self):
        keys = [reference_key(asset.reference) for asset in self.inputs]
        keys += [reference_key(ref) for output in self.outputs for ref in output.references]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate provider asset reference")
        if len({o.producer for o in self.outputs}) != len(self.outputs):
            raise ValueError("A provider can emit only one declared output")
        if len({o.relative_path for o in self.outputs}) != len(self.outputs):
            raise ValueError("Duplicate provider output path")
        return self


class ProviderOutputAssociationV1(_Frozen):
    target: ProviderOutputTargetV1
    producer_settings_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    producer_version: str = Field(min_length=1, max_length=200)
    producer_distribution: str = Field(min_length=1, max_length=200)
    build_id: str = Field(min_length=1, max_length=200)
    corpus_id: str = Field(min_length=1)
    chunk_set_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)

    def verify_build(self, build):
        database = [a.sha256 for a in build.artifacts if a.kind == "canonical_database"]
        if (
            build.status != "succeeded"
            or len(database) != 1
            or (
                self.build_id,
                self.corpus_id,
                self.chunk_set_id,
                self.source_sha256,
                self.profile_sha256,
                self.database_sha256,
            )
            != (
                build.build_id,
                build.corpus_id,
                build.chunk_set_id,
                build.source_snapshot_sha256,
                build.profile_digest,
                database[0],
            )
        ):
            raise ValueError("Provider output does not belong to selected build")
        matches = [a for a in build.artifacts if a.relative_path == self.target.relative_path]
        if len(matches) != 1 or matches[0].sha256 != self.sha256:
            raise ValueError("Provider output does not match selected build artifact")


class EngineOutputAssociationsV1(_Frozen):
    schema_version: Literal["chatspark.engine-output-associations.v1"] = (
        "chatspark.engine-output-associations.v1"
    )
    outputs: tuple[ProviderOutputAssociationV1, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def unique_outputs(self):
        targets = EngineBuildAssetsV1(
            profile_sha256="0" * 64,
            outputs=tuple(output.target for output in self.outputs),
        )
        del targets
        return self


class ProviderPublishedAssetV1(_Frozen):
    association: ProviderOutputAssociationV1
    path: Path

    def verify(self):
        association = self.association
        if file_digest(self.path, sealed=True, expected_size=association.size_bytes) != (
            association.sha256,
            association.size_bytes,
        ):
            raise ValueError("Published provider output digest mismatch")


class EngineServingAssetsV1(_Frozen):
    schema_version: Literal["chatspark.engine-serving-assets.v1"] = (
        "chatspark.engine-serving-assets.v1"
    )
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inputs: tuple[ProviderInputAssetV1, ...] = Field(default=(), max_length=128)
    outputs: tuple[ProviderPublishedAssetV1, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def unique_assets(self):
        EngineBuildAssetsV1(
            profile_sha256=self.profile_sha256,
            inputs=self.inputs,
            outputs=tuple(output.association.target for output in self.outputs),
        )
        return self


def _references(options, field):
    value = options
    for part in field.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _set_reference(options, field, value):
    selected = options
    parts = field.split(".")
    for part in parts[:-1]:
        selected = selected[part]
    selected[parts[-1]] = str(value)


class AssetBoundRegistry:
    """Scoped composition over trusted descriptors, without editing profile values."""

    def __init__(self, registry, profile, assets, *, output_root=None, build=None):
        if isinstance(assets, EngineServingAssetsV1):
            assets = EngineServingAssetsV1.model_validate_json(assets.model_dump_json())
        elif isinstance(assets, EngineBuildAssetsV1):
            assets = EngineBuildAssetsV1.model_validate_json(assets.model_dump_json())
        else:
            raise ValueError("Unsupported provider asset contract")
        self.registry = registry
        self.profile_sha256 = profile_digest(profile)
        if assets.profile_sha256 != self.profile_sha256:
            raise ValueError("Provider asset profile mismatch")
        self._inputs = {reference_key(asset.reference): asset for asset in assets.inputs}
        self._outputs = {}
        self._targets = {}
        self._producers = {}
        self._selections = set()
        self.output_root = output_root
        self.serving = isinstance(assets, EngineServingAssetsV1)
        required = {
            reference_key(ref) for ref in describe_profile_references(profile, registry).providers
        }
        for selection in profile.artifact_builders:
            if selection.provider in self._producers:
                raise ValueError("Ambiguous provider output producer")
            descriptor, options = registry.descriptor(selection, "artifact")
            self._producers[selection.provider] = (selection_key(selection), descriptor, options)

        def visit(value):
            if isinstance(value, list):
                for child in value:
                    visit(child)
            elif isinstance(value, dict):
                provider = value.get("provider")
                if isinstance(provider, str) and provider in registry.providers:
                    self._selections.add(
                        selection_key(
                            ProviderSelection(
                                provider=provider,
                                settings=value.get("settings", {}),
                            )
                        )
                    )
                for child in value.values():
                    visit(child)

        visit(profile.model_dump(mode="json"))
        for output in assets.outputs:
            target = output.association.target if self.serving else output
            producer = self._producers.get(target.producer)
            if producer is None:
                raise ValueError("Provider output producer is absent from frozen profile")
            self._targets[target.producer] = target
            if self.serving:
                if build is None:
                    raise ValueError("Published provider assets require selected build")
                output.association.verify_build(build)
                descriptor, options = producer[1:]
                if (
                    output.association.producer_settings_sha256 != _settings_digest(options)
                    or output.association.producer_version != descriptor.implementation_version
                    or output.association.producer_distribution != descriptor.distribution
                ):
                    raise ValueError("Published provider output implementation mismatch")
                output.verify()
                path = output.path
            else:
                if output_root is None:
                    raise ValueError("Provider build assets require attempt output root")
                path = output_root / target.relative_path
                _canonical_path(path)
                if path.exists():
                    raise ValueError("Fresh provider output must be absent")
            for reference in target.references:
                key = reference_key(reference)
                descriptor = registry.providers.get(reference.provider)
                if (
                    descriptor is None
                    or reference.reference_field not in descriptor.build_output_fields
                ):
                    raise ValueError("Provider reference is not declared as a fresh build output")
                self._outputs[key] = (path, target, output if self.serving else None)
        for key, asset in self._inputs.items():
            descriptor = registry.providers.get(key[0])
            if descriptor is None or key[1] in descriptor.build_output_fields:
                raise ValueError("Fresh provider output cannot be supplied as build input")
            if key[1] not in descriptor.immutable_input_fields:
                raise ValueError("Provider input reference lacks a trusted immutable-input role")
            asset.verify()
        if required != set(self._inputs) | set(self._outputs):
            raise ValueError("Provider assets do not exactly match frozen profile references")
        # Every declared fresh output role must be backed by a producer, never an input.
        for key in required:
            descriptor = registry.providers[key[0]]
            if key[1] in descriptor.build_output_fields and key not in self._outputs:
                raise ValueError("Fresh provider output lacks a build target")
        self._receipts = tuple(
            _SealedReceipt(asset.path, asset.sha256, asset.size_bytes) for asset in assets.inputs
        )
        if self.serving:
            self._receipts += tuple(
                _SealedReceipt(asset.path, asset.association.sha256, asset.association.size_bytes)
                for asset in assets.outputs
            )

    def verify_files(self):
        for receipt in self._receipts:
            receipt.verify()

    @property
    def allowed(self):
        return self.registry.allowed

    @property
    def providers(self):
        return self.registry.providers

    def manifest(self):
        return self.registry.manifest()

    def descriptor(self, selection, capability):
        if self.serving:
            selection = self.prepare_selection(selection, capability)
        descriptor, options = self.registry.descriptor(selection, capability)
        return descriptor, options

    def prepare_selection(self, selection, capability):
        selection = ProviderSelection.model_validate(selection)
        descriptor, options = self.registry.descriptor(selection, capability)
        effective = options.model_dump(mode="json")
        for field in descriptor.reference_fields:
            original = _references(effective, field)
            if not original:
                continue
            if selection_key(selection) not in self._selections:
                raise ValueError("Provider selection differs from frozen profile")
            key = descriptor.provider_id, field, original
            if key in self._inputs:
                asset = self._inputs[key]
                asset.verify()
                path = asset.path
            elif key in self._outputs:
                path, target, published = self._outputs[key]
                if self.serving:
                    published.verify()
                elif (
                    capability != "artifact"
                    or target.producer != descriptor.provider_id
                    or selection_key(selection) != self._producers[target.producer][0]
                ):
                    raise ValueError("Fresh provider output cannot be read during candidate build")
                else:
                    _canonical_path(path)
                    if path.exists():
                        raise ValueError("Fresh provider output must be absent")
                    path.parent.mkdir(parents=True, exist_ok=True)
            else:
                raise ValueError("Provider reference lacks explicit binding")
            _set_reference(effective, field, path)
        return ProviderSelection(provider=selection.provider, settings=effective)

    def resolve(self, selection, capability):
        selection = ProviderSelection.model_validate(selection)
        if not self.serving and capability == "artifact" and selection.provider in self._targets:
            target = self._targets[selection.provider]
            path = self.output_root / target.relative_path
            _canonical_path(path)
            if path.exists():
                raise ValueError("Fresh provider output must be absent")
            path.parent.mkdir(parents=True, exist_ok=True)
        effective = self.prepare_selection(selection, capability)
        # Preserve trusted dependency/model-pool composition in registry adapters.
        return self.registry.resolve(effective, capability)

    def capture_output(self, selection, artifact):
        target = self._targets.get(selection.provider)
        if target is None:
            return
        if selection_key(selection) != self._producers[selection.provider][0]:
            raise ValueError("Provider output producer settings mismatch")
        if artifact.relative_path != target.relative_path:
            raise ValueError("Provider emitted an unexpected output path")
        path = self.output_root / target.relative_path
        digest, _size = file_digest(path)
        if artifact.sha256 != digest:
            raise ValueError("Provider output digest mismatch")
        path.chmod(0o444)

    def associations(self, result):
        for asset in self._inputs.values():
            asset.verify()
        database = [a.sha256 for a in result.artifacts if a.kind == "canonical_database"]
        if len(database) != 1:
            raise ValueError("Provider outputs require exactly one canonical database")
        outputs = []
        for producer, target in self._targets.items():
            matches = [a for a in result.artifacts if a.relative_path == target.relative_path]
            if len(matches) != 1:
                raise ValueError("Declared provider output was not produced")
            digest, size = file_digest(self.output_root / target.relative_path, sealed=True)
            if matches[0].sha256 != digest:
                raise ValueError("Provider output changed after construction")
            descriptor, options = self._producers[producer][1:]
            outputs.append(
                ProviderOutputAssociationV1(
                    target=target,
                    producer_settings_sha256=_settings_digest(options),
                    producer_version=descriptor.implementation_version,
                    producer_distribution=descriptor.distribution,
                    build_id=result.build_id,
                    corpus_id=result.corpus_id,
                    chunk_set_id=result.chunk_set_id,
                    source_sha256=result.source_snapshot_sha256,
                    profile_sha256=result.profile_digest,
                    database_sha256=database[0],
                    sha256=digest,
                    size_bytes=size,
                )
            )
        return EngineOutputAssociationsV1(outputs=tuple(outputs))


def _settings_digest(options):
    return hashlib.sha256(
        json.dumps(
            options.model_dump(mode="json"),
            sort_keys=True,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def load_assets_file(path, *, result_path, model):
    if path is None:
        return None
    source, output = Path(path), Path(result_path)
    if source.resolve() == output.resolve() or (
        source.exists() and output.exists() and source.samefile(output)
    ):
        raise ValueError("Engine result must not overwrite provider asset manifest")
    return model.model_validate_json(source.read_bytes())


def validate_serving_build(snapshot, build):
    databases = [a.sha256 for a in build.artifacts if a.kind == "canonical_database"]
    if (
        build.status != "succeeded"
        or len(databases) != 1
        or (
            snapshot.build_id,
            snapshot.corpus_id,
            snapshot.chunk_set_id,
            snapshot.profile_sha256,
            snapshot.corpus_sha256,
        )
        != (build.build_id, build.corpus_id, build.chunk_set_id, build.profile_digest, databases[0])
    ):
        raise ValueError("Serving snapshot does not match selected build")
