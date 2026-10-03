"""Explicit operator-selected application composition; installed code is trusted."""

from chatspark.plugins import ProviderSelection
from chatspark.runtime.config import settings
from chatspark.runtime.registry import get_registry


def selected_runtime():
    provider = settings.CHATSPARK_RUNTIME_PROVIDER
    if not provider:
        return None
    return get_registry().resolve(ProviderSelection(provider=provider), "runtime")
