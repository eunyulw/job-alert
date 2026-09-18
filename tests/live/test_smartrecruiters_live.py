"""Opt-in live contract probe for the SmartRecruiters adapter.

Run with::

    uv run pytest tests/live/test_smartrecruiters_live.py -m live -v

The probe collects the first enabled, supported SmartRecruiters source from ``config/sources.yaml``
twice and requires both collections to be complete and to agree. Until the Task 11 audit enables
such a source it skips deliberately instead of failing; a board that is genuinely empty is still a
valid observation, because the audit is what proves the board is worth polling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from job_alert.adapters.base import validate_collection
from job_alert.adapters.smartrecruiters import SmartRecruitersAdapter
from job_alert.config import load_app_config
from job_alert.fetch import HttpFetcher
from job_alert.models import SourceConfig, SupportStatus
from tests.helpers import assert_normalized_job, assert_repeat_is_stable

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def verified_smartrecruiters_source() -> SourceConfig:
    """The first audited SmartRecruiters source, or a deliberate skip when none exists yet."""
    for source in load_app_config(CONFIG_DIR).sources:
        if (
            source.provider == "smartrecruiters"
            and source.enabled
            and source.support_status is SupportStatus.SUPPORTED
        ):
            return source
    pytest.skip(
        "config/sources.yaml has no enabled SUPPORTED smartrecruiters source: the Task 11 source "
        "audit has not enabled one yet"
    )


@pytest.mark.live
@pytest.mark.asyncio
async def test_smartrecruiters_live_board_is_complete_and_stable() -> None:
    source = verified_smartrecruiters_source()
    adapter = SmartRecruitersAdapter()

    async with HttpFetcher() as fetcher:
        first = validate_collection(await adapter.collect(source, fetcher))
        second = validate_collection(await adapter.collect(source, fetcher))

    # SmartRecruiters publishes the size of its own board, so completeness is a count the adapter
    # can check: the unique posting ids it read must equal the ``totalFound`` the board declared.
    assert first.declared_total is not None
    assert first.warnings == []
    assert first.page_count >= 1
    assert_repeat_is_stable(first, second)
    for job in first.jobs:
        assert_normalized_job(job)
