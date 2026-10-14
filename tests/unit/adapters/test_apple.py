"""Offline tests for the jobs.apple.com adapter, which reads the search page's hydration JSON."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
import respx

from job_alert.adapters.apple import AppleAdapter
from job_alert.adapters.base import AdapterError, validate_collection
from job_alert.fetch import HttpFetcher
from job_alert.models import SourceClass, SourceConfig, SupportStatus
from tests.helpers import assert_normalized_job, assert_repeat_is_stable

FIXTURE = Path("tests/fixtures/apple/search.html")
SEARCH = "https://jobs.apple.com/en-us/search"
ENDPOINT = f"{SEARCH}?team=internships-STDNT-INTRN&sort=newest"


def captured() -> list[dict[str, Any]]:
    text = FIXTURE.read_text(encoding="utf-8")
    encoded = re.search(r'JSON\.parse\((".*?")\);', text, re.S)
    assert encoded is not None
    return list(json.loads(json.loads(encoded.group(1)))["loaderData"]["search"]["searchResults"])


def result(job_id: str, **overrides: object) -> dict[str, Any]:
    record = dict(captured()[0])
    record.update(
        id=job_id,
        positionId=job_id.split("-")[0],
        postingTitle=f"Robotics Intern {job_id}",
        transformedPostingTitle="robotics-intern",
    )
    record.update(overrides)
    return record


def page(*records: object, total: int | None = None) -> httpx.Response:
    data = {
        "loaderData": {
            "root": {"locale": "en-us"},
            "search": {
                "searchResults": list(records),
                "totalRecords": len(records) if total is None else total,
            },
        }
    }
    encoded = json.dumps(json.dumps(data))
    body = f"<html><script>window.__staticRouterHydrationData = JSON.parse({encoded});</script>"
    return httpx.Response(200, headers={"Content-Type": "text/html"}, text=body)


def source() -> SourceConfig:
    return SourceConfig(
        id="apple-apple",
        company_id="apple",
        provider="apple",
        source_class=SourceClass.HTML,
        support_status=SupportStatus.SUPPORTED_UNDOCUMENTED,
        endpoint=ENDPOINT,
        interval_minutes=30,
    )


@respx.mock
@pytest.mark.asyncio
async def test_apple_maps_the_captured_page() -> None:
    respx.get(url__startswith=SEARCH).mock(
        return_value=httpx.Response(
            200, headers={"Content-Type": "text/html"}, text=FIXTURE.read_text(encoding="utf-8")
        )
    )

    async with HttpFetcher() as fetcher:
        collection = validate_collection(await AppleAdapter().collect(source(), fetcher))

    jobs = {job.source_job_id: job for job in collection.jobs}
    assert sorted(jobs) == ["200685035-4170", "200688776-2611"]
    job = jobs["200688776-2611"]
    assert job.title == "Intern - Full Stack Engineer"
    assert job.canonical_job_key == "apple:apple:200688776-2611"
    assert job.requisition_id == "200688776"
    assert str(job.posting_url) == (
        "https://jobs.apple.com/en-us/details/200688776-2611/intern-full-stack-engineer"
    )
    assert job.locations == ["Prague, Czechia"]
    assert job.team == "Students"
    assert job.source_published_at is not None
    assert job.source_published_at.tzinfo is not None
    for item in collection.jobs:
        assert_normalized_job(item)


@respx.mock
@pytest.mark.asyncio
async def test_apple_pages_by_number_and_keeps_the_scope() -> None:
    route = respx.get(url__startswith=SEARCH).mock(
        side_effect=[page(result("1-1"), result("2-2"), total=3), page(result("3-3"), total=3)]
    )

    async with HttpFetcher() as fetcher:
        collection = validate_collection(await AppleAdapter().collect(source(), fetcher))

    assert [job.source_job_id for job in collection.jobs] == ["1-1", "2-2", "3-3"]
    queries = [dict(parse_qsl(urlsplit(str(call.request.url)).query)) for call in route.calls]
    assert queries == [
        {"team": "internships-STDNT-INTRN", "sort": "newest", "page": "1"},
        {"team": "internships-STDNT-INTRN", "sort": "newest", "page": "2"},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_apple_marks_an_unsafe_id_or_slug_incomplete() -> None:
    respx.get(url__startswith=SEARCH).mock(
        return_value=page(result("1-1"), result("2-2", transformedPostingTitle="../../x"))
    )

    async with HttpFetcher() as fetcher:
        collection = await AppleAdapter().collect(source(), fetcher)

    assert collection.complete is False
    assert [job.source_job_id for job in collection.jobs] == ["1-1"]


@respx.mock
@pytest.mark.asyncio
async def test_apple_rejects_a_page_without_hydration_data() -> None:
    respx.get(url__startswith=SEARCH).mock(
        return_value=httpx.Response(200, headers={"Content-Type": "text/html"}, text="<html/>")
    )

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="apple"):
            await AppleAdapter().collect(source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_apple_repeats_as_stable_normalized_jobs() -> None:
    respx.get(url__startswith=SEARCH).mock(return_value=page(*captured()))

    async with HttpFetcher() as fetcher:
        first = await AppleAdapter().collect(source(), fetcher)
        second = await AppleAdapter().collect(source(), fetcher)

    assert_repeat_is_stable(first, second)
