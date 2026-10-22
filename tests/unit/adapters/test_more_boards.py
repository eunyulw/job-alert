"""Offline tests for the Meta, Avature, Paylocity, Paycor, Dayforce, Gem, BrassRing, Teamtailor,
ADP Workforce Now and browser-relay adapters."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from job_alert.adapters.adp_wfn import AdpWorkforceNowAdapter
from job_alert.adapters.avature import AvatureAdapter
from job_alert.adapters.base import AdapterError, validate_collection
from job_alert.adapters.brassring import BrassRingAdapter
from job_alert.adapters.browser_relay import BrowserRelayAdapter
from job_alert.adapters.dayforce import DayforceAdapter
from job_alert.adapters.gem import GemAdapter
from job_alert.adapters.meta import MetaAdapter
from job_alert.adapters.paycor import PaycorAdapter
from job_alert.adapters.paylocity import PaylocityAdapter
from job_alert.adapters.teamtailor import TeamtailorAdapter
from job_alert.fetch import HttpFetcher
from job_alert.models import CollectionResult, SourceClass, SourceConfig, SupportStatus
from tests.helpers import assert_normalized_job

HTML = {"content-type": "text/html; charset=utf-8"}


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


# ---- Meta ------------------------------------------------------------------------------------

META = "https://www.metacareers.com/graphql"
META_PAGE = '<html><script>["LSD",[],{"token":"tok123"},323]</script></html>'


def meta_answer(*jobs: dict[str, Any]) -> httpx.Response:
    document = {"data": {"job_search_with_featured_jobs_v2": {"all_jobs": list(jobs)}}}
    return httpx.Response(200, text=json.dumps(document) + "\n{\"extensions\":{}}")


@respx.mock
@pytest.mark.asyncio
async def test_meta_sends_the_page_token_and_reads_every_job() -> None:
    respx.get("https://www.metacareers.com/jobsearch/").mock(
        return_value=httpx.Response(200, text=META_PAGE, headers=HTML)
    )
    query = respx.post(META).mock(
        return_value=meta_answer(
            {"id": "1078644001656413", "title": "Research Intern, Robotics", "locations":
             ["Menlo Park, CA", "Remote, US"], "teams": ["AI Research", "Research"]},
            {"id": "1050177734644029", "title": "Research Intern", "locations": ["Paris, France"]},
        )
    )

    result = validate_collection(await collect(MetaAdapter(), source("meta", META)))

    assert [job.source_job_id for job in result.jobs] == ["1078644001656413", "1050177734644029"]
    first = result.jobs[0]
    assert first.locations == ["Menlo Park, CA", "Remote, US"]
    assert first.department == "AI Research"
    assert str(first.posting_url) == (
        "https://www.metacareers.com/profile/job_details/1078644001656413"
    )
    sent = parse_qs(query.calls[0].request.content.decode())
    assert sent["lsd"] == ["tok123"] and sent["doc_id"] == ["27129360303422352"]
    assert json.loads(sent["variables"][0])["search_input"]["roles"] == ["Internship"]
    assert query.calls[0].request.headers["X-FB-LSD"] == "tok123"
    for job in result.jobs:
        assert_normalized_job(job)


@respx.mock
@pytest.mark.asyncio
async def test_meta_reports_a_rotated_query_id_instead_of_an_empty_board() -> None:
    respx.get("https://www.metacareers.com/jobsearch/").mock(
        return_value=httpx.Response(200, text=META_PAGE, headers=HTML)
    )
    respx.post(META).mock(return_value=httpx.Response(200, text='{"errors":[{"code":1675012}]}'))

    with pytest.raises(AdapterError, match="doc_id"):
        await collect(MetaAdapter(), source("meta", META))


# ---- Avature ---------------------------------------------------------------------------------

AVATURE = "https://jobs.acme.com/en_US/careers/SearchJobs/"


def article(job_id: str, title: str, place: str) -> str:
    return (
        f'<article class="article article--result 1"><h3><a class="link"'
        f' href="https://jobs.acme.com/en_US/careers/JobDetail/{job_id}">{title}</a></h3>'
        f'<div class="article__header__text__subtitle"> {place} • Job ID: {job_id} • Ops</div>'
        "</article>"
    )


def item(job_id: str, title: str, place: str) -> str:
    slug = title.replace(" ", "-")
    return (
        '<li class="listSingleColumnItem"><h3 class="listSingleColumnItemTitle"><a'
        f' href="https://jobs.acme.com/en_US/careers/JobDetail/{slug}/{job_id}">{title}</a></h3>'
        f'<p><span class="listSingleColumnItemMiscDataItem">{place}. </span></p></li>'
    )


@respx.mock
@pytest.mark.asyncio
async def test_avature_article_template_walks_by_folder_offset_to_the_total() -> None:
    paging = '<a href="https://jobs.acme.com/en_US/careers/SearchJobs/?folderOffset=2">2</a>'
    route = respx.get(url__startswith=AVATURE).mock(
        side_effect=[
            httpx.Response(
                200,
                text="1 - 2 of 3 results" + paging
                + article("11", "Robotics Intern", "Raleigh , North Carolina , United States")
                + article("12", "Controls Intern", "Austin , Texas , United States"),
                headers=HTML,
            ),
            httpx.Response(
                200,
                text="3 - 3 of 3 results" + paging + article("13", "Test Intern", "Remote"),
                headers=HTML,
            ),
        ]
    )

    result = validate_collection(
        await collect(AvatureAdapter(), source("avature", f"{AVATURE}?42386=%5B812209%5D"))
    )

    assert [job.source_job_id for job in result.jobs] == ["11", "12", "13"]
    assert result.jobs[0].locations == ["Raleigh, North Carolina, United States"]
    queries = [parse_qs(urlsplit(str(call.request.url)).query) for call in route.calls]
    assert queries[1]["folderOffset"] == ["2"] and queries[1]["42386"] == ["[812209]"]


@respx.mock
@pytest.mark.asyncio
async def test_avature_list_template_without_a_total_ends_at_the_empty_page() -> None:
    paging = '<a href="https://jobs.acme.com/en_US/careers/SearchJobs/?jobOffset=20">2</a>'
    respx.get(url__startswith=AVATURE).mock(
        side_effect=[
            httpx.Response(200, text=paging + item("8488", "Engineering Intern", "Twinsburg, OH"),
                           headers=HTML),
            httpx.Response(200, text=paging, headers=HTML),
        ]
    )

    result = await collect(AvatureAdapter(), source("avature", AVATURE))

    assert result.complete and [job.locations for job in result.jobs] == [["Twinsburg, OH"]]
    assert result.page_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_avature_capped_total_cannot_be_read_whole() -> None:
    respx.get(url__startswith=AVATURE).mock(
        return_value=httpx.Response(
            200, text="1 - 1 of 999+ results" + article("1", "A", "B"), headers=HTML
        )
    )

    result = await collect(AvatureAdapter(), source("avature", AVATURE))

    assert not result.complete and "cap" in result.warnings[0]


# ---- Paylocity -------------------------------------------------------------------------------

PAYLOCITY = (
    "https://recruiting.paylocity.com/recruiting/jobs/All/58719e28-3ea1-47ab-b11e-1fcd3cff5b03"
)


@respx.mock
@pytest.mark.asyncio
async def test_paylocity_reads_the_page_data_jobs() -> None:
    data = {
        "Jobs": [
            {"JobId": 4546033, "JobTitle": "Intern, Engineering Co-Op", "LocationName": "SDKY",
             "PublishedDate": "2026-10-14T10:56:41-05:00", "HiringDepartment": "Internships",
             "JobLocation": {"City": "Sandusky", "State": "OH", "Country": "USA"}},
            {"JobId": 4579005, "JobTitle": "Controls Engineer", "LocationName": "Remote",
             "IsRemote": True, "JobLocation": None},
        ]
    }
    respx.get(PAYLOCITY).mock(
        return_value=httpx.Response(
            200, text=f"<script>window.pageData = {json.dumps(data)};\n</script>", headers=HTML
        )
    )

    result = validate_collection(await collect(PaylocityAdapter(), source("paylocity", PAYLOCITY)))

    first, second = result.jobs
    assert first.locations == ["Sandusky, OH, USA"] and first.department == "Internships"
    assert str(first.posting_url) == (
        "https://recruiting.paylocity.com/recruiting/jobs/Details/4546033"
    )
    assert second.locations == ["Remote"] and second.workplace_type == "remote"


# ---- Paycor ----------------------------------------------------------------------------------

PAYCOR = (
    "https://recruitingbypaycor.com/career/CareerHome.action?clientId=8a7883d0821e1a6301823c22a8bd068b"
)


@respx.mock
@pytest.mark.asyncio
async def test_paycor_reads_rows_under_their_department_headers() -> None:
    def row(job_id: str, title: str, place: str) -> str:
        return (
            '<div class="gnewtonCareerGroupRowClass"><div class="gnewtonCareerGroupJobTitleClass">'
            '<a href="https://recruitingbypaycor.com/career/JobIntroduction.action?clientId='
            f'8a7883d0821e1a6301823c22a8bd068b&amp;id={job_id}&amp;source=&amp;lang=en">'
            f" {title} </a></div>"
            f'<div class="gnewtonCareerGroupJobDescriptionClass"> {place} </div></div>'
        )

    body = (
        '<div class="gnewtonCareerGroupHeaderClass"> Engineering </div>'
        + row("8a78859e93dc3ace01943c8b40f3080b", "Flight Software Intern", "Denver, CO")
        + '<div class="gnewtonCareerGroupHeaderClass"> Facilities </div>'
        + row("8a7883ac96d0a138019732c4be842111", "Facilities Technician", "Greenwood Village, CO")
        + "</body>"
    )
    respx.get(PAYCOR).mock(return_value=httpx.Response(200, text=body, headers=HTML))

    result = validate_collection(await collect(PaycorAdapter(), source("paycor", PAYCOR)))

    assert [(job.title, job.locations, job.department) for job in result.jobs] == [
        ("Flight Software Intern", ["Denver, CO"], "Engineering"),
        ("Facilities Technician", ["Greenwood Village, CO"], "Facilities"),
    ]
    assert "id=8a78859e93dc3ace01943c8b40f3080b" in str(result.jobs[0].posting_url)


# ---- Dayforce --------------------------------------------------------------------------------

DAYFORCE = "https://jobs.dayforcehcm.com/en-US/eclipse/CANDIDATEPORTAL"


@respx.mock
@pytest.mark.asyncio
async def test_dayforce_sends_the_csrf_token_and_walks_to_max_count() -> None:
    respx.get("https://jobs.dayforcehcm.com/api/auth/csrf").mock(
        return_value=httpx.Response(200, json={"csrfToken": "csrf-1"})
    )

    def posting(number: int, address: str) -> dict[str, Any]:
        return {"jobPostingId": number, "jobReqId": 900 + number, "jobTitle": f"Co-op {number}",
                "postingLocations": [{"formattedAddress": address}],
                "postingStartTimestampUTC": "2026-10-21T04:00:00+00:00"}

    search = respx.post("https://jobs.dayforcehcm.com/api/geo/eclipse/jobposting/search").mock(
        side_effect=[
            httpx.Response(200, json={"jobPostings": [posting(1, "Rock Hill, SC, USA"),
                                                      posting(2, "Cambridge, ON, Canada")],
                                      "maxCount": 3}),
            httpx.Response(200, json={"jobPostings": [posting(3, "Rock Hill, SC, USA")],
                                      "maxCount": 3}),
        ]
    )

    result = validate_collection(await collect(DayforceAdapter(), source("dayforce", DAYFORCE)))

    assert [job.source_job_id for job in result.jobs] == ["1", "2", "3"]
    assert str(result.jobs[0].posting_url) == f"{DAYFORCE}/jobs/1"
    assert result.jobs[0].requisition_id == "901"
    bodies = [json.loads(call.request.content) for call in search.calls]
    assert [body["paginationStart"] for body in bodies] == [0, 2]
    assert bodies[0]["jobBoardCode"] == "CANDIDATEPORTAL"
    assert search.calls[0].request.headers["X-CSRF-TOKEN"] == "csrf-1"


# ---- Gem -------------------------------------------------------------------------------------

GEM = "https://api.gem.com/job_board/v0/astroforge-io/job_posts/"


@respx.mock
@pytest.mark.asyncio
async def test_gem_reads_offices_departments_and_rejects_foreign_links() -> None:
    respx.get(GEM).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "am9i1", "title": "Spacecraft Intern",
                 "absolute_url": "https://jobs.gem.com/astroforge-io/am9i1",
                 "offices": [{"location": {"name": "Seal Beach, United States"}}],
                 "departments": [{"name": "Structures"}], "location_type": "in_office",
                 "first_published_at": "2025-09-08T19:23:35.946Z"},
                {"id": "am9i2", "title": "Elsewhere", "absolute_url": "https://evil.example/x"},
            ],
        )
    )

    result = await collect(GemAdapter(), source("gem", GEM))

    assert not result.complete and [job.source_job_id for job in result.jobs] == ["am9i1"]
    job = result.jobs[0]
    assert job.locations == ["Seal Beach, United States"] and job.department == "Structures"
    assert job.workplace_type == "onsite"


# ---- BrassRing -------------------------------------------------------------------------------

BRASSRING = "https://sjobs.brassring.com/TGnewUI/Search/Home/Home?partnerid=25539&siteid=5313"
BRASSRING_HOME = (
    '<div ng-include="\'LoginCaptchaTemplate\'"></div>'
    '<input name="__RequestVerificationToken" type="hidden" value="rft-1" />'
    "&quot;LocationCustomSolrFields&quot;:&quot;FORMTEXT4,FORMTEXT5&quot;"
)


def brassring_job(req: str, title: str) -> dict[str, Any]:
    answers = {"reqid": req, "jobtitle": title, "department": "General Atomics Aeronautical ",
               "formtext4": "California ", "formtext5": "Poway", "lastupdated": "21-Oct-2026",
               "autoreq": f"{req}BR"}
    return {"Questions": [{"QuestionName": k, "Value": v} for k, v in answers.items()]}


@respx.mock
@pytest.mark.asyncio
async def test_brassring_reads_its_home_page_token_and_walks_in_title_order() -> None:
    respx.get(BRASSRING).mock(return_value=httpx.Response(200, text=BRASSRING_HOME, headers=HTML))
    search = respx.post(
        "https://sjobs.brassring.com/TgNewUI/Search/Ajax/ProcessSortAndShowMoreJobs"
    ).mock(
        side_effect=[
            httpx.Response(200, json={"Jobs": {"Job": [brassring_job("5237639", "Engineer I"),
                                                       brassring_job("5237640", "Intern")]},
                                      "JobsCount": 3}),
            httpx.Response(200, json={"Jobs": {"Job": [brassring_job("5237641", "Machinist")]},
                                      "JobsCount": 3}),
        ]
    )

    result = validate_collection(await collect(BrassRingAdapter(), source("brassring", BRASSRING)))

    assert [job.source_job_id for job in result.jobs] == ["5237639", "5237640", "5237641"]
    first = result.jobs[0]
    assert first.locations == ["Poway, California"]
    assert first.department == "General Atomics Aeronautical"
    assert first.requisition_id == "5237639BR"
    assert first.source_published_at == datetime(2026, 10, 21, tzinfo=UTC)
    bodies = [json.loads(call.request.content) for call in search.calls]
    assert [body["pageNumber"] for body in bodies] == [1, 2]
    assert {body["SortType"] for body in bodies} == {"JobTitle"}
    assert search.calls[0].request.headers["RFT"] == "rft-1"


# ---- Teamtailor ------------------------------------------------------------------------------

TEAMTAILOR = "https://starship.teamtailor.com/jobs.rss"


@respx.mock
@pytest.mark.asyncio
async def test_teamtailor_reads_the_feed_items() -> None:
    feed = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:tt="https://teamtailor.com/locations"><channel><title>Starship</title>
<item><title>Robot Technician</title><pubDate>Thu, 24 Sep 2026 12:51:01 +0300</pubDate>
<link>https://starship.teamtailor.com/jobs/8447974-robot-technician</link>
<remoteStatus>onsite</remoteStatus><tt:locations><tt:location><tt:city>Hamburg</tt:city>
<tt:country>Germany</tt:country></tt:location></tt:locations>
<tt:department>Operations</tt:department></item></channel></rss>"""
    respx.get(TEAMTAILOR).mock(return_value=httpx.Response(200, text=feed))

    result = validate_collection(
        await collect(TeamtailorAdapter(), source("teamtailor", TEAMTAILOR))
    )

    (job,) = result.jobs
    assert job.source_job_id == "8447974" and job.locations == ["Hamburg, Germany"]
    assert job.department == "Operations" and job.workplace_type == "onsite"
    assert job.source_published_at == datetime(2026, 9, 24, 9, 51, 1, tzinfo=UTC)


