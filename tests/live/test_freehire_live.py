"""Opt-in live contract probe for the Freehire company fallback.

Run with::

    uv run pytest tests/live/test_freehire_live.py -m live -v

CMR Surgical is the smallest configured feed, so two adjacent collections verify the public API
contract without walking a large company catalogue.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from job_alert.adapters.base import validate_collection
from job_alert.adapters.freehire import FreehireAdapter
from job_alert.config import load_app_config
from job_alert.fetch import HttpFetcher
from job_alert.models import SourceConfig
from tests.helpers import assert_normalized_job, assert_repeat_is_stable

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
SOURCE_ID = "cmr-surgical-freehire"


def configured_source() -> SourceConfig:
    source = next(
        (item for item in load_app_config(CONFIG_DIR).sources if item.id == SOURCE_ID),
        None,
    )
    if source is None or not source.enabled:
        pytest.skip(f"{SOURCE_ID} is not configured and enabled")
    return source


@pytest.mark.live
@pytest.mark.asyncio
async def test_freehire_live_company_feed_is_complete_and_stable() -> None:
    source = configured_source()
    adapter = FreehireAdapter()

    async with HttpFetcher() as fetcher:
        first = validate_collection(await adapter.collect(source, fetcher))
        second = validate_collection(await adapter.collect(source, fetcher))

    assert first.declared_total == len(first.jobs)
    assert first.warnings == []
    assert_repeat_is_stable(first, second)
    for job in first.jobs:
        assert_normalized_job(job)
