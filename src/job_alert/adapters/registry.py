"""Provider-name dispatch for source adapters.

`AdapterRegistry` is the only object a caller needs to turn a `SourceConfig.provider` into the
adapter that can read that provider's listings. It holds the adapters it was given and fails closed
on a provider it does not know, so an unregistered provider is an explicit error rather than a
silent fallback to another provider's parsing rules.

The registry deliberately does not know which providers exist. `AdapterRegistry.default()` builds
itself from `audit.PROVIDER_ADAPTERS`, the mapping the audit owns, which keeps one place in the tree
that wires a provider name to an adapter instance.
"""

from __future__ import annotations

from collections.abc import Iterable

from job_alert.adapters.base import AdapterError, SourceAdapter


class AdapterRegistry:
    """Maps a provider name to the adapter that serves that provider."""

    def __init__(self, adapters: Iterable[SourceAdapter]) -> None:
        registered: dict[str, SourceAdapter] = {}
        for adapter in adapters:
            provider = adapter.provider
            if provider in registered:
                raise AdapterError(f"duplicate provider {provider}")
            registered[provider] = adapter
        self._registered = registered

    @classmethod
    def default(cls) -> AdapterRegistry:
        """Build the registry for the providers this project can currently collect.

        `job_alert.audit` is imported inside the call on purpose. The audit module imports the
        provider adapters, so importing it at module scope would place the registry in the middle
        of that chain and break as soon as the adapters package re-exports `AdapterRegistry`, which
        is the natural place for later tasks to look for it.
        """
        from job_alert.audit import PROVIDER_ADAPTERS

        return cls(PROVIDER_ADAPTERS.values())

    def get(self, provider: str) -> SourceAdapter:
        try:
            return self._registered[provider]
        except KeyError:
            raise AdapterError(f"unsupported provider {provider}") from None

    def supports(self, provider: str) -> bool:
        return provider in self._registered