# ---- ADP Workforce Now -----------------------------------------------------------------------

WFN = (
    "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
    "?cid=f31e80b7-a166-4f1d-947b-e1fbe422313d"
)


@respx.mock
@pytest.mark.asyncio
async def test_adp_workforce_now_walks_to_the_total_number() -> None:
    respx.get(url__startswith="https://workforcenow.adp.com/mascsr/default/careercenter/").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobRequisitions": [
                    {"itemID": "9201792244769_1", "requisitionTitle": "Robotics Intern",
                     "clientRequisitionID": "1292", "postDate": "2026-09-23T17:08:00.000-04:00",
                     "workLevelCode": {"shortName": "Intern"},
                     "requisitionLocations": [{"nameCode": {"shortName": " San Jose, CA, US"}}]}
                ],
                "meta": {"totalNumber": 1},
            },
        )
    )

    result = validate_collection(await collect(AdpWorkforceNowAdapter(), source("adp-wfn", WFN)))

    (job,) = result.jobs
    assert job.locations == ["San Jose, CA, US"] and job.requisition_id == "1292"
    assert job.employment_type == "Intern"
    assert "jobId=9201792244769_1" in str(job.posting_url)


# ---- Browser relay ---------------------------------------------------------------------------

RELAY = "https://raw.githubusercontent.com/eunyulw/job-alert/tesla-feed/parker.json"
NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


