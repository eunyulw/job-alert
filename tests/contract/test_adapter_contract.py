from datetime import UTC, datetime

import pytest

from job_alert.adapters.base import AdapterError, PartialCollectionError, validate_collection
from job_alert.models import CollectionResult, NormalizedJob
from tests.helpers import STABLE_FIELDS, assert_normalized_job, assert_repeat_is_stable

SOURCE_ID = "acme-greenhouse"


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


FULL_JOB_FIELDS: dict[str, object] = {
    "company_id": "acme",
    "source_id": SOURCE_ID,
    "source_job_id": "1",
    "canonical_job_key": "greenhouse:acme:1",
    "dedupe_key": "requisition:acme:1",
    "requisition_id": "REQ-1",
    "title": "Software Engineer",
    "locations": ["Chicago, IL"],
    "department": "Engineering",
    "team": "Platform",
    "employment_type": "Full-time",
    "workplace_type": "Hybrid",
    "description_text": "Build job alerts for every supported provider.",
    "content_hash": "sha256:aaaa",
    "posting_url": "https://boards.greenhouse.io/acme/jobs/1",
    "apply_url": "https://boards.greenhouse.io/acme/jobs/1/apply",
    "source_published_at": datetime(2026, 9, 1, tzinfo=UTC),
    "source_updated_at": datetime(2026, 9, 2, tzinfo=UTC),
    "first_seen_at": datetime(2026, 9, 18, tzinfo=UTC),
    "raw_metadata": {"provider_field": "value"},
}

# One different, valid value per stable field. The coverage tests below fail when NormalizedJob
# gains a field without a matching mutation here.
STABLE_FIELD_MUTATIONS: dict[str, object] = {
    "company_id": "globex",
    "source_id": "globex-greenhouse",
    "source_job_id": "2",
    "canonical_job_key": "greenhouse:acme:2",
    "dedupe_key": "requisition:acme:2",
    "requisition_id": "REQ-2",
    "title": "Staff Engineer",
    "locations": ["Austin, TX"],
    "department": "Sales",
    "team": "Growth",
    "employment_type": "Contract",
    "workplace_type": "Remote",
    "description_text": "Sell job alerts for every supported provider.",
    "content_hash": "sha256:bbbb",
    "posting_url": "https://boards.greenhouse.io/acme/jobs/2",
    "apply_url": "https://boards.greenhouse.io/acme/jobs/2/apply",
    "source_published_at": datetime(2026, 9, 3, tzinfo=UTC),
    "source_updated_at": datetime(2026, 9, 4, tzinfo=UTC),
}


def make_full_job(**overrides: object) -> NormalizedJob:
    return NormalizedJob.model_validate({**FULL_JOB_FIELDS, **overrides})


def make_full_collection(**job_overrides: object) -> CollectionResult:
    return CollectionResult(
        source_id=SOURCE_ID, complete=True, jobs=[make_full_job(**job_overrides)]
    )


def assert_changed_field_is_detected(field: str) -> None:
    changed = make_full_collection(**{field: STABLE_FIELD_MUTATIONS[field]})

    with pytest.raises(AssertionError) as excinfo:
        assert_repeat_is_stable(make_full_collection(), changed)

    message = str(excinfo.value)
    assert field in message or "canonical keys" in message


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


def test_validate_collection_accepts_empty_complete_collection_with_zero_total() -> None:
    result = CollectionResult(source_id="source", complete=True, jobs=[], declared_total=0)
    assert validate_collection(result) is result


def test_validate_collection_accepts_complete_collection_without_declared_total() -> None:
    result = CollectionResult(source_id="source", complete=True, jobs=[make_job()])
    assert result.declared_total is None
    assert validate_collection(result) is result


def test_validate_collection_rejects_incomplete_collection() -> None:
    result = CollectionResult(source_id="source", complete=False, jobs=[make_job()])
    with pytest.raises(PartialCollectionError, match="incomplete collection"):
        validate_collection(result)


def test_validate_collection_rejects_incomplete_collection_with_mismatched_total() -> None:
    result = CollectionResult(
        source_id="source", complete=False, jobs=[make_job()], declared_total=5
    )
    with pytest.raises(PartialCollectionError, match="adapter returned an incomplete collection"):
        validate_collection(result)


def test_stable_field_mutations_cover_every_stable_field() -> None:
    assert set(STABLE_FIELD_MUTATIONS) == set(STABLE_FIELDS)


def test_full_job_fixture_covers_every_model_field() -> None:
    assert set(FULL_JOB_FIELDS) == set(NormalizedJob.model_fields)


def test_assert_normalized_job_accepts_a_realistic_job() -> None:
    assert_normalized_job(make_full_job())


def test_assert_normalized_job_rejects_placeholder_urls() -> None:
    with pytest.raises(AssertionError, match="test placeholder"):
        assert_normalized_job(make_job())


def test_assert_repeat_is_stable_accepts_identical_collections() -> None:
    assert_repeat_is_stable(make_full_collection(), make_full_collection())


@pytest.mark.parametrize("field", sorted(STABLE_FIELDS))
def test_assert_repeat_is_stable_detects_each_stable_field_change(field: str) -> None:
    assert_changed_field_is_detected(field)


def test_assert_repeat_is_stable_detects_company_id_change() -> None:
    assert_changed_field_is_detected("company_id")


def test_assert_repeat_is_stable_detects_description_text_change() -> None:
    assert_changed_field_is_detected("description_text")


def test_assert_repeat_is_stable_detects_content_hash_change() -> None:
    assert_changed_field_is_detected("content_hash")


def test_assert_repeat_is_stable_detects_description_change_without_content_hash() -> None:
    baseline = CollectionResult(
        source_id=SOURCE_ID, complete=True, jobs=[make_full_job(content_hash=None)]
    )
    changed = CollectionResult(
        source_id=SOURCE_ID,
        complete=True,
        jobs=[make_full_job(content_hash=None, description_text="Rewritten description.")],
    )

    with pytest.raises(AssertionError, match="description_text"):
        assert_repeat_is_stable(baseline, changed)


def test_assert_repeat_is_stable_ignores_volatile_fields() -> None:
    later = make_full_collection(
        first_seen_at=datetime(2026, 9, 19, tzinfo=UTC),
        raw_metadata={"provider_field": "changed", "extra": 1},
    )
    assert_repeat_is_stable(make_full_collection(), later)
