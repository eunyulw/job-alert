"""Lever Job Board adapter.

``https://api.lever.co/v0/postings/{site}?mode=json`` answers with a JSON array of at most
``limit`` postings per request, so one collection walks the board page by page with ``skip`` until a
page comes back shorter than ``limit``. Lever's list endpoint publishes no total, so
``declared_total`` stays ``None`` and completeness rests on that short-page rule, the pagination
loop guard, and the page cap described below.

Mapping decisions the payload does not spell out:

- ``source_job_id`` is the posting ``id`` and per-source identity is ``lever:{site}:{id}``.
- ``locations`` is ``categories.location`` followed by every ``categories.allLocations`` entry,
  deduplicated case-insensitively in provider order: Lever repeats the display location inside
  ``allLocations`` and lists one entry per site for postings open in several places.
- ``team``, ``department`` and ``employment_type`` come from ``categories.team``,
  ``categories.department`` and ``categories.commitment``, and ``workplace_type`` from the
  top-level ``workplaceType``.
- ``description_text`` is ``descriptionPlain`` unchanged. Lever publishes ready-made plain text, so
  the adapter neither extracts HTML nor reflows the description.
- ``posting_url`` and ``apply_url`` are ``hostedUrl`` and ``applyUrl``. Lever publishes both, so the
  adapter renders no apply-URL template and shares no HTML or template helper with Greenhouse.
- ``createdAt`` is a millisecond epoch and becomes a UTC ``source_published_at``. A posting without
  ``createdAt`` keeps ``None`` instead of a synthesized timestamp. ``source_updated_at`` stays
  ``None``: the list endpoint in the captured fixture does not publish an update timestamp and the
  adapter does not invent one.

Pagination rules:

- Every request asks for ``mode=json``, the current ``skip`` and the ``limit`` taken from the
  source's ``page_size`` parameter (default ``100``). Other parameters of the configured endpoint,
  such as a ``team`` filter, are preserved; ``mode``, ``skip`` and ``limit`` belong to the adapter.
- The walk ends as soon as a page holds fewer than ``page_size`` postings. ``skip`` advances by the
  number of postings actually returned, which keeps pages from overlapping if a provider answers a
  full page with more postings than it was asked for.
- A page whose job ids are exactly a page already seen is a loop: the adapter keeps the jobs it
  already has and reports ``complete=False`` with a warning instead of following the loop or
  truncating the collection silently.
- ``MAX_PAGES`` is a last-resort cap. Reaching it is an incomplete collection, never a complete
  one, so a runaway walk cannot look like a finished board.

Challenge pages and HTTP failures are classified by ``HttpFetcher`` before this adapter sees the
body, so a challenge is never mistaken for an empty board. A JSON document that is not an array is
an error, not an empty board.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import HttpUrl, ValidationError

from job_alert.adapters.base import AdapterError
from job_alert.fetch import HttpFetcher, HttpPayload, RequestSpec
from job_alert.identity import build_dedupe_key, build_job_key, content_hash
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "lever"

PAGE_SIZE_PARAMETER = "page_size"
DEFAULT_PAGE_SIZE = 100
MAX_PAGES = 1000

# Query parameters the adapter sets for every page request, whatever the configured endpoint says.
OWNED_QUERY_PARAMETERS = frozenset({"mode", "skip", "limit"})

_POSTINGS_PATH = re.compile(r"/v0/postings/(?P<site>[^/]+)/?$")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _UnusableJob(ValueError):
    """One provider record cannot be normalized, so the collection is incomplete."""


class LeverAdapter:
    """Normalize one Lever board into jobs, one JSON page per request."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        tenant = _resolve_tenant(source)
        page_size = _page_size(source)
        list_url = _list_url(str(source.endpoint))
        first_seen_at = self._now()

        jobs: list[NormalizedJob] = []
        warnings: list[str] = []
        seen_job_keys: set[str] = set()
        seen_page_id_sets: set[frozenset[str]] = set()
        records_read = 0
        skip = 0
        page_count = 0

        while True:
            payload = await fetcher.get(
                RequestSpec(
                    url=_page_url(list_url, skip=skip, limit=page_size),
                    headers=dict(source.headers),
                    expected="json",
                )
            )
            records = _parse_page(payload)
            page_count += 1

            page_ids = _page_ids(records)
            if page_ids in seen_page_id_sets:
                warnings.append(
                    f"pagination loop detected at page {page_count}: the page repeated an earlier "
                    "page's job ids, so the collection is incomplete"
                )
                break
            seen_page_id_sets.add(page_ids)

            for offset, record in enumerate(records):
                index = records_read + offset
                try:
                    job = _normalize_job(
                        record, source=source, tenant=tenant, first_seen_at=first_seen_at
                    )
                except _UnusableJob as exc:
                    warnings.append(f"skipped malformed job record at index {index}: {exc}")
                    continue
                if job.canonical_job_key in seen_job_keys:
                    warnings.append(
                        f"skipped duplicate job record at index {index}: "
                        "the source job id already appeared"
                    )
                    continue
                seen_job_keys.add(job.canonical_job_key)
                jobs.append(job)
            records_read += len(records)

            if len(records) < page_size:
                break
            if page_count >= MAX_PAGES:
                warnings.append(
                    f"stopped after {page_count} pages: the page cap is reached, so the collection "
                    "may be incomplete"
                )
                break
            skip += len(records)

        return CollectionResult(
            source_id=source.id,
            complete=not warnings,
            jobs=jobs,
            warnings=warnings,
            page_count=page_count,
            # Lever's list endpoint publishes no total, so there is no declared total to compare
            # with the parsed postings; completeness comes from the walk rules above.
            declared_total=None,
        )


