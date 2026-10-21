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
- ``locations`` is ``locationsText`` unless it only counts places (``"2 Locations"``); then it is
  the primary place named by the posting path (``/job/US-CA-Santa-Clara/…`` gives
  ``US CA Santa Clara``).
- The list view carries no description and only a relative ``postedOn``, so ``description_text``
  and ``source_published_at`` stay ``None``.

A walk whose only defects are those of a board shifting mid-walk (a repeated id, a count short of
the total) is walked once more, and the two walks are pooled when the second is short too (see
``job_alert.adapters.paged``). Workday's order is unstable between requests, so a whole board
repeats and hides postings at page boundaries with nothing published. A record without a title
earns the re-walk as well: on 2026-10-21 KLA, Caterpillar, Magna and Trane each served title-less
records at indexes that moved from walk to walk, and a later read of KLA and Caterpillar found none.

A record that is nothing but its requisition (``{"bulletFields": ["R2628975"]}``, no title and no
``externalPath``) is counted in ``total`` but publishes no posting, so it is a placeholder: it makes
no job, and it counts toward the total once. Applied Materials served two in one job family on
every walk on 2026-10-21, which no number of walks could otherwise complete.

A board over the cap can still be read whole with ``partition_facet`` (for example
``jobFamilyGroup``): the adapter reads that facet's values from the board's first page, walks each
value as its own scoped search, and merges the walks. Workday caps only an unfiltered total (a
filtered search reports and pages past 2,000), so every partition can be walked to its own total.
The merged result is complete only when every partition is, and the partitions' totals add up to
the board's total, or reach at least the cap when the board's own total is capped. Each
partition walk is judged against its own total, as a scoped walk is. Measured
2026-10-20: RTX, NVIDIA, Abbott and Applied Materials tag some internships ``Regular``, outside
their intern worker subtype, and every one of their job families is under 2,400 postings.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from pydantic import HttpUrl, ValidationError

from job_alert.adapters.base import AdapterError
from job_alert.adapters.common import optional_text
from job_alert.adapters.paged import Placeholder, UnusableRecord, walk_scoped
from job_alert.fetch import HttpFetcher, HttpPayload, RequestSpec
from job_alert.identity import build_dedupe_key, build_job_key, content_hash
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "workday"
DEFAULT_PAGE_SIZE = 20
TOTAL_CAP = 2000
MAX_PAGES = TOTAL_CAP // 10
#: A filtered search is not capped, so one partition may run past 2,000; this bounds a runaway walk.
PARTITION_CAP = 10_000
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
        partition = _partition_facet(source)

        async def search(
            applied: dict[str, list[str]], limit: int, offset: int
        ) -> tuple[list[Any], int, Any]:
            payload = await fetcher.post(
                RequestSpec(
                    url=str(source.endpoint),
                    headers=dict(source.headers),
                    expected="json",
                    json_body={
                        "appliedFacets": applied,
                        "limit": limit,
                        "offset": offset,
                        "searchText": search_text,
                    },
                    # A search is read-only, so retrying it can never duplicate anything.
                    idempotent=True,
                )
            )
            return _parse_page(payload)

        def walk(applied: dict[str, list[str]], *, cap: int) -> Awaitable[CollectionResult]:
            async def fetch_page(page_index: int, offset: int) -> tuple[list[Any], int | None]:
                records, total, _ = await search(applied, page_size, offset)
                # Workday reports the total on the first page only; later pages say 0.
                return records, total if page_index == 0 else None

            return walk_scoped(
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
                cap=cap,
                max_pages=cap // 10,
                cap_advice="narrow the source's applied_facets or search_text",
                rewalk_malformed=True,
            )

        if partition is None:
            return await walk(facets, cap=TOTAL_CAP)
        _, board_total, document_facets = await search(facets, 1, 0)
        values = _facet_values(document_facets, partition, source.id)
        results = [
            await walk({**facets, partition: [value_id]}, cap=PARTITION_CAP)
            for value_id, _ in values
        ]
        return _merge_partitions(source, partition, board_total, values, results)


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


def _partition_facet(source: SourceConfig) -> str | None:
    value = source.parameters.get("partition_facet")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AdapterError(f"workday source {source.id} has an unusable partition_facet: {value!r}")
    return value


def _facet_values(facets: Any, name: str, source_id: str) -> list[tuple[str, int]]:
    """The ``(id, count)`` of each value of one facet, as the board's first page lists them."""
    group = next(
        (
            facet
            for facet in (facets if isinstance(facets, list) else [])
            if isinstance(facet, Mapping) and facet.get("facetParameter") == name
        ),
        None,
    )
    raw = group.get("values") if group is not None else None
    values = [
        (value["id"], value["count"])
        for value in raw or ()
        if isinstance(value, Mapping)
        and isinstance(value.get("id"), str)
        and isinstance(value.get("count"), int)
        and not isinstance(value.get("count"), bool)
    ]
    if not values:
        raise AdapterError(f"workday source {source_id} found no values for facet {name}")
    return values


def _merge_partitions(
    source: SourceConfig,
    partition: str,
    board_total: int,
    values: list[tuple[str, int]],
    results: list[CollectionResult],
) -> CollectionResult:
    """One result from the partition walks, complete only if they cover the board between them."""
    jobs: dict[str, NormalizedJob] = {}
    warnings: list[str] = []
    for (value_id, _), result in zip(values, results, strict=True):
        warnings.extend(f"{partition}={value_id}: {warning}" for warning in result.warnings)
        for job in result.jobs:
            jobs.setdefault(job.canonical_job_key, job)
    # The facet counts and the board total come from one response, so comparing them cannot be
    # thrown off by postings that change during the walks; each walk judges its own total.
    counted = sum(count for _, count in values)
    covered = counted == board_total or (board_total >= TOTAL_CAP and counted >= TOTAL_CAP)
    if not covered:
        warnings.append(
            f"the {partition} values count {counted} postings but the board declares"
            f" {board_total}, so some postings carry no {partition} value"
        )
    declared = sum(result.declared_total or 0 for result in results)
    return CollectionResult(
        source_id=source.id,
        complete=covered and all(result.complete for result in results),
        jobs=list(jobs.values()),
        warnings=warnings,
        page_count=1 + sum(result.page_count for result in results),
        declared_total=declared,
    )


def _parse_page(payload: HttpPayload) -> tuple[list[Any], int, Any]:
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
    return postings, total, document.get("facets")


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
    path = optional_text(record.get("externalPath"))
    bullets = record.get("bulletFields")
    requisition = optional_text(bullets[0]) if isinstance(bullets, list) and bullets else None
    if not isinstance(title, str) or not title.strip():
        if path is None and requisition is not None:
            raise Placeholder(requisition)
        raise UnusableRecord("missing title")
    if path is None or not path.startswith("/job/") or "//" in path:
        raise UnusableRecord("externalPath is not a relative /job/ path")
    source_job_id = path.rsplit("_", 1)[-1].strip()
    if "_" not in path or not source_job_id or "/" in source_job_id:
        raise UnusableRecord("externalPath carries no requisition suffix")
    posting_url = f"{origin}/{site}{path}"
    location_text = optional_text(record.get("locationsText"))
    if location_text and not _LOCATION_COUNT.match(location_text):
        locations = [location_text]
    else:
        # The list only counts the places ("2 Locations"); the posting path names the primary one
        # ("/job/US-CA-Santa-Clara/…"), which is enough for a country filter.
        segment = path.split("/")[2] if path.count("/") >= 3 else ""
        primary = " ".join(segment.replace("-", " ").split())
        locations = [primary] if primary else []
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
