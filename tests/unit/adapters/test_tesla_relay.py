"""Offline tests for the relayed Tesla feed.

The ``extension/tesla-relay`` Chrome extension commits Tesla's internships to ``tesla.json`` on the
``tesla-feed`` branch. These tests pin the US scoping, Tesla's official ids and links, the
staleness gate that turns a stopped relay into a failed read, and the shapes that fail closed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from job_alert.adapters.base import AdapterError, validate_collection
from job_alert.adapters.tesla_relay import TeslaRelayAdapter
from job_alert.fetch import HttpFetcher
from job_alert.models import SourceClass, SourceConfig, SupportStatus
from tests.helpers import assert_normalized_job, assert_repeat_is_stable

FEED = "https://raw.githubusercontent.com/eunyulw/job-alert/tesla-feed/tesla.json"
NOW = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)


def source(**parameters: object) -> SourceConfig:
    return SourceConfig(
        id="tesla-relay",
        company_id="tesla",
        provider="tesla-relay",
        source_class=SourceClass.COMPANY_JSON,
        support_status=SupportStatus.SUPPORTED_UNDOCUMENTED,
        endpoint=FEED,
        interval_minutes=15,
        tenant="tesla",
        parameters=parameters,
    )


def internship(job_id: str, title: str, location: str | None, department: str | None = None):
    return {"id": job_id, "title": title, "location": location, "department": department}


def feed(*internships: object, generated_at: datetime = NOW - timedelta(minutes=8)) -> Any:
    return {
        "schema": 1,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "board_total": 8244,
        "internships": list(internships),
    }


BOARD = feed(
    internship(
        "287407",
        "Internship, Electrical Engineer, Embedded Systems & Test Engineering, Supply Chain"
        " (Winter/Spring 2027)",
        "Palo Alto, California",
        "Supply Chain",
    ),
    internship(
        "285664",
        "Internship, Process Engineer, Wet Etch, Terafab (Winter/Spring 2027)  ",
        "Austin, Texas",
    ),
    internship(
        "285872",
        "Stamping Process Engineering Internship (m/f/d) - Gigafactory Berlin-Brandenburg",
        "Grünheide (mark), Brandenburg",
    ),
    internship("281000", "Internship, Software Engineer (Summer 2027)", "Shanghai, Shanghai"),
    internship("281001", "Internship, Vehicle Engineer (Summer 2027)", "Berlin, Germany"),
    internship("281002", "Internship, Unplaced (Summer 2027)", None),
)


async def collect(document: Any, **parameters: object):
    respx.get(FEED).mock(return_value=httpx.Response(200, json=document))
    async with HttpFetcher() as fetcher:
        return await TeslaRelayAdapter(now=lambda: NOW).collect(source(**parameters), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_keeps_only_us_internships_with_official_ids_and_links() -> None:
    result = validate_collection(await collect(BOARD))

    assert [job.source_job_id for job in result.jobs] == ["287407", "285664"]
    first, second = result.jobs
    assert first.requisition_id == "287407"
    assert first.canonical_job_key == "tesla-relay:tesla:287407"
    assert str(first.posting_url) == (
        "https://www.tesla.com/careers/search/job/internship-electrical-engineer-embedded-systems"
        "-test-engineering-supply-chain-winter-spring-2027-287407"
    )
    assert first.locations == ["Palo Alto, California"]
    assert (first.department, first.employment_type) == ("Supply Chain", "intern")
    assert second.title == "Internship, Process Engineer, Wet Etch, Terafab (Winter/Spring 2027)"
    assert result.declared_total == 2 and result.warnings == []
    for job in result.jobs:
        assert_normalized_job(job)


@respx.mock
@pytest.mark.asyncio
async def test_repeated_reads_are_stable() -> None:
    first = await collect(BOARD)
    second = await collect(BOARD)

    assert_repeat_is_stable(first, second)


@respx.mock
@pytest.mark.asyncio
async def test_a_stale_feed_fails_instead_of_passing_for_the_current_board() -> None:
    old = feed(*BOARD["internships"], generated_at=NOW - timedelta(hours=25))

    with pytest.raises(AdapterError, match="Chrome relay has not run since"):
        await collect(old)


@respx.mock
@pytest.mark.asyncio
async def test_max_age_is_configurable() -> None:
    recent = feed(*BOARD["internships"], generated_at=NOW - timedelta(hours=3))

    with pytest.raises(AdapterError, match="3 hours ago"):
        await collect(recent, max_age_hours=2)


@respx.mock
@pytest.mark.asyncio
async def test_a_malformed_internship_makes_the_read_incomplete() -> None:
    result = await collect(
        feed(internship("287407", "Internship, A", "Austin, Texas"), {"id": "x y", "title": "B"})
    )

    assert result.complete is False
    assert result.warnings == ["skipped malformed internship at index 1: missing a usable id"]


@respx.mock
@pytest.mark.asyncio
async def test_a_repeated_id_makes_the_read_incomplete() -> None:
    result = await collect(
        feed(
            internship("287407", "Internship, A", "Austin, Texas"),
            internship("287407", "Internship, A", "Austin, Texas"),
        )
    )

    assert result.complete is False


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"schema": 2, "generated_at": "2026-10-21T17:55:00Z", "internships": [{}]}, "schema 1"),
        (["x"], "schema 1"),
        ({"schema": 1, "generated_at": "2026-10-21T17:55:00", "internships": [{}]}, "timezone"),
        ({"schema": 1, "generated_at": "soon", "internships": [{}]}, "timezone"),
        (
            {"schema": 1, "generated_at": "2026-10-21T17:55:00Z", "internships": []},
            "no internships",
        ),
    ],
)
async def test_rejects_a_feed_it_cannot_trust(document: object, message: str) -> None:
    with pytest.raises(AdapterError, match=message):
        await collect(document)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 169, "24", True])
async def test_rejects_an_unusable_max_age(value: object) -> None:
    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="max_age_hours"):
            await TeslaRelayAdapter().collect(source(max_age_hours=value), fetcher)
