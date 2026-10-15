"""Offline tests for the small ATS adapters: Rippling, Workable, BambooHR, Pinpoint, Oracle, UKG."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
import respx

from job_alert.adapters.bamboohr import BambooHrAdapter
from job_alert.adapters.base import AdapterError
from job_alert.adapters.oracle import OracleAdapter
from job_alert.adapters.pinpoint import PinpointAdapter
from job_alert.adapters.rippling import RipplingAdapter
from job_alert.adapters.ukg import UkgAdapter
from job_alert.adapters.workable import WorkableAdapter
from job_alert.fetch import HttpFetcher
from job_alert.models import CollectionResult, SourceClass, SourceConfig, SupportStatus

RIPPLING = "https://api.rippling.com/platform/api/ats/v1/board/acme/jobs"
WORKABLE = "https://apply.workable.com/api/v1/widget/accounts/acme"
BAMBOO = "https://acme.bamboohr.com/careers/list"
PINPOINT = "https://acme.pinpointhq.com/postings.json"
ORACLE_HOST = "https://abcd.fa.us2.oraclecloud.com"
ORACLE = (
    f"{ORACLE_HOST}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
    "&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber=CX_1,keyword=intern"
)
BOARD = "8a2317d2-a61e-4259-a857-722d164a211c"
UKG = f"https://recruiting.ultipro.com/ACME1000/JobBoard/{BOARD}/JobBoardView/LoadSearchResults"
GUID = "64b71096-a4d4-4341-bc89-b1e0715150{:02d}"


def source(provider: str, endpoint: str, **parameters: object) -> SourceConfig:
    return SourceConfig(
        id=f"acme-{provider}",
        company_id="acme",
        provider=provider,
        source_class=SourceClass.PUBLIC_ATS,
        support_status=SupportStatus.SUPPORTED_UNDOCUMENTED,
        endpoint=endpoint,
        interval_minutes=10,
        parameters=parameters,
    )


async def collect(adapter: Any, config: SourceConfig) -> CollectionResult:
    async with HttpFetcher() as fetcher:
        result: CollectionResult = await adapter.collect(config, fetcher)
        return result


@respx.mock
@pytest.mark.asyncio
async def test_rippling_merges_one_posting_listed_once_per_location() -> None:
    def job(uuid: str, place: str) -> dict[str, Any]:
        return {
            "uuid": uuid,
            "name": "Robotics Intern",
            "department": {"label": "Engineering"},
            "url": f"https://ats.rippling.com/acme/jobs/{uuid}",
            "workLocation": {"label": place},
        }

    respx.get(RIPPLING).mock(
        return_value=httpx.Response(
            200, json=[job("a1", "Austin, TX"), job("a1", "Remote (United States)"), job("b2", "")]
        )
    )

    result = await collect(RipplingAdapter(), source("rippling", RIPPLING))

    assert result.complete
    assert [(job.source_job_id, job.locations) for job in result.jobs] == [
        ("a1", ["Austin, TX", "Remote (United States)"]),
        ("b2", []),
    ]
    assert result.jobs[0].department == "Engineering"
    assert result.jobs[0].canonical_job_key == "rippling:acme:a1"


@respx.mock
@pytest.mark.asyncio
async def test_workable_merges_repeated_shortcodes_and_reads_remote() -> None:
    def job(city: str, *, telecommuting: bool = False) -> dict[str, Any]:
        return {
            "title": "Mechanical Engineer Intern",
            "shortcode": "D7DF96B399",
            "url": "https://apply.workable.com/j/D7DF96B399",
            "application_url": "https://apply.workable.com/j/D7DF96B399/apply",
            "telecommuting": telecommuting,
            "published_on": "2026-09-08",
            "locations": [{"city": city, "region": "California", "country": "United States"}],
        }

    respx.get(WORKABLE).mock(
        return_value=httpx.Response(
            200, json={"name": "Acme", "jobs": [job("Palo Alto", telecommuting=True), job("Napa")]}
        )
    )

    result = await collect(WorkableAdapter(), source("workable", WORKABLE))

    assert result.complete and len(result.jobs) == 1
    posting = result.jobs[0]
    assert posting.locations == [
        "Palo Alto, California, United States",
        "Napa, California, United States",
    ]
    assert posting.workplace_type == "remote"
    assert str(posting.apply_url) == "https://apply.workable.com/j/D7DF96B399/apply"
    assert posting.source_published_at is not None


@respx.mock
@pytest.mark.asyncio
async def test_bamboohr_links_the_public_page_and_checks_the_declared_total() -> None:
    record = {
        "id": "110",
        "jobOpeningName": "Mechanical Design Engineer",
        "departmentLabel": "Engineering",
        "employmentStatusLabel": "Internship",
        "location": {"city": "San Antonio", "state": "Texas"},
        "atsLocation": {"country": None},
        "isRemote": None,
    }
    respx.get(BAMBOO).mock(
        return_value=httpx.Response(200, json={"meta": {"totalCount": 2}, "result": [record]})
    )

    result = await collect(BambooHrAdapter(), source("bamboohr", BAMBOO))

    assert not result.complete
    assert "declares 2 postings but published 1" in result.warnings[0]
    assert str(result.jobs[0].posting_url) == "https://acme.bamboohr.com/careers/110"
    assert result.jobs[0].locations == ["San Antonio, Texas"]


@respx.mock
@pytest.mark.asyncio
async def test_pinpoint_refuses_a_posting_link_on_another_host() -> None:
    def record(job_id: str, url: str) -> dict[str, Any]:
        return {
            "id": job_id,
            "title": "Propulsion Intern",
            "url": url,
            "workplace_type": "onsite",
            "employment_type_text": "Internship",
            "job": {"requisition_id": "REQ-1", "department": {"name": "Propulsion"}},
            "location": {"city": "Redondo Beach", "province": "California"},
        }

    respx.get(PINPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    record("1", "https://acme.pinpointhq.com/en/postings/abc"),
                    record("2", "https://elsewhere.example/postings/def"),
                ]
            },
        )
    )

    result = await collect(PinpointAdapter(), source("pinpoint", PINPOINT))

    assert not result.complete
    assert [job.source_job_id for job in result.jobs] == ["1"]
    assert result.jobs[0].locations == ["Redondo Beach, California"]
    assert result.jobs[0].workplace_type == "onsite"
    assert result.jobs[0].requisition_id == "REQ-1"


@respx.mock
@pytest.mark.asyncio
async def test_oracle_pages_through_the_finder_and_links_the_public_job_page() -> None:
    def requisition(job_id: int) -> dict[str, Any]:
        return {
            "Id": str(job_id),
            "Title": f"Manufacturing Intern {job_id}",
            "PrimaryLocation": "Morristown, TN, United States",
            "secondaryLocations": [{"Name": "Knoxville, TN, United States"}],
            "PostedDate": "2026-09-15",
            "WorkplaceType": "On-site",
        }

    def page(*ids: int) -> httpx.Response:
        result = {"TotalJobsCount": 201, "requisitionList": [requisition(i) for i in ids]}
        return httpx.Response(200, json={"items": [result]})

    route = respx.get(url__startswith=ORACLE_HOST).mock(
        side_effect=[page(*range(1, 201)), page(201)]
    )

    result = await collect(OracleAdapter(), source("oracle", ORACLE))

    assert result.complete and len(result.jobs) == 201 and result.declared_total == 201
    first = result.jobs[0]
    assert str(first.posting_url) == (
        f"{ORACLE_HOST}/hcmUI/CandidateExperience/en/sites/CX_1/job/1"
    )
    assert first.locations == ["Morristown, TN, United States", "Knoxville, TN, United States"]
    assert first.workplace_type == "onsite"
    finders = [
        dict(parse_qsl(urlsplit(str(call.request.url)).query))["finder"] for call in route.calls
    ]
    assert finders == [
        "findReqs;siteNumber=CX_1,keyword=intern,sortBy=POSTING_DATES_DESC,limit=200,offset=0",
        "findReqs;siteNumber=CX_1,keyword=intern,sortBy=POSTING_DATES_DESC,limit=200,offset=200",
    ]


@respx.mock
@pytest.mark.asyncio
async def test_ukg_posts_the_search_and_pages_by_skip() -> None:
    def opportunity(index: int) -> dict[str, Any]:
        return {
            "Id": GUID.format(index),
            "Title": f"Controls Engineering Intern {index}",
            "RequisitionNumber": f"REQ{index}",
            "FullTime": True,
            "JobCategoryName": "Engineering",
            "PostedDate": "2026-10-14T15:51:28.214Z",
            "Locations": [
                {
                    "LocalizedDescription": "IL Headquarters",
                    "Address": {
                        "City": "Waukegan",
                        "State": {"Code": "IL"},
                        "Country": {"Code": "USA"},
                    },
                }
            ],
        }

    pages = [list(range(50)), [50]]
    route = respx.post(UKG).mock(
        side_effect=[
            httpx.Response(
                200, json={"opportunities": [opportunity(i) for i in ids], "totalCount": 51}
            )
            for ids in pages
        ]
    )

    result = await collect(UkgAdapter(), source("ukg", UKG, query="intern"))

    assert result.complete and len(result.jobs) == 51
    bodies = [json.loads(call.request.content) for call in route.calls]
    assert [body["opportunitySearch"]["Skip"] for body in bodies] == [0, 50]
    assert all(body["opportunitySearch"]["QueryString"] == "intern" for body in bodies)
    first = result.jobs[0]
    assert first.locations == ["Waukegan, IL, USA"]
    assert first.employment_type == "Full-time" and first.requisition_id == "REQ0"
    assert str(first.posting_url).endswith(f"/OpportunityDetail?opportunityId={GUID.format(0)}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter", "provider", "endpoint"),
    [
        (RipplingAdapter(), "rippling", "https://ats.rippling.com/acme/jobs"),
        (WorkableAdapter(), "workable", "https://apply.workable.com/acme/"),
        (BambooHrAdapter(), "bamboohr", "https://acme.bamboohr.com/careers"),
        (PinpointAdapter(), "pinpoint", "https://acme.pinpointhq.com/"),
        (OracleAdapter(), "oracle", f"{ORACLE_HOST}/hcmUI/CandidateExperience/en/sites/CX_1"),
        (UkgAdapter(), "ukg", f"https://recruiting.ultipro.com/ACME1000/JobBoard/{BOARD}/"),
    ],
)
async def test_an_endpoint_of_the_wrong_shape_is_refused(
    adapter: Any, provider: str, endpoint: str
) -> None:
    with pytest.raises(AdapterError):
        await collect(adapter, source(provider, endpoint))


def oracle_page_by_offset(
    total: int, pages: dict[int, list[int]]
) -> Any:
    """A responder that answers each finder offset with the given requisition ids."""

    def respond(request: httpx.Request) -> httpx.Response:
        finder = dict(parse_qsl(urlsplit(str(request.url)).query))["finder"]
        offset = int(finder.rsplit("offset=", 1)[1])
        rows = [
            {"Id": str(job_id), "Title": f"Intern {job_id}", "PrimaryLocation": "Austin, TX"}
            for job_id in pages[offset]
        ]
        result = {"TotalJobsCount": total, "requisitionList": rows}
        return httpx.Response(200, json={"items": [result]})

    return respond


@respx.mock
@pytest.mark.asyncio
async def test_oracle_counts_a_double_listing_after_a_short_page_as_complete() -> None:
    # The site collapsed requisition 199's double listing inside page one (199 rows for a
    # 200-row request) yet counted both, then repeated it at the start of page two.
    respx.get(url__startswith=ORACLE_HOST).mock(
        side_effect=oracle_page_by_offset(
            201, {0: list(range(1, 200)), 199: [199, 200]}
        )
    )

    result = await collect(OracleAdapter(), source("oracle", ORACLE))

    assert result.complete and result.warnings == []
    assert len(result.jobs) == 200 and result.declared_total == 200


@respx.mock
@pytest.mark.asyncio
async def test_oracle_keeps_an_unexplained_repeat_incomplete() -> None:
    # A full first page whose last posting repeats at the start of the next page is what a
    # shifting board looks like; posting 201 is missing on every walk.
    respx.get(url__startswith=ORACLE_HOST).mock(
        side_effect=oracle_page_by_offset(
            202, {0: list(range(1, 201)), 200: [200, 202]}
        )
    )

    result = await collect(OracleAdapter(), source("oracle", ORACLE))

    assert not result.complete
    assert any("declared 202" in warning for warning in result.warnings)
