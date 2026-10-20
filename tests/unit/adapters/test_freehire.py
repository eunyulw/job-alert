"""Offline tests for the Freehire company fallback adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
import respx

from job_alert.adapters.base import AdapterError, validate_collection
from job_alert.adapters.freehire import FreehireAdapter
from job_alert.fetch import HttpFetcher
from job_alert.models import SourceClass, SourceConfig, SupportStatus
from tests.helpers import assert_normalized_job

SEARCH = "https://freehire.me/api/v1/jobs/search"
ENDPOINT = f"{SEARCH}?company_slug=microsoft"


def source() -> SourceConfig:
    return SourceConfig(
        id="microsoft-freehire",
        company_id="microsoft",
        provider="freehire",
        source_class=SourceClass.FALLBACK,
        support_status=SupportStatus.FALLBACK_ONLY,
        endpoint=ENDPOINT,
        interval_minutes=30,
    )


def record() -> dict[str, object]:
    return {
        "public_slug": "software-engineering-intern-microsoft-abc123",
        "source": "eightfold",
        "external_id": "1970393557022579",
        "url": "https://apply.careers.microsoft.com/careers/job/1970393557022579",
        "title": "Software Engineering Intern",
        "company": "Microsoft",
        "company_slug": "microsoft",
        "location": "Redmond, WA",
        "description": "<p>Build systems for the internship.</p>",
        "cities": ["Redmond"],
        "countries": ["us"],
        "work_mode": "onsite",
        "posted_at": "2026-10-17T12:00:00Z",
        "enrichment": {
            "category": "software_engineering",
            "employment_type": "internship",
        },
    }


def page(*records: object, total: int | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": list(records),
            "meta": {
                "total": len(records) if total is None else total,
                "limit": 100,
                "offset": 0,
            },
        },
    )


@respx.mock
@pytest.mark.asyncio
async def test_freehire_maps_a_company_result_with_its_original_ats_link() -> None:
    route = respx.get(url__startswith=SEARCH).mock(return_value=page(record()))

    async with HttpFetcher() as fetcher:
        collection = validate_collection(await FreehireAdapter().collect(source(), fetcher))

    assert len(collection.jobs) == 1
    job = collection.jobs[0]
    assert job.source_job_id == "software-engineering-intern-microsoft-abc123"
    assert job.canonical_job_key == (
        "freehire:microsoft:software-engineering-intern-microsoft-abc123"
    )
    assert str(job.posting_url) == (
        "https://apply.careers.microsoft.com/careers/job/1970393557022579"
    )
    assert job.locations == ["Redmond, WA"]
    assert job.employment_type == "intern"
    assert job.workplace_type == "onsite"
    assert job.source_published_at == datetime(2026, 10, 17, 12, tzinfo=UTC)
    assert job.raw_metadata["freehire_source"] == "eightfold"
    assert_normalized_job(job)
    query = dict(parse_qsl(urlsplit(str(route.calls[0].request.url)).query))
    assert query == {"company_slug": "microsoft", "limit": "100", "offset": "0"}


@respx.mock
@pytest.mark.asyncio
async def test_freehire_pages_by_limit_and_offset_and_keeps_the_company_scope() -> None:
    first = record()
    second = {**record(), "public_slug": "mechanical-engineering-intern-microsoft-def456"}
    route = respx.get(url__startswith=SEARCH).mock(
        side_effect=[page(first, total=2), page(second, total=2)]
    )

    async with HttpFetcher() as fetcher:
        collection = validate_collection(await FreehireAdapter().collect(source(), fetcher))

    assert [job.source_job_id for job in collection.jobs] == [
        "software-engineering-intern-microsoft-abc123",
        "mechanical-engineering-intern-microsoft-def456",
    ]
    queries = [dict(parse_qsl(urlsplit(str(call.request.url)).query)) for call in route.calls]
    assert queries == [
        {"company_slug": "microsoft", "limit": "100", "offset": "0"},
        {"company_slug": "microsoft", "limit": "100", "offset": "1"},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_freehire_marks_a_result_for_another_company_incomplete() -> None:
    wrong = {**record(), "company_slug": "microfocus"}
    respx.get(url__startswith=SEARCH).mock(return_value=page(wrong))

    async with HttpFetcher() as fetcher:
        collection = await FreehireAdapter().collect(source(), fetcher)

    assert collection.complete is False
    assert collection.jobs == []
    assert "not 'microsoft'" in collection.warnings[0]


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"data": []},
        {"meta": {"total": 0}},
        {"data": [], "meta": {"total": -1}},
    ],
)
async def test_freehire_rejects_an_envelope_it_cannot_trust(body: object) -> None:
    respx.get(url__startswith=SEARCH).mock(return_value=httpx.Response(200, json=body))

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="freehire"):
            await FreehireAdapter().collect(source(), fetcher)
