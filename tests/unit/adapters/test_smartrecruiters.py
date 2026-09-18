"""Offline tests for the SmartRecruiters Postings adapter.

SmartRecruiters answers one JSON page per request and publishes the board's own size as
``totalFound``, so these tests pin four things beyond the field mapping: the walk stops only when
the unique posting ids it has read reach that total (or the page is empty), a page shorter than the
requested ``limit`` never ends the walk, a repeated page is a loop rather than an ending, and a
final unique count that disagrees with ``totalFound`` is an incomplete collection instead of a
complete one.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
import respx

from job_alert.adapters import smartrecruiters as smartrecruiters_module
from job_alert.adapters.base import AdapterError, PartialCollectionError, validate_collection
from job_alert.adapters.smartrecruiters import DEFAULT_PAGE_SIZE, SmartRecruitersAdapter
from job_alert.fetch import ChallengeDetected, HttpFetcher
from job_alert.identity import content_hash
from job_alert.models import SourceClass, SourceConfig, SupportStatus
from tests.helpers import assert_normalized_job, assert_repeat_is_stable

FIXTURE_DIR = Path("tests/fixtures/smartrecruiters")
PAGE_1 = FIXTURE_DIR / "postings-page-1.json"
PAGE_2 = FIXTURE_DIR / "postings-page-2.json"

COMPANY = "Ubisoft2"
ENDPOINT = f"https://api.smartrecruiters.com/v1/companies/{COMPANY}/postings"
NOW = datetime(2026, 9, 18, tzinfo=UTC)

# The four postings of the two captured pages, in board order.
CAPTURED_POSTING_IDS = (
    "744000150454779",
    "744000150453690",
    "744000150333355",
    "744000150335344",
)


def fixture_page(path: Path) -> dict[str, object]:
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def fixture_records(path: Path) -> list[dict[str, object]]:
    content = fixture_page(path)["content"]
    assert isinstance(content, list)
    return [record for record in content if isinstance(record, dict)]


def fixture_record(path: Path, index: int) -> dict[str, object]:
    """A captured SmartRecruiters posting, kept verbatim for the mapping assertions."""
    return deepcopy(fixture_records(path)[index])


def record(job_id: str = "744000150000001", **overrides: object) -> dict[str, object]:
    """A realistic SmartRecruiters posting record, with fields overridable per test."""
    posting: dict[str, object] = {
        "id": job_id,
        "name": "Software Engineer",
        "uuid": "0f8fad5b-d9cb-469f-a165-70867728950e",
        "jobAdId": "6e2a0f1e-6c6a-4d18-9a2e-1d0e6f4c9b71",
        "defaultJobAd": True,
        "refNumber": f"REF{job_id[-4:]}",
        "company": {"identifier": COMPANY, "name": "Ubisoft"},
        "releasedDate": "2026-09-18T20:43:37.105Z",
        "location": {
            "city": "Montreal",
            "region": "QC",
            "country": "ca",
            "remote": False,
            "hybrid": False,
            "latitude": "45.5018869",
            "longitude": "-73.56739189999999",
            "fullLocation": "Montreal, QC, Canada",
        },
        "industry": {"id": "entertainment", "label": "Entertainment"},
        "department": {"id": "13847731", "label": "Ubisoft"},
        "function": {"id": "engineering", "label": "Engineering"},
        "typeOfEmployment": {"id": "permanent", "label": "Full-time"},
        "experienceLevel": {"id": "mid_senior_level", "label": "Mid-Senior Level"},
        "customField": [],
        "visibility": "PUBLIC",
        "ref": f"https://api.smartrecruiters.com/v1/companies/{COMPANY}/postings/{job_id}",
        "language": {"code": "en", "label": "English", "labelNative": "English (US)"},
    }
    posting.update(overrides)
    return posting


def page(
    *records: object,
    offset: int = 0,
    limit: int | None = None,
    total: int | None = None,
) -> httpx.Response:
    """One SmartRecruiters page envelope; ``total`` defaults to the page's own record count."""
    return httpx.Response(
        200,
        json={
            "offset": offset,
            "limit": len(records) if limit is None else limit,
            "totalFound": len(records) if total is None else total,
            "content": list(records),
        },
    )


