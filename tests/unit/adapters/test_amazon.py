"""Offline tests for the amazon.jobs search adapter.

The endpoint pages by ``offset``/``result_limit`` and declares a ``hits`` total for the query, which
it caps at 10,000. These tests pin the field mapping from a captured response, the scoped walk, the
cap refusal, and the one re-walk a newest-first board earns when it shifts mid-walk.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
import respx

from job_alert.adapters.amazon import SEARCH_CAP, AmazonAdapter
from job_alert.adapters.base import AdapterError, validate_collection
from job_alert.fetch import HttpFetcher
from job_alert.models import SourceClass, SourceConfig, SupportStatus
from tests.helpers import assert_normalized_job, assert_repeat_is_stable

FIXTURE = Path("tests/fixtures/amazon/search.json")
ENDPOINT = "https://www.amazon.jobs/en/search.json"
SCOPED = f"{ENDPOINT}?base_query=intern&sort=recent"
NOW = datetime(2026, 10, 14, tzinfo=UTC)


def captured() -> list[dict[str, Any]]:
    return list(json.loads(FIXTURE.read_text(encoding="utf-8"))["jobs"])


def record(job_id: str, **overrides: object) -> dict[str, Any]:
    """A posting with the captured shape, renumbered."""
    posting = deepcopy(captured()[0])
    posting.update(
        id_icims=job_id,
        id=f"uuid-{job_id}",
        title=f"Robotics Intern {job_id}",
        job_path=f"/en/jobs/{job_id}/robotics-intern",
        url_next_step=f"https://account.amazon.jobs/jobs/{job_id}/apply",
    )
    posting.update(overrides)
    return posting


def page(*records: object, hits: int | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "error": None,
            "hits": len(records) if hits is None else hits,
            "facets": {},
            "content": {},
            "jobs": list(records),
        },
    )


def source(**overrides: object) -> SourceConfig:
    values: dict[str, object] = {
        "id": "amazon-amazon",
        "company_id": "amazon",
        "provider": "amazon",
        "source_class": SourceClass.COMPANY_JSON,
        "support_status": SupportStatus.SUPPORTED_UNDOCUMENTED,
        "endpoint": SCOPED,
        "interval_minutes": 15,
        "tenant": "amazon",
    }
    values.update(overrides)
    return SourceConfig.model_validate(values)


def adapter() -> AmazonAdapter:
    return AmazonAdapter(now=lambda: NOW)


def params(request: httpx.Request) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(str(request.url)).query))


@respx.mock
@pytest.mark.asyncio
async def test_amazon_maps_the_captured_postings() -> None:
    respx.get(url__startswith=ENDPOINT).mock(return_value=page(*captured()))

    async with HttpFetcher() as fetcher:
        result = validate_collection(await adapter().collect(source(), fetcher))

    assert result.complete is True
    assert result.declared_total == 2
    jobs = {job.source_job_id: job for job in result.jobs}
    assert sorted(jobs) == ["10578090", "10578321"]
    hardware = jobs["10578090"]
    assert hardware.title == "Hardware Development Engineer Intern,  Summer 2027"
    assert hardware.canonical_job_key == "amazon:amazon:10578090"
    assert hardware.requisition_id == "10578090"
    assert str(hardware.posting_url) == (
        "https://www.amazon.jobs/en/jobs/10578090/hardware-development-engineer-intern-summer-2027"
    )
    assert str(hardware.apply_url) == "https://account.amazon.jobs/jobs/10578090/apply"
    assert hardware.locations == [
        "Sunnyvale, California, USA",
        "North Reading, Massachusetts, USA",
        "Hawthorne, California, USA",
    ]
    assert hardware.workplace_type == "onsite"
    assert hardware.department == "Hardware Development"
    assert hardware.employment_type == "full-time"
    assert hardware.source_published_at == datetime(2026, 10, 13, tzinfo=UTC)
    assert hardware.description_text and "<" not in hardware.description_text
    for job in result.jobs:
        assert_normalized_job(job)


@respx.mock
@pytest.mark.asyncio
async def test_amazon_walks_the_scoped_query_by_offset_until_the_declared_hits() -> None:
    route = respx.get(url__startswith=ENDPOINT).mock(
        side_effect=[
            page(record("3"), record("2"), hits=3),
            page(record("1"), hits=3),
        ]
    )

    async with HttpFetcher() as fetcher:
        result = validate_collection(
            await adapter().collect(source(parameters={"page_size": 2}), fetcher)
        )

    assert [job.source_job_id for job in result.jobs] == ["3", "2", "1"]
    assert result.page_count == 2
    assert result.declared_total == 3
    first, second = (params(call.request) for call in route.calls)
    # The scope is the operator's; paging belongs to the adapter.
    assert first == {"base_query": "intern", "sort": "recent", "offset": "0", "result_limit": "2"}
    assert second["offset"] == "2"


@respx.mock
@pytest.mark.asyncio
async def test_amazon_refuses_a_query_the_search_cap_truncates() -> None:
    route = respx.get(url__startswith=ENDPOINT).mock(
        return_value=page(record("1"), hits=SEARCH_CAP)
    )

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source(), fetcher)

    assert result.complete is False
    assert route.call_count == 1  # no point walking a board that cannot be read whole
    assert any("search cap" in warning for warning in result.warnings)


@respx.mock
@pytest.mark.asyncio
async def test_amazon_rewalks_once_when_a_posting_is_published_mid_walk() -> None:
    route = respx.get(url__startswith=ENDPOINT).mock(
        side_effect=[
            page(record("3"), record("2"), hits=3),
            page(record("2"), record("1"), hits=4),
            page(record("4"), record("3"), hits=4),
            page(record("2"), record("1"), hits=4),
        ]
    )

    async with HttpFetcher() as fetcher:
        result = validate_collection(
            await adapter().collect(source(parameters={"page_size": 2}), fetcher)
        )

    assert sorted(job.source_job_id for job in result.jobs) == ["1", "2", "3", "4"]
    assert result.warnings == []
    assert route.call_count == 4


@respx.mock
@pytest.mark.asyncio
async def test_amazon_reports_a_count_the_walk_never_reaches_as_incomplete() -> None:
    respx.get(url__startswith=ENDPOINT).mock(
        side_effect=[page(record("1"), hits=2), page(hits=2)] * 2
    )

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source(), fetcher)

    assert result.complete is False
    assert any("declared hits=2" in warning for warning in result.warnings)


@respx.mock
@pytest.mark.asyncio
async def test_amazon_marks_an_unusable_record_incomplete() -> None:
    respx.get(url__startswith=ENDPOINT).mock(
        return_value=page(record("1"), record("2", job_path="//evil.example/x"), hits=2)
    )

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source(), fetcher)

    assert result.complete is False
    assert [job.source_job_id for job in result.jobs] == ["1"]
    assert any("index 1" in warning for warning in result.warnings)


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"error": "boom", "hits": 0, "jobs": []},
        {"hits": "many", "jobs": []},
        {"hits": 1},
        ["not", "an", "envelope"],
    ],
)
async def test_amazon_rejects_an_envelope_it_cannot_trust(body: object) -> None:
    respx.get(url__startswith=ENDPOINT).mock(return_value=httpx.Response(200, json=body))

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="amazon"):
            await adapter().collect(source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_amazon_repeats_as_stable_normalized_jobs() -> None:
    respx.get(url__startswith=ENDPOINT).mock(return_value=page(*captured()))

    async with HttpFetcher() as fetcher:
        first = await adapter().collect(source(), fetcher)
        second = await adapter().collect(source(), fetcher)

    assert_repeat_is_stable(first, second)
