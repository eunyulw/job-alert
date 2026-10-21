"""Offline tests for the iCIMS, Phenom, Jibe, Taleo, ADP, HRM Direct, Radancy, Breezy and JazzHR
adapters."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
import respx

from job_alert.adapters.adp import AdpAdapter
from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import dashed_location
from job_alert.adapters.breezy import BreezyAdapter
from job_alert.adapters.hrmdirect import HrmDirectAdapter
from job_alert.adapters.icims import IcimsAdapter
from job_alert.adapters.jazzhr import JazzHrAdapter
from job_alert.adapters.jibe import JibeAdapter
from job_alert.adapters.phenom import PhenomAdapter
from job_alert.adapters.radancy import RadancyAdapter
from job_alert.adapters.taleo import TaleoAdapter
from job_alert.fetch import HttpFetcher
from job_alert.models import CollectionResult, SourceClass, SourceConfig, SupportStatus

PHENOM = "https://careers.acme.com/widgets"
JIBE = "https://careers.acme.com/api/jobs"
ICIMS_HOST = "https://careers-acme.icims.com"
ICIMS = f"{ICIMS_HOST}/jobs/search?ss=1"
TALEO_HOST = "https://acme.taleo.net"
TALEO = f"{TALEO_HOST}/careersection/ext/jobsearch.ftl"
TALEO_SEARCH = f"{TALEO_HOST}/careersection/rest/jobboard/searchjobs"
ADP = "https://myjobs.adp.com/public/staffing/v1/career-site/acmecx"
ADP_LIST = "https://my.adp.com/myadp_prefix/mycareer/public/staffing/v1/job-requisitions"
HRM = "https://acme.hrmdirect.com/employment/job-openings.php?search=true"
RADANCY_HOST = "https://careers.acme.com"
RADANCY = f"{RADANCY_HOST}/search-jobs/results?SearchType=5"
BREEZY = "https://acme.breezy.hr/json"
JAZZ = "https://acme.applytojob.com/apply"


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


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("US-CA-Marina", "Marina, CA, US"),
        ("CA-ON-Toronto", "Toronto, ON, Canada"),
        ("IN-Bangalore", "Bangalore, India"),
        ("US-NC-Winston-Salem", "Winston-Salem, NC, US"),
        ("US-California-Point Mugu", "Point Mugu, California, US"),
        ("Austin, TX", "Austin, TX"),
        ("  ", None),
    ],
)
def test_dashed_location_puts_the_country_last_and_spells_out_state_codes(
    label: str, expected: str | None
) -> None:
    assert dashed_location(label) == expected


@respx.mock
@pytest.mark.asyncio
async def test_phenom_pages_by_from_and_builds_the_locale_job_link() -> None:
    bodies: list[dict[str, Any]] = []

    def job(seq: int) -> dict[str, Any]:
        return {
            "jobSeqNo": f"ACMEUS{seq}EXTERNALENUS",
            "title": f"Robotics Intern {seq}",
            "locale": "en_US",
            "multi_location": ["Austin, Texas, United States", "Remote, United States"],
            "location": "Austin, Texas, United States",
            "category": "Engineering",
            "type": "Intern",
            "reqId": f"R{seq}",
            "postedDate": "2026-10-01T00:00:00.000+0000",
        }

    def reply(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        start = body["from"]
        jobs = [job(n) for n in range(start, min(start + 100, 150))]
        search = {"totalHits": 150, "data": {"jobs": jobs}}
        return httpx.Response(200, json={"refineSearch": search})

    respx.post(PHENOM).mock(side_effect=reply)

    result = await collect(
        PhenomAdapter(), source("phenom", PHENOM, selected_fields={"country": ["United States"]})
    )

    assert result.complete and len(result.jobs) == 150 and result.declared_total == 150
    assert [body["from"] for body in bodies] == [0, 100]
    assert bodies[0]["selected_fields"] == {"country": ["United States"]}
    first = result.jobs[0]
    assert str(first.posting_url) == "https://careers.acme.com/us/en/job/ACMEUS0EXTERNALENUS"
    assert first.locations == ["Austin, Texas, United States", "Remote, United States"]
    assert (first.department, first.employment_type, first.requisition_id) == (
        "Engineering",
        "Intern",
        "R0",
    )
    assert first.source_published_at is not None and first.source_published_at.day == 1


@respx.mock
@pytest.mark.asyncio
async def test_jibe_keeps_the_query_splits_multiple_locations_and_checks_the_total() -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        query = dict(parse_qsl(urlsplit(str(request.url)).query))
        assert query["location"] == "United States" and query["limit"] == "100"
        assert (query["sortBy"], query["descending"]) == ("posted_date", "true")
        if query["page"] == "1":
            jobs = [
                {
                    "data": {
                        "slug": "30449",
                        "req_id": "30449",
                        "title": "Mechanical Engineering Intern",
                        "full_location": "Torrance, California; Irvine, California",
                        "location_type": "HYBRID",
                        "categories": [{"name": "Engineering"}],
                        "employment_type": "INTERN",
                        "posted_date": "2026-10-13T23:17:00+0000",
                    }
                },
                {"data": {"slug": "30450", "title": "Technician", "city": "Normal",
                          "state": "Illinois", "country": "United States"}},
            ]
            return httpx.Response(200, json={"totalCount": 3, "jobs": jobs})
        return httpx.Response(200, json={"totalCount": 3, "jobs": []})

    respx.get(url__startswith=JIBE).mock(side_effect=reply)

    result = await collect(JibeAdapter(), source("jibe", f"{JIBE}?location=United+States"))

    assert not result.complete
    assert "declared 3" in result.warnings[-1]
    first, second = result.jobs
    assert str(first.posting_url) == "https://careers.acme.com/careers-home/jobs/30449"
    assert first.locations == ["Torrance, California", "Irvine, California"]
    assert (first.workplace_type, first.department) == ("hybrid", "Engineering")
    assert second.locations == ["Normal, Illinois, United States"]


def icims_card(job_id: int, title: str, places: str, category: str | None = None) -> str:
    extra = (
        '<dl class="iCIMS_JobHeaderGroup"><div class="iCIMS_JobHeaderTag">'
        f'<dt class="iCIMS_JobHeaderField">Category</dt>\n<dd class="iCIMS_JobHeaderData"><span >'
        f"\n{category}</span>\n</dd></div></dl>"
        if category
        else ""
    )
    return (
        '<li class="iCIMS_JobCardItem">\n<div class="row">\n<div class="col-xs-6 header left">\n'
        '<span class="sr-only field-label">Job Locations</span>\n<span >\n'
        f"{places}</span>\n</div>\n"
        '<div class="col-xs-6 header right">\n'
        '<span class="sr-only field-label">Posted Date</span>\n'
        '<span title="10/15/2026 11:49 AM">\n3 days ago<span class="sr-only">(10/15/2026 11:49 AM)'
        "</span></span>\n</div>\n"
        f'<div class="col-xs-12 title">\n<a href="{ICIMS_HOST}/jobs/{job_id}/slug/job?in_iframe=1"'
        f' class="iCIMS_Anchor" title="{job_id} - {title}">\n'
        f'<span class="sr-only field-label">Title</span>\n<h3 >\n{title}</h3>\n</a>\n</div>\n'
        f"{extra}</div>\n</li>\n"
    )


def icims_page(cards: list[str], pages: int) -> str:
    return (
        f'<ul class="container-fluid iCIMS_JobsTable">{"".join(cards)}</ul>'
        f'<div class="iCIMS_Paging">Page 1 of {pages}</div>'
    )


def sitemap(ids: list[int]) -> str:
    urls = "".join(
        f"<url><loc>{ICIMS_HOST}/jobs/{job_id}/slug/job</loc></url>" for job_id in ids
    )
    intro = f"<url><loc>{ICIMS_HOST}/jobs/intro</loc></url>"
    return f'<?xml version="1.0"?><urlset>{intro}{urls}</urlset>'


@respx.mock
@pytest.mark.asyncio
async def test_icims_walks_every_page_and_checks_the_ids_against_the_sitemap() -> None:
    respx.get(f"{ICIMS_HOST}/sitemap.xml").mock(
        return_value=httpx.Response(200, text=sitemap([1, 2, 3]))
    )
    pages = {
        "0": icims_page(
            [
                icims_card(1, "Robotics Intern", "US-CA-Marina | US-CA-Watsonville", "Engineering"),
                icims_card(2, "Controls Engineer", "CA-ON-Toronto"),
            ],
            2,
        ),
        "1": icims_page([icims_card(3, "Technician &amp; Mechanic", "US-IN-Hobart")], 2),
    }
    respx.get(url__startswith=f"{ICIMS_HOST}/jobs/search").mock(
        side_effect=lambda request: httpx.Response(
            200, text=pages[dict(parse_qsl(urlsplit(str(request.url)).query))["pr"]]
        )
    )

    result = await collect(IcimsAdapter(), source("icims", ICIMS))

    assert result.complete and result.declared_total == 3 and result.page_count == 2
    first, second, third = result.jobs
    assert first.locations == ["Marina, CA, US", "Watsonville, CA, US"]
    assert first.department == "Engineering"
    assert str(first.posting_url) == f"{ICIMS_HOST}/jobs/1/slug/job"
    assert first.source_published_at is not None and first.source_published_at.hour == 11
    assert second.locations == ["Toronto, ON, Canada"]
    assert third.title == "Technician & Mechanic"


@respx.mock
@pytest.mark.asyncio
async def test_icims_reads_a_location_field_when_the_card_header_has_none() -> None:
    respx.get(f"{ICIMS_HOST}/sitemap.xml").mock(return_value=httpx.Response(200, text=sitemap([7])))
    card = (
        '<li class="iCIMS_JobCardItem">\n<div class="row">\n<div class="col-xs-12 title">\n'
        f'<a href="{ICIMS_HOST}/jobs/7/x/job?in_iframe=1" class="iCIMS_Anchor">\n'
        "<h3 >\nEngineering Summer Internship</h3>\n</a>\n</div>\n"
        '<dl class="iCIMS_JobHeaderGroup"><div class="iCIMS_JobHeaderTag">\n'
        '<dt class="iCIMS_JobHeaderField">Location</dt>\n'
        '<dd class="iCIMS_JobHeaderData"><span >\nUS-CT-Groton</span>\n</dd>\n</div></dl>\n</li>\n'
    )
    respx.get(url__startswith=f"{ICIMS_HOST}/jobs/search").mock(
        return_value=httpx.Response(200, text=icims_page([card], 1))
    )

    result = await collect(IcimsAdapter(), source("icims", ICIMS))

    assert result.complete
    assert result.jobs[0].locations == ["Groton, CT, US"]


@respx.mock
@pytest.mark.asyncio
async def test_icims_is_incomplete_when_the_sitemap_keeps_disagreeing() -> None:
    sitemap_route = respx.get(f"{ICIMS_HOST}/sitemap.xml").mock(
        return_value=httpx.Response(200, text=sitemap([1, 2]))
    )
    respx.get(url__startswith=f"{ICIMS_HOST}/jobs/search").mock(
        return_value=httpx.Response(
            200, text=icims_page([icims_card(1, "Intern", "US-TX-Austin")], 1)
        )
    )

    result = await collect(IcimsAdapter(), source("icims", ICIMS))

    assert not result.complete
    assert sitemap_route.call_count == 2
    assert "1 only in the sitemap" in result.warnings[-1]


def taleo_record(contest: int, title: str = "Intern") -> dict[str, Any]:
    return {
        "jobId": str(9000 + contest),
        "contestNo": str(contest),
        "column": [title, '["US-Texas-Fort Worth","CA-Quebec-Mirabel"]', "10/17/2026"],
        "locationsColumns": [1],
    }


def taleo_routes(pages: dict[int, list[dict[str, Any]]], total: int) -> respx.Route:
    respx.get(url__startswith=TALEO).mock(
        return_value=httpx.Response(200, text="<script>var p = {portalNo: '8140'};</script>")
    )

    def reply(request: httpx.Request) -> httpx.Response:
        assert dict(parse_qsl(urlsplit(str(request.url)).query))["portal"] == "8140"
        assert request.headers["tzname"] == "UTC"
        page = json.loads(request.content)["pageNo"]
        return httpx.Response(
            200,
            json={"requisitionList": pages.get(page, []),
                  "pagingData": {"totalCount": total, "pageSize": 25}},
        )

    return respx.post(url__startswith=TALEO_SEARCH).mock(side_effect=reply)


@respx.mock
@pytest.mark.asyncio
async def test_taleo_reads_the_portal_and_maps_the_columns() -> None:
    route = taleo_routes({1: [taleo_record(1, "Mechanical Intern"), taleo_record(2)]}, 2)

    result = await collect(TaleoAdapter(), source("taleo", TALEO))

    assert result.complete and result.declared_total == 2
    assert route.call_count == 2  # the pages, then one empty page; no second walk was needed
    first = result.jobs[0]
    assert first.title == "Mechanical Intern"
    assert first.locations == ["Fort Worth, Texas, US", "Mirabel, Quebec, Canada"]
    assert str(first.posting_url) == f"{TALEO_HOST}/careersection/ext/jobdetail.ftl?job=1&lang=en"
    assert first.source_published_at is not None and first.source_published_at.month == 10


@respx.mock
@pytest.mark.asyncio
async def test_taleo_accepts_a_short_board_only_when_two_walks_agree() -> None:
    route = taleo_routes({1: [taleo_record(1), taleo_record(2)], 2: [taleo_record(3)]}, 5)

    result = await collect(TaleoAdapter(), source("taleo", TALEO))

    assert result.complete and result.declared_total == 3
    assert route.call_count == 6


@respx.mock
@pytest.mark.asyncio
async def test_taleo_stops_when_a_section_repeats_its_last_page() -> None:
    repeated = [taleo_record(1), taleo_record(2)]
    route = taleo_routes({page: repeated for page in range(1, 400)}, 40)

    result = await collect(TaleoAdapter(), source("taleo", TALEO))

    assert not result.complete
    assert route.call_count == 2
    assert "repeated earlier records only" in result.warnings[-1]


@respx.mock
@pytest.mark.asyncio
async def test_adp_sends_the_sites_orgoid_and_reads_requisition_locations() -> None:
    respx.get(ADP).mock(return_value=httpx.Response(200, json={"orgoid": "G3ABC123"}))

    def reply(request: httpx.Request) -> httpx.Response:
        assert request.headers["orgoid"] == "G3ABC123"
        assert "%24skip=0" in str(request.url) or "$skip=0" in str(request.url)
        place = {"address": {"cityName": "Auburn Hills",
                             "countrySubdivisionLevel1": {"longName": "Michigan"},
                             "country": {"longName": "United States"}}}
        record = {"reqId": "5001232569100", "jobTitle": "2027 Engineering Internship",
                  "clientRequisitionID": "2022166", "requisitionLocations": [place]}
        return httpx.Response(200, json={"count": 1, "jobRequisitions": [record]})

    respx.get(url__startswith=ADP_LIST).mock(side_effect=reply)

    result = await collect(AdpAdapter(), source("adp", ADP))

    assert result.complete
    (job,) = result.jobs
    assert job.locations == ["Auburn Hills, Michigan, United States"]
    assert str(job.posting_url) == "https://myjobs.adp.com/acmecx/cx/job/5001232569100"
    assert job.requisition_id == "2022166"


def hrm_row(req: int, loc: int, title: str, state: str, category: str) -> str:
    return (
        f'<tr class="reqitem ReqRowClick" data-req-id="{req}"'
        f""" onClick='window.location="job-opening.php?req={req}&req_loc={loc}&#job ";'>"""
        f'<td class="leftBorder">&nbsp;</td>'
        f'<td id=\'c0\' class="custSort1 reqitem ReqRowClick">{category}&nbsp;</td>'
        f'<td id=\'p0\' class="posTitle reqitem ReqRowClick">'
        f'<a href="job-opening.php?req={req}&req_loc={loc}&&amp;#job">{title}</td>'
        f'<td id=\'s0\' class="state reqitem ReqRowClick">{state}</td></tr>\n'
    )


HRM_PAGE = (
    '<table><tr><td class="leftBorderReqhead">&nbsp;</td>'
    '<td class="reqhead"><a href="?sort=c1a">Category</a></td>'
    '<td class="reqhead"><a href="?sort=pa">Position Title</a></td>'
    '<td class="reqhead"><a href="?sort=sa">State</a></td></tr>\n'
    + hrm_row(11, 1, "Propulsion Intern", "TX", "Propulsion")
    + hrm_row(11, 2, "Propulsion Intern", "Alabama", "Propulsion")
    + hrm_row(12, 3, "Technician", "Ontario", "Operations")
    + "</table>"
)


@respx.mock
@pytest.mark.asyncio
async def test_hrmdirect_merges_one_requisition_listed_once_per_place() -> None:
    respx.get(HRM).mock(return_value=httpx.Response(200, text=HRM_PAGE))

    result = await collect(HrmDirectAdapter(), source("hrmdirect", HRM))

    assert result.complete
    intern, technician = result.jobs
    assert intern.source_job_id == "11"
    assert intern.locations == ["TX, US", "Alabama, US"]
    assert intern.department == "Propulsion"
    expected = "https://acme.hrmdirect.com/employment/job-opening.php?req=11&req_loc=1"
    assert str(intern.posting_url) == expected
    assert technician.locations == ["Ontario"]


def radancy_fragment(items: str, jobs: int, everything: int) -> dict[str, Any]:
    section = (
        f'<section id="search-results" data-total-results="{everything}"'
        f' data-total-job-results="{jobs}"><ul>{items}</ul></section>'
    )
    return {"filters": "<section></section>", "results": section, "hasJobs": True}


@respx.mock
@pytest.mark.asyncio
async def test_radancy_counts_jobs_only_and_reads_the_place_from_a_vague_label() -> None:
    items = (
        '<li><a href="/en/job/united-states/systems-intern/4832/101" data-job-id="101">'
        '<h2>Systems Engineering Intern</h2><span class="results-facet job-category">Engineering'
        '</span><span class="results-facet job-location">Multiple Locations</span></a></li>'
        '<li class=""><a href="/job/needham/mechanical-intern/47204/102" data-job-id="102">'
        '<h2>Mechanical Engineering Intern &amp; Co-op</h2><span class="job-location">Needham, MA'
        '</span><span class="job-date-posted">10/09/2026</span></a></li>'
    )

    def reply(request: httpx.Request) -> httpx.Response:
        query = dict(parse_qsl(urlsplit(str(request.url)).query))
        assert request.headers["X-Requested-With"] == "XMLHttpRequest"
        assert (query["SearchType"], query["CurrentPage"], query["RecordsPerPage"]) == (
            "5",
            "1",
            "100",
        )
        return httpx.Response(200, json=radancy_fragment(items, 2, 28))

    respx.get(url__startswith=f"{RADANCY_HOST}/search-jobs/results").mock(side_effect=reply)

    result = await collect(RadancyAdapter(), source("radancy", RADANCY))

    assert result.complete and result.declared_total == 2
    systems, mechanical = result.jobs
    assert systems.locations == ["Multiple Locations", "United States"]
    assert systems.department == "Engineering"
    assert str(systems.posting_url) == (
        "https://careers.acme.com/en/job/united-states/systems-intern/4832/101"
    )
    assert mechanical.title == "Mechanical Engineering Intern & Co-op"
    assert mechanical.locations == ["Needham, MA"]
    assert mechanical.source_published_at is not None


@respx.mock
@pytest.mark.asyncio
async def test_breezy_reads_every_location_and_the_remote_flag() -> None:
    record = {
        "id": "cd745ed0e1d4",
        "name": "Robotics Engineering Intern",
        "url": "https://acme.breezy.hr/p/cd745ed0e1d4-robotics-engineering-intern",
        "published_date": "2026-09-03T13:42:50.161Z",
        "type": {"id": "internship", "name": "Internship"},
        "department": "Engineering",
        "location": {"city": "Charlestown", "state": {"id": "MA", "name": "Massachusetts"},
                     "country": {"id": "US", "name": "United States"}, "is_remote": False},
        "locations": [
            {"city": "Charlestown", "state": {"id": "MA", "name": "Massachusetts"},
             "country": {"id": "US", "name": "United States"}, "is_remote": False},
            {"country": {"id": "DE", "name": "Germany"}, "is_remote": True, "name": "Germany"},
        ],
    }
    respx.get(BREEZY).mock(return_value=httpx.Response(200, json=[record]))

    result = await collect(BreezyAdapter(), source("breezy", BREEZY))

    assert result.complete
    (job,) = result.jobs
    assert job.locations == ["Charlestown, Massachusetts, United States", "Germany"]
    assert (job.workplace_type, job.employment_type, job.department) == (
        "remote",
        "Internship",
        "Engineering",
    )


JAZZ_PAGE = """
<ul class="list-group">
<li class="list-group-item">
  <h3 class='list-group-item-heading'>
    <a href="https://acme.applytojob.com/apply/Jyzh5UV6HP/Robotics-Intern">
      Robotics Intern &amp; Co-op    </a>
  </h3>
  <ul class='list-inline list-group-item-text'>
    <li><i class='fa fa-map-marker'></i>Los Angeles, CA</li>
    <li><i class='fa fa-sitemap'></i>Engineering</li>
  </ul>
