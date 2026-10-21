"""amazon.jobs search adapter.

``https://www.amazon.jobs/en/search.json`` is the endpoint the amazon.jobs site itself calls. It is
public and undocumented, answers JSON with a ``jobs`` array and a ``hits`` total for the query, and
pages by ``offset`` and ``result_limit`` (at most 100). ``hits`` is capped at 10,000, and the full
board is larger than that, so an Amazon source is always *scoped*: its configured endpoint carries a
provider-side query such as ``base_query=intern&sort=recent``, and completeness is judged against
the ``hits`` that query declares. A query whose ``hits`` reaches the cap cannot be read whole and is
reported incomplete after one request instead of being walked.

Mapping decisions the payload does not spell out:

- ``source_job_id`` and ``requisition_id`` are ``id_icims``, the number in the posting URL; identity
  is ``amazon:{tenant}:{id_icims}``.
- ``posting_url`` is ``job_path`` resolved against ``https://www.amazon.jobs`` and nothing else, and
  ``apply_url`` is ``url_next_step`` when it is an absolute HTTPS URL, else the posting URL.
- ``locations`` decodes each JSON-encoded ``locations`` entry and keeps ``normalizedLocation`` in
  provider order, deduplicated; ``normalized_location`` is the fallback. ``workplace_type`` is the
  entries' shared ``type`` lowercased (``onsite``, ``remote``, ``hybrid``), or ``None`` when they
  disagree.
- ``department`` is ``job_category``, ``team`` is ``team.label``, ``employment_type`` is
  ``job_schedule_type``. ``is_intern`` is ignored: it is ``null`` even on intern postings.
- ``posted_date`` (``"October 13, 2026"``) becomes a UTC midnight ``source_published_at``;
  ``updated_time`` is relative (``"8 minutes"``) and is not stored as a timestamp.

The list is newest first, so a posting published mid-walk shifts every later offset: one record
repeats and the new one is missed. A walk whose only defects are those is walked once more, and the
two walks are pooled when the second is short too (see ``job_alert.adapters.paged``); a malformed
record or the page cap never earns a re-walk.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import HttpUrl, ValidationError

from job_alert.adapters.base import AdapterError
from job_alert.adapters.common import (
    html_to_text,
    is_absolute_https,
    optional_text,
    resolve_https_url,
)
from job_alert.adapters.paged import UnusableRecord, walk_scoped
from job_alert.fetch import HttpFetcher, HttpPayload, RequestSpec
from job_alert.identity import build_dedupe_key, build_job_key, content_hash
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "amazon"
DEFAULT_TENANT = "amazon"
SITE_ORIGIN = "https://www.amazon.jobs"
PAGE_SIZE_PARAMETER = "page_size"
DEFAULT_PAGE_SIZE = 100
SEARCH_CAP = 10_000
MAX_PAGES = SEARCH_CAP // 10
OWNED_QUERY_PARAMETERS = frozenset({"offset", "result_limit"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AmazonAdapter:
    """Normalize one scoped amazon.jobs query into jobs, one JSON page per request."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        tenant = (source.tenant or DEFAULT_TENANT).strip() or DEFAULT_TENANT
        page_size = _page_size(source)
        list_url = _list_url(str(source.endpoint))
        first_seen_at = self._now()

        async def fetch_page(_: int, offset: int) -> tuple[list[object], int]:
            payload = await fetcher.get(
                RequestSpec(
                    url=_page_url(list_url, offset=offset, limit=page_size),
                    headers=dict(source.headers),
                    expected="json",
                )
            )
            return _parse_page(payload)

        return await walk_scoped(
            source_id=source.id,
            fetch_page=fetch_page,
            normalize=lambda record: _normalize_job(
                record, source=source, tenant=tenant, first_seen_at=first_seen_at
            ),
            cap=SEARCH_CAP,
            max_pages=MAX_PAGES,
            cap_advice="narrow the source's query",
        )


def _page_size(source: SourceConfig) -> int:
    value = source.parameters.get(PAGE_SIZE_PARAMETER, DEFAULT_PAGE_SIZE)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise AdapterError(
            f"amazon source {source.id} has an unusable {PAGE_SIZE_PARAMETER}: expected an integer"
            f" from 1 to 100, got {value!r}"
        )
    return int(value)


