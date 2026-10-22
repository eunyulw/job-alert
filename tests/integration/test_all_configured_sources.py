"""The committed registry, checked offline against the adapters this build can actually run.

A source that is enabled but has no adapter would fail every cycle at runtime; these tests make that
a build failure instead, before any company-wide dry run is attempted.
"""

from __future__ import annotations

from job_alert.adapters.registry import AdapterRegistry
from job_alert.audit import DIRECT_STATUSES
from job_alert.config import AppConfig


def test_every_enabled_source_has_registered_adapter(app_config: AppConfig) -> None:
    registry = AdapterRegistry.default()
    missing = sorted(
        source.id
        for source in app_config.sources
        if source.enabled and not registry.supports(source.provider)
    )
    assert missing == []


def test_every_enabled_source_is_direct_or_an_approved_secondary(app_config: AppConfig) -> None:
    """Only SUPPORTED or SUPPORTED_UNDOCUMENTED evidence may put a source on the schedule.

    One narrow exception exists: an email-alert source the user subscribed to for a refusing
    careers site. No aggregator feed may be scheduled.
    """
    undirect = sorted(
        (source.id, str(source.support_status))
        for source in app_config.sources
        if source.enabled
        and source.support_status not in DIRECT_STATUSES
        and source.provider != "email-alerts"
    )
    assert undirect == []


def test_the_registry_enables_at_least_one_source(app_config: AppConfig) -> None:
    """An empty schedule would make every later company-wide check pass vacuously."""
    assert any(source.enabled for source in app_config.sources)
