"""Eightfold jobs adapter: the v2 API and the newer pcsx search.

Careers sites hosted on Eightfold (Netflix's explore.jobs.netflix.net, and audit candidates such as
Qualcomm and Lockheed Martin) load listings from ``GET {origin}/api/apply/v2/jobs?domain={domain}``.
The endpoint is public and undocumented; it answers ``{"positions": [...], "count": N}`` and pages
by ``start`` in fixed pages of 10. A source is scoped through its endpoint's query (``domain`` is
required; ``query=intern`` narrows it); the adapter owns ``start`` and ``num``. Some tenants refuse
this API (``{"message": "Not authorized …"}``), which is an error, never an empty board.

Mapping decisions:

- ``source_job_id`` is ``id``; identity is ``eightfold:{domain}:{id}``; ``requisition_id`` is
  ``ats_job_id``.
- ``posting_url`` is ``canonicalPositionUrl`` when it is on the source's own origin, else
  ``{origin}/careers/job/{id}``.
- ``locations`` are ``locations`` in provider order; ``department`` and ``business_unit`` map to
  ``department`` and ``team``; ``t_create`` (epoch seconds) is ``source_published_at``.

Many tenants now refuse the v2 API with HTTP 403 while their careers pages read the newer
``GET {origin}/api/pcsx/search?domain=…`` search, which answers ordinary requests (measured
2026-10-22: Lockheed Martin, Eaton, Qualcomm, Lam Research, Whirlpool). A source whose endpoint is
that path is read the same way: ``{"data": {"positions": [...], "count": N}}``, fixed pages of 10
by ``start``, with ``sort_by=timestamp`` keeping the order stable. Its records name the same
things differently: ``standardizedLocations`` (``"Camden, NJ, US"``, which carries the country)
before ``locations``, ``atsJobId``, ``positionUrl`` (relative to the origin), ``workLocationOption``
and ``postedTs``. Identity is the same ``eightfold:{domain}:{id}``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import HttpUrl, ValidationError

from job_alert.adapters.base import AdapterError
from job_alert.adapters.common import optional_text
from job_alert.adapters.paged import UnusableRecord, walk_scoped
from job_alert.fetch import HttpFetcher, HttpPayload, RequestSpec
from job_alert.identity import build_dedupe_key, build_job_key, content_hash
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "eightfold"
API_PATH = "/api/apply/v2/jobs"
PCSX_PATH = "/api/pcsx/search"
PAGE_SIZE = 10
SEARCH_CAP = 100_000
MAX_PAGES = 1000
OWNED = frozenset({"start", "num"})
_DIGITS = re.compile(r"^\d+$")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class EightfoldAdapter:
    """Normalize one scoped Eightfold v2 search into jobs, one JSON page per request."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        parts = urlsplit(str(source.endpoint))
        path = parts.path.rstrip("/")
        if parts.scheme != "https" or path not in (API_PATH, PCSX_PATH):
            raise AdapterError(
                f"eightfold endpoint {source.endpoint} is not an {API_PATH} or {PCSX_PATH} URL"
            )
        pcsx = path == PCSX_PATH
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        query = [(name, value) for name, value in pairs if name not in OWNED]
        domain = next((v for k, v in query if k == "domain" and v.strip()), None)
        if domain is None:
            raise AdapterError(f"eightfold source {source.id} has no domain in its endpoint query")
        origin = f"https://{parts.netloc}"
        first_seen_at = self._now()

        async def fetch_page(_: int, offset: int) -> tuple[Sequence[object], int]:
            page_query = urlencode([*query, ("start", str(offset)), ("num", str(PAGE_SIZE))])
            payload = await fetcher.get(
                RequestSpec(
                    url=urlunsplit(("https", parts.netloc, parts.path, page_query, "")),
                    headers=dict(source.headers),
                    expected="json",
                )
            )
            return _parse_page(payload, pcsx=pcsx)

        return await walk_scoped(
            source_id=source.id,
            fetch_page=fetch_page,
            normalize=lambda record: _normalize_job(
                record,
                source=source,
                domain=domain,
                origin=origin,
                first_seen_at=first_seen_at,
                pcsx=pcsx,
            ),
            cap=SEARCH_CAP,
            max_pages=MAX_PAGES,
            cap_advice="narrow the source's query",
        )