def _list_url(endpoint: str) -> str:
    """The configured scoped query with the paging parameters the adapter owns removed."""
    parts = urlsplit(endpoint)
    query = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if name not in OWNED_QUERY_PARAMETERS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _page_url(list_url: str, *, offset: int, limit: int) -> str:
    parts = urlsplit(list_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend([("offset", str(offset)), ("result_limit", str(limit))])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _parse_page(payload: HttpPayload) -> tuple[list[object], int]:
    if payload.not_modified:
        raise AdapterError(f"amazon returned 304 Not Modified for {payload.final_url}")
    try:
        document = json.loads(payload.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError(f"amazon payload from {payload.final_url} is not JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise AdapterError("amazon payload is not a JSON object")
    if document.get("error"):
        raise AdapterError(f"amazon search answered with an error: {document['error']!r}")
    hits = document.get("hits")
    jobs = document.get("jobs")
    if isinstance(hits, bool) or not isinstance(hits, int) or hits < 0:
        raise AdapterError("amazon payload has no non-negative integer hits")
    if not isinstance(jobs, list):
        raise AdapterError("amazon payload has no jobs array")
    return jobs, hits


def _normalize_job(
    record: object, *, source: SourceConfig, tenant: str, first_seen_at: datetime
) -> NormalizedJob:
    if not isinstance(record, Mapping):
        raise UnusableRecord("record is not a JSON object")
    source_job_id = optional_text(record.get("id_icims"))
    if source_job_id is None:
        raise UnusableRecord("missing id_icims")
    title = record.get("title")
    if not isinstance(title, str) or not title.strip():
        raise UnusableRecord("missing title")
    try:
        posting_url = resolve_https_url(
            record.get("job_path"), origin=SITE_ORIGIN, field="job_path"
        )
    except ValueError as exc:
        raise UnusableRecord(str(exc)) from exc
    if not posting_url.startswith(SITE_ORIGIN + "/"):
        raise UnusableRecord("job_path does not resolve to an amazon.jobs posting")
    next_step = optional_text(record.get("url_next_step"))
    apply_url = next_step if next_step and is_absolute_https(next_step) else posting_url
    locations, workplace_type = _locations(record)
    department = optional_text(record.get("job_category"))
    team_value = record.get("team")
    team = optional_text(team_value.get("label")) if isinstance(team_value, Mapping) else None
    description_text = html_to_text(record.get("description"))
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
                requisition_id=source_job_id,
                apply_url=apply_url,
                title=title,
                locations=locations,
                department=department,
            ),
            requisition_id=source_job_id,
            title=title,
            locations=locations,
            department=department,
            team=team,
            employment_type=optional_text(record.get("job_schedule_type")),
            workplace_type=workplace_type,
            description_text=description_text,
            content_hash=content_hash(title, locations, description_text),
            posting_url=HttpUrl(posting_url),
            apply_url=HttpUrl(apply_url),
            source_published_at=_posted_date(record.get("posted_date")),
            first_seen_at=first_seen_at,
            raw_metadata={
                "uuid": optional_text(record.get("id")),
                "business_category": optional_text(record.get("business_category")),
            },
        )
    except ValidationError as exc:
        raise UnusableRecord("record does not normalize into a job") from exc


def _locations(record: Mapping[str, object]) -> tuple[list[str], str | None]:
    names: list[str] = []
    types: set[str] = set()
    entries = record.get("locations")
    for entry in entries if isinstance(entries, list) else []:
        try:
            decoded = json.loads(entry) if isinstance(entry, str) else entry
        except json.JSONDecodeError:
            continue
        if not isinstance(decoded, Mapping):
            continue
        name = optional_text(decoded.get("normalizedLocation")) or optional_text(
            decoded.get("location")
        )
        if name and name.casefold() not in {known.casefold() for known in names}:
            names.append(name)
        kind = optional_text(decoded.get("type"))
        if kind:
            types.add(kind.casefold())
    if not names:
        fallback = optional_text(record.get("normalized_location"))
        if fallback:
            names.append(fallback)
    return names, (types.pop() if len(types) == 1 else None)


def _posted_date(value: object) -> datetime | None:
    text = optional_text(value)
    if text is None:
        return None
    try:
        return datetime.strptime(text, "%B %d, %Y").replace(tzinfo=UTC)
    except ValueError:
        return None
