"""What a source's registry parameters stamp onto every posting it collected."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import HttpUrl

from job_alert.adapters.base import AdapterError, apply_source_assertions
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig


def source(**parameters: object) -> SourceConfig:
    return SourceConfig.model_validate(
        {
            "id": "acme-greenhouse",
            "company_id": "acme",
            "provider": "greenhouse",
            "source_class": "documented_api",
            "support_status": "SUPPORTED",
            "endpoint": "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
            "interval_minutes": 5,
            "parameters": parameters,
        }
    )


def job(job_id: str, *locations: str) -> NormalizedJob:
    url = f"https://jobs.acme.example/{job_id}"
    return NormalizedJob(
        company_id="acme",
        source_id="acme-greenhouse",
        source_job_id=job_id,
        canonical_job_key=f"greenhouse:acme:{job_id}",
        dedupe_key=f"acme:{job_id}",
        title="Mechanical Engineer Intern",
        locations=list(locations),
        content_hash="0" * 64,
        posting_url=HttpUrl(url),
        apply_url=HttpUrl(url),
        first_seen_at=datetime(2026, 10, 14, tzinfo=UTC),
    )


def collected(*jobs: NormalizedJob) -> CollectionResult:
    return CollectionResult(source_id="acme-greenhouse", complete=True, jobs=list(jobs))


def test_an_asserted_country_names_itself_only_on_labels_that_place_nothing() -> None:
    result = apply_source_assertions(
        collected(
            job("1", "San Francisco"),
            job("2", "Remote", "Huntsville AL"),
            job("3", "London, United Kingdom"),
            job("4"),
        ),
        source(asserted_country="United States"),
    )

    assert [item.locations for item in result.jobs] == [
        ["San Francisco, United States"],
        ["Remote, United States", "Huntsville AL"],
        ["London, United Kingdom"],
        ["United States"],
    ]


def test_without_assertions_the_collection_is_returned_unchanged() -> None:
    original = collected(job("1", "San Francisco"))

    assert apply_source_assertions(original, source()) is original


@pytest.mark.parametrize("value", ["", "Narnia", 7])
def test_an_asserted_country_that_names_no_country_is_a_configuration_error(
    value: object,
) -> None:
    with pytest.raises(AdapterError, match="asserted_country"):
        apply_source_assertions(collected(job("1", "Boston")), source(asserted_country=value))