def relay_feed(*jobs: dict[str, Any], declared: int | None = None, age: float = 0.5) -> Any:
    return {
        "schema": 1,
        "board": "parker",
        "generated_at": (NOW - timedelta(hours=age)).isoformat().replace("+00:00", "Z"),
        "declared_total": len(jobs) if declared is None else declared,
        "jobs": list(jobs),
    }


PARKER_JOB = {
    "id": "18379018",
    "title": "Engineering Intern - Summer 2027",
    "location": "MARYSVILLE, OH, United States",
    "url": "https://parkercareers.ttcportals.com/jobs/18379018-engineering-intern-summer-2027",
    "posted": "2026-10-20T00:00:00.000Z",
}


async def relay(document: Any, **parameters: object) -> CollectionResult:
    respx.get(RELAY).mock(return_value=httpx.Response(200, json=document))
    config = source(
        "browser-relay", RELAY, board="parker", site="parkercareers.ttcportals.com", **parameters
    )
    async with HttpFetcher() as fetcher:
        return await BrowserRelayAdapter(now=lambda: NOW).collect(config, fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_browser_relay_reads_the_relayed_board() -> None:
    result = validate_collection(await relay(relay_feed(PARKER_JOB)))

    (job,) = result.jobs
    assert job.canonical_job_key.startswith("browser-relay:parker:")
    assert job.locations == ["MARYSVILLE, OH, United States"]
    assert job.source_published_at == datetime(2026, 10, 20, tzinfo=UTC)


@respx.mock
@pytest.mark.asyncio
async def test_browser_relay_fails_a_stale_feed() -> None:
    with pytest.raises(AdapterError, match="Chrome relay has not run since"):
        await relay(relay_feed(PARKER_JOB, age=30))


@respx.mock
@pytest.mark.asyncio
async def test_browser_relay_short_of_its_declared_total_is_incomplete() -> None:
    result = await relay(relay_feed(PARKER_JOB, declared=2))

    assert not result.complete


@respx.mock
@pytest.mark.asyncio
async def test_browser_relay_drops_a_link_off_the_site() -> None:
    foreign = {**PARKER_JOB, "url": "https://evil.example/jobs/1"}

    result = await relay(relay_feed(foreign))

    assert not result.complete and result.jobs == []


@respx.mock
@pytest.mark.asyncio
async def test_browser_relay_refuses_another_boards_feed() -> None:
    with pytest.raises(AdapterError, match="names another board"):
        await relay({**relay_feed(PARKER_JOB), "board": "tesla"})
