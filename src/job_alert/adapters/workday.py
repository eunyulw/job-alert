"""Workday candidate-experience (CXS) search adapter.

A Workday careers site at ``https://{host}/{site}`` loads its listings from
``POST https://{host}/wday/cxs/{tenant}/{site}/jobs`` with a JSON body
``{"appliedFacets": …, "limit": …, "offset": …, "searchText": …}``. The endpoint is public and
undocumented. It answers ``{"total": N, "jobPostings": [...]}``, pages at most 20 postings, reports
``total`` on the first page only (later pages say ``0``), and caps ``total`` at 2,000.

Because of that cap a Workday source is always **scoped**: its ``parameters`` carry
``applied_facets`` (for example a tenant's internship worker subtype) and/or ``search_text``, and
completeness is judged against the ``total`` the scoped search declares on its first page. A board
an operator knows is smaller than the cap may instead set ``whole_board: true``. A source with none
of these is a configuration error, and a search whose ``total`` reaches the cap is reported
incomplete after one request, whole board or not.

Mapping decisions:

- ``source_job_id`` is the requisition suffix after the last ``_`` of ``externalPath``
  (``…_JR2025948``); identity is ``workday:{tenant}/{site}:{id}``. ``requisition_id`` is
  ``bulletFields[0]`` when present.
- ``posting_url`` and ``apply_url`` are ``https://{host}/{site}{externalPath}``; the path must be
  relative and start with ``/job/``, so a payload cannot point a link at another host.
- ``locations`` is ``locationsText`` unless it only counts places (``"2 Locations"``), in which case
  it is empty rather than wrong.
- The list view carries no description and only a relative ``postedOn``, so ``description_text``
  and ``source_published_at`` stay ``None``.

A walk whose only defects are those of a board shifting mid-walk (a repeated id, a count short of
the total) is walked once more, and that second walk's verdict stands.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from pydantic import HttpUrl, ValidationError

from job_alert.adapters.base import AdapterError
from job_alert.adapters.common import optional_text
from job_alert.adapters.paged import UnusableRecord, walk_scoped
from job_alert.fetch import HttpFetcher, HttpPayload, RequestSpec
from job_alert.identity import build_dedupe_key, build_job_key, content_hash
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "workday"
DEFAULT_PAGE_SIZE = 20
TOTAL_CAP = 2000
MAX_PAGES = TOTAL_CAP // 10
_CXS_PATH = re.compile(r"^/wday/cxs/(?P<tenant>[^/]+)/(?P<site>[^/]+)/jobs/?$")
_LOCATION_COUNT = re.compile(r"^\d+\s+locations?$", re.IGNORECASE)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def site_of(endpoint: str) -> tuple[str, str, str]:
    """The ``(origin, tenant, site)`` of a CXS jobs endpoint, or an error naming the form."""
    parts = urlsplit(endpoint)
    match = _CXS_PATH.match(parts.path)
    if parts.scheme != "https" or not parts.netloc or match is None:
        raise AdapterError(
            f"workday endpoint {endpoint} is not a cxs jobs URL"
            " (https://{host}/wday/cxs/{tenant}/{site}/jobs)"
        )
    return f"https://{parts.netloc}", match.group("tenant"), match.group("site")


class WorkdayAdapter:
    """Normalize one scoped Workday search into jobs, one JSON ``POST`` per page."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        origin, tenant, site = site_of(str(source.endpoint))
        facets, search_text = _scope(source)
        page_size = _page_size(source)
        first_seen_at = self._now()

        async def fetch_page(page_index: int, offset: int) -> tuple[list[Any], int | None]:
            payload = await fetcher.post(
                RequestSpec(
                    url=str(source.endpoint),
                    headers=dict(source.headers),
                    expected="json",
                    json_body={
                        "appliedFacets": facets,
                        "limit": page_size,
                        "offset": offset,
                        "searchText": search_text,
                    },
                    # A search is read-only, so retrying it can never duplicate anything.
                    idempotent=True,
                )
            )
            records, total = _parse_page(payload)
            # Workday reports the total on the first page only; later pages say 0.
            return records, total if page_index == 0 else None

        return await walk_scoped(
            source_id=source.id,
            fetch_page=fetch_page,
            normalize=lambda record: _normalize_job(
                record,
                source=source,
                origin=origin,
                tenant=f"{tenant}/{site}",
                site=site,
                first_seen_at=first_seen_at,
            ),
            cap=TOTAL_CAP,
            max_pages=MAX_PAGES,
            cap_advice="narrow the source's applied_facets or search_text",
        )


