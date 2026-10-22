"""Offline tests for the Eightfold v2 jobs adapter (``/api/apply/v2/jobs``)."""

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

from job_alert.adapters.base import AdapterError, validate_collection
from job_alert.adapters.eightfold import EightfoldAdapter
from job_alert.fetch import HttpFetcher
from job_alert.models import SourceClass, SourceConfig, SupportStatus
from tests.helpers import assert_normalized_job, assert_repeat_is_stable

FIXTURE = Path("tests/fixtures/eightfold/jobs.json")
API = "https://explore.jobs.netflix.net/api/apply/v2/jobs"
ENDPOINT = f"{API}?domain=netflix.com&query=intern"


def captured() -> list[dict[str, Any]]:
    return list(json.loads(FIXTURE.read_text(encoding="utf-8"))["positions"])


def position(job_id: int, **overrides: object) -> dict[str, Any]:
    record = deepcopy(captured()[0])
    record.update(
        id=job_id,
        name=f"Robotics Intern {job_id}",
        canonicalPositionUrl=f"https://explore.jobs.netflix.net/careers/job/{job_id}",
    )
    record.update(overrides)
    return record


def page(*records: object, count: int | None = None) -> httpx.Response:
    return httpx.Response(
        200, json={"positions": list(records), "count": len(records) if count is None else count}
    )


def source() -> SourceConfig:
    return SourceConfig(
        id="netflix-eightfold",
        company_id="netflix",
        provider="eightfold",
        source_class=SourceClass.COMPANY_JSON,
        support_status=SupportStatus.SUPPORTED_UNDOCUMENTED,
        endpoint=ENDPOINT,
        interval_minutes=15,
    )


@respx.mock
@pytest.mark.asyncio
async def test_eightfold_maps_the_captured_positions() -> None:
    respx.get(url__startswith=API).mock(return_value=page(*captured()))

    async with HttpFetcher() as fetcher:
        collection = validate_collection(await EightfoldAdapter().collect(source(), fetcher))

    jobs = {job.source_job_id: job for job in collection.jobs}
    assert sorted(jobs) == ["790317300617", "790317917022"]
    ml = jobs["790317917022"]
    assert ml.canonical_job_key == "eightfold:netflix.com:790317917022"
    assert ml.requisition_id == "JR42220"
    assert str(ml.posting_url) == "https://explore.jobs.netflix.net/careers/job/790317917022"
    assert ml.locations == ["Los Gatos,California,United States of America"]
    assert ml.department == "Engineering"
    assert ml.source_published_at == datetime.fromtimestamp(1787097600, tz=UTC)
    assert len(jobs["790317300617"].locations) == 3
    for job in collection.jobs:
        assert_normalized_job(job)


