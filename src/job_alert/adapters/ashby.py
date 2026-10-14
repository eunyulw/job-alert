"""Ashby Job Postings adapter.

``https://api.ashbyhq.com/posting-api/job-board/{board}`` answers with the whole published board
in one JSON document, so a collection costs exactly one request. Ashby publishes no total and no
pagination on this endpoint, and the adapter invents neither: completeness rests on the validated
envelope, the single request, and the unique-id check, which is why ``declared_total`` stays
``None``.

Mapping decisions the payload does not spell out:

- ``source_job_id`` is the published record ``id`` and identity is ``ashby:{tenant}:{id}``.
- ``locations`` is ``location`` followed by every ``secondaryLocations[*].location``,
  deduplicated case-insensitively in provider order: the board publishes one display string for the
  primary site and an object per additional site.
- ``department``, ``team``, ``employmentType`` and ``workplaceType`` are copied
  verbatim. The board publishes ``workplaceType`` as ``Hybrid``, ``OnSite``,
  ``Remote`` or ``null``, and an absent or blank optional field leaves the normalized field
  ``None`` instead of invalidating the job.
- ``description_text`` is ``descriptionPlain`` when the posting publishes nonblank plain text,
  which every posting of the captured board did. When it does not, the adapter converts
  ``descriptionHtml`` with the shared ``html_to_text()`` helper, so an HTML-only posting still
  gets deterministic text and a content hash the next collection will reproduce.
- ``posting_url`` is ``jobUrl`` and ``apply_url`` is ``applyUrl``. The payload
  publishes both, so the adapter renders no apply-URL template and the two URLs stay distinct.
- ``publishedAt`` is the publication timestamp: an ISO 8601 value that must carry a UTC offset and
  is normalized to UTC. ``source_updated_at`` stays ``None`` because this endpoint publishes
  no update timestamp.
- ``requisition_id`` stays ``None``: the payload publishes no requisition id, so the dedupe
  key is ``url:{company_id}:{applyUrl}``.

Challenge pages and HTTP failures are classified by ``HttpFetcher`` before this adapter sees the
body, so a challenge is never mistaken for an empty board. A document without a top-level
``jobs`` array is an error, not an empty board.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from urllib.parse import urlsplit

from pydantic import HttpUrl, ValidationError

from job_alert.adapters.base import AdapterError
from job_alert.adapters.common import html_to_text, https_url, optional_text
from job_alert.fetch import HttpFetcher, HttpPayload, RequestSpec
from job_alert.geo import location_country
from job_alert.identity import build_dedupe_key, build_job_key, content_hash
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "ashby"

_BOARD_PATH = re.compile(r"/job-board/(?P<board>[^/]+)/?$")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _UnusableJob(ValueError):
    """One provider record cannot be normalized, so the collection is incomplete."""


class AshbyAdapter:
    """Normalize one Ashby board into jobs with a single request per collection."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        tenant = _resolve_tenant(source)
        payload = await fetcher.get(
            RequestSpec(
                url=str(source.endpoint),
                headers=dict(source.headers),
                expected="json",
            )
        )
        records = _parse_board(payload)
        first_seen_at = self._now()

        jobs: list[NormalizedJob] = []
        warnings: list[str] = []
        seen: set[str] = set()
        for index, record in enumerate(records):
            try:
                job = _normalize_job(
                    record, source=source, tenant=tenant, first_seen_at=first_seen_at
                )
            except _UnusableJob as exc:
                warnings.append(f"skipped malformed job record at index {index}: {exc}")
                continue
            if job.canonical_job_key in seen:
                warnings.append(
                    f"skipped duplicate job record at index {index}: "
                    "the source job id already appeared"
                )
                continue
            seen.add(job.canonical_job_key)
            jobs.append(job)

        return CollectionResult(
            source_id=source.id,
            complete=not warnings,
            jobs=jobs,
            warnings=warnings,
            page_count=1,
            # The posting endpoint publishes no total to compare the parsed postings with, so
            # completeness comes from the validated envelope and the unique-id check above.
            declared_total=None,
        )


def _resolve_tenant(source: SourceConfig) -> str:
    """The board token that makes a source job ID unique."""
    if source.tenant is not None and source.tenant.strip():
        return source.tenant.strip()
    match = _BOARD_PATH.search(urlsplit(str(source.endpoint)).path)
    if match is not None:
        return match.group("board")
    raise AdapterError(
        f"ashby source {source.id} has no tenant: set tenant or point the endpoint at "
        "/posting-api/job-board/{board}"
    )


