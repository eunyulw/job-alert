from datetime import UTC, datetime

import pytest

from job_alert.adapters.base import AdapterError, PartialCollectionError, validate_collection
from job_alert.models import CollectionResult, NormalizedJob
from tests.helpers import assert_normalized_job, assert_repeat_is_stable


def make_job(key: str = "provider:tenant:1") -> NormalizedJob:
    return NormalizedJob(
        company_id="company",
        source_id="source",
        source_job_id="1",
        canonical_job_key=key,
        dedupe_key="requisition:company:1",
        title="Engineer",
        locations=["Chicago, IL"],
        posting_url="https://example.com/jobs/1",
        apply_url="https://example.com/jobs/1/apply",
        first_seen_at=datetime.now(UTC),
    )


def make_realistic_job(key: str = "greenhouse:acme:1") -> NormalizedJob:
    return NormalizedJob(
        company_id="acme",
        source_id="acme-greenhouse",
        source_job_id="1",
        canonical_job_key=key,
        dedupe_key="requisition:acme:1",
        title="Software Engineer",
        locations=["Chicago, IL"],
        posting_url="https://boards.greenhouse.io/acme/jobs/1",
        apply_url="https://boards.greenhouse.io/acme/jobs/1/apply",
        first_seen_at=datetime.now(UTC),
    )


def test_validate_collection_rejects_declared_total_mismatch() -> None:
    result = CollectionResult(
        source_id="source", complete=True, jobs=[make_job()], declared_total=2
    )
    with pytest.raises(AdapterError, match="declared 2 jobs but parsed 1"):
        validate_collection(result)


def test_validate_collection_returns_matching_complete_collection() -> None:
    result = CollectionResult(
        source_id="source", complete=True, jobs=[make_job()], declared_total=1
    )
    assert validate_collection(result) is result


def test_validate_collection_rejects_incomplete_collection() -> None:
    result = CollectionResult(source_id="source", complete=False, jobs=[make_job()])
    with pytest.raises(PartialCollectionError, match="incomplete collection"):
        validate_collection(result)


def test_shared_helpers_accept_a_realistic_normalized_job() -> None:
    job = make_realistic_job()
    assert_normalized_job(job)

    first = CollectionResult(source_id="acme-greenhouse", complete=True, jobs=[job])
    second = CollectionResult(source_id="acme-greenhouse", complete=True, jobs=[job])
    assert_repeat_is_stable(first, second)


def test_shared_helpers_reject_placeholder_urls() -> None:
    with pytest.raises(AssertionError, match="test placeholder"):
        assert_normalized_job(make_job())


def test_shared_helpers_reject_changed_core_fields() -> None:
    first = CollectionResult(
        source_id="acme-greenhouse", complete=True, jobs=[make_realistic_job()]
    )
    changed = make_realistic_job().model_copy(update={"title": "Staff Engineer"})
    second = CollectionResult(source_id="acme-greenhouse", complete=True, jobs=[changed])

    with pytest.raises(AssertionError, match="changed title"):
        assert_repeat_is_stable(first, second)