def _scope(source: SourceConfig) -> tuple[dict[str, list[str]], str]:
    facets = source.parameters.get("applied_facets", {})
    search_text = source.parameters.get("search_text", "")
    valid_facets = isinstance(facets, Mapping) and all(
        isinstance(name, str)
        and isinstance(values, list)
        and values
        and all(isinstance(value, str) and value for value in values)
        for name, values in facets.items()
    )
    if not valid_facets or not isinstance(search_text, str):
        raise AdapterError(
            f"workday source {source.id} has an unusable scope: applied_facets must map names to"
            " non-empty lists of ids, and search_text must be text"
        )
    whole_board = source.parameters.get("whole_board", False) is True
    if not facets and not search_text.strip() and not whole_board:
        raise AdapterError(
            f"workday source {source.id} has no scope: Workday caps a search at {TOTAL_CAP}"
            " postings, so set applied_facets or search_text, or whole_board: true for a board"
            " known to be smaller"
        )
    return {str(name): list(values) for name, values in facets.items()}, search_text


def _page_size(source: SourceConfig) -> int:
    value = source.parameters.get("page_size", DEFAULT_PAGE_SIZE)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 20:
        raise AdapterError(
            f"workday source {source.id} has an unusable page_size: expected 1 to 20, got {value!r}"
        )
    return int(value)


def _parse_page(payload: HttpPayload) -> tuple[list[Any], int]:
    if payload.not_modified:
        raise AdapterError(f"workday returned 304 Not Modified for {payload.final_url}")
    try:
        document = json.loads(payload.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError(f"workday payload from {payload.final_url} is not JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise AdapterError("workday payload is not a JSON object")
    total = document.get("total")
    postings = document.get("jobPostings")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AdapterError("workday payload has no non-negative integer total")
    if not isinstance(postings, list):
        raise AdapterError("workday payload has no jobPostings array")
    return postings, total


def _normalize_job(
    record: object,
    *,
    source: SourceConfig,
    origin: str,
    tenant: str,
    site: str,
    first_seen_at: datetime,
) -> NormalizedJob:
    if not isinstance(record, Mapping):
        raise UnusableRecord("record is not a JSON object")
    title = record.get("title")
    if not isinstance(title, str) or not title.strip():
        raise UnusableRecord("missing title")
    path = optional_text(record.get("externalPath"))
    if path is None or not path.startswith("/job/") or "//" in path:
        raise UnusableRecord("externalPath is not a relative /job/ path")
    source_job_id = path.rsplit("_", 1)[-1].strip()
    if "_" not in path or not source_job_id or "/" in source_job_id:
        raise UnusableRecord("externalPath carries no requisition suffix")
    bullets = record.get("bulletFields")
    requisition = optional_text(bullets[0]) if isinstance(bullets, list) and bullets else None
    posting_url = f"{origin}/{site}{path}"
    location_text = optional_text(record.get("locationsText"))
    locations = (
        [location_text]
        if location_text and not _LOCATION_COUNT.match(location_text)
        else []
    )
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
                apply_url=posting_url,
            ),
            dedupe_key=build_dedupe_key(
                company_id=source.company_id,
                requisition_id=requisition,
                apply_url=posting_url,
                title=title,
                locations=locations,
                department=None,
            ),
            requisition_id=requisition,
            title=title,
            locations=locations,
            content_hash=content_hash(title, locations, None),
            posting_url=HttpUrl(posting_url),
            apply_url=HttpUrl(posting_url),
            first_seen_at=first_seen_at,
            raw_metadata={"posted_on": optional_text(record.get("postedOn"))},
        )
    except ValidationError as exc:
        raise UnusableRecord("record does not normalize into a job") from exc