def _list_url(endpoint: str) -> str:
    """The configured endpoint with the parameters the adapter sets itself removed.

    A configured ``skip`` or ``limit`` would otherwise decide where the walk starts or how long a
    page is, and an existing ``mode`` may ask for HTML instead of JSON. Every other query
    parameter is kept, so a registry entry can still filter a board, for example by ``team``. The
    query string is rebuilt through ``urlencode``, so an already clean endpoint is preserved
    semantically rather than byte for byte (parameter order and percent-encoding may differ).
    """
    parts = urlsplit(endpoint)
    query = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if name not in OWNED_QUERY_PARAMETERS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _page_url(list_url: str, *, skip: int, limit: int) -> str:
    parts = urlsplit(list_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend([("mode", "json"), ("skip", str(skip)), ("limit", str(limit))])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _resolve_tenant(source: SourceConfig) -> str:
    """The site token that makes a source job ID unique."""
    if source.tenant is not None and source.tenant.strip():
        return source.tenant.strip()
    match = _POSTINGS_PATH.search(urlsplit(str(source.endpoint)).path)
    if match is not None:
        return match.group("site")
    raise AdapterError(
        f"lever source {source.id} has no tenant: set tenant or point the endpoint at "
        "/v0/postings/{site}"
    )


def _page_size(source: SourceConfig) -> int:
    value = source.parameters.get(PAGE_SIZE_PARAMETER)
    if value is None:
        return DEFAULT_PAGE_SIZE
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AdapterError(
            f"lever source {source.id} has an unusable {PAGE_SIZE_PARAMETER}: expected a positive "
            f"integer, got {value!r}"
        )
    return value


def _parse_page(payload: HttpPayload) -> list[object]:
    """Validate one page envelope and return its postings."""
    if payload.not_modified:
        raise AdapterError(f"lever returned 304 Not Modified for {payload.final_url}")
    media_type = payload.headers.get("Content-Type")
    try:
        document = json.loads(payload.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError(
            f"lever payload from {payload.final_url} is not JSON ({media_type}): {exc}"
        ) from exc
    if not isinstance(document, list):
        raise AdapterError("lever payload is not a JSON array")
    return document


def _page_ids(records: list[object]) -> frozenset[str]:
    """The posting ids of one page, ignoring records that cannot even name themselves."""
    ids: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            continue
        try:
            ids.add(_source_job_id(record))
        except _UnusableJob:
            continue
    return frozenset(ids)


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
    posting_url = _https_url(record, "hostedUrl")
    apply_url = _https_url(record, "applyUrl")
    categories = record.get("categories")
    category_values: Mapping[str, object] = categories if isinstance(categories, Mapping) else {}
    locations = _locations(category_values)
    department = _optional_text(category_values.get("department"))
    team = _optional_text(category_values.get("team"))
    employment_type = _optional_text(category_values.get("commitment"))
    description_text = _plain_text(record.get("descriptionPlain"))
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
            team=team,
            employment_type=employment_type,
            workplace_type=_optional_text(record.get("workplaceType")),
            description_text=description_text,
            content_hash=content_hash(title, locations, description_text),
            # ``NormalizedJob`` types both URLs as ``HttpUrl``, so validate them here and let the
            # surrounding handler turn an unusable URL into an incomplete collection.
            posting_url=HttpUrl(posting_url),
            apply_url=HttpUrl(apply_url),
            source_published_at=_millis_timestamp(record.get("createdAt"), "createdAt"),
            first_seen_at=first_seen_at,
            raw_metadata=_raw_metadata(record, categories),
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
    value = record.get("text")
    if not isinstance(value, str) or not value.strip():
        raise _UnusableJob("missing title")
    return value


def _https_url(record: Mapping[str, object], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _UnusableJob(f"missing {field}")
    url = value.strip()
    if not _is_absolute_https(url):
        raise _UnusableJob(f"{field} is not an absolute https URL")
    return url


def _is_absolute_https(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        # ``urlsplit`` rejects malformed URLs itself, for example an unmatched IPv6 bracket. A
        # provider value like that is a bad record, not a reason to abort the whole board.
        return False
    return parts.scheme == "https" and bool(parts.netloc)


def _locations(categories: Mapping[str, object]) -> list[str]:
    """The display location followed by every listed site, deduplicated case-insensitively."""
    candidates: list[object] = [categories.get("location")]
    all_locations = categories.get("allLocations")
    if isinstance(all_locations, list):
        candidates.extend(all_locations)

    locations: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        text = " ".join(candidate.split())
        key = text.casefold()
        if key not in seen:
            seen.add(key)
            locations.append(text)
    return locations


def _optional_text(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value).strip()
    return text or None


def _millis_timestamp(value: object, field: str) -> datetime | None:
    """Convert a Lever millisecond epoch to an aware UTC timestamp."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _UnusableJob(f"{field} is not a millisecond epoch timestamp")
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise _UnusableJob(f"{field} is not a millisecond epoch timestamp") from exc


def _plain_text(value: object) -> str | None:
    """``descriptionPlain`` is already text; an absent or blank description is simply missing."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _raw_metadata(record: Mapping[str, object], categories: object) -> dict[str, object]:
    """Provider bookkeeping kept for diagnosis; not part of job identity."""
    return {
        "country": _optional_text(record.get("country")),
        "categories": dict(categories) if isinstance(categories, Mapping) else {},
        "created_at_millis": record.get("createdAt"),
    }
