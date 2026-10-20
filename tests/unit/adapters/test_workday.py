"""Offline tests for the Workday CXS adapter.

The CXS endpoint is a JSON ``POST`` that pages by ``offset``/``limit`` (at most 20) and declares a
``total`` for the search, capped at 2,000. A source is scoped through ``applied_facets`` (or
``search_text``) in its parameters. These tests pin the mapping from a captured NVIDIA response, the
request body, the scoped walk, the cap refusal, and the one re-walk a shifting board earns.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from job_alert.adapters.base import AdapterError, validate_collection
from job_alert.adapters.workday import TOTAL_CAP, WorkdayAdapter
from job_alert.fetch import HttpFetcher
from job_alert.models import SourceClass, SourceConfig, SupportStatus
from tests.helpers import assert_normalized_job, assert_repeat_is_stable

FIXTURE = Path("tests/fixtures/workday/jobs.json")
ENDPOINT = "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs"
INTERN = {"workerSubType": ["0c40f6bd1d8f10adf6dae42e46d44a17"]}


def captured() -> list[dict[str, Any]]:
    return list(json.loads(FIXTURE.read_text(encoding="utf-8"))["jobPostings"])


def posting(req: str, **overrides: object) -> dict[str, Any]:
    record: dict[str, Any] = {
        "title": f"Robotics Intern {req}",
        "externalPath": f"/job/US-CA-Santa-Clara/Robotics-Intern_{req}",
        "locationsText": "US, CA, Santa Clara",
        "postedOn": "Posted Today",
        "bulletFields": [req],
    }
    record.update(overrides)
    return record


def page(*records: object, total: int | None = None) -> httpx.Response:
    return httpx.Response(
        200, json={"total": len(records) if total is None else total, "jobPostings": list(records)}
    )


def source(**parameters: object) -> SourceConfig:
    return SourceConfig(
        id="nvidia-workday",
        company_id="nvidia",
        provider="workday",
        source_class=SourceClass.PUBLIC_ATS,
        support_status=SupportStatus.SUPPORTED_UNDOCUMENTED,
        endpoint=ENDPOINT,
        interval_minutes=10,
        parameters={"applied_facets": INTERN, **parameters},
    )


def bodies(route: respx.Route) -> list[dict[str, Any]]:
    return [json.loads(call.request.content) for call in route.calls]


@respx.mock
@pytest.mark.asyncio
async def test_workday_maps_the_captured_postings() -> None:
    respx.post(ENDPOINT).mock(return_value=page(*captured()))

    async with HttpFetcher() as fetcher:
        result = validate_collection(await WorkdayAdapter().collect(source(), fetcher))

    jobs = {job.source_job_id: job for job in result.jobs}
    assert sorted(jobs) == ["JR2025948", "JR2027948"]
    taipei = jobs["JR2025948"]
    assert taipei.title == "System Software Engineer - Base OS (RDSS Intern)"
    assert taipei.canonical_job_key == "workday:nvidia/NVIDIAExternalCareerSite:JR2025948"
    assert taipei.requisition_id == "JR2025948"
    assert str(taipei.posting_url) == (
        "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
        "/job/Taiwan-Taipei/System-Software-Engineer---Base-OS--RDSS-Intern-_JR2025948"
    )
    assert taipei.apply_url == taipei.posting_url
    assert taipei.locations == ["Taiwan, Taipei"]
    # "2 Locations" is a count the list view prints, not a place; the path names the primary one.
    assert jobs["JR2027948"].locations == ["UK Bristol"]
    for job in result.jobs:
        assert_normalized_job(job)


@respx.mock
@pytest.mark.asyncio
async def test_workday_posts_the_scope_and_walks_until_the_first_total() -> None:
    # Workday reports the total on the first page only; later pages say 0.
    route = respx.post(ENDPOINT).mock(
        side_effect=[page(posting("A"), posting("B"), total=3), page(posting("C"), total=0)]
    )

    async with HttpFetcher() as fetcher:
        result = validate_collection(
            await WorkdayAdapter().collect(source(page_size=2), fetcher)
        )

    assert [job.source_job_id for job in result.jobs] == ["A", "B", "C"]
    assert result.declared_total == 3
    assert bodies(route) == [
        {"appliedFacets": INTERN, "limit": 2, "offset": 0, "searchText": ""},
        {"appliedFacets": INTERN, "limit": 2, "offset": 2, "searchText": ""},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_workday_refuses_a_search_the_total_cap_truncates() -> None:
    route = respx.post(ENDPOINT).mock(return_value=page(posting("A"), total=TOTAL_CAP))

    async with HttpFetcher() as fetcher:
        result = await WorkdayAdapter().collect(source(), fetcher)

    assert result.complete is False
    assert route.call_count == 1
    assert any("cap" in warning for warning in result.warnings)


@respx.mock
@pytest.mark.asyncio
async def test_workday_requires_an_explicit_scope() -> None:
    unscoped = source().model_copy(update={"parameters": {}})

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="scope"):
            await WorkdayAdapter().collect(unscoped, fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_workday_reads_a_whole_board_an_operator_declared_small() -> None:
    whole = source().model_copy(update={"parameters": {"whole_board": True}})
    route = respx.post(ENDPOINT).mock(return_value=page(posting("A"), total=1))

    async with HttpFetcher() as fetcher:
        result = validate_collection(await WorkdayAdapter().collect(whole, fetcher))

    assert [job.source_job_id for job in result.jobs] == ["A"]
    assert bodies(route)[0]["appliedFacets"] == {}


@respx.mock
@pytest.mark.asyncio
async def test_workday_whole_board_still_refuses_the_cap() -> None:
    whole = source().model_copy(update={"parameters": {"whole_board": True}})
    respx.post(ENDPOINT).mock(return_value=page(posting("A"), total=TOTAL_CAP))

    async with HttpFetcher() as fetcher:
        result = await WorkdayAdapter().collect(whole, fetcher)

    assert result.complete is False


@respx.mock
@pytest.mark.asyncio
async def test_workday_rewalks_once_when_the_board_shifts() -> None:
    route = respx.post(ENDPOINT).mock(
        side_effect=[
            page(posting("C"), posting("B"), total=3),
            page(posting("B"), total=0),
            page(posting("D"), posting("C"), total=3),
            page(posting("B"), total=0),
        ]
    )

    async with HttpFetcher() as fetcher:
        result = validate_collection(
            await WorkdayAdapter().collect(source(page_size=2), fetcher)
        )

    assert sorted(job.source_job_id for job in result.jobs) == ["B", "C", "D"]
    assert route.call_count == 4


@respx.mock
@pytest.mark.asyncio
async def test_workday_marks_a_posting_without_a_usable_path_incomplete() -> None:
    respx.post(ENDPOINT).mock(
        return_value=page(posting("A"), posting("B", externalPath="https://evil.example/x"))
    )

    async with HttpFetcher() as fetcher:
        result = await WorkdayAdapter().collect(source(), fetcher)

    assert result.complete is False
    assert [job.source_job_id for job in result.jobs] == ["A"]


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("body", [{"jobPostings": []}, {"total": 1}, ["x"], {"total": "1"}])
async def test_workday_rejects_an_envelope_it_cannot_trust(body: object) -> None:
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=body))

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="workday"):
            await WorkdayAdapter().collect(source(), fetcher)


def test_workday_rejects_an_endpoint_that_is_not_a_cxs_jobs_url() -> None:
    from job_alert.adapters.workday import site_of

    with pytest.raises(AdapterError, match="cxs"):
        site_of("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite")


@respx.mock
@pytest.mark.asyncio
async def test_workday_repeats_as_stable_normalized_jobs() -> None:
    respx.post(ENDPOINT).mock(return_value=page(*captured()))

    async with HttpFetcher() as fetcher:
        first = await WorkdayAdapter().collect(source(), fetcher)
        second = await WorkdayAdapter().collect(source(), fetcher)

    assert_repeat_is_stable(first, second)


def facet_page(total: int, eng: int = 2, ops: int = 1) -> httpx.Response:
    families = {
        "facetParameter": "jobFamilyGroup",
        "values": [{"id": "eng", "count": eng}, {"id": "ops", "count": ops}],
    }
    return httpx.Response(200, json={"total": total, "jobPostings": [], "facets": [families]})


def partitioned() -> SourceConfig:
    return source().model_copy(
        update={"parameters": {"whole_board": True, "partition_facet": "jobFamilyGroup"}}
    )


@respx.mock
@pytest.mark.asyncio
async def test_workday_reads_a_capped_board_one_partition_at_a_time() -> None:
    # The unfiltered total is capped; each family is a filtered search with its own total.
    route = respx.post(ENDPOINT).mock(
        side_effect=[
            facet_page(TOTAL_CAP, eng=1500, ops=900),
            page(posting("A"), posting("B"), total=2),
            page(posting("C"), total=1),
        ]
    )

    async with HttpFetcher() as fetcher:
        result = validate_collection(await WorkdayAdapter().collect(partitioned(), fetcher))

    assert result.complete is True
    assert sorted(job.source_job_id for job in result.jobs) == ["A", "B", "C"]
    assert [body["appliedFacets"] for body in bodies(route)] == [
        {},
        {"jobFamilyGroup": ["eng"]},
        {"jobFamilyGroup": ["ops"]},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_workday_partitions_that_miss_part_of_the_board_are_incomplete() -> None:
    # The board declares 5 postings, but its families only account for 3 of them.
    respx.post(ENDPOINT).mock(
        side_effect=[
            facet_page(5),
            page(posting("A"), posting("B"), total=2),
            page(posting("C"), total=1),
        ]
    )

    async with HttpFetcher() as fetcher:
        result = await WorkdayAdapter().collect(partitioned(), fetcher)

    assert result.complete is False
    assert any("carry no jobFamilyGroup" in warning for warning in result.warnings)


@respx.mock
@pytest.mark.asyncio
async def test_workday_one_short_partition_makes_the_board_incomplete() -> None:
    respx.post(ENDPOINT).mock(
        side_effect=[
            facet_page(3),
            page(posting("A"), total=2),  # declares 2, delivers 1
            page(total=0),
            page(posting("A"), total=2),  # the re-walk a short walk earns
            page(total=0),
            page(posting("C"), total=1),
        ]
    )

    async with HttpFetcher() as fetcher:
        result = await WorkdayAdapter().collect(partitioned(), fetcher)

    assert result.complete is False
