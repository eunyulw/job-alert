"""Opt-in live contract probe for the Ashby adapter.

Run with::

    uv run pytest tests/live/test_ashby_live.py -m live -v

The probe collects the first enabled, supported Ashby source from `config/sources.yaml` twice and
requires both collections to be complete and to agree. Until the Task 11 audit enables such a
source it skips deliberately instead of failing; a board that is genuinely empty is still a valid
observation, because the audit is what proves the board is worth polling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from job_alert.adapters.ashby import AshbyAdapter
from job_alert.adapters.base import validate_collection
from job_alert.config import load_app_config
from job_alert.fetch import HttpFetcher
from job_alert.models import SourceConfig, SupportStatus
from tests.helpers import assert_normalized_job, assert_repeat_is_stable

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def verified_ashby_source() -> SourceConfig:
    """The first audited Ashby source, or a deliberate skip when none exists yet."""
    for source in load_app_config(CONFIG_DIR).sources:
        if (
            source.provider == "ashby"
            and source.enabled
            and source.support_status is SupportStatus.SUPPORTED
        ):
            return source
    pytest.skip(
        "config/sources.yaml has no enabled SUPPORTED ashby source: the Task 11 source audit "
        "has not enabled one yet"
    )


@pytest.mark.live
@pytest.mark.asyncio
async def test_ashby_live_board_is_complete_and_stable() -> None:
    source = verified_ashby_source()
    adapter = AshbyAdapter()

    async with HttpFetcher() as fetcher:
        first = validate_collection(await adapter.collect(source, fetcher))
        second = validate_collection(await adapter.collect(source, fetcher))

    # Ashby's posting endpoint publishes no total, so completeness is the adapter's own verdict:
    # one request whose envelope carried a jobs array and whose ids were all unique.
    assert first.declared_total is None
    assert first.warnings == []
    assert first.page_count == 1
    assert_repeat_is_stable(first, second)
    for job in first.jobs:
        assert_normalized_job(job)
