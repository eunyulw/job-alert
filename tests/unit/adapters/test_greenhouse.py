from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from job_alert.adapters.base import AdapterError, PartialCollectionError, validate_collection
from job_alert.adapters.greenhouse import GreenhouseAdapter
from job_alert.fetch import ChallengeDetected, HttpFetcher
from job_alert.identity import content_hash
from job_alert.models import SourceClass, SourceConfig, SupportStatus

FIXTURE = Path("tests/fixtures/greenhouse/jobs.json")
ENDPOINT = "https://boards-api.greenhouse.io/v1/boards/example/jobs?content=true"
NOW = datetime(2026, 9, 18, tzinfo=UTC)


def fixture_payload() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def fixture_job(index: int = 0) -> dict[str, object]:
    return fixture_payload()["jobs"][index]  # type: ignore[index]


def board(*records: object, total: int | None = None) -> dict[str, object]:
    return {
        "jobs": list(records),
        "meta": {"total": len(records) if total is None else total},
    }


def record(**overrides: object) -> dict[str, object]:
    """A realistic Greenhouse posting record, with fields overridable per test."""
    posting: dict[str, object] = {
        "id": 6199608004,
        "internal_job_id": 4904996004,
        "title": "Software Engineer",
        "updated_at": "2026-09-17T18:07:49-04:00",
        "first_published": "2026-09-17T17:56:20-04:00",
        "requisition_id": "824",
        "location": {"name": "Hybrid - Chicago"},
        "absolute_url": "https://boards.greenhouse.io/example/jobs/6199608004",
        "departments": [{"id": 1, "name": "Engineering", "child_ids": [], "parent_id": None}],
        "offices": [
            {
                "id": 2,
                "name": "Office - Chicago",
                "location": "Chicago, Illinois, United States",
                "child_ids": [],
                "parent_id": None,
            }
        ],
        "content": "<p>Build job alerts.</p>",
        "metadata": [
            {
                "id": 3,
                "name": "Career Site Categories",
                "value": "Engineering",
                "value_type": "single_select",
            }
        ],
    }
    posting.update(overrides)
    return posting


def make_source(**overrides: object) -> SourceConfig:
    values: dict[str, object] = {
        "id": "example-greenhouse",
        "company_id": "example",
        "provider": "greenhouse",
        "source_class": SourceClass.DOCUMENTED_API,
        "support_status": SupportStatus.SUPPORTED,
        "endpoint": ENDPOINT,
        "interval_minutes": 5,
        "tenant": "example",
    }
    return SourceConfig.model_validate({**values, **overrides})


def adapter() -> GreenhouseAdapter:
    return GreenhouseAdapter(now=lambda: NOW)


@respx.mock
@pytest.mark.asyncio
async def test_greenhouse_normalizes_ids_locations_and_urls() -> None:
    endpoint = "https://boards-api.greenhouse.io/v1/boards/example/jobs?content=true"
    respx.get(endpoint).mock(return_value=httpx.Response(200, json=json.loads(FIXTURE.read_text())))
    source = SourceConfig(
        id="example-greenhouse",
        company_id="example",
        provider="greenhouse",
        source_class=SourceClass.DOCUMENTED_API,
        support_status=SupportStatus.SUPPORTED,
        endpoint=endpoint,
        interval_minutes=5,
        tenant="example",
    )
    async with HttpFetcher() as fetcher:
        result = await GreenhouseAdapter(now=lambda: datetime(2026, 9, 18, tzinfo=UTC)).collect(
            source, fetcher
        )
    assert result.complete is True
    assert result.declared_total == 2
    assert result.jobs[0].canonical_job_key.startswith("greenhouse:example:")
    assert result.jobs[0].locations
    assert str(result.jobs[0].apply_url).startswith("https://")


