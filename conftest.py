"""Pytest bootstrap that makes the shared test helpers importable.

The tests live outside any installed package, so pytest's default import mode does not put the
repository root on ``sys.path``. Adapter tests import the shared assertions from
``tests.helpers``, so add the repository root and the ``tests`` directory explicitly.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from job_alert.config import AppConfig, FilterConfig, load_app_config
from job_alert.models import NormalizedJob

ROOT = Path(__file__).resolve().parent

for entry in (ROOT, ROOT / "tests"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))


@pytest.fixture()
def app_config() -> AppConfig:
    """The repository's canonical company, source, and filter configuration."""
    return load_app_config(ROOT / "config")


@pytest.fixture()
def filter_config() -> Callable[..., FilterConfig]:
    """Build a filter record whose fields tests override one at a time."""

    def build(**overrides: Any) -> FilterConfig:
        values: dict[str, Any] = {"id": "test-filter"}
        values.update(overrides)
        return FilterConfig(**values)

    return build


@pytest.fixture()
def engineering_job() -> Callable[..., NormalizedJob]:
    """Build a normalized robotics-engineering posting with concrete valid values."""

    def build(**overrides: Any) -> NormalizedJob:
        values: dict[str, Any] = {
            "company_id": "nvidia",
            "source_id": "nvidia-greenhouse",
            "source_job_id": "12345",
            "canonical_job_key": "greenhouse:nvidia:12345",
            "dedupe_key": "url:nvidia:https://boards.greenhouse.io/nvidia/jobs/12345",
            "title": "Robotics Software Engineer",
            "locations": ["Santa Clara, CA"],
            "department": "Engineering",
            "employment_type": "Full-time",
            "workplace_type": "hybrid",
            "description_text": "Build robot arms.",
            "posting_url": "https://boards.greenhouse.io/nvidia/jobs/12345",
            "apply_url": "https://boards.greenhouse.io/nvidia/jobs/12345",
            "first_seen_at": datetime(2026, 9, 19, 12, 0, tzinfo=UTC),
        }
        values.update(overrides)
        return NormalizedJob(**values)

    return build
