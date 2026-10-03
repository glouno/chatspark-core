from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, Callable

from pydantic import BaseModel

from chatspark.plugins.contracts import ProviderSelection

PLUGIN_API_VERSION = 2
CAPABILITIES = frozenset(
    {
        "retrieval",
        "strategy",
        "planner",
        "processor",
        "context",
        "artifact",
        "pdf",
        "pdf-inspector",
        "ocr",
        "prompt",
        "html",
        "policy",
        "chunker",
        "runtime",
    }
)


class PluginError(ValueError):
    pass


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    implementation_version: str
    capabilities: frozenset[str]
    settings_model: type[BaseModel]
    factory: Callable[[BaseModel], Any]
    api_version: int = PLUGIN_API_VERSION
    distribution: str = "chatspark-core"
    # Paths into settings, declared by trusted provider code. Resolve these
    # against the file declaring the selection, before inheritance/merging.
    reference_fields: tuple[str, ...] = ()


class Registry:
    def __init__(self, *, allowed=(), entrypoint_reader=entry_points):
        self.allowed = frozenset(allowed)
        self.providers = {}
        self._reader = entrypoint_reader

    def register(self, descriptor, *, external=False):
        if not isinstance(descriptor, ProviderDescriptor):
            raise PluginError("Provider must declare a versioned descriptor")
        if descriptor.api_version != PLUGIN_API_VERSION:
            raise PluginError("Incompatible plugin API")
        if not descriptor.provider_id or not descriptor.capabilities <= CAPABILITIES:
            raise PluginError("Invalid provider descriptor")
        if any(
            not field or any(not component.isidentifier() for component in field.split("."))
            for field in descriptor.reference_fields
        ):
            raise PluginError("Invalid provider reference field")
        if external and descriptor.provider_id not in self.allowed:
            raise PluginError("Provider is not allowlisted")
        if descriptor.provider_id in self.providers:
            raise PluginError("Duplicate provider ID")
        self.providers[descriptor.provider_id] = descriptor

    def discover(self):
        # Enumerate metadata without importing unapproved entry points.
        seen = set()
        for point in self._reader(group="chatspark.plugins"):
            if point.name not in self.allowed:
                continue
            if point.name in seen:
                raise PluginError("Duplicate approved entry point")
            seen.add(point.name)
            descriptor = point.load()()
            if descriptor.provider_id != point.name:
                raise PluginError("Entry-point and provider IDs differ")
            self.register(descriptor, external=True)

    def descriptor(self, selection, capability):
        if not isinstance(selection, ProviderSelection):
            selection = ProviderSelection.model_validate(selection)
        descriptor = self.providers.get(selection.provider)
        if descriptor is None or capability not in descriptor.capabilities:
            raise PluginError(f"Unavailable {capability} provider {selection.provider!r}")
        try:
            options = descriptor.settings_model.model_validate(selection.settings)
        except Exception as error:
            raise PluginError("Invalid provider settings") from error
        return descriptor, options

    def resolve(self, selection, capability):
        descriptor, options = self.descriptor(selection, capability)
        return descriptor.factory(options)

    def manifest(self):
        return [
            {
                "id": p.provider_id,
                "version": p.implementation_version,
                "api_version": p.api_version,
                "distribution": p.distribution,
                "capabilities": sorted(p.capabilities),
            }
            for p in sorted(self.providers.values(), key=lambda d: d.provider_id)
        ]