@respx.mock
@pytest.mark.asyncio
async def test_greenhouse_maps_the_captured_board_into_normalized_jobs() -> None:
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=fixture_payload()))

    async with HttpFetcher() as fetcher:
        result = validate_collection(await adapter().collect(make_source(), fetcher))

    first, second = result.jobs
    assert result.source_id == "example-greenhouse"
    assert result.page_count == 1
    assert result.warnings == []
    assert route.call_count == 1

    assert first.source_job_id == "6199608004"
    assert first.canonical_job_key == "greenhouse:example:6199608004"
    assert first.dedupe_key == "requisition:example:824"
    assert first.requisition_id == "824"
    assert first.title == "Software Engineer, Agentic Infrastructure"
    assert first.locations == ["Hybrid - San Francisco, New York City", "Remote - United States"]
    assert first.department == "Engineering"
    assert first.team is None
    assert first.employment_type is None
    assert first.workplace_type is None
    assert first.description_text is not None
    assert first.description_text.startswith("About Vercel:")
    assert "&lt;" not in first.description_text
    assert "<" not in first.description_text
    assert first.content_hash == content_hash(first.title, first.locations, first.description_text)
    assert str(first.posting_url) == "https://job-boards.greenhouse.io/vercel/jobs/6199608004"
    assert str(first.apply_url) == str(first.posting_url)
    assert first.source_published_at == datetime(2026, 9, 17, 21, 56, 20, tzinfo=UTC)
    assert first.source_updated_at == datetime(2026, 9, 17, 22, 7, 49, tzinfo=UTC)
    assert first.first_seen_at == NOW
    assert first.raw_metadata["internal_job_id"] == "4904996004"
    assert first.raw_metadata["office_names"] == ["Remote - United States"]
    assert first.raw_metadata["board_metadata"] == [
        {
            "id": 18783104004,
            "name": "Career Site Categories",
            "value": "Engineering",
            "value_type": "single_select",
        }
    ]

    assert second.source_job_id == "6142097004"
    assert second.canonical_job_key == "greenhouse:example:6142097004"
    assert second.title == "Scaled Commercial Account Executive, Install base "
    assert second.department == "Account Executive"
    assert second.locations == [
        "Hybrid - San Francisco, New York City, Austin",
        "Office - Austin",
        "Office - New York City",
        "Office - San Francisco",
    ]


@respx.mock
@pytest.mark.asyncio
async def test_greenhouse_accepts_an_empty_board() -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"jobs": [], "meta": {"total": 0}})
    )

    async with HttpFetcher() as fetcher:
        result = validate_collection(await adapter().collect(make_source(), fetcher))

    assert result.complete is True
    assert result.jobs == []
    assert result.declared_total == 0
    assert result.warnings == []


@respx.mock
@pytest.mark.asyncio
async def test_greenhouse_reports_the_declared_total_for_validate_collection() -> None:
    payload = fixture_payload()
    payload["meta"] = {"total": 3}  # type: ignore[index]
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_source(), fetcher)

    assert result.complete is True
    assert result.declared_total == 3
    assert len(result.jobs) == 2
    with pytest.raises(AdapterError, match="declared 3 jobs but parsed 2"):
        validate_collection(result)


@respx.mock
@pytest.mark.asyncio
async def test_greenhouse_marks_the_collection_incomplete_when_a_record_is_unusable() -> None:
    unusable = dict(fixture_job(1))
    del unusable["title"]
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(200, json=board(fixture_job(0), unusable))
    )

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_source(), fetcher)

    assert result.complete is False
    assert [job.source_job_id for job in result.jobs] == ["6199608004"]
    assert result.warnings == ["skipped malformed job record at index 1: missing title"]
    with pytest.raises(PartialCollectionError, match="incomplete collection"):
        validate_collection(result)


@respx.mock
@pytest.mark.asyncio
async def test_greenhouse_marks_the_collection_incomplete_when_a_record_repeats_an_id() -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(200, json=board(record(), record(title="Other job")))
    )

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_source(), fetcher)

    assert result.complete is False
    assert [job.source_job_id for job in result.jobs] == ["6199608004"]
    assert result.warnings == [
        "skipped duplicate job record at index 1: the source job id already appeared"
    ]


@respx.mock
@pytest.mark.asyncio
async def test_greenhouse_rejects_a_record_without_a_postable_url() -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(200, json=board(record(absolute_url="http://example.com/jobs/1")))
    )

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_source(), fetcher)

    assert result.complete is False
    assert result.jobs == []
    assert result.warnings == [
        "skipped malformed job record at index 0: absolute_url is not an absolute https URL"
    ]


@respx.mock
@pytest.mark.asyncio
async def test_greenhouse_marks_a_record_incomplete_when_its_url_cannot_parse() -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=board(
                record(absolute_url="https://boards. greenhouse.io/example/jobs/6199608004")
            ),
        )
    )

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_source(), fetcher)

    assert result.complete is False
    assert result.warnings == [
        "skipped malformed job record at index 0: record does not normalize into a job"
    ]