</li>
</ul>
"""


@respx.mock
@pytest.mark.asyncio
async def test_jazzhr_reads_the_listing_items() -> None:
    respx.get(JAZZ).mock(return_value=httpx.Response(200, text=JAZZ_PAGE))

    result = await collect(JazzHrAdapter(), source("jazzhr", JAZZ))

    assert result.complete
    (job,) = result.jobs
    assert (job.source_job_id, job.title) == ("Jyzh5UV6HP", "Robotics Intern & Co-op")
    assert (job.locations, job.department) == (["Los Angeles, CA"], "Engineering")
    assert str(job.posting_url) == "https://acme.applytojob.com/apply/Jyzh5UV6HP/Robotics-Intern"


@pytest.mark.parametrize(
    ("adapter", "provider", "endpoint"),
    [
        (PhenomAdapter(), "phenom", "https://careers.acme.com/api/jobs"),
        (JibeAdapter(), "jibe", "https://careers.acme.com/widgets"),
        (IcimsAdapter(), "icims", f"{ICIMS_HOST}/jobs/search?ss=1&searchCategory=12"),
        (TaleoAdapter(), "taleo", f"{TALEO_HOST}/careersection/ext/jobdetail.ftl"),
        (AdpAdapter(), "adp", "https://myjobs.adp.com/acmecx/cx"),
        (HrmDirectAdapter(), "hrmdirect", "https://acme.hrmdirect.com/employment/job-openings.php"),
        (RadancyAdapter(), "radancy", f"{RADANCY_HOST}/search-jobs"),
        (BreezyAdapter(), "breezy", "https://acme.breezy.hr/p/abc"),
        (JazzHrAdapter(), "jazzhr", "https://acme.applytojob.com/jobs"),
    ],
)
@pytest.mark.asyncio
async def test_each_adapter_rejects_an_endpoint_of_the_wrong_shape(
    adapter: Any, provider: str, endpoint: str
) -> None:
    with pytest.raises(AdapterError):
        await collect(adapter, source(provider, endpoint))