def _parse_board(payload: HttpPayload) -> list[object]:
    """Validate the response envelope and return its postings."""
    if payload.not_modified:
        raise AdapterError(f"ashby returned 304 Not Modified for {payload.final_url}")
    media_type = payload.headers.get("Content-Type")
    try:
        document = json.loads(payload.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError(
            f"ashby payload from {payload.final_url} is not JSON ({media_type}): {exc}"
        ) from exc
    if not isinstance(document, Mapping):
        raise AdapterError("ashby payload is not a JSON object")
    records = document.get("jobs")
    if not isinstance(records, list):
        raise AdapterError("ashby payload has no 'jobs' array")
    return records


def _normalize_job(
    record: object,
    *,
    source: SourceConfig,
    tenant: str,
    first_seen_at: datetime,
) -> NormalizedJob:
    if not isinstance(record, Mapping):
        raise _UnusableJob("record is not a JSON object")
    source_job_id = _source_job_id(record)
    title = _title(record)
    try:
        posting_url = https_url(record.get("jobUrl"), field="jobUrl")
        apply_url = https_url(record.get("applyUrl"), field="applyUrl")
    except ValueError as exc:
        raise _UnusableJob(str(exc)) from exc
    locations = _locations(record)
    department = optional_text(record.get("department"))
    description_text = _description_text(record)
    try:
        return NormalizedJob(
            company_id=source.company_id,
            source_id=source.id,
            source_job_id=source_job_id,
            canonical_job_key=build_job_key(
                provider=PROVIDER,
                tenant=tenant,
                source_job_id=source_job_id,
                company_id=source.company_id,
                title=title,
                locations=locations,
                apply_url=apply_url,
            ),
            dedupe_key=build_dedupe_key(
                company_id=source.company_id,
                requisition_id=None,
                apply_url=apply_url,
                title=title,
                locations=locations,
                department=department,
            ),
            title=title,
            locations=locations,
            department=department,
            team=optional_text(record.get("team")),
            employment_type=optional_text(record.get("employmentType")),
            workplace_type=optional_text(record.get("workplaceType")),
            description_text=description_text,
            content_hash=content_hash(title, locations, description_text),
            # ``NormalizedJob`` types both URLs as ``HttpUrl``, so validate them here and let
            # the surrounding handler turn an unusable URL into an incomplete collection.
            posting_url=HttpUrl(posting_url),
            apply_url=HttpUrl(apply_url),
            source_published_at=_timestamp(record.get("publishedAt"), "publishedAt"),
            first_seen_at=first_seen_at,
            raw_metadata=_raw_metadata(record),
        )
    except ValidationError as exc:
        raise _UnusableJob("record does not normalize into a job") from exc


def _source_job_id(record: Mapping[str, object]) -> str:
    value = record.get("id")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise _UnusableJob("missing id")
    source_job_id = str(value).strip()
    if not source_job_id:
        raise _UnusableJob("missing id")
    return source_job_id


def _title(record: Mapping[str, object]) -> str:
    value = record.get("title")
    if not isinstance(value, str) or not value.strip():
        raise _UnusableJob("missing title")
    return value


def _locations(record: Mapping[str, object]) -> list[str]:
    """The primary display location followed by every additional site, deduplicated.

    A display label that names no country ("San Francisco") gets the country of the site's postal
    address appended ("San Francisco, United States"), so a country filter can place it.
    """
    candidates: list[tuple[object, object]] = [(record.get("location"), record.get("address"))]
    secondary = record.get("secondaryLocations")
    if isinstance(secondary, list):
        candidates.extend(_secondary_location(item) for item in secondary)

    locations: list[str] = []
    seen: set[str] = set()
    for candidate, address in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        text = " ".join(candidate.split())
        key = text.casefold()  # deduplicate on the display label, before a country is added
        if key in seen:
            continue
        seen.add(key)
        country = _address_country(address)
        if country and location_country(text) is None:
            text = f"{text}, {country}"
        locations.append(text)
    return locations


def _address_country(address: object) -> str | None:
    postal = address.get("postalAddress") if isinstance(address, Mapping) else None
    country = postal.get("addressCountry") if isinstance(postal, Mapping) else None
    return " ".join(country.split()) if isinstance(country, str) and country.strip() else None


def _secondary_location(item: object) -> tuple[object, object]:
    """The display location and address of one additional site.

    The captured board publishes each site as an object with a ``location`` string; a bare string
    is accepted too, so a board that changes shape does not silently lose a site.
    """
    if isinstance(item, Mapping):
        return item.get("location"), item.get("address")
    return item, None


def _description_text(record: Mapping[str, object]) -> str | None:
    """The published plain text, else the shared conversion of the published HTML."""
    plain = record.get("descriptionPlain")
    if isinstance(plain, str) and plain.strip():
        return plain
    return html_to_text(record.get("descriptionHtml"))


def _timestamp(value: object, field: str) -> datetime | None:
    """Parse an ISO 8601 publication timestamp into an aware UTC datetime."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _UnusableJob(f"{field} is not an ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _UnusableJob(f"{field} is not an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise _UnusableJob(f"{field} has no UTC offset")
    return parsed.astimezone(UTC)


def _raw_metadata(record: Mapping[str, object]) -> dict[str, object]:
    """Provider bookkeeping kept for diagnosis; not part of job identity."""
    address = record.get("address")
    secondary = record.get("secondaryLocations")
    return {
        "is_listed": record.get("isListed"),
        "is_remote": record.get("isRemote"),
        "address": dict(address) if isinstance(address, Mapping) else {},
        "secondary_location_count": len(secondary) if isinstance(secondary, list) else 0,
    }