def make_smartrecruiters_source(**overrides: object) -> SourceConfig:
    values: dict[str, object] = {
        "id": "ubisoft-smartrecruiters",
        "company_id": "ubisoft",
        "provider": "smartrecruiters",
        "source_class": SourceClass.DOCUMENTED_API,
        "support_status": SupportStatus.SUPPORTED,
        "endpoint": ENDPOINT,
        "interval_minutes": 5,
        "tenant": COMPANY,
    }
    return SourceConfig.model_validate({**values, **overrides})


def fixed_now() -> datetime:
    return NOW


def adapter() -> SmartRecruitersAdapter:
    return SmartRecruitersAdapter(now=fixed_now)


def request_params(request: httpx.Request) -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(str(request.url)).query, keep_blank_values=True)


def test_smartrecruiters_fixture_keeps_two_captured_pages_of_one_board() -> None:
    first = fixture_page(PAGE_1)
    second = fixture_page(PAGE_2)

    assert (first["offset"], first["limit"], first["totalFound"]) == (0, 2, 300)
    assert (second["offset"], second["limit"], second["totalFound"]) == (2, 2, 300)
    assert [item["id"] for item in first["content"]] == list(CAPTURED_POSTING_IDS[:2])
    assert [item["id"] for item in second["content"]] == list(CAPTURED_POSTING_IDS[2:])

    captured = fixture_records(PAGE_1) + fixture_records(PAGE_2)
    for item in captured:
        for field in (
            "id",
            "name",
            "refNumber",
            "releasedDate",
            "location",
            "department",
            "typeOfEmployment",
        ):
            assert field in item
        # The live list endpoint publishes no public posting URL at all, which is why the adapter
        # derives one from the company identifier and the posting id.
        assert "postingUrl" not in item
        assert "applyUrl" not in item


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_walks_two_pages_of_three_jobs_and_maps_them() -> None:
    captured = fixture_records(PAGE_1)
    third = fixture_record(PAGE_2, 0)
    route = respx.get(ENDPOINT).mock(
        side_effect=[
            page(captured[0], captured[1], offset=0, limit=2, total=3),
            page(third, offset=2, limit=2, total=3),
        ]
    )
    source = make_smartrecruiters_source(parameters={"page_size": 2})

    async with HttpFetcher() as fetcher:
        result = validate_collection(await adapter().collect(source, fetcher))

    assert result.source_id == "ubisoft-smartrecruiters"
    assert result.complete is True
    assert result.warnings == []
    assert result.page_count == 2
    assert result.declared_total == 3
    assert route.call_count == 2
    assert [request_params(call.request) for call in route.calls] == [
        [("offset", "0"), ("limit", "2")],
        [("offset", "2"), ("limit", "2")],
    ]
    assert [job.source_job_id for job in result.jobs] == list(CAPTURED_POSTING_IDS[:3])

    first, second, third_job = result.jobs
    assert first.canonical_job_key == f"smartrecruiters:{COMPANY}:{CAPTURED_POSTING_IDS[0]}"
    assert first.dedupe_key == "requisition:ubisoft:REF31466O"
    assert first.requisition_id == "REF31466O"
    assert first.title == "Team Lead Level Designer [Splinter Cell Remake]"
    assert first.locations == ["Toronto, ON, CA"]
    assert first.department == "Ubisoft"
    assert first.employment_type == "Full-time"
    assert first.workplace_type is None
    assert first.team is None
    # The list endpoint publishes no job ad, so the hash covers the fields it does publish.
    assert first.description_text is None
    assert first.content_hash == content_hash(first.title, first.locations, None)
    assert str(first.posting_url) == f"https://jobs.smartrecruiters.com/{COMPANY}/744000150454779"
    assert str(first.apply_url) == f"https://jobs.smartrecruiters.com/{COMPANY}/744000150454779"
    assert first.source_published_at == datetime(2026, 9, 18, 20, 43, 37, 105000, tzinfo=UTC)
    assert first.source_updated_at is None
    assert first.first_seen_at == NOW
    assert first.raw_metadata["location"] == captured[0]["location"]
    assert first.raw_metadata["visibility"] == "PUBLIC"
    assert first.raw_metadata["language_code"] == captured[0]["language"]["code"]

    assert second.locations == ["Montreal, QC, CA"]
    assert second.requisition_id == "REF31908V"
    assert third_job.locations == ["Newcastle upon Tyne, England, GB"]
    assert third_job.source_published_at == datetime(2026, 9, 18, 10, 29, 48, 227000, tzinfo=UTC)

    for job in result.jobs:
        assert_normalized_job(job)


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_repeats_the_same_pages_as_stable_normalized_jobs() -> None:
    walk = [
        page(record("1"), record("2"), offset=0, limit=2, total=3),
        page(record("3"), offset=2, limit=2, total=3),
    ]
    respx.get(ENDPOINT).mock(side_effect=walk + walk)
    source = make_smartrecruiters_source(parameters={"page_size": 2})

    async with HttpFetcher() as fetcher:
        first = await adapter().collect(source, fetcher)
        second = await adapter().collect(source, fetcher)

    assert first.complete is True
    assert first.declared_total == 3
    assert_repeat_is_stable(first, second)
    for job in first.jobs:
        assert_normalized_job(job)


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_collects_past_a_short_page_when_the_server_caps_it() -> None:
    route = respx.get(ENDPOINT).mock(
        side_effect=[
            page(record("1"), offset=0, limit=1000, total=4),
            page(record("2"), record("3"), offset=1, limit=1000, total=4),
            page(record("4"), offset=3, limit=1000, total=4),
        ]
    )
    source = make_smartrecruiters_source(parameters={"page_size": 1000})

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is True
    assert [job.source_job_id for job in result.jobs] == ["1", "2", "3", "4"]
    assert result.page_count == 3
    assert [request_params(call.request) for call in route.calls] == [
        [("offset", "0"), ("limit", "1000")],
        [("offset", "1"), ("limit", "1000")],
        [("offset", "3"), ("limit", "1000")],
    ]


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_stops_when_the_unique_count_reaches_the_declared_total() -> None:
    route = respx.get(ENDPOINT).mock(return_value=page(record("1"), offset=0, total=1))

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_smartrecruiters_source(), fetcher)

    assert result.complete is True
    assert result.declared_total == 1
    assert result.page_count == 1
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_tracks_the_declared_total_of_the_newest_page() -> None:
    growing = respx.get(ENDPOINT).mock(
        side_effect=[
            page(record("1"), record("2"), offset=0, limit=2, total=3),
            page(record("3"), offset=2, limit=2, total=4),
            page(record("4"), offset=3, limit=2, total=4),
            page(record("1"), record("2"), offset=0, limit=2, total=4),
            page(record("3"), offset=2, limit=2, total=3),
        ]
    )
    source = make_smartrecruiters_source(parameters={"page_size": 2})

    async with HttpFetcher() as fetcher:
        grown = await adapter().collect(source, fetcher)
        shrunk = await adapter().collect(source, fetcher)

    assert grown.complete is True
    assert grown.declared_total == 4
    assert [job.source_job_id for job in grown.jobs] == ["1", "2", "3", "4"]
    assert [request_params(call.request) for call in growing.calls[:3]] == [
        [("offset", "0"), ("limit", "2")],
        [("offset", "2"), ("limit", "2")],
        [("offset", "3"), ("limit", "2")],
    ]

    assert shrunk.complete is True
    assert shrunk.declared_total == 3
    assert [job.source_job_id for job in shrunk.jobs] == ["1", "2", "3"]
    assert shrunk.page_count == 2
    assert len(growing.calls) == 5


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_marks_a_total_count_mismatch_incomplete() -> None:
    route = respx.get(ENDPOINT).mock(
        side_effect=[
            page(record("1"), record("2"), offset=0, limit=2, total=3),
            page(offset=2, limit=2, total=3),
        ]
    )
    source = make_smartrecruiters_source(parameters={"page_size": 2})

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is False
    assert result.page_count == 2
    assert route.call_count == 2
    assert [job.source_job_id for job in result.jobs] == ["1", "2"]
    assert result.declared_total == 3
    assert result.warnings == [
        "collected 2 unique postings but the board declared totalFound=3, so the collection is "
        "incomplete"
    ]
    with pytest.raises(PartialCollectionError, match="incomplete collection"):
        validate_collection(result)


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_marks_more_postings_than_the_declared_total_incomplete() -> None:
    respx.get(ENDPOINT).mock(return_value=page(record("1"), record("2"), total=1))

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_smartrecruiters_source(), fetcher)

    assert result.complete is False
    assert result.page_count == 1
    assert result.declared_total == 1
    assert result.warnings == [
        "collected 2 unique postings but the board declared totalFound=1, so the collection is "
        "incomplete"
    ]


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_reports_a_repeated_page_id_set_as_an_incomplete_collection() -> None:
    route = respx.get(ENDPOINT).mock(
        side_effect=[
            page(record("1"), record("2"), offset=0, limit=2, total=4),
            page(record("2"), record("1"), offset=2, limit=2, total=4),
        ]
    )
    source = make_smartrecruiters_source(parameters={"page_size": 2})

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is False
    assert result.page_count == 2
    assert route.call_count == 2
    assert [job.source_job_id for job in result.jobs] == ["1", "2"]
    assert result.warnings == [
        "pagination loop detected at page 2: the page repeated an earlier page's job ids, so the "
        "collection is incomplete",
        "collected 2 unique postings but the board declared totalFound=4, so the collection is "
        "incomplete",
    ]
    with pytest.raises(PartialCollectionError, match="incomplete collection"):
        validate_collection(result)


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_marks_a_posting_id_repeated_across_pages_incomplete() -> None:
    route = respx.get(ENDPOINT).mock(
        side_effect=[
            page(record("1"), record("2"), offset=0, limit=2, total=3),
            page(record("3"), record("2"), offset=2, limit=2, total=3),
        ]
    )
    source = make_smartrecruiters_source(parameters={"page_size": 2})

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is False
    assert result.page_count == 2
    assert route.call_count == 2
    assert [job.source_job_id for job in result.jobs] == ["1", "2", "3"]
    assert result.warnings == [
        "skipped duplicate job record at index 3: the source job id already appeared"
    ]


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_stops_at_the_page_cap_without_reporting_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(smartrecruiters_module, "MAX_PAGES", 2)
    route = respx.get(ENDPOINT).mock(
        side_effect=[
            page(record("1"), record("2"), offset=0, limit=2, total=6),
            page(record("3"), record("4"), offset=2, limit=2, total=6),
        ]
    )
    source = make_smartrecruiters_source(parameters={"page_size": 2})

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is False
    assert result.page_count == 2
    assert route.call_count == 2
    assert [job.source_job_id for job in result.jobs] == ["1", "2", "3", "4"]
    assert result.warnings == [
        "stopped after 2 pages: the page cap is reached, so the collection may be incomplete",
        "collected 4 unique postings but the board declared totalFound=6, so the collection is "
        "incomplete",
    ]


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_accepts_an_empty_board() -> None:
    respx.get(ENDPOINT).mock(return_value=page(offset=0, limit=100, total=0))

    async with HttpFetcher() as fetcher:
        collected = await adapter().collect(make_smartrecruiters_source(), fetcher)
        result = validate_collection(collected)

    assert result.complete is True
    assert result.jobs == []
    assert result.page_count == 1
    assert result.declared_total == 0
    assert result.warnings == []


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_marks_an_unusable_record_incomplete_but_keeps_the_page() -> None:
    respx.get(ENDPOINT).mock(return_value=page(record("1"), record("2", name=None), total=2))

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_smartrecruiters_source(), fetcher)

    assert result.complete is False
    assert [job.source_job_id for job in result.jobs] == ["1"]
    assert result.declared_total == 2
    assert result.warnings == ["skipped malformed job record at index 1: missing title"]
    with pytest.raises(PartialCollectionError, match="incomplete collection"):
        validate_collection(result)


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_counts_a_record_that_cannot_name_itself_as_incomplete() -> None:
    respx.get(ENDPOINT).mock(
        side_effect=[
            page("not a record", record("2"), offset=0, limit=2, total=2),
            page(offset=2, limit=2, total=2),
        ]
    )
    source = make_smartrecruiters_source(parameters={"page_size": 2})

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is False
    assert result.page_count == 2
    assert [job.source_job_id for job in result.jobs] == ["2"]
    assert result.warnings == [
        "skipped malformed job record at index 0: record is not a JSON object",
        "collected 1 unique postings but the board declared totalFound=2, so the collection is "
        "incomplete",
    ]


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("released_date", "warning"),
    [
        ("yesterday", "releasedDate is not an ISO 8601 timestamp"),
        ("2026-09-18T20:43:37.105", "releasedDate has no UTC offset"),
        (20260918, "releasedDate is not an ISO 8601 timestamp"),
    ],
)
async def test_smartrecruiters_rejects_an_unusable_publish_timestamp(
    released_date: object, warning: str
) -> None:
    respx.get(ENDPOINT).mock(return_value=page(record("1", releasedDate=released_date), total=1))

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_smartrecruiters_source(), fetcher)

    assert result.complete is False
    assert result.jobs == []
    assert result.warnings == [f"skipped malformed job record at index 0: {warning}"]


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_derives_the_public_posting_url_from_the_company_identifier() -> None:
    respx.get(ENDPOINT).mock(return_value=page(record("744000150454779"), total=1))

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_smartrecruiters_source(tenant=None), fetcher)

    job = result.jobs[0]
    assert result.complete is True
    assert job.canonical_job_key == f"smartrecruiters:{COMPANY}:744000150454779"
    assert str(job.posting_url) == f"https://jobs.smartrecruiters.com/{COMPANY}/744000150454779"
    assert str(job.apply_url) == f"https://jobs.smartrecruiters.com/{COMPANY}/744000150454779"


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_prefers_published_posting_and_apply_urls() -> None:
    respx.get(ENDPOINT).mock(
        return_value=page(
            record(
                "1",
                postingUrl=f"https://jobs.smartrecruiters.com/{COMPANY}/1-software-engineer",
                applyUrl=f"https://jobs.smartrecruiters.com/{COMPANY}/1-software-engineer?oga=true",
            ),
            total=1,
        )
    )

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_smartrecruiters_source(), fetcher)

    job = result.jobs[0]
    assert str(job.posting_url) == f"https://jobs.smartrecruiters.com/{COMPANY}/1-software-engineer"
    assert str(job.apply_url) == (
        f"https://jobs.smartrecruiters.com/{COMPANY}/1-software-engineer?oga=true"
    )


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_rejects_a_published_url_that_is_not_usable() -> None:
    respx.get(ENDPOINT).mock(
        return_value=page(
            record("1", postingUrl=f"http://jobs.smartrecruiters.com/{COMPANY}/1"), total=1
        )
    )

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_smartrecruiters_source(), fetcher)

    assert result.complete is False
    assert result.jobs == []
    assert result.warnings == [
        "skipped malformed job record at index 0: postingUrl is not an absolute https URL"
    ]


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_normalizes_the_location_object_and_workplace_flags() -> None:
    respx.get(ENDPOINT).mock(
        side_effect=[
            page(
                record(
                    "1",
                    location={
                        "city": "Bucharest",
                        "region": "",
                        "country": "ro",
                        "hybrid": True,
                    },
                )
            ),
            page(
                record(
                    "2", location={"city": None, "region": None, "country": None, "remote": True}
                )
            ),
            page(record("3", location=None)),
            page(record("4", location={"city": "Toronto", "region": "ON", "country": "Canada"})),
        ]
    )

    async with HttpFetcher() as fetcher:
        hyphenated = await adapter().collect(make_smartrecruiters_source(), fetcher)
        remote = await adapter().collect(make_smartrecruiters_source(), fetcher)
        missing = await adapter().collect(make_smartrecruiters_source(), fetcher)
        named_country = await adapter().collect(make_smartrecruiters_source(), fetcher)

    assert hyphenated.jobs[0].locations == ["Bucharest, RO"]
    assert hyphenated.jobs[0].workplace_type == "hybrid"
    assert remote.jobs[0].locations == []
    assert remote.jobs[0].workplace_type == "remote"
    assert missing.jobs[0].locations == []
    assert missing.jobs[0].workplace_type is None
    # Only a two-letter ISO country code is upper-cased; a spelled-out country stays as published.
    assert named_country.jobs[0].locations == ["Toronto, ON, Canada"]


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_falls_back_to_a_url_key_without_a_ref_number() -> None:
    respx.get(ENDPOINT).mock(
        side_effect=[page(record("1", refNumber=None)), page(record("2", refNumber="  "))]
    )

    async with HttpFetcher() as fetcher:
        first = await adapter().collect(make_smartrecruiters_source(), fetcher)
        second = await adapter().collect(make_smartrecruiters_source(), fetcher)

    missing = first.jobs[0]
    blank = second.jobs[0]
    assert missing.requisition_id is None
    assert missing.dedupe_key == f"url:ubisoft:https://jobs.smartrecruiters.com/{COMPANY}/1"
    assert blank.requisition_id is None
    assert blank.dedupe_key == f"url:ubisoft:https://jobs.smartrecruiters.com/{COMPANY}/2"


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_treats_a_challenge_page_as_an_error() -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="<html><title>Just a moment...</title></html>",
        )
    )

    async with HttpFetcher() as fetcher:
        with pytest.raises(ChallengeDetected):
            await adapter().collect(make_smartrecruiters_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_rejects_a_payload_that_is_not_json() -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200, headers={"Content-Type": "text/plain"}, text="<html>maintenance</html>"
        )
    )

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="is not JSON"):
            await adapter().collect(make_smartrecruiters_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("document", "message"),
    [
        ([], "is not a JSON object"),
        ({"offset": 0, "limit": 2, "totalFound": 2}, "has no 'content' array"),
        ({"totalFound": 2, "content": {}}, "has no 'content' array"),
        ({"content": []}, "has no non-negative integer totalFound"),
        ({"totalFound": -1, "content": []}, "has no non-negative integer totalFound"),
        ({"totalFound": "3", "content": []}, "has no non-negative integer totalFound"),
        ({"totalFound": True, "content": []}, "has no non-negative integer totalFound"),
    ],
)
async def test_smartrecruiters_rejects_a_wrong_shaped_payload(
    document: object, message: str
) -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=document))

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match=message):
            await adapter().collect(make_smartrecruiters_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_rejects_a_not_modified_response() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(304))

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="304"):
            await adapter().collect(make_smartrecruiters_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_owns_offset_and_limit_but_keeps_other_parameters() -> None:
    route = respx.get(ENDPOINT).mock(return_value=page(offset=0, limit=100, total=0))
    source = make_smartrecruiters_source(endpoint=f"{ENDPOINT}?q=engineer&offset=9&limit=1")

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is True
    params = request_params(route.calls[0].request)
    assert dict(params) == {
        "q": "engineer",
        "offset": "0",
        "limit": str(DEFAULT_PAGE_SIZE),
    }
    for name in ("offset", "limit"):
        assert [key for key, _ in params].count(name) == 1


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_sends_the_configured_source_headers() -> None:
    route = respx.get(ENDPOINT).mock(return_value=page(offset=0, limit=100, total=0))
    source = make_smartrecruiters_source(
        headers={"Accept": "application/json", "X-Source-Audit": "2026-09-18"}
    )

    async with HttpFetcher() as fetcher:
        await adapter().collect(source, fetcher)

    sent = route.calls[0].request.headers
    assert sent["Accept"] == "application/json"
    assert sent["X-Source-Audit"] == "2026-09-18"


@respx.mock
@pytest.mark.asyncio
async def test_smartrecruiters_rejects_a_source_that_names_no_company() -> None:
    source = make_smartrecruiters_source(tenant=None, endpoint="https://careers.example.com/jobs")

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="tenant"):
            await adapter().collect(source, fetcher)


@pytest.mark.asyncio
@pytest.mark.parametrize("page_size", [0, -1, "2", True, 2.5])
async def test_smartrecruiters_rejects_an_unusable_page_size(page_size: object) -> None:
    source = make_smartrecruiters_source(parameters={"page_size": page_size})

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="page_size"):
            await adapter().collect(source, fetcher)
