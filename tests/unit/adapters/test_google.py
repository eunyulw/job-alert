"""Offline tests for the Google Careers adapter, which reads the results page's ``ds:1`` block."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
import respx

from job_alert.adapters.base import AdapterError, validate_collection
from job_alert.adapters.google import GoogleAdapter
from job_alert.fetch import HttpFetcher
from job_alert.models import SourceClass, SourceConfig, SupportStatus
from tests.helpers import assert_normalized_job, assert_repeat_is_stable

FIXTURE = Path("tests/fixtures/google/results.html")
RESULTS = "https://www.google.com/about/careers/applications/jobs/results/"
ENDPOINT = f"{RESULTS}?employment_type=INTERN"


def captured() -> list[list[Any]]:
    text = FIXTURE.read_text(encoding="utf-8")
    match = re.search(r"data:(.*?), sideChannel", text, re.S)
    assert match is not None
    return list(json.loads(match.group(1))[0])


def job(job_id: str, title: str = "Robotics Intern", **positions: object) -> list[Any]:
    record = deepcopy(captured()[1])
    record[0] = job_id
    record[1] = title
    for index, value in positions.items():
        record[int(index.removeprefix("i"))] = value
    return record


def page(*records: object, total: int | None = None) -> httpx.Response:
    data = [list(records) or None, None, len(records) if total is None else total, 20]
    body = (
        "<html><script>AF_initDataCallback({key: 'ds:0', hash: '1', data:[[]], sideChannel: {}});"
        f"AF_initDataCallback({{key: 'ds:1', hash: '2', data:{json.dumps(data)},"
        " sideChannel: {}});</script></html>"
    )
    return httpx.Response(200, headers={"Content-Type": "text/html"}, text=body)


def source() -> SourceConfig:
    return SourceConfig(
        id="alphabet-google-google",
        company_id="alphabet-google",
        provider="google",
        source_class=SourceClass.HTML,
        support_status=SupportStatus.SUPPORTED_UNDOCUMENTED,
        endpoint=ENDPOINT,
        interval_minutes=30,
    )


@respx.mock
@pytest.mark.asyncio
async def test_google_maps_the_captured_page() -> None:
    respx.get(url__startswith=RESULTS).mock(
        return_value=httpx.Response(
            200, headers={"Content-Type": "text/html"}, text=FIXTURE.read_text(encoding="utf-8")
        )
    )

    async with HttpFetcher() as fetcher:
        collection = validate_collection(await GoogleAdapter().collect(source(), fetcher))

    jobs = {item.source_job_id: item for item in collection.jobs}
    assert sorted(jobs) == ["115691171337904838", "93402210922046150"]
    mba = jobs["93402210922046150"]
    assert mba.title == "Finance MBA Intern, 2027"
    assert mba.canonical_job_key == "google:google:93402210922046150"
    assert str(mba.posting_url) == f"{RESULTS}93402210922046150-finance-mba-intern-2027"
    assert mba.locations == ["Dublin, Ireland"]
    assert mba.source_published_at == datetime.fromtimestamp(1790946563, tz=UTC)
    assert mba.description_text and "<" not in mba.description_text
    assert mba.raw_metadata["company"] == "Google"
    for item in collection.jobs:
        assert_normalized_job(item)


@respx.mock
@pytest.mark.asyncio
async def test_google_pages_from_one_and_keeps_the_scope() -> None:
    route = respx.get(url__startswith=RESULTS).mock(
        side_effect=[page(job("11"), job("12"), total=3), page(job("13"), total=3)]
    )

    async with HttpFetcher() as fetcher:
        collection = validate_collection(await GoogleAdapter().collect(source(), fetcher))

    assert [item.source_job_id for item in collection.jobs] == ["11", "12", "13"]
    queries = [dict(parse_qsl(urlsplit(str(call.request.url)).query)) for call in route.calls]
    assert queries == [
        {"employment_type": "INTERN", "page": "1"},
        {"employment_type": "INTERN", "page": "2"},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_google_marks_a_record_of_the_wrong_shape_incomplete() -> None:
    respx.get(url__startswith=RESULTS).mock(
        return_value=page(job("11"), job("not-digits"), ["too", "short"], total=3)
    )

    async with HttpFetcher() as fetcher:
        collection = await GoogleAdapter().collect(source(), fetcher)

    assert collection.complete is False
    assert [item.source_job_id for item in collection.jobs] == ["11"]


@respx.mock
@pytest.mark.asyncio
async def test_google_rejects_a_page_without_the_results_block() -> None:
    respx.get(url__startswith=RESULTS).mock(
        return_value=httpx.Response(200, headers={"Content-Type": "text/html"}, text="<html/>")
    )

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="google"):
            await GoogleAdapter().collect(source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_google_repeats_as_stable_normalized_jobs() -> None:
    respx.get(url__startswith=RESULTS).mock(return_value=page(*captured()))

    async with HttpFetcher() as fetcher:
        first = await GoogleAdapter().collect(source(), fetcher)
        second = await GoogleAdapter().collect(source(), fetcher)

    assert_repeat_is_stable(first, second)
