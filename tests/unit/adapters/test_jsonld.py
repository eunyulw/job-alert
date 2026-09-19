"""Offline tests for the `JobPosting` JSON-LD fallback adapter.

The adapter reads only `application/ld+json` script blocks, so these tests pin block discovery,
`@graph` traversal, the field mapping the schema.org document shapes require, relative-URL
resolution against the audited source origin, duplicate handling, and the rule that a page which
publishes no `JobPosting` document is an error rather than an empty board.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from job_alert.adapters.base import AdapterError, PartialCollectionError, validate_collection
from job_alert.adapters.jsonld import JsonLdAdapter, extract_job_postings, extract_jsonld
from job_alert.fetch import ChallengeDetected, HttpFetcher
from job_alert.identity import content_hash
from job_alert.models import SourceClass, SourceConfig, SupportStatus
from tests.helpers import assert_normalized_job, assert_repeat_is_stable

FIXTURE = Path("tests/fixtures/jsonld/jobs.html")
ENDPOINT = "https://careers.northwind.example/careers"
HOST_TENANT = "careers.northwind.example"
NOW = datetime(2026, 9, 19, tzinfo=UTC)


def fixture_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def jsonld_page(*payloads: object) -> str:
    """A minimal page whose only structured data is the JSON-LD blocks given to it."""
    blocks = "".join(
        f'<script type="application/ld+json">{json.dumps(payload)}</script>'
        for payload in payloads
    )
    return f"<!doctype html><html><head>{blocks}</head><body><h1>Careers</h1></body></html>"


def posting(identifier: str | None = "1", **overrides: object) -> dict[str, object]:
    """A realistic `JobPosting` document, with fields overridable per test."""
    document: dict[str, object] = {
        "@type": "JobPosting",
        "title": "Software Engineer",
        "datePosted": "2026-09-15T13:30:00+00:00",
        "description": "<p>Build job alerts.</p>",
        "employmentType": "FULL_TIME",
        "hiringOrganization": {"@type": "Organization", "name": "Northwind Robotics"},
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": "Chicago",
                "addressRegion": "IL",
                "addressCountry": "US",
            },
        },
        "url": "/postings/1",
    }
    if identifier is not None:
        document["identifier"] = {"@type": "PropertyValue", "value": identifier}
    document.update(overrides)
    return document


def make_source(**overrides: object) -> SourceConfig:
    values: dict[str, object] = {
        "id": "northwind-json-ld",
        "company_id": "northwind-robotics",
        "provider": "json-ld",
        "source_class": SourceClass.HTML,
        "support_status": SupportStatus.HTML_ONLY,
        "endpoint": ENDPOINT,
        "interval_minutes": 30,
        "tenant": None,
    }
    return SourceConfig.model_validate({**values, **overrides})


def adapter() -> JsonLdAdapter:
    return JsonLdAdapter(now=lambda: NOW)


def html_response(document: str) -> httpx.Response:
    return httpx.Response(200, headers={"Content-Type": "text/html"}, text=document)


# --------------------------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------------------------


def test_jsonld_extracts_multiple_job_postings_from_graph() -> None:
    jobs = extract_job_postings(fixture_html())

    assert [job["@type"] for job in jobs] == ["JobPosting", "JobPosting"]
    assert [job["title"] for job in jobs] == ["Staff Autonomy Engineer", "Perception Engineer"]


def test_jsonld_reads_only_ld_json_script_blocks() -> None:
    document = (
        "<!doctype html><html><head>"
        '<script type="text/javascript">'
        '{"@type": "JobPosting", "title": "not structured data"}'
        "</script>"
        '<script type="application/json">{"@type": "JobPosting"}</script>'
        "</head><body><pre>{\"@type\": \"JobPosting\"}</pre></body></html>"
    )

    assert extract_job_postings(document) == []


def test_jsonld_traverses_top_level_arrays_and_nested_graphs() -> None:
    nested = {
        "@context": "https://schema.org",
        "@graph": {"@graph": [{"@type": "JobPosting", "title": "Nested"}]},
    }
    array = [{"@type": "Thing"}, {"@type": "JobPosting", "title": "Top level"}]

    assert [job["title"] for job in extract_job_postings(jsonld_page(nested, array))] == [
        "Nested",
        "Top level",
    ]


def test_jsonld_accepts_a_list_of_types_that_contains_job_posting() -> None:
    document = jsonld_page({"@type": ["Thing", "JobPosting"], "title": "Listed"})

    assert [job["title"] for job in extract_job_postings(document)] == ["Listed"]


def test_jsonld_warns_about_a_malformed_block_and_continues_to_other_blocks() -> None:
    document = (
        '<script type="application/ld+json">{not json}</script>'
        + jsonld_page({"@type": "JobPosting", "title": "Still read"})
    )

    extraction = extract_jsonld(document)

    assert [job["title"] for job in extraction.postings] == ["Still read"]
    assert len(extraction.warnings) == 1
    assert extraction.warnings[0].startswith("skipped malformed JSON-LD script block 1:")


# --------------------------------------------------------------------------------------------
# Field mapping
# --------------------------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_jsonld_maps_the_fixture_into_normalized_jobs() -> None:
    route = respx.get(ENDPOINT).mock(return_value=html_response(fixture_html()))

    async with HttpFetcher() as fetcher:
        result = validate_collection(await adapter().collect(make_source(), fetcher))

    assert route.call_count == 1
    assert result.source_id == "northwind-json-ld"
    assert result.page_count == 1
    assert result.declared_total is None
    assert result.warnings == []
    assert route.calls[0].request.headers["Accept"] == "*/*"

    first, second = result.jobs
    assert first.source_job_id == "NW-2001"
    assert first.requisition_id == "NW-2001"
    assert first.canonical_job_key == f"json-ld:{HOST_TENANT}:NW-2001"
    assert first.dedupe_key == "requisition:northwind-robotics:NW-2001"
    assert first.title == "Staff Autonomy Engineer"
    assert first.locations == ["Pittsburgh, PA, US", "United States"]
    assert first.employment_type == "FULL_TIME"
    assert first.workplace_type is None
    assert first.description_text == (
        "Own the trajectory planner that runs on every deployed robot.\n"
        "Work with field teams weekly."
    )
    assert first.content_hash == content_hash(first.title, first.locations, first.description_text)
    assert str(first.posting_url) == "https://careers.northwind.example/postings/NW-2001"
    assert str(first.apply_url) == (
        "https://careers.northwind.example/postings/NW-2001/apply"
    )
    assert first.source_published_at == datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
    assert first.source_updated_at is None
    assert first.first_seen_at == NOW
    assert first.raw_metadata == {
        "valid_through": "2026-11-30T23:59:59+00:00",
        "hiring_organization": "Northwind Robotics",
        "job_location_type": None,
    }

    assert second.source_job_id == "NW-2002"
    assert second.locations == ["Austin, TX", "Remote, US"]
    assert second.workplace_type == "remote"
    assert second.description_text == "Build the perception stack."
    assert str(second.apply_url) == str(second.posting_url)
    assert second.requisition_id == "NW-2002"
    assert second.dedupe_key == "requisition:northwind-robotics:NW-2002"

    for job in result.jobs:
        assert_normalized_job(job)


@respx.mock
@pytest.mark.asyncio
async def test_jsonld_repeats_the_same_page_as_stable_normalized_jobs() -> None:
    respx.get(ENDPOINT).mock(return_value=html_response(fixture_html()))
    source = make_source()

    async with HttpFetcher() as fetcher:
        first = await adapter().collect(source, fetcher)
        second = await adapter().collect(source, fetcher)

    assert first.complete is True
    assert_repeat_is_stable(first, second)
    for job in first.jobs:
        assert_normalized_job(job)


@respx.mock
@pytest.mark.asyncio
async def test_jsonld_uses_the_registry_tenant_when_the_source_sets_one() -> None:
    respx.get(ENDPOINT).mock(return_value=html_response(jsonld_page(posting("1"))))

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_source(tenant="northwind"), fetcher)

    assert result.jobs[0].canonical_job_key == "json-ld:northwind:1"


@respx.mock
@pytest.mark.asyncio
async def test_jsonld_resolves_relative_urls_against_the_audited_source_origin() -> None:
    respx.get(ENDPOINT).mock(
        return_value=html_response(
            jsonld_page(posting("1", url="postings/1?utm_source=careers", applicationUrl=None))
        )
    )

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_source(), fetcher)

    job = result.jobs[0]
    assert str(job.posting_url) == "https://careers.northwind.example/postings/1"
    assert str(job.apply_url) == "https://careers.northwind.example/postings/1"


@respx.mock
@pytest.mark.asyncio
async def test_jsonld_falls_back_to_a_deterministic_identity_without_an_identifier() -> None:
    respx.get(ENDPOINT).mock(
        return_value=html_response(jsonld_page(posting(None, url="/postings/9")))
    )

    async with HttpFetcher() as fetcher:
        first = await adapter().collect(make_source(), fetcher)
        second = await adapter().collect(make_source(), fetcher)

    job = first.jobs[0]
    assert job.requisition_id is None
    assert job.source_job_id.startswith("sha256:")
    assert job.canonical_job_key == f"json-ld:{HOST_TENANT}:{job.source_job_id}"
    assert job.dedupe_key == (
        "url:northwind-robotics:https://careers.northwind.example/postings/9"
    )
    assert_repeat_is_stable(first, second)


@respx.mock
@pytest.mark.asyncio
async def test_jsonld_accepts_a_plain_string_and_a_numeric_identifier() -> None:
    respx.get(ENDPOINT).mock(
        return_value=html_response(
            jsonld_page(
                posting("NW-3001", url="/postings/3001"),
                {
                    "@type": "JobPosting",
                    "identifier": 3002,
                    "title": "Second role",
                    "url": "https://careers.northwind.example/postings/3002",
                },
            )
        )
    )

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_source(), fetcher)

    assert [job.source_job_id for job in result.jobs] == ["NW-3001", "3002"]
    assert result.jobs[1].dedupe_key == "requisition:northwind-robotics:3002"


@respx.mock
@pytest.mark.asyncio
async def test_jsonld_collapses_identical_duplicates_without_a_warning() -> None:
    duplicate = posting("NW-4001", url="/postings/4001")
    respx.get(ENDPOINT).mock(
        return_value=html_response(jsonld_page(duplicate, dict(duplicate)))
    )

    async with HttpFetcher() as fetcher:
        result = validate_collection(await adapter().collect(make_source(), fetcher))

    assert result.complete is True
    assert result.warnings == []
    assert [job.source_job_id for job in result.jobs] == ["NW-4001"]


@respx.mock
@pytest.mark.asyncio
async def test_jsonld_fails_when_two_records_claim_one_identity_with_different_content() -> None:
    respx.get(ENDPOINT).mock(
        return_value=html_response(
            jsonld_page(
                posting("NW-5001", url="/postings/5001"),
                posting("NW-5001", title="A different role", url="/postings/5001"),
            )
        )
    )

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="conflicting"):
            await adapter().collect(make_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_jsonld_marks_a_posting_with_unusable_urls_or_timestamps_incomplete() -> None:
    respx.get(ENDPOINT).mock(
        return_value=html_response(
            jsonld_page(
                posting("1", url="http://careers.northwind.example/postings/1"),
                posting("2", url="//mirror.northwind.example/postings/2"),
                posting("3", url=None, applicationUrl=None),
                posting("4", datePosted="yesterday"),
                posting("5", validThrough="whenever"),
                posting("6", title="   "),
            )
        )
    )

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_source(), fetcher)

    assert result.complete is False
    assert result.jobs == []
    assert result.warnings == [
        "skipped malformed job posting at index 0: url is not an absolute https URL",
        "skipped malformed job posting at index 1: url is a protocol-relative URL, not a "
        "relative path",
        "skipped malformed job posting at index 2: missing url",
        "skipped malformed job posting at index 3: datePosted is not an ISO 8601 timestamp",
        "skipped malformed job posting at index 4: validThrough is not an ISO 8601 timestamp",
        "skipped malformed job posting at index 5: missing title",
    ]
    with pytest.raises(PartialCollectionError, match="incomplete collection"):
        validate_collection(result)


@respx.mock
@pytest.mark.asyncio
async def test_jsonld_reports_a_malformed_block_as_an_incomplete_collection() -> None:
    document = (
        '<script type="application/ld+json">{not json}</script>'
        + f'<script type="application/ld+json">{json.dumps(posting("1"))}</script>'
    )
    respx.get(ENDPOINT).mock(return_value=html_response(document))

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_source(), fetcher)

    assert result.complete is False
    assert [job.source_job_id for job in result.jobs] == ["1"]
    assert len(result.warnings) == 1
    assert result.warnings[0].startswith("skipped malformed JSON-LD script block 1:")


# --------------------------------------------------------------------------------------------
# Response shape and transport
# --------------------------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_jsonld_rejects_a_page_that_publishes_no_job_posting() -> None:
    respx.get(ENDPOINT).mock(
        return_value=html_response(
            jsonld_page({"@type": "Organization", "name": "Northwind Robotics"})
        )
    )

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="publishes no JobPosting"):
            await adapter().collect(make_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_jsonld_rejects_a_page_whose_only_blocks_are_malformed() -> None:
    document = '<script type="application/ld+json">{not json}</script>'
    respx.get(ENDPOINT).mock(return_value=html_response(document))

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="malformed"):
            await adapter().collect(make_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_jsonld_rejects_an_html_page_without_script_blocks() -> None:
    respx.get(ENDPOINT).mock(
        return_value=html_response("<!doctype html><html><body>No roles today.</body></html>")
    )

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="publishes no JobPosting"):
            await adapter().collect(make_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_jsonld_rejects_a_not_modified_response() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(304))

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="304"):
            await adapter().collect(make_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_jsonld_treats_a_challenge_page_as_an_error() -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="<html><title>Just a moment...</title></html>",
        )
    )

    async with HttpFetcher() as fetcher:
        with pytest.raises(ChallengeDetected):
            await adapter().collect(make_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_jsonld_sends_the_configured_source_headers() -> None:
    route = respx.get(ENDPOINT).mock(return_value=html_response(jsonld_page(posting("1"))))
    source = make_source(headers={"Accept": "text/html", "X-Source-Audit": "2026-09-19"})

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    sent = route.calls[0].request.headers
    assert sent["Accept"] == "text/html"
    assert sent["X-Source-Audit"] == "2026-09-19"
    assert result.jobs[0].first_seen_at == NOW
