"""Offline tests for the Ashby Job Postings adapter.

Ashby answers one board with a single JSON document, so these tests pin the captured field shapes
and the decisions the payload does not spell out: identity is the published record id, and a
collection is complete only after the response shape and its unique ids validate.

What the captured response says about the fields this adapter reads:

- `publishedAt` is the publication timestamp. The endpoint also publishes an `isListed` flag but
  no update timestamp and no total count, so `source_updated_at` and `declared_total` stay empty.
- `workplaceType` is a real provider field that the board publishes as `Hybrid`, `OnSite`,
  `Remote`, or `null`; the second committed record carries `null`, like 247 of the 817 live
  postings. `isRemote` is `null` on the same 247.
- `department`, `team`, `employmentType`, `location`, `jobUrl`, `applyUrl`,
  `descriptionPlain` and `descriptionHtml` were present and nonblank on all 817 postings, so a
  record without a department or without plain text is a synthetic override below, not captured
  data, and it is labeled as such where it is built.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from job_alert.adapters.ashby import AshbyAdapter
from job_alert.adapters.base import AdapterError, PartialCollectionError, validate_collection
from job_alert.fetch import ChallengeDetected, HttpFetcher
from job_alert.identity import content_hash
from job_alert.models import SourceClass, SourceConfig, SupportStatus
from tests.helpers import assert_normalized_job, assert_repeat_is_stable

FIXTURE = Path("tests/fixtures/ashby/job_board.json")
BOARD = "example"
ENDPOINT = f"https://api.ashbyhq.com/posting-api/job-board/{BOARD}"
NOW = datetime(2026, 9, 18, tzinfo=UTC)

CAPTURED_JOBS = (
    "f873dd71-46c9-4be1-8580-bc67c56b4cad",
    "b60d3040-38e4-4abe-8af8-881286b10d50",
)


def fixture_payload() -> dict[str, object]:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def fixture_job(job_id: str) -> dict[str, object]:
    """A captured Ashby posting, relabeled with a compact id so expectations stay readable."""
    posting = deepcopy(fixture_payload()["jobs"][int(job_id) - 1])
    assert isinstance(posting, dict)
    posting["id"] = job_id
    return posting


def record(job_id: str = "1", **overrides: object) -> dict[str, object]:
    """A realistic Ashby posting record, with fields overridable per test."""
    posting: dict[str, object] = {
        "id": job_id,
        "title": "Software Engineer",
        "department": "Engineering",
        "team": "Platform",
        "employmentType": "FullTime",
        "location": "San Francisco",
        "secondaryLocations": [
            {
                "location": "New York City",
                "address": {
                    "postalAddress": {
                        "addressRegion": "New York",
                        "addressCountry": "United States",
                        "addressLocality": "New York",
                    }
                },
            }
        ],
        "publishedAt": "2026-05-07T19:45:42.110+00:00",
        "isListed": True,
        "isRemote": True,
        "workplaceType": "Hybrid",
        "address": {
            "postalAddress": {
                "addressRegion": "California",
                "addressCountry": "United States",
                "addressLocality": "San Francisco",
            }
        },
        "jobUrl": f"https://jobs.ashbyhq.com/example/{job_id}",
        "applyUrl": f"https://jobs.ashbyhq.com/example/{job_id}/application",
        "descriptionPlain": "Build job alerts for every supported provider.",
        "descriptionHtml": "<p>Build job alerts for every supported provider.</p>",
    }
    posting.update(overrides)
    return posting


def make_ashby_source(**overrides: object) -> SourceConfig:
    values: dict[str, object] = {
        "id": "example-ashby",
        "company_id": "example",
        "provider": "ashby",
        "source_class": SourceClass.DOCUMENTED_API,
        "support_status": SupportStatus.SUPPORTED,
        "endpoint": ENDPOINT,
        "interval_minutes": 5,
        "tenant": BOARD,
    }
    return SourceConfig.model_validate({**values, **overrides})


def fixed_now() -> datetime:
    return NOW


def adapter() -> AshbyAdapter:
    return AshbyAdapter(now=fixed_now)


def board(*records: object) -> httpx.Response:
    return httpx.Response(200, json={"apiVersion": "1", "jobs": list(records)})


def test_ashby_fixture_keeps_two_captured_postings_and_the_fields_the_board_omits() -> None:
    payload = fixture_payload()
    records = payload["jobs"]
    assert isinstance(records, list)

    assert list(payload) == ["apiVersion", "jobs"]
    assert [posting["id"] for posting in records] == list(CAPTURED_JOBS)
    for posting in records:
        for field in (
            "id",
            "title",
            "department",
            "team",
            "employmentType",
            "location",
            "secondaryLocations",
            "publishedAt",
            "jobUrl",
            "applyUrl",
            "descriptionPlain",
            "descriptionHtml",
        ):
            assert field in posting

    # The endpoint publishes no board-wide total, and every record carries exactly these keys.
    assert "total" not in payload
    assert set(records[0]) == {
        "id",
        "title",
        "department",
        "team",
        "employmentType",
        "location",
        "secondaryLocations",
        "publishedAt",
        "isListed",
        "isRemote",
        "workplaceType",
        "address",
        "jobUrl",
        "applyUrl",
        "descriptionHtml",
        "descriptionPlain",
    }

    # The first posting carries one additional site; the second carries two and a null workplace.
    assert records[0]["secondaryLocations"] == [
        {
            "location": "Mountain View",
            "address": {
                "postalAddress": {
                    "addressRegion": "California",
                    "addressCountry": "United States",
                    "addressLocality": "Mountain View",
                }
            },
        }
    ]
    assert records[1]["workplaceType"] is None
    assert records[1]["isRemote"] is None
    assert [entry["location"] for entry in records[1]["secondaryLocations"]] == [
        "Washington, DC",
        "US - Remote",
    ]


@respx.mock
@pytest.mark.asyncio
async def test_ashby_maps_the_captured_board_into_normalized_jobs() -> None:
    captured = fixture_payload()["jobs"]
    route = respx.get(ENDPOINT).mock(return_value=board(fixture_job("1"), fixture_job("2")))

    async with HttpFetcher() as fetcher:
        result = validate_collection(await adapter().collect(make_ashby_source(), fetcher))

    first, second = result.jobs
    assert result.source_id == "example-ashby"
    assert result.page_count == 1
    # Ashby's posting endpoint publishes no total, so there is no number to compare with.
    assert result.declared_total is None
    assert result.warnings == []
    assert route.call_count == 1

    assert first.source_job_id == "1"
    assert first.canonical_job_key == "ashby:example:1"
    assert first.dedupe_key == (
        "url:example:https://jobs.ashbyhq.com/openai/"
        "f873dd71-46c9-4be1-8580-bc67c56b4cad/application"
    )
    assert first.title == "Full-Stack Software Engineer, Applied Foundations"
    assert first.locations == ["San Francisco, United States", "Mountain View, United States"]
    assert first.department == "Applied AI"
    assert first.team == "Applied AI Engineering"
    assert first.employment_type == "FullTime"
    assert first.workplace_type == "Hybrid"
    assert first.description_text == captured[0]["descriptionPlain"]
    assert first.content_hash == content_hash(first.title, first.locations, first.description_text)
    assert str(first.posting_url) == captured[0]["jobUrl"]
    assert str(first.apply_url) == captured[0]["applyUrl"]
    assert first.source_published_at == datetime(2026, 5, 7, 19, 45, 42, 110000, tzinfo=UTC)
    assert first.source_updated_at is None
    assert first.first_seen_at == NOW
    assert first.raw_metadata["is_listed"] is True
    assert first.raw_metadata["is_remote"] is True
    assert first.raw_metadata["address"] == captured[0]["address"]

    assert second.source_job_id == "2"
    assert second.canonical_job_key == "ashby:example:2"
    assert second.title == "Researcher, Safety Training, National Security"
    assert second.locations == ["San Francisco, United States", "Washington, DC", "US - Remote"]
    assert second.department == "Safety Systems"
    assert second.team == "Safety Systems"
    assert second.employment_type == "FullTime"
    assert second.workplace_type is None
    assert second.source_published_at == datetime(2026, 9, 12, 3, 21, 15, 873000, tzinfo=UTC)
    assert second.raw_metadata["is_remote"] is None

    for job in result.jobs:
        assert_normalized_job(job)


@respx.mock
@pytest.mark.asyncio
async def test_ashby_combines_primary_and_secondary_locations_without_duplicates() -> None:
    respx.get(ENDPOINT).mock(
        return_value=board(
            record(
                location="San Francisco",
                secondaryLocations=[
                    {"location": "San Francisco"},
                    {"location": "New York  City"},
                    {"location": "new york city"},
                    {"location": "  "},
                    # A bare location string is not what the captured board publishes, but a board
                    # that lists one must not lose the site.
                    "London, UK",
                ],
            )
        )
    )

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_ashby_source(), fetcher)

    # The primary site's postal address names its country; the bare secondaries have none.
    assert result.jobs[0].locations == [
        "San Francisco, United States",
        "New York City",
        "London, UK",
    ]


@respx.mock
@pytest.mark.asyncio
async def test_ashby_keeps_posting_and_application_urls_distinct() -> None:
    respx.get(ENDPOINT).mock(return_value=board(fixture_job("1")))

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_ashby_source(), fetcher)

    job = result.jobs[0]
    assert str(job.posting_url) == f"https://jobs.ashbyhq.com/openai/{CAPTURED_JOBS[0]}"
    assert str(job.apply_url) == f"{job.posting_url}/application"
    assert str(job.posting_url) != str(job.apply_url)
    assert job.dedupe_key == f"url:example:{job.apply_url}"


@respx.mock
@pytest.mark.asyncio
async def test_ashby_prefers_the_plain_description_and_falls_back_to_html() -> None:
    html = (
        "<h2>About the role</h2><p>Build &amp; ship job alerts.</p>"
        "<ul><li>Watch boards</li><li>Send alerts</li></ul>"
    )
    respx.get(ENDPOINT).mock(
        side_effect=[
            board(record("1", descriptionPlain="Plain text wins.", descriptionHtml=html)),
            board(record("2", descriptionPlain=None, descriptionHtml=html)),
            # No posting on the captured board omits both descriptions; this override pins the
            # absent-description path the way the Lever adapter pins an absent timestamp.
            board(record("3", descriptionPlain=None, descriptionHtml=None)),
        ]
    )

    async with HttpFetcher() as fetcher:
        plain = (await adapter().collect(make_ashby_source(), fetcher)).jobs[0]
        converted = (await adapter().collect(make_ashby_source(), fetcher)).jobs[0]
        absent = (await adapter().collect(make_ashby_source(), fetcher)).jobs[0]

    assert plain.description_text == "Plain text wins."
    assert converted.description_text == (
        "About the role\nBuild & ship job alerts.\n- Watch boards\n- Send alerts"
    )
    assert "<" not in (converted.description_text or "")
    assert converted.content_hash == content_hash(
        converted.title, converted.locations, converted.description_text
    )
    assert absent.description_text is None
    assert absent.content_hash == content_hash(absent.title, absent.locations, None)


@respx.mock
@pytest.mark.asyncio
async def test_ashby_keeps_a_job_without_optional_fields() -> None:
    posting = record("1", publishedAt=None)
    # `department` and `team` were present on all 817 captured postings, so this case is a
    # synthetic override: no value is invented, two optional keys are removed and `workplaceType`
    # and the timestamp are set to the `null` this board itself publishes elsewhere.
    del posting["department"]
    del posting["team"]
    posting["workplaceType"] = None
    respx.get(ENDPOINT).mock(return_value=board(posting))

    async with HttpFetcher() as fetcher:
        result = validate_collection(await adapter().collect(make_ashby_source(), fetcher))

    job = result.jobs[0]
    assert result.warnings == []
    assert job.department is None
    assert job.team is None
    assert job.workplace_type is None
    assert job.source_published_at is None
    assert job.content_hash == content_hash(job.title, job.locations, job.description_text)
    assert_normalized_job(job)


@respx.mock
@pytest.mark.asyncio
async def test_ashby_repeats_the_captured_board_as_stable_normalized_jobs() -> None:
    captured = board(fixture_job("1"), fixture_job("2"))
    respx.get(ENDPOINT).mock(side_effect=[captured, captured])

    async with HttpFetcher() as fetcher:
        first = await adapter().collect(make_ashby_source(), fetcher)
        second = await adapter().collect(make_ashby_source(), fetcher)

    assert first.complete is True
    assert_repeat_is_stable(first, second)
    for job in first.jobs:
        assert_normalized_job(job)


@respx.mock
@pytest.mark.asyncio
async def test_ashby_accepts_an_empty_board() -> None:
    respx.get(ENDPOINT).mock(return_value=board())

    async with HttpFetcher() as fetcher:
        result = validate_collection(await adapter().collect(make_ashby_source(), fetcher))

    assert result.complete is True
    assert result.jobs == []
    assert result.page_count == 1
    assert result.declared_total is None
    assert result.warnings == []


@respx.mock
@pytest.mark.asyncio
async def test_ashby_marks_an_unusable_record_incomplete_but_keeps_the_board() -> None:
    respx.get(ENDPOINT).mock(
        return_value=board(fixture_job("1"), fixture_job("2") | {"title": None})
    )

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_ashby_source(), fetcher)

    assert result.complete is False
    assert [job.source_job_id for job in result.jobs] == ["1"]
    assert result.warnings == ["skipped malformed job record at index 1: missing title"]
    with pytest.raises(PartialCollectionError, match="incomplete collection"):
        validate_collection(result)


@respx.mock
@pytest.mark.asyncio
async def test_ashby_skips_a_repeated_job_id_as_an_incomplete_collection() -> None:
    respx.get(ENDPOINT).mock(return_value=board(record("1"), record("1", title="Other job")))

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_ashby_source(), fetcher)

    assert result.complete is False
    assert [job.source_job_id for job in result.jobs] == ["1"]
    assert result.warnings == [
        "skipped duplicate job record at index 1: the source job id already appeared"
    ]


@respx.mock
@pytest.mark.asyncio
async def test_ashby_rejects_records_without_usable_urls_or_a_publish_timestamp() -> None:
    respx.get(ENDPOINT).mock(
        side_effect=[
            board(record("1", jobUrl=None)),
            board(record("1", applyUrl="http://jobs.ashbyhq.com/example/1/application")),
            board(record("1", publishedAt="yesterday")),
            board(record("1", publishedAt="2026-05-07T19:45:42.110")),
        ]
    )

    async with HttpFetcher() as fetcher:
        missing = await adapter().collect(make_ashby_source(), fetcher)
        insecure = await adapter().collect(make_ashby_source(), fetcher)
        bad_timestamp = await adapter().collect(make_ashby_source(), fetcher)
        naive_timestamp = await adapter().collect(make_ashby_source(), fetcher)

    assert missing.complete is False
    assert missing.jobs == []
    assert missing.warnings == ["skipped malformed job record at index 0: missing jobUrl"]
    assert insecure.complete is False
    assert insecure.jobs == []
    assert insecure.warnings == [
        "skipped malformed job record at index 0: applyUrl is not an absolute https URL"
    ]
    assert bad_timestamp.complete is False
    assert bad_timestamp.jobs == []
    assert bad_timestamp.warnings == [
        "skipped malformed job record at index 0: publishedAt is not an ISO 8601 timestamp"
    ]
    assert naive_timestamp.complete is False
    assert naive_timestamp.jobs == []
    assert naive_timestamp.warnings == [
        "skipped malformed job record at index 0: publishedAt has no UTC offset"
    ]


@respx.mock
@pytest.mark.asyncio
async def test_ashby_normalizes_a_non_utc_publish_timestamp_to_utc() -> None:
    respx.get(ENDPOINT).mock(
        return_value=board(record("1", publishedAt="2026-05-07T12:45:42.110-07:00"))
    )

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_ashby_source(), fetcher)

    published_at = result.jobs[0].source_published_at
    assert published_at == datetime(2026, 5, 7, 19, 45, 42, 110000, tzinfo=UTC)
    assert published_at is not None
    assert published_at.utcoffset() == datetime(2026, 1, 1, tzinfo=UTC).utcoffset()


@respx.mock
@pytest.mark.asyncio
async def test_ashby_sends_the_configured_endpoint_and_source_headers() -> None:
    endpoint = f"{ENDPOINT}?includeCompensation=true"
    route = respx.get(endpoint).mock(return_value=board(record("1")))
    source = make_ashby_source(
        endpoint=endpoint,
        headers={"Accept": "application/json", "X-Source-Audit": "2026-09-18"},
    )

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is True
    sent = route.calls[0].request
    assert str(sent.url) == endpoint
    assert sent.headers["Accept"] == "application/json"
    assert sent.headers["X-Source-Audit"] == "2026-09-18"


@respx.mock
@pytest.mark.asyncio
async def test_ashby_falls_back_to_the_board_token_for_the_tenant() -> None:
    respx.get(ENDPOINT).mock(return_value=board(record("1")))

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_ashby_source(tenant=None), fetcher)

    assert result.jobs[0].canonical_job_key == "ashby:example:1"


@pytest.mark.asyncio
async def test_ashby_rejects_a_source_that_names_no_board() -> None:
    source = make_ashby_source(tenant=None, endpoint="https://careers.example.com/jobs")

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="tenant"):
            await adapter().collect(source, fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_ashby_rejects_a_payload_that_is_not_json() -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200, headers={"Content-Type": "text/plain"}, text="<html>maintenance</html>"
        )
    )

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="is not JSON"):
            await adapter().collect(make_ashby_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_ashby_rejects_a_payload_that_is_not_a_json_object() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=[]))

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="is not a JSON object"):
            await adapter().collect(make_ashby_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_ashby_rejects_a_payload_without_a_jobs_array() -> None:
    respx.get(ENDPOINT).mock(
        side_effect=[
            httpx.Response(200, json={"apiVersion": "1"}),
            httpx.Response(200, json={"apiVersion": "1", "jobs": {"1": {}}}),
        ]
    )

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="has no 'jobs' array"):
            await adapter().collect(make_ashby_source(), fetcher)
        with pytest.raises(AdapterError, match="has no 'jobs' array"):
            await adapter().collect(make_ashby_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_ashby_rejects_a_not_modified_response() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(304))

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="304"):
            await adapter().collect(make_ashby_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_ashby_treats_a_challenge_page_as_an_error() -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="<html><title>Just a moment...</title></html>",
        )
    )

    async with HttpFetcher() as fetcher:
        with pytest.raises(ChallengeDetected):
            await adapter().collect(make_ashby_source(), fetcher)