@respx.mock
@pytest.mark.asyncio
async def test_greenhouse_treats_a_challenge_page_as_an_error() -> None:
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
async def test_greenhouse_rejects_a_payload_that_is_not_json() -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200, headers={"Content-Type": "text/plain"}, text="<html>maintenance</html>"
        )
    )

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="is not JSON"):
            await adapter().collect(make_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_greenhouse_rejects_a_payload_without_a_jobs_array_or_total() -> None:
    respx.get(ENDPOINT).mock(
        side_effect=[
            httpx.Response(200, json={"meta": {"total": 0}}),
            httpx.Response(200, json={"jobs": [], "meta": {}}),
            httpx.Response(200, json={"jobs": [], "meta": {"total": "2"}}),
        ]
    )

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="has no 'jobs' array"):
            await adapter().collect(make_source(), fetcher)
        with pytest.raises(AdapterError, match="meta.total"):
            await adapter().collect(make_source(), fetcher)
        with pytest.raises(AdapterError, match="meta.total"):
            await adapter().collect(make_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_greenhouse_converts_html_content_to_plain_text_deterministically() -> None:
    content = (
        "<div><p>Tom &amp; Jerry &lt; 3</p><ul><li>Ship it</li><li>Fix it</li></ul>"
        "<script>alert('ignore me')</script><style>p{color:red}</style></div>"
    )
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(200, json=board(record(content=content)))
    )

    async with HttpFetcher() as fetcher:
        first = await adapter().collect(make_source(), fetcher)
        second = await adapter().collect(make_source(), fetcher)

    description = first.jobs[0].description_text
    assert description == "Tom & Jerry < 3\n- Ship it\n- Fix it"
    assert second.jobs[0].description_text == description
    assert second.jobs[0].content_hash == first.jobs[0].content_hash


@respx.mock
@pytest.mark.asyncio
async def test_greenhouse_recovers_html_escaped_document_content() -> None:
    escaped = "&lt;h2&gt;About the role&lt;/h2&gt;&lt;p&gt;Build &amp;amp; ship&lt;/p&gt;"
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(200, json=board(record(content=escaped)))
    )

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_source(), fetcher)

    assert result.jobs[0].description_text == "About the role\nBuild & ship"


@respx.mock
@pytest.mark.asyncio
async def test_greenhouse_hashes_a_posting_without_content_as_absent_text() -> None:
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=board(
                record(content=None, offices=[], departments=[], requisition_id=None),
            ),
        )
    )

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_source(), fetcher)

    job = result.jobs[0]
    assert job.description_text is None
    assert job.content_hash == content_hash(job.title, job.locations, None)
    assert job.department is None
    assert job.dedupe_key == "url:example:https://boards.greenhouse.io/example/jobs/6199608004"


@respx.mock
@pytest.mark.asyncio
async def test_greenhouse_sends_the_configured_source_headers() -> None:
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=board(record())))
    source = make_source(headers={"Accept": "application/json", "X-Source-Audit": "2026-09-18"})

    async with HttpFetcher() as fetcher:
        await adapter().collect(source, fetcher)

    sent = route.calls[0].request.headers
    assert sent["Accept"] == "application/json"
    assert sent["X-Source-Audit"] == "2026-09-18"


@respx.mock
@pytest.mark.asyncio
async def test_greenhouse_rejects_a_not_modified_response() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(304))

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="304"):
            await adapter().collect(make_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_greenhouse_uses_a_safe_application_url_template_when_configured() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=board(record())))
    source = make_source(
        parameters={
            "apply_url_template": (
                "https://boards.greenhouse.io/embed/job_app?for={tenant}&token={job_id}"
            )
        }
    )

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    job = result.jobs[0]
    assert str(job.apply_url) == "https://boards.greenhouse.io/embed/job_app?for=example&token=6199608004"
    assert str(job.posting_url) == "https://boards.greenhouse.io/example/jobs/6199608004"
    assert job.dedupe_key == "requisition:example:824"


@respx.mock
@pytest.mark.asyncio
async def test_greenhouse_rejects_an_unsafe_application_url_template() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=board(record())))
    source = make_source(parameters={"apply_url_template": "http://example.com/apply/{job_id}"})

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="apply_url_template"):
            await adapter().collect(source, fetcher)

    broken = make_source(parameters={"apply_url_template": "https://example.com/apply/{unknown}"})
    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="apply_url_template"):
            await adapter().collect(broken, fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_greenhouse_requests_descriptions_when_the_endpoint_omits_content() -> None:
    route = respx.get(ENDPOINT).mock(
        return_value=httpx.Response(200, json=board(fixture_job(0)))
    )
    source = make_source(
        endpoint="https://boards-api.greenhouse.io/v1/boards/example/jobs?content=false"
    )

    async with HttpFetcher() as fetcher:
        await adapter().collect(source, fetcher)

    assert str(route.calls[0].request.url) == ENDPOINT


@respx.mock
@pytest.mark.asyncio
async def test_greenhouse_falls_back_to_the_board_token_for_the_tenant() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=board(fixture_job(0))))

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_source(tenant=None), fetcher)

    assert result.jobs[0].canonical_job_key == "greenhouse:example:6199608004"


@respx.mock
@pytest.mark.asyncio
async def test_greenhouse_rejects_a_source_that_names_no_board() -> None:
    source = make_source(tenant=None, endpoint="https://careers.example.com/jobs")

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="tenant"):
            await adapter().collect(source, fetcher)
