"""Shared pieces for the small ATS adapters (Rippling, Workable, BambooHR, Pinpoint, Oracle, UKG).

Each of those providers publishes a public JSON board whose records differ only in field names, so
the adapters share two things:

- ``make_job()`` builds the ``NormalizedJob`` every adapter would otherwise spell out, with the
  identity rules the rest of the tree uses (``{provider}:{tenant}:{id}`` and the shared dedupe key).
  A record that cannot become a job raises ``UnusableRecord``.
- ``collect_document()`` turns one whole-board document into a ``CollectionResult``: an unusable
  record or a duplicate id is a warning that makes the collection incomplete, and a board that
  declares its size must match it.

The paged providers (Oracle, UKG) walk with ``paged.walk_scoped`` instead of ``collect_document()``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

from pydantic import HttpUrl, ValidationError

from job_alert.adapters.base import AdapterError
from job_alert.adapters.common import optional_text
from job_alert.adapters.paged import UnusableRecord
from job_alert.fetch import HttpPayload
from job_alert.identity import build_dedupe_key, build_job_key, content_hash
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig


def utcnow() -> datetime:
    return datetime.now(UTC)


def json_document(payload: HttpPayload, provider: str) -> Any:
    """The parsed body, or an ``AdapterError`` naming the provider."""
    if payload.not_modified:
        raise AdapterError(f"{provider} returned 304 Not Modified for {payload.final_url}")
    try:
        return json.loads(payload.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        detail = f"{provider} payload from {payload.final_url} is not JSON: {exc}"
        raise AdapterError(detail) from exc


def joined(*parts: object) -> str | None:
    """Nonblank parts joined with ``, `` (``"Austin, Texas, US"``), or ``None`` if all blank."""
    texts = [text for text in (optional_text(part) for part in parts) if text is not None]
    return ", ".join(texts) or None


def unique(values: Iterable[str | None]) -> list[str]:
    """Nonblank values in first-seen order without repeats."""
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


def parse_date(value: object) -> datetime | None:
    """An ISO 8601 date or timestamp as UTC; a bare date is midnight UTC; anything else ``None``."""
    text = optional_text(value)
    if text is None:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            day = date.fromisoformat(text[:10])
        except ValueError:
            return None
        return datetime(day.year, day.month, day.day, tzinfo=UTC)
    return moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)


def make_job(
    *,
    provider: str,
    tenant: str,
    source: SourceConfig,
    job_id: object,
    title: object,
    posting_url: str | None,
    first_seen_at: datetime,
    locations: Sequence[str] = (),
    apply_url: str | None = None,
    department: object = None,
    team: object = None,
    employment_type: object = None,
    workplace_type: str | None = None,
    requisition_id: object = None,
    published_at: datetime | None = None,
) -> NormalizedJob:
    """One normalized job, or ``UnusableRecord`` when the record lacks an id, title or link."""
    identifier, name = optional_text(job_id), optional_text(title)
    if identifier is None:
        raise UnusableRecord("missing id")
    if name is None:
        raise UnusableRecord("missing title")
    if posting_url is None or not posting_url.startswith("https://"):
        raise UnusableRecord("missing an absolute https posting URL")
    name = " ".join(name.split())
    places = list(locations)
    dept = optional_text(department)
    requisition = optional_text(requisition_id)
    application = apply_url if apply_url and apply_url.startswith("https://") else posting_url
    try:
        return NormalizedJob(
            company_id=source.company_id,
            source_id=source.id,
            source_job_id=identifier,
            canonical_job_key=build_job_key(
                provider=provider,
                tenant=tenant,
                source_job_id=identifier,
                company_id=source.company_id,
                title=name,
                locations=places,
                apply_url=application,
            ),
            dedupe_key=build_dedupe_key(
                company_id=source.company_id,
                requisition_id=requisition,
                apply_url=application,
                title=name,
                locations=places,
                department=dept,
            ),
            requisition_id=requisition,
            title=name,
            locations=places,
            department=dept,
            team=optional_text(team),
            employment_type=optional_text(employment_type),
            workplace_type=workplace_type,
            content_hash=content_hash(name, places, None),
            posting_url=HttpUrl(posting_url),
            apply_url=HttpUrl(application),
            source_published_at=published_at,
            first_seen_at=first_seen_at,
        )
    except ValidationError as exc:
        raise UnusableRecord("record does not normalize into a job") from exc


def collect_document(
    *,
    source: SourceConfig,
    records: Sequence[object],
    normalize: Callable[[Mapping[str, Any]], NormalizedJob],
    declared: int | None = None,
) -> CollectionResult:
    """A whole-board document as a collection; any defect makes it incomplete."""
    jobs: list[NormalizedJob] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            warnings.append(f"skipped malformed job record at index {index}: not an object")
            continue
        try:
            job = normalize(record)
        except UnusableRecord as exc:
            warnings.append(f"skipped malformed job record at index {index}: {exc}")
            continue
        if job.source_job_id in seen:
            warnings.append(f"skipped duplicate job record at index {index}: the id repeated")
            continue
        seen.add(job.source_job_id)
        jobs.append(job)
    if declared is not None and declared != len(records):
        warnings.append(
            f"the board declares {declared} postings but published {len(records)},"
            " so the collection is incomplete"
        )
    return CollectionResult(
        source_id=source.id,
        complete=not warnings,
        jobs=jobs,
        warnings=warnings,
        page_count=1,
        declared_total=declared,
    )
