"""Shared assertions for provider adapter tests."""

from __future__ import annotations

from datetime import datetime

from job_alert.models import CollectionResult, NormalizedJob

PLACEHOLDER_URL_MARKERS = (
    "example.com",
    "example.org",
    "example.net",
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    ".invalid",
    "placeholder",
    "changeme",
    "your-domain",
    "yourcompany",
)

CORE_FIELDS = (
    "source_job_id",
    "canonical_job_key",
    "dedupe_key",
    "requisition_id",
    "title",
    "locations",
    "department",
    "team",
    "employment_type",
    "workplace_type",
    "content_hash",
    "posting_url",
    "apply_url",
    "source_published_at",
    "source_updated_at",
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _is_blank(value: str | None) -> bool:
    return value is None or not value.strip()


def _contains_placeholder(url: str) -> bool:
    lowered = url.lower()
    return any(marker in lowered for marker in PLACEHOLDER_URL_MARKERS)


def assert_normalized_job(job: NormalizedJob) -> None:
    """Assert a normalized job is complete, stable, and free of test stand-ins."""
    label = f"{job.source_id}/{job.source_job_id}"
    _require(not _is_blank(job.source_job_id), f"{label}: source_job_id must be nonblank")
    _require(not _is_blank(job.canonical_job_key), f"{label}: canonical_job_key must be nonblank")
    _require(not _is_blank(job.dedupe_key), f"{label}: dedupe_key must be nonblank")
    _require(not _is_blank(job.title), f"{label}: title must be nonblank")

    for field in ("posting_url", "apply_url"):
        url = str(getattr(job, field))
        _require(url.startswith("https://"), f"{label}: {field} must use https, got {url}")
        _require(
            not _contains_placeholder(url),
            f"{label}: {field} looks like a test placeholder: {url}",
        )

    first_seen_at: datetime = job.first_seen_at
    _require(
        first_seen_at.tzinfo is not None and first_seen_at.utcoffset() is not None,
        f"{label}: first_seen_at must be timezone-aware, got {first_seen_at.isoformat()}",
    )


def assert_repeat_is_stable(first: CollectionResult, second: CollectionResult) -> None:
    """Assert two collections from the same source describe the same normalized jobs."""
    _require(
        first.source_id == second.source_id,
        f"repeat collections came from different sources: {first.source_id} != {second.source_id}",
    )

    first_keys = sorted(job.canonical_job_key for job in first.jobs)
    second_keys = sorted(job.canonical_job_key for job in second.jobs)
    _require(
        first_keys == second_keys,
        f"repeat collections produced different canonical keys: {first_keys} != {second_keys}",
    )

    first_by_key = {job.canonical_job_key: job for job in first.jobs}
    second_by_key = {job.canonical_job_key: job for job in second.jobs}
    for key in first_keys:
        changed = [
            field
            for field in CORE_FIELDS
            if getattr(first_by_key[key], field) != getattr(second_by_key[key], field)
        ]
        _require(
            not changed,
            f"repeated collection changed {', '.join(changed)} for canonical key {key}",
        )