def _parse_page(payload: HttpPayload, *, pcsx: bool) -> tuple[list[Any], int]:
    if payload.not_modified:
        raise AdapterError(f"eightfold returned 304 Not Modified for {payload.final_url}")
    try:
        document = json.loads(payload.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError(
            f"eightfold payload from {payload.final_url} is not JSON: {exc}"
        ) from exc
    if not isinstance(document, Mapping):
        raise AdapterError("eightfold payload is not a JSON object")
    if pcsx:
        data = document.get("data")
        if not isinstance(data, Mapping):
            detail = optional_text(document.get("error")) or "no data object"
            raise AdapterError(f"eightfold pcsx payload has no data: {detail}")
        document = data
    positions, count = document.get("positions"), document.get("count")
    if not isinstance(positions, list):
        detail = optional_text(document.get("message")) or "no positions array"
        raise AdapterError(f"eightfold payload has no positions: {detail}")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise AdapterError("eightfold payload has no non-negative integer count")
    return positions, count


def _normalize_job(
    record: object,
    *,
    source: SourceConfig,
    domain: str,
    origin: str,
    first_seen_at: datetime,
    pcsx: bool = False,
) -> NormalizedJob:
    if not isinstance(record, Mapping):
        raise UnusableRecord("record is not a JSON object")
    job_id = optional_text(record.get("id"))
    if job_id is None or not _DIGITS.match(job_id):
        raise UnusableRecord("id is missing or not numeric")
    title = optional_text(record.get("name")) or optional_text(record.get("posting_name"))
    if title is None:
        raise UnusableRecord("missing name")
    canonical = optional_text(record.get("canonicalPositionUrl"))
    relative = optional_text(record.get("positionUrl"))
    if canonical is None and relative is not None and relative.startswith("/careers/job/"):
        canonical = f"{origin}{relative}"
    posting_url = (
        canonical
        if canonical and canonical.startswith(f"{origin}/")
        else f"{origin}/careers/job/{job_id}"
    )
    raw_locations = record.get("standardizedLocations") if pcsx else None
    if not isinstance(raw_locations, list) or not raw_locations:
        raw_locations = record.get("locations")
    locations: list[str] = []
    for entry in raw_locations if isinstance(raw_locations, list) else []:
        name = optional_text(entry)
        if name and name not in locations:
            locations.append(name)
    department = optional_text(record.get("department"))
    requisition = optional_text(record.get("ats_job_id")) or optional_text(record.get("atsJobId"))
    try:
        return NormalizedJob(
            company_id=source.company_id,
            source_id=source.id,
            source_job_id=job_id,
            canonical_job_key=build_job_key(
                provider=PROVIDER,
                tenant=domain,
                source_job_id=job_id,
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
                department=department,
            ),
            requisition_id=requisition,
            title=title,
            locations=locations,
            department=department,
            team=optional_text(record.get("business_unit")),
            workplace_type=optional_text(record.get("work_location_option"))
            or optional_text(record.get("workLocationOption")),
            content_hash=content_hash(title, locations, None),
            posting_url=HttpUrl(posting_url),
            apply_url=HttpUrl(posting_url),
            source_published_at=_epoch(record.get("postedTs" if pcsx else "t_create")),
            first_seen_at=first_seen_at,
            raw_metadata={
                "display_job_id": optional_text(record.get("display_job_id"))
                or optional_text(record.get("displayJobId"))
            },
        )
    except ValidationError as exc:
        raise UnusableRecord("record does not normalize into a job") from exc


def _epoch(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
