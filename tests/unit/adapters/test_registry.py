"""Adapter registry tests."""

from __future__ import annotations

import pytest

from job_alert.adapters.base import AdapterError
from job_alert.adapters.greenhouse import GreenhouseAdapter
from job_alert.adapters.registry import AdapterRegistry

DOCUMENTED_PROVIDERS = ("greenhouse", "lever", "ashby", "smartrecruiters")


def test_registry_returns_documented_adapter() -> None:
    assert AdapterRegistry.default().get("greenhouse").provider == "greenhouse"


def test_registry_rejects_unknown_provider() -> None:
    with pytest.raises(AdapterError, match="unsupported provider unknown"):
        AdapterRegistry.default().get("unknown")


def test_registry_rejects_duplicate_provider() -> None:
    with pytest.raises(AdapterError, match="duplicate provider greenhouse"):
        AdapterRegistry([GreenhouseAdapter(), GreenhouseAdapter()])


def test_registry_default_resolves_all_documented_providers() -> None:
    registry = AdapterRegistry.default()

    for provider in DOCUMENTED_PROVIDERS:
        assert registry.supports(provider)
        assert registry.get(provider).provider == provider


def test_registry_supports_is_false_for_unknown_provider() -> None:
    registry = AdapterRegistry.default()

    assert not registry.supports("unknown")
    assert not registry.supports("workday")


def test_registry_default_reuses_the_audit_mapping() -> None:
    from job_alert.audit import PROVIDER_ADAPTERS

    registry = AdapterRegistry.default()

    for provider, adapter in PROVIDER_ADAPTERS.items():
        assert registry.get(provider) is adapter
