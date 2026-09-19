"""Offline tests for the registry-driven generic JSON fallback adapter.

A generic JSON source is a company endpoint the audit marked as a fallback, so the adapter owns no
provider knowledge: every field it reads is named by a registry parameter, and every one of those
selectors is compiled through the bounded dotted-key grammar first. These tests pin the grammar's
accepted and rejected forms, the explicit field mapping, the rule that a record missing a mapped
field makes the collection incomplete, and the bounded next-page walk.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
import respx

from job_alert.adapters import generic_json as generic_json_module
from job_alert.adapters.base import AdapterError, PartialCollectionError, validate_collection
from job_alert.adapters.generic_json import GenericJsonAdapter, compile_selector, extract_records
from job_alert.fetch import ChallengeDetected, HttpFetcher
from job_alert.identity import content_hash
from job_alert.models import SourceClass, SourceConfig, SupportStatus
from tests.helpers import assert_normalized_job, assert_repeat_is_stable

FIXTURE = Path("tests/fixtures/generic_json/jobs.json")
ENDPOINT = "https://api.northwind.example/v1/jobs"
NOW = datetime(2026, 9, 19, tzinfo=UTC)

# The mapping a reviewer would write for the synthetic company search response in the fixture.
PARAMETERS: dict[str, object] = {
    "collection": "results.jobs[]",
    "id": "jobId",
    "title": "name",
    "locations": "locations[]",
    "posting_url": "url",
    "apply_url": "applyUrl",
    "description": "description",
    "department": "department",
    "published_at": "postedAt",
}


def fixture_payload() -> dict[str, object]:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def fixture_record(index: int) -> dict[str, object]:
    results = fixture_payload()["results"]
    assert isinstance(results, dict)
    jobs = results["jobs"]
    assert isinstance(jobs, list)
    record = jobs[index]
    assert isinstance(record, dict)
    return record


def record(job_id: str = "1", **overrides: object) -> dict[str, object]:
    """A realistic company-search record, with fields overridable per test."""
    posting: dict[str, object] = {
        "jobId": job_id,
        "name": "Software Engineer",
        "department": "Engineering",
        "locations": ["Chicago, IL"],
        "url": f"https://careers.northwind.example/postings/{job_id}",
        "applyUrl": f"https://careers.northwind.example/postings/{job_id}/apply",
        "description": "<p>Build job alerts.</p>",
        "postedAt": "2026-09-15T13:30:00+00:00",
    }
    posting.update(overrides)
    return posting


def page(*records: object, **extra: object) -> httpx.Response:
    payload: dict[str, object] = {"results": {"jobs": list(records)}}
    payload.update(extra)
    return httpx.Response(200, json=payload)


def make_source(**overrides: object) -> SourceConfig:
    values: dict[str, object] = {
        "id": "northwind-generic-json",
        "company_id": "northwind-robotics",
        "provider": "generic-json",
        "source_class": SourceClass.COMPANY_JSON,
        "support_status": SupportStatus.FALLBACK_ONLY,
        "endpoint": ENDPOINT,
        "interval_minutes": 30,
        "tenant": "northwind",
        "parameters": dict(PARAMETERS),
    }
    return SourceConfig.model_validate({**values, **overrides})


def adapter() -> GenericJsonAdapter:
    return GenericJsonAdapter(now=lambda: NOW)


def request_params(request: httpx.Request) -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(str(request.url)).query, keep_blank_values=True)


# --------------------------------------------------------------------------------------------
# Selector grammar
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "selector",
    ["jobs", "jobs[]", "results.jobs[]", "a.b.c.d", "_private.value", "jobs[].name"],
)
def test_compile_selector_accepts_plain_dotted_keys_and_the_expansion_token(
    selector: str,
) -> None:
    assert compile_selector(selector)


def test_generic_json_rejects_executable_or_recursive_selector() -> None:
    with pytest.raises(AdapterError):
        compile_selector("jobs[*].__class__")


@pytest.mark.parametrize(
    "selector",
    [
        "",  # empty selector
        " ",  # blank selector
        " jobs",  # surrounding whitespace is not a key
        "jobs ",  # trailing whitespace is not a key
        ".jobs",  # empty segment
        "jobs.",  # empty segment
        "jobs..name",  # empty segment
        "jobs[*]",  # only the literal [] token expands
        "jobs[0]",  # index access
        "jobs[*].__class__",  # executable and recursive
        "jobs.__class__",  # dunder name
        "__class__",  # dunder name
        "jobs.__dict__",  # dunder name
        "jobs[](1)",  # call syntax
        "jobs()",  # call syntax
        "jobs.name()",  # call syntax
        "$..jobs",  # recursive descent
        "..jobs",  # recursive descent
        "jobs[?(@.name)]",  # filter expression
        "jobs[] | length",  # arbitrary expression
        "1+2",  # arbitrary expression
        "jobs->name",  # arbitrary expression
        "jobs[",  # unbalanced bracket
        "jobs]",  # unbalanced bracket
        "[jobs]",  # bracket wrapper
        "jobs[][]",  # the expansion token repeated inside one segment
        "@type",  # not a plain identifier
        "jobs-name",  # hyphen is not an identifier character
    ],
)
def test_compile_selector_rejects_every_unsafe_form(selector: str) -> None:
    with pytest.raises(AdapterError):
        compile_selector(selector)


def test_compile_selector_rejects_a_selector_deeper_than_the_bound() -> None:
    with pytest.raises(AdapterError, match="segments"):
        compile_selector(".".join(["level"] * (generic_json_module.MAX_SELECTOR_SEGMENTS + 1)))


def test_compile_selector_rejects_a_non_string_selector() -> None:
    with pytest.raises(AdapterError, match="selector"):
        compile_selector(None)


def test_extract_records_expands_the_collection_selector() -> None:
    assert [item["jobId"] for item in extract_records(fixture_payload(), "results.jobs[]")] == [
        "NW-1001",
        "NW-1002",
    ]


def test_extract_records_accepts_a_plain_list_selector_and_maps_over_lists() -> None:
    nested = {"results": {"jobs": [{"jobId": "1"}]}}
    mapped = {"results": [{"jobs": [{"jobId": "1"}]}, {"jobs": [{"jobId": "2"}]}]}

    assert extract_records(nested, "results.jobs") == [{"jobId": "1"}]
    assert extract_records(mapped, "results.jobs[]") == [{"jobId": "1"}, {"jobId": "2"}]


def test_extract_records_rejects_a_payload_without_the_collection_key() -> None:
    with pytest.raises(AdapterError, match="results.jobs"):
        extract_records({"results": {"postings": []}}, "results.jobs[]")


def test_extract_records_treats_a_present_but_empty_collection_as_an_empty_board() -> None:
    assert extract_records({"results": {"jobs": []}}, "results.jobs[]") == []


@pytest.mark.parametrize(
    ("document", "selector"),
    [
        ({"results": []}, "results.jobs[]"),
        ({"a": {"b": []}}, "a.b.c"),
    ],
)
def test_extract_records_rejects_an_empty_intermediate_list(
    document: dict[str, object], selector: str
) -> None:
    # An empty list one level above the collection is a payload that no longer publishes the
    # collected key, not a board with no openings.
    with pytest.raises(AdapterError, match="does not publish"):
        extract_records(document, selector)


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_rejects_a_payload_whose_intermediate_list_is_empty() -> None:
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"results": []}))

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="results.jobs"):
            await adapter().collect(make_source(), fetcher)

    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_accepts_an_explicitly_empty_terminal_collection() -> None:
    # The terminal key is published and holds an empty list, which is an empty board. Only the
    # terminal segment may resolve this way: an empty intermediate list fails above.
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"results": {"jobs": []}}))

    async with HttpFetcher() as fetcher:
        result = validate_collection(await adapter().collect(make_source(), fetcher))

    assert result.complete is True
    assert result.jobs == []
    assert result.page_count == 1
    assert result.warnings == []


# --------------------------------------------------------------------------------------------
# Field mapping
# --------------------------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_maps_the_fixture_into_normalized_jobs() -> None:
    route = respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=fixture_payload()))
    source = make_source(parameters={**PARAMETERS, "total": "totalJobs"})

    async with HttpFetcher() as fetcher:
        result = validate_collection(await adapter().collect(source, fetcher))

    assert route.call_count == 1
    assert result.source_id == "northwind-generic-json"
    assert result.page_count == 1
    assert result.declared_total == 2
    assert result.warnings == []

    first, second = result.jobs
    assert first.source_job_id == "NW-1001"
    assert first.canonical_job_key == "generic-json:northwind:NW-1001"
    assert first.dedupe_key == (
        "url:northwind-robotics:https://careers.northwind.example/postings/NW-1001/apply"
    )
    assert first.title == "Senior Robotics Software Engineer"
    assert first.locations == ["Pittsburgh, PA", "Remote, US"]
    assert first.department == "Autonomy"
    assert first.employment_type is None
    assert first.description_text == (
        "Design and ship motion-planning software for warehouse robots.\n"
        "- Own the planner\n"
        "- Mentor two engineers"
    )
    assert first.content_hash == content_hash(first.title, first.locations, first.description_text)
    assert str(first.posting_url) == fixture_record(0)["url"]
    assert str(first.apply_url) == fixture_record(0)["applyUrl"]
    assert first.source_published_at == datetime(2026, 9, 15, 13, 30, tzinfo=UTC)
    assert first.first_seen_at == NOW
    assert first.raw_metadata == {"page": 1, "record_index": 0}

    assert second.source_job_id == "NW-1002"
    assert second.canonical_job_key == "generic-json:northwind:NW-1002"
    assert second.locations == ["Austin, TX"]
    assert second.description_text == "Keep deployed fleets running."

    for job in result.jobs:
        assert_normalized_job(job)


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_repeats_the_same_pages_as_stable_normalized_jobs() -> None:
    payload = fixture_payload()
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
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
async def test_generic_json_maps_location_objects_through_the_expansion_token() -> None:
    payload = {
        "results": {
            "jobs": [
                record("1", locations=[{"name": "Chicago, IL"}, {"name": "Chicago, IL"}])
            ]
        }
    }
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
    source = make_source(parameters={**PARAMETERS, "locations": "locations[].name"})

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.jobs[0].locations == ["Chicago, IL"]


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_falls_back_to_the_posting_url_when_no_apply_url_is_mapped() -> None:
    respx.get(ENDPOINT).mock(return_value=page(record("1")))
    parameters = {key: value for key, value in PARAMETERS.items() if key != "apply_url"}

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_source(parameters=parameters), fetcher)

    job = result.jobs[0]
    assert str(job.apply_url) == str(job.posting_url)
    assert job.dedupe_key == (
        "url:northwind-robotics:https://careers.northwind.example/postings/1"
    )


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["collection", "id", "title", "posting_url"])
async def test_generic_json_requires_the_registry_to_map_every_required_field(
    missing: str,
) -> None:
    route = respx.get(ENDPOINT).mock(return_value=page())
    parameters = {key: value for key, value in PARAMETERS.items() if key != missing}

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match=missing):
            await adapter().collect(make_source(parameters=parameters), fetcher)

    assert route.call_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_rejects_an_executable_selector_registry_parameter() -> None:
    route = respx.get(ENDPOINT).mock(return_value=page())
    source = make_source(parameters={**PARAMETERS, "collection": "results.jobs[*]"})

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="unusable collection selector"):
            await adapter().collect(source, fetcher)

    assert route.call_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_rejects_ambiguous_pagination_configuration() -> None:
    route = respx.get(ENDPOINT).mock(return_value=page())
    source = make_source(
        parameters={
            **PARAMETERS,
            "next_page_url": "nextPageUrl",
            "next_page_token": "nextPageToken",
        }
    )

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="next_page"):
            await adapter().collect(source, fetcher)

    assert route.call_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_rejects_an_unusable_token_parameter_name() -> None:
    route = respx.get(ENDPOINT).mock(return_value=page())
    source = make_source(
        parameters={
            **PARAMETERS,
            "next_page_token": "nextPageToken",
            "token_parameter": "page token",
        }
    )

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="token_parameter"):
            await adapter().collect(source, fetcher)

    assert route.call_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_marks_a_record_without_a_mapped_field_incomplete() -> None:
    respx.get(ENDPOINT).mock(side_effect=[page(record("1"), record("2", name=None)), page()])

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_source(), fetcher)

    assert result.complete is False
    assert [job.source_job_id for job in result.jobs] == ["1"]
    assert result.warnings == ["skipped malformed job record at index 1: missing name"]
    with pytest.raises(PartialCollectionError, match="incomplete collection"):
        validate_collection(result)


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_rejects_records_with_unusable_urls_or_timestamps() -> None:
    respx.get(ENDPOINT).mock(
        side_effect=[
            page(record("1", url="http://careers.northwind.example/postings/1")),
            page(record("1", url=None)),
            page(record("1", postedAt="yesterday")),
        ]
    )

    async with HttpFetcher() as fetcher:
        insecure = await adapter().collect(make_source(), fetcher)
        missing = await adapter().collect(make_source(), fetcher)
        bad_timestamp = await adapter().collect(make_source(), fetcher)

    assert insecure.jobs == []
    assert insecure.warnings == [
        "skipped malformed job record at index 0: url is not an absolute https URL"
    ]
    assert missing.warnings == ["skipped malformed job record at index 0: missing url"]
    assert bad_timestamp.warnings == [
        "skipped malformed job record at index 0: postedAt is not an ISO 8601 timestamp"
    ]


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_marks_a_duplicate_job_id_incomplete() -> None:
    respx.get(ENDPOINT).mock(side_effect=[page(record("1"), record("1")), page()])

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_source(), fetcher)

    assert result.complete is False
    assert [job.source_job_id for job in result.jobs] == ["1"]
    assert result.warnings == [
        "skipped duplicate job record at index 1: the source job id already appeared"
    ]


# --------------------------------------------------------------------------------------------
# Bounded pagination
# --------------------------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_follows_the_next_page_url_until_the_payload_stops() -> None:
    first_page = {
        "results": {"jobs": [record("1")]},
        "nextPageUrl": "https://api.northwind.example/v1/jobs?page=2",
    }
    second_page = {"results": {"jobs": [record("2")]}, "nextPageUrl": None}
    route = respx.get(ENDPOINT).mock(
        side_effect=[httpx.Response(200, json=first_page), httpx.Response(200, json=second_page)]
    )
    source = make_source(parameters={**PARAMETERS, "next_page_url": "nextPageUrl"})

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is True
    assert result.page_count == 2
    assert [job.source_job_id for job in result.jobs] == ["1", "2"]
    assert [str(call.request.url) for call in route.calls] == [
        ENDPOINT,
        "https://api.northwind.example/v1/jobs?page=2",
    ]


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_follows_the_next_page_token() -> None:
    first_page = {"results": {"jobs": [record("1")]}, "nextPageToken": "cursor-2"}
    last_page = {"results": {"jobs": [record("2")]}, "nextPageToken": None}
    route = respx.get(ENDPOINT).mock(
        side_effect=[httpx.Response(200, json=first_page), httpx.Response(200, json=last_page)]
    )
    source = make_source(
        parameters={
            **PARAMETERS,
            "next_page_token": "nextPageToken",
            "token_parameter": "page_token",
        }
    )

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is True
    assert [job.source_job_id for job in result.jobs] == ["1", "2"]
    assert [request_params(call.request) for call in route.calls] == [
        [],
        [("page_token", "cursor-2")],
    ]


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_owns_the_token_parameter_but_keeps_other_endpoint_parameters() -> None:
    route = respx.get(ENDPOINT).mock(return_value=page())
    source = make_source(
        endpoint=f"{ENDPOINT}?team=robotics&page_token=stale",
        parameters={
            **PARAMETERS,
            "next_page_token": "nextPageToken",
            "token_parameter": "page_token",
        },
    )

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is True
    assert request_params(route.calls[0].request) == [("team", "robotics")]


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("reference", [None, "", "   "])
async def test_generic_json_ends_the_walk_on_a_blank_next_page_reference(
    reference: object,
) -> None:
    route = respx.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200, json={"results": {"jobs": [record("1")]}, "nextPageUrl": reference}
        )
    )
    source = make_source(parameters={**PARAMETERS, "next_page_url": "nextPageUrl"})

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is True
    assert result.warnings == []
    assert result.page_count == 1
    assert route.call_count == 1
    assert [job.source_job_id for job in result.jobs] == ["1"]


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "", "   "])
async def test_generic_json_ends_the_walk_on_a_blank_next_page_token(token: object) -> None:
    route = respx.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200, json={"results": {"jobs": [record("1")]}, "nextPageToken": token}
        )
    )
    source = make_source(parameters={**PARAMETERS, "next_page_token": "nextPageToken"})

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is True
    assert result.page_count == 1
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_rejects_a_non_https_next_page_reference() -> None:
    route = respx.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": {"jobs": [record("1")]},
                "nextPageUrl": "http://api.northwind.example/v1/jobs?page=2",
            },
        )
    )
    source = make_source(parameters={**PARAMETERS, "next_page_url": "nextPageUrl"})

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="is not an absolute https URL"):
            await adapter().collect(source, fetcher)

    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_rejects_a_next_page_reference_that_is_not_a_url_string() -> None:
    route = respx.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": {"jobs": [record("1")]},
                "nextPageUrl": {"href": "https://api.northwind.example/v1/jobs?page=2"},
            },
        )
    )
    source = make_source(parameters={**PARAMETERS, "next_page_url": "nextPageUrl"})

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="is not a URL string"):
            await adapter().collect(source, fetcher)

    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_does_not_treat_a_short_page_as_the_end() -> None:
    pages = [
        httpx.Response(
            200,
            json={
                "results": {"jobs": [record("1")]},
                "nextPageUrl": "https://api.northwind.example/v1/jobs?page=2",
            },
        ),
        httpx.Response(
            200, json={"results": {"jobs": [record("2"), record("3")]}, "nextPageUrl": None}
        ),
    ]
    route = respx.get(ENDPOINT).mock(side_effect=pages)
    source = make_source(parameters={**PARAMETERS, "next_page_url": "nextPageUrl"})

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is True
    assert result.page_count == 2
    assert route.call_count == 2
    assert [job.source_job_id for job in result.jobs] == ["1", "2", "3"]


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_ends_a_complete_walk_on_an_empty_last_page() -> None:
    pages = [
        httpx.Response(
            200,
            json={
                "results": {"jobs": [record("1")]},
                "nextPageUrl": "https://api.northwind.example/v1/jobs?page=2",
            },
        ),
        httpx.Response(200, json={"results": {"jobs": []}, "nextPageUrl": None}),
    ]
    route = respx.get(ENDPOINT).mock(side_effect=pages)
    source = make_source(parameters={**PARAMETERS, "next_page_url": "nextPageUrl"})

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is True
    assert result.warnings == []
    assert result.page_count == 2
    assert route.call_count == 2
    assert [job.source_job_id for job in result.jobs] == ["1"]


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_stops_on_an_empty_page_that_still_publishes_a_next_page() -> None:
    pages = [
        httpx.Response(
            200,
            json={
                "results": {"jobs": [record("1")]},
                "nextPageUrl": "https://api.northwind.example/v1/jobs?page=2",
            },
        ),
        httpx.Response(
            200,
            json={
                "results": {"jobs": []},
                "nextPageUrl": "https://api.northwind.example/v1/jobs?page=3",
            },
        ),
    ]
    route = respx.get(ENDPOINT).mock(side_effect=pages)
    source = make_source(parameters={**PARAMETERS, "next_page_url": "nextPageUrl"})

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is False
    assert result.page_count == 2
    assert route.call_count == 2
    assert [job.source_job_id for job in result.jobs] == ["1"]
    assert result.warnings == [
        "stopped at page 2: the page published no job records while it still published a next "
        "page, so the collection may be incomplete"
    ]
    with pytest.raises(PartialCollectionError, match="incomplete collection"):
        validate_collection(result)


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_reports_a_repeated_next_page_reference_as_incomplete() -> None:
    route = respx.get(ENDPOINT).mock(
        return_value=httpx.Response(
            200, json={"results": {"jobs": [record("1")]}, "nextPageUrl": ENDPOINT}
        )
    )
    source = make_source(parameters={**PARAMETERS, "next_page_url": "nextPageUrl"})

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is False
    assert route.call_count == 1
    assert result.page_count == 1
    assert result.warnings == [
        "pagination loop detected after page 1: the next page reference repeated one already "
        "read, so the collection is incomplete"
    ]


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_reports_a_repeated_page_as_incomplete() -> None:
    pages = [
        httpx.Response(
            200,
            json={
                "results": {"jobs": [record("1"), record("2")]},
                "nextPageUrl": "https://api.northwind.example/v1/jobs?page=2",
            },
        ),
        httpx.Response(
            200,
            json={
                "results": {"jobs": [record("2"), record("1")]},
                "nextPageUrl": "https://api.northwind.example/v1/jobs?page=3",
            },
        ),
    ]
    route = respx.get(ENDPOINT).mock(side_effect=pages)
    source = make_source(parameters={**PARAMETERS, "next_page_url": "nextPageUrl"})

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is False
    assert route.call_count == 2
    assert result.page_count == 2
    assert [job.source_job_id for job in result.jobs] == ["1", "2"]
    assert result.warnings == [
        "pagination loop detected at page 2: the page repeated an earlier page's job ids, so the "
        "collection is incomplete"
    ]
    with pytest.raises(PartialCollectionError, match="incomplete collection"):
        validate_collection(result)


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_stops_at_the_page_cap_without_reporting_the_walk_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(generic_json_module, "MAX_PAGES", 2)
    pages = [
        httpx.Response(
            200,
            json={
                "results": {"jobs": [record(str(page_number))]},
                "nextPageUrl": f"https://api.northwind.example/v1/jobs?page={page_number + 1}",
            },
        )
        for page_number in (1, 2)
    ]
    route = respx.get(ENDPOINT).mock(side_effect=pages)
    source = make_source(parameters={**PARAMETERS, "next_page_url": "nextPageUrl"})

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is False
    assert result.page_count == 2
    assert route.call_count == 2
    assert result.warnings == [
        "stopped after 2 pages: the page cap is reached, so the collection may be incomplete"
    ]


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_accepts_an_empty_board() -> None:
    respx.get(ENDPOINT).mock(return_value=page())

    async with HttpFetcher() as fetcher:
        result = validate_collection(await adapter().collect(make_source(), fetcher))

    assert result.complete is True
    assert result.jobs == []
    assert result.page_count == 1
    assert result.declared_total is None
    assert result.warnings == []


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_compares_a_mapped_declared_total() -> None:
    payload = {"results": {"jobs": [record("1"), record("2")]}, "totalJobs": 3}
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
    source = make_source(parameters={**PARAMETERS, "total": "totalJobs"})

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(source, fetcher)

    assert result.complete is False
    assert result.declared_total == 3
    assert result.warnings == [
        "collected 2 unique postings but the payload declared totalJobs=3, so the collection is "
        "incomplete"
    ]


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_requires_a_mapped_total_on_every_page() -> None:
    respx.get(ENDPOINT).mock(return_value=page(record("1")))
    source = make_source(parameters={**PARAMETERS, "total": "totalJobs"})

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="totalJobs"):
            await adapter().collect(source, fetcher)


# --------------------------------------------------------------------------------------------
# Response shape, transport, and identity defaults
# --------------------------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_rejects_a_payload_without_the_collection() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"results": {"postings": []}}))

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="results.jobs"):
            await adapter().collect(make_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_rejects_a_payload_that_is_not_json() -> None:
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
async def test_generic_json_rejects_a_payload_that_is_not_a_json_object() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=[1, 2, 3]))

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="is not a JSON object"):
            await adapter().collect(make_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_rejects_a_not_modified_response() -> None:
    respx.get(ENDPOINT).mock(return_value=httpx.Response(304))

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="304"):
            await adapter().collect(make_source(), fetcher)


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_treats_a_challenge_page_as_an_error() -> None:
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
async def test_generic_json_sends_the_configured_source_headers() -> None:
    route = respx.get(ENDPOINT).mock(return_value=page(record("1")))
    source = make_source(headers={"Accept": "application/json", "X-Source-Audit": "2026-09-19"})

    async with HttpFetcher() as fetcher:
        await adapter().collect(source, fetcher)

    sent = route.calls[0].request.headers
    assert sent["Accept"] == "application/json"
    assert sent["X-Source-Audit"] == "2026-09-19"


@respx.mock
@pytest.mark.asyncio
async def test_generic_json_falls_back_to_the_endpoint_host_for_the_tenant() -> None:
    respx.get(ENDPOINT).mock(return_value=page(record("1")))

    async with HttpFetcher() as fetcher:
        result = await adapter().collect(make_source(tenant=None), fetcher)

    assert result.jobs[0].canonical_job_key == "generic-json:api.northwind.example:1"