@respx.mock
@pytest.mark.asyncio
async def test_eightfold_pages_by_start_and_keeps_the_scope() -> None:
    route = respx.get(url__startswith=API).mock(
        side_effect=[page(position(1), position(2), count=3), page(position(3), count=3)]
    )

    async with HttpFetcher() as fetcher:
        collection = validate_collection(await EightfoldAdapter().collect(source(), fetcher))

    assert [job.source_job_id for job in collection.jobs] == ["1", "2", "3"]
    queries = [dict(parse_qsl(urlsplit(str(call.request.url)).query)) for call in route.calls]
    assert queries == [
        {"domain": "netflix.com", "query": "intern", "start": "0", "num": "10"},
        {"domain": "netflix.com", "query": "intern", "start": "2", "num": "10"},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_eightfold_builds_the_link_when_the_canonical_one_points_elsewhere() -> None:
    respx.get(url__startswith=API).mock(
        return_value=page(position(7, canonicalPositionUrl="https://evil.example/careers/job/7"))
    )

    async with HttpFetcher() as fetcher:
        collection = validate_collection(await EightfoldAdapter().collect(source(), fetcher))

    assert str(collection.jobs[0].posting_url) == "https://explore.jobs.netflix.net/careers/job/7"


@respx.mock
@pytest.mark.asyncio
async def test_eightfold_requires_a_domain() -> None:
    unscoped = source().model_copy(update={"endpoint": f"{API}?query=intern"})

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="domain"):
            await EightfoldAdapter().collect(unscoped, fetcher)


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"positions": []}, {"count": 1}, {"message": "Not authorized"}])
async def test_eightfold_rejects_an_envelope_it_cannot_trust(body: object) -> None:
    respx.get(url__startswith=API).mock(return_value=httpx.Response(200, json=body))

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="eightfold"):
            await EightfoldAdapter().collect(source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_eightfold_repeats_as_stable_normalized_jobs() -> None:
    respx.get(url__startswith=API).mock(return_value=page(*captured()))

    async with HttpFetcher() as fetcher:
        first = await EightfoldAdapter().collect(source(), fetcher)
        second = await EightfoldAdapter().collect(source(), fetcher)

    assert_repeat_is_stable(first, second)


# The pcsx search (``/api/pcsx/search``), which tenants that refuse the v2 API still answer.
PCSX = "https://lockheedmartin.eightfold.ai/api/pcsx/search"
PCSX_ENDPOINT = (
    f"{PCSX}?domain=lockheedmartin.com&query=intern&location=United%20States&sort_by=timestamp"
)


def pcsx_position(job_id: int, **overrides: object) -> dict[str, Any]:
    # Shape captured from lockheedmartin.eightfold.ai on 2026-10-22.
    record: dict[str, Any] = {
        "id": job_id,
        "displayJobId": "19602",
        "name": "Systems Engineer (Modeling & Simulation) - Intern",
        "locations": ["Camden, NJ"],
        "standardizedLocations": ["Camden, NJ, US"],
        "postedTs": 1792614081,
        "department": "RMS Core",
        "creationTs": 1791907092,
        "workLocationOption": "onsite",
        "atsJobId": "19602",
        "positionUrl": f"/careers/job/{job_id}",
    }
    record.update(overrides)
    return record


def pcsx_page(*records: object, count: int | None = None) -> httpx.Response:
    data = {"positions": list(records), "count": len(records) if count is None else count}
    return httpx.Response(200, json={"status": 200, "error": None, "data": data})


def pcsx_source() -> SourceConfig:
    return source().model_copy(
        update={
            "id": "lockheed-martin-eightfold",
            "company_id": "lockheed-martin",
            "endpoint": PCSX_ENDPOINT,
        }
    )


@respx.mock
@pytest.mark.asyncio
async def test_eightfold_pcsx_maps_a_position() -> None:
    respx.get(url__startswith=PCSX).mock(return_value=pcsx_page(pcsx_position(996477362267)))

    async with HttpFetcher() as fetcher:
        collection = validate_collection(await EightfoldAdapter().collect(pcsx_source(), fetcher))

    job = collection.jobs[0]
    assert job.canonical_job_key == "eightfold:lockheedmartin.com:996477362267"
    assert job.title == "Systems Engineer (Modeling & Simulation) - Intern"
    assert job.locations == ["Camden, NJ, US"]
    assert (job.requisition_id, job.department, job.workplace_type) == (
        "19602",
        "RMS Core",
        "onsite",
    )
    assert str(job.posting_url) == "https://lockheedmartin.eightfold.ai/careers/job/996477362267"
    assert job.source_published_at == datetime.fromtimestamp(1792614081, tz=UTC)
    assert_normalized_job(job)


@respx.mock
@pytest.mark.asyncio
async def test_eightfold_pcsx_pages_by_start_and_keeps_the_scope() -> None:
    route = respx.get(url__startswith=PCSX).mock(
        side_effect=[
            pcsx_page(pcsx_position(1), pcsx_position(2), count=3),
            pcsx_page(pcsx_position(3), count=3),
        ]
    )

    async with HttpFetcher() as fetcher:
        collection = validate_collection(await EightfoldAdapter().collect(pcsx_source(), fetcher))

    assert [job.source_job_id for job in collection.jobs] == ["1", "2", "3"]
    queries = [dict(parse_qsl(urlsplit(str(call.request.url)).query)) for call in route.calls]
    assert [query["start"] for query in queries] == ["0", "2"]
    assert all(query["sort_by"] == "timestamp" for query in queries)


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"status": 403, "error": "Not authorized", "data": None},
        {"positions": [], "count": 0},
        {"status": 200, "data": {"count": 1}},
    ],
)
async def test_eightfold_pcsx_rejects_an_envelope_it_cannot_trust(body: object) -> None:
    respx.get(url__startswith=PCSX).mock(return_value=httpx.Response(200, json=body))

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="eightfold"):
            await EightfoldAdapter().collect(pcsx_source(), fetcher)
