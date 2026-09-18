"""Offline tests for the Lever Postings adapter.

Lever publishes one JSON array per page, so these tests pin three things beyond the field mapping:
a collection stops on the first short page, the next request advances ``skip`` by the postings it
already read, and a page that repeats an earlier page's job ids is reported as an incomplete
collection instead of being followed forever.
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

from job_alert.adapters import lever as lever_module
from job_alert.adapters.base import AdapterError, PartialCollectionError, validate_collection
from job_alert.adapters.lever import DEFAULT_PAGE_SIZE, LeverAdapter
from job_alert.fetch import ChallengeDetected, HttpFetcher
from job_alert.identity import content_hash
from job_alert.models import SourceClass, SourceConfig, SupportStatus
from tests.helpers import assert_normalized_job, assert_repeat_is_stable

FIXTURE = Path("tests/fixtures/lever/postings.json")
SITE = "example"
ENDPOINT = f"https://api.lever.co/v0/postings/{SITE}"
NOW = datetime(2026, 9, 18, tzinfo=UTC)

CAPTURED_JOBS = (
    "78d6f6d5-1f08-4d5d-87be-c4250567bfb5",
    "26d453d0-f9df-4396-a655-70f6dbceb328",
)


def fixture_payload() -> list[dict[str, object]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def fixture_job(job_id: str) -> dict[str, object]:
    """A captured Lever posting, relabeled with a compact id so page expectations stay readable."""
    record = deepcopy(fixture_payload()[int(job_id) - 1])
    record["id"] = job_id
    return record


def record(job_id: str = "1", **overrides: object) -> dict[str, object]:
    """A realistic Lever posting record, with fields overridable per test."""
    posting: dict[str, object] = {
        "id": job_id,
        "text": "Software Engineer",
        "categories": {
            "commitment": "Full-time",
            "department": "Engineering",
            "location": "Chicago, IL",
            "team": "Platform",
            "allLocations": ["Chicago, IL", "Austin, TX"],
        },
        "country": "US",
        "descriptionPlain": "Build job alerts for every supported provider.",
        "hostedUrl": f"https://jobs.lever.co/example/{job_id}",
        "applyUrl": f"https://jobs.lever.co/example/{job_id}/apply",
        "workplaceType": "hybrid",
        "createdAt": 1758000000000,
    }
    posting.update(overrides)
    return posting


def make_lever_source(**overrides: object) -> SourceConfig:
    values: dict[str, object] = {
        "id": "example-lever",
        "company_id": "example",
        "provider": "lever",
        "source_class": SourceClass.DOCUMENTED_API,
        "support_status": SupportStatus.SUPPORTED,
        "endpoint": ENDPOINT,
        "interval_minutes": 5,
        "tenant": SITE,
    }
    return SourceConfig.model_validate({**values, **overrides})


def fixed_now() -> datetime:
    return NOW


def adapter() -> LeverAdapter:
    return LeverAdapter(now=fixed_now)


def page(*records: object) -> httpx.Response:
    return httpx.Response(200, json=list(records))


def request_params(request: httpx.Request) -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(str(request.url)).query, keep_blank_values=True)


def test_lever_fixture_keeps_two_captured_postings_with_one_synthetic_timestamp_removal() -> None:
    records = fixture_payload()

    assert [posting["id"] for posting in records] == list(CAPTURED_JOBS)
    for posting in records:
        for field in (
            "text",
            "categories",
            "country",
            "descriptionPlain",
            "hostedUrl",
            "applyUrl",
            "workplaceType",
        ):
            assert field in posting

    assert "createdAt" in records[0]
    assert "createdAt" not in records[1]
    categories = records[0]["categories"]
    assert isinstance(categories, dict)
    # The captured board repeats the display location inside ``allLocations``.
    assert categories["allLocations"][0] == categories["location"]


@respx.mock
@pytest.mark.asyncio
async def test_lever_collects_until_empty_page_and_deduplicates_locations() -> None:
    first_page = [fixture_job("1"), fixture_job("2")]
    respx.get(
        "https://api.lever.co/v0/postings/example",
        params={"mode": "json", "skip": "0", "limit": "2"},
    ).mock(return_value=httpx.Response(200, json=first_page))
    respx.get(
        "https://api.lever.co/v0/postings/example",
        params={"mode": "json", "skip": "2", "limit": "2"},
    ).mock(return_value=httpx.Response(200, json=[]))
    source = make_lever_source(parameters={"page_size": 2})
    async with HttpFetcher() as fetcher:
        result = await LeverAdapter(now=fixed_now).collect(source, fetcher)
    assert result.complete is True
    assert result.page_count == 2
    assert [job.source_job_id for job in result.jobs] == ["1", "2"]

    assert result.jobs[0].locations == [
        "Palo Alto, CA",
        "Seattle, WA",
        "San Francisco, CA",
        "New York City, NY",
    ]
    assert result.jobs[0].first_seen_at == NOW
    assert result.jobs[1].source_published_at is None


@respx.mock
@pytest.mark.asyncio
async def test_lever_maps_the_captured_board_into_normalized_jobs() -> None:
    captured = fixture_payload()
    route = respx.get(ENDPOINT).mock(side_effect=[page(fixture_job("1"), fixture_job("2")), page()])

    async with HttpFetcher() as fetcher:
        result = validate_collection(await adapter().collect(make_lever_source(), fetcher))

    first, second = result.jobs
    assert result.source_id == "example-lever"
    assert result.page_count == 2
    assert result.declared_total is None
    assert result.warnings == []
    assert route.call_count == 2

    assert first.source_job_id == "1"
    assert first.canonical_job_key == "lever:example:1"
    assert first.dedupe_key == (
        "url:example:https://jobs.lever.co/wealthfront/78d6f6d5-1f08-4d5d-87be-c4250567bfb5/apply"
    )
    assert first.title == "Android Engineer"
    assert first.locations == [
        "Palo Alto, CA",
        "Seattle, WA",
        "San Francisco, CA",
        "New York City, NY",
    ]
    assert first.department == "Engineering"
    assert first.team == "Engineering - Android"
    assert first.employment_type == "Full-time"
    assert first.workplace_type == "hybrid"
    assert first.description_text == captured[0]["descriptionPlain"]
    assert first.content_hash == content_hash(first.title, first.locations, first.description_text)
    assert str(first.posting_url) == captured[0]["hostedUrl"]
    assert str(first.apply_url) == captured[0]["applyUrl"]
    assert first.source_published_at == datetime(2023, 9, 11, 20, 23, 16, 9000, tzinfo=UTC)
    assert first.source_updated_at is None
    assert first.first_seen_at == NOW
    assert first.raw_metadata["country"] == "US"
    assert first.raw_metadata["categories"] == captured[0]["categories"]

    assert second.source_job_id == "2"
    assert second.canonical_job_key == "lever:example:2"
    assert second.title == "Anti-Money Laundering Monitoring Analyst"
    assert second.locations == ["Palo Alto, CA (Open to US-based Remote)"]
    assert second.department == "Legal and Compliance"
    assert second.team == "Compliance"
    assert second.employment_type == "Full-time"
    assert second.workplace_type == "remote"
    assert second.source_published_at is None

    for job in result.jobs:
        assert_normalized_job(job)


@respx.mock
@pytest.mark.asyncio
async def test_lever_repeats_the_same_pages_as_stable_normalized_jobs() -> None:
    board = [fixture_job("1"), fixture_job("2")]
    respx.get(ENDPOINT).mock(side_effect=[page(*board), page(), page(*board), page()])
    source = make_lever_source(parameters={"page_size": 2})

    async with HttpFetcher() as fetcher:
        first = await adapter().collect(source, fetcher)
        second = await adapter().collect(source, fetcher)

    assert first.complete is True
    assert_repeat_is_stable(first, second)
    for job in first.jobs:
        assert_normalized_job(job)


@respx.mock
@pytest.mark.asyncio
async def test_lever_collects_past_short_pages_when_the_server_caps_the_response() -> None:
    route = respx.get(ENDPOINT).mock(
        side_effect=[
            page(record("1")),
            page(record("2"), record("3")),
            page(record("4")),
            page(),
        ]
    )
    source = make_lever_source(parameters={"page_size": 1000})

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is True
    assert [job.source_job_id for job in result.jobs] == ["1", "2", "3", "4"]
    assert result.page_count == 4
    assert [request_params(call.request) for call in route.calls] == [
        [("mode", "json"), ("skip", "0"), ("limit", "1000")],
        [("mode", "json"), ("skip", "1"), ("limit", "1000")],
        [("mode", "json"), ("skip", "3"), ("limit", "1000")],
        [("mode", "json"), ("skip", "4"), ("limit", "1000")],
    ]


@respx.mock
@pytest.mark.asyncio
async def test_lever_advances_skip_by_the_postings_it_already_read() -> None:
    route = respx.get(ENDPOINT).mock(
        side_effect=[page(record("1"), record("2")), page(record("3")), page()]
    )
    source = make_lever_source(parameters={"page_size": 2})

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert [job.source_job_id for job in result.jobs] == ["1", "2", "3"]
    assert result.page_count == 3
    assert route.call_count == 3
    assert [request_params(call.request) for call in route.calls] == [
        [("mode", "json"), ("skip", "0"), ("limit", "2")],
        [("mode", "json"), ("skip", "2"), ("limit", "2")],
        [("mode", "json"), ("skip", "3"), ("limit", "2")],
    ]


@respx.mock
@pytest.mark.asyncio
async def test_lever_uses_the_default_page_size_when_the_source_sets_none() -> None:
    route = respx.get(ENDPOINT).mock(side_effect=[page(record("1")), page()])

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_lever_source(), fetcher)

    assert result.complete is True
    assert request_params(route.calls[0].request) == [
        ("mode", "json"),
        ("skip", "0"),
        ("limit", str(DEFAULT_PAGE_SIZE)),
    ]
    assert route.call_count == 2
    assert request_params(route.calls[1].request) == [
        ("mode", "json"),
        ("skip", "1"),
        ("limit", str(DEFAULT_PAGE_SIZE)),
    ]


@respx.mock
@pytest.mark.asyncio
async def test_lever_owns_mode_skip_and_limit_but_keeps_other_endpoint_parameters() -> None:
    route = respx.get(ENDPOINT).mock(return_value=page())
    source = make_lever_source(endpoint=f"{ENDPOINT}?team=Platform&mode=html&skip=9&limit=1")

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is True
    params = request_params(route.calls[0].request)
    assert dict(params) == {
        "team": "Platform",
        "mode": "json",
        "skip": "0",
        "limit": str(DEFAULT_PAGE_SIZE),
    }
    for name in ("mode", "skip", "limit"):
        assert [key for key, _ in params].count(name) == 1


@respx.mock
@pytest.mark.asyncio
async def test_lever_reports_a_repeated_page_id_set_as_an_incomplete_collection() -> None:
    route = respx.get(ENDPOINT).mock(
        side_effect=[
            page(fixture_job("1"), fixture_job("2")),
            page(fixture_job("2"), fixture_job("1")),
        ]
    )
    source = make_lever_source(parameters={"page_size": 2})

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is False
    assert result.page_count == 2
    assert route.call_count == 2
    assert [job.source_job_id for job in result.jobs] == ["1", "2"]
    assert result.warnings == [
        "pagination loop detected at page 2: the page repeated an earlier page's job ids, so the "
        "collection is incomplete"
    ]
    with pytest.raises(PartialCollectionError, match="incomplete collection"):
        validate_collection(result)


@respx.mock
@pytest.mark.asyncio
async def test_lever_marks_a_job_id_repeated_across_pages_incomplete() -> None:
    route = respx.get(ENDPOINT).mock(
        side_effect=[
            page(record("1"), record("2")),
            page(record("3"), record("2")),
            page(),
        ]
    )
    source = make_lever_source(parameters={"page_size": 2})

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is False
    assert result.page_count == 3
    assert route.call_count == 3
    assert [job.source_job_id for job in result.jobs] == ["1", "2", "3"]
    assert result.warnings == [
        "skipped duplicate job record at index 3: the source job id already appeared"
    ]


@respx.mock
@pytest.mark.asyncio
async def test_lever_stops_at_the_page_cap_without_reporting_the_collection_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lever_module, "MAX_PAGES", 2)
    route = respx.get(ENDPOINT).mock(
        side_effect=[page(record("1"), record("2")), page(record("3"), record("4"))]
    )
    source = make_lever_source(parameters={"page_size": 2})

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is False
    assert result.page_count == 2
    assert route.call_count == 2
    assert [job.source_job_id for job in result.jobs] == ["1", "2", "3", "4"]
    assert result.warnings == [
        "stopped after 2 pages: the page cap is reached, so the collection may be incomplete"
    ]


@respx.mock
@pytest.mark.asyncio
async def test_lever_accepts_an_empty_board() -> None:
    respx.get(ENDPOINT).mock(return_value=page())

    async with HttpFetcher() as fetcher:
        result = validate_collection(await adapter().collect(make_lever_source(), fetcher))

    assert result.complete is True
    assert result.jobs == []
    assert result.page_count == 1
    assert result.declared_total is None
    assert result.warnings == []


@respx.mock
@pytest.mark.asyncio
async def test_lever_marks_an_unusable_record_incomplete_but_keeps_the_page() -> None:
    respx.get(ENDPOINT).mock(side_effect=[page(record("1"), record("2", text=None)), page()])

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_lever_source(), fetcher)

    assert result.complete is False
    assert [job.source_job_id for job in result.jobs] == ["1"]
    assert result.warnings == ["skipped malformed job record at index 1: missing title"]
    with pytest.raises(PartialCollectionError, match="incomplete collection"):
        validate_collection(result)


@respx.mock
@pytest.mark.asyncio
async def test_lever_rejects_records_without_usable_urls_or_a_publish_timestamp() -> None:
    respx.get(ENDPOINT).mock(
        side_effect=[
            page(record("1", hostedUrl=None)),
            page(),
            page(record("1", applyUrl="http://jobs.lever.co/example/1/apply")),
            page(),
            page(record("1", createdAt="yesterday")),
            page(),
        ]
    )

    async with HttpFetcher() as fetcher:
        missing = await adapter().collect(make_lever_source(), fetcher)
        insecure = await adapter().collect(make_lever_source(), fetcher)
        bad_timestamp = await adapter().collect(make_lever_source(), fetcher)

    assert missing.complete is False
    assert missing.jobs == []
    assert missing.warnings == ["skipped malformed job record at index 0: missing hostedUrl"]
    assert insecure.complete is False
    assert insecure.jobs == []
    assert insecure.warnings == [
        "skipped malformed job record at index 0: applyUrl is not an absolute https URL"
    ]
    assert bad_timestamp.complete is False
    assert bad_timestamp.jobs == []
    assert bad_timestamp.warnings == [
        "skipped malformed job record at index 0: createdAt is not a millisecond epoch timestamp"
    ]


@respx.mock
@pytest.mark.asyncio
async def test_lever_hashes_a_posting_without_a_description_or_categories() -> None:
    respx.get(ENDPOINT).mock(
        side_effect=[
            page(record("1", categories={}, descriptionPlain="", workplaceType=None)),
            page(),
        ]
    )

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_lever_source(), fetcher)

    job = result.jobs[0]
    assert job.description_text is None
    assert job.locations == []
    assert job.department is None
    assert job.team is None
    assert job.employment_type is None
    assert job.workplace_type is None
    assert job.content_hash == content_hash(job.title, [], None)
    assert job.dedupe_key == "url:example:https://jobs.lever.co/example/1/apply"


@respx.mock
@pytest.mark.asyncio
async def test_lever_treats_a_challenge_page_as_an_error() -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="<html><title>Just a moment...</title></html>",
        )
    )

    async with HttpFetcher() as fetcher:
        with pytest.raises(ChallengeDetected):
            await adapter().collect(make_lever_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_lever_rejects_a_payload_that_is_not_json() -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200, headers={"Content-Type": "text/plain"}, text="<html>maintenance</html>"
        )
    )

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="is not JSON"):
            await adapter().collect(make_lever_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_lever_rejects_a_payload_that_is_not_a_json_array() -> None:
    respx.get(ENDPOINT).mock(
        side_effect=[
            httpx.Response(200, json={"postings": []}),
            httpx.Response(200, json={"ok": False, "error": "Document not found"}),
        ]
    )

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="is not a JSON array"):
            await adapter().collect(make_lever_source(), fetcher)
        with pytest.raises(AdapterError, match="is not a JSON array"):
            await adapter().collect(make_lever_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_lever_rejects_a_not_modified_response() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(304))

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="304"):
            await adapter().collect(make_lever_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_lever_sends_the_configured_source_headers() -> None:
    route = respx.get(ENDPOINT).mock(side_effect=[page(record("1")), page()])
    source = make_lever_source(
        headers={"Accept": "application/json", "X-Source-Audit": "2026-09-18"}
    )

    async with HttpFetcher() as fetcher:
        await adapter().collect(source, fetcher)

    sent = route.calls[0].request.headers
    assert sent["Accept"] == "application/json"
    assert sent["X-Source-Audit"] == "2026-09-18"


@respx.mock
@pytest.mark.asyncio
async def test_lever_falls_back_to_the_site_token_for_the_tenant() -> None:
    respx.get(ENDPOINT).mock(side_effect=[page(record("1")), page()])

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_lever_source(tenant=None), fetcher)

    assert result.jobs[0].canonical_job_key == "lever:example:1"


@respx.mock
@pytest.mark.asyncio
async def test_lever_rejects_a_source_that_names_no_site() -> None:
    source = make_lever_source(tenant=None, endpoint="https://careers.example.com/jobs")

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="tenant"):
            await adapter().collect(source, fetcher)


@pytest.mark.asyncio
@pytest.mark.parametrize("page_size", [0, -1, "2", True, 2.5])
async def test_lever_rejects_an_unusable_page_size(page_size: object) -> None:
    source = make_lever_source(parameters={"page_size": page_size})

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="page_size"):
            await adapter().collect(source, fetcher)
