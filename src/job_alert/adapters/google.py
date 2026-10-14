"""Google Careers adapter.

google.com/about/careers renders each results page on the server and embeds the data in
``AF_initDataCallback({key: 'ds:1', … data: [jobs, null, total, page_size]})``. The jobs are
positional arrays with no field names, so the adapter checks the type of every position it reads
and treats any other shape as a malformed record: a layout change fails closed instead of
producing wrong jobs. A source is scoped through its endpoint's query (for example
``employment_type=INTERN``); the adapter owns only ``page`` (1-based).

Positions read: ``0`` id (digits), ``1`` title, ``3`` responsibilities HTML, ``7`` company
(``Google``, ``DeepMind``, …), ``9`` locations (``[[display, …], …]``), ``10`` description HTML,
``12`` created ``[seconds, nanos]``. Position ``2`` is a sign-in apply link carrying an opaque
token, so the posting link is built instead: ``…/jobs/results/{id}-{slug of title}``, which the
site resolves.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import HttpUrl, ValidationError

from job_alert.adapters.base import AdapterError
from job_alert.adapters.common import html_to_text
from job_alert.adapters.paged import UnusableRecord, walk_scoped
from job_alert.fetch import HttpFetcher, HttpPayload, RequestSpec
from job_alert.identity import build_dedupe_key, build_job_key, content_hash
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "google"
DEFAULT_TENANT = "google"
RESULTS_PATH = "/about/careers/applications/jobs/results/"
SEARCH_CAP = 100_000
MAX_PAGES = 500
_BLOCK = re.compile(r"AF_initDataCallback\(\{key: 'ds:1',.*?data:(.*?), sideChannel:", re.S)
_DIGITS = re.compile(r"^\d+$")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class GoogleAdapter:
    """Normalize one scoped Google Careers search into jobs, one results page at a time."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        parts = urlsplit(str(source.endpoint))
        if parts.netloc != "www.google.com" or parts.path != RESULTS_PATH:
            raise AdapterError(
                f"google endpoint {source.endpoint} is not https://www.google.com{RESULTS_PATH}"
            )
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "page"]
        tenant = (source.tenant or DEFAULT_TENANT).strip() or DEFAULT_TENANT
        first_seen_at = self._now()

        async def fetch_page(page_index: int, _: int) -> tuple[Sequence[object], int]:
            page_query = urlencode([*query, ("page", str(page_index + 1))])
            url = urlunsplit(("https", parts.netloc, parts.path, page_query, ""))
            payload = await fetcher.get(
                RequestSpec(url=url, headers=dict(source.headers), expected="html")
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


def _parse_page(payload: HttpPayload) -> tuple[list[Any], int]:
    if payload.not_modified:
        raise AdapterError(f"google returned 304 Not Modified for {payload.final_url}")
    match = _BLOCK.search(payload.body.decode("utf-8", errors="replace"))
    if match is None:
        raise AdapterError(f"google page {payload.final_url} carries no ds:1 results block")
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise AdapterError(f"google results block is not JSON: {exc}") from exc
    if not isinstance(data, list) or len(data) < 3:
        raise AdapterError("google results block has an unexpected shape")
    jobs, total = data[0], data[2]
    if jobs is None:
        jobs = []
    if not isinstance(jobs, list):
        raise AdapterError("google results block has no job array")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AdapterError("google results block has no non-negative integer total")
    return jobs, total


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-") or "job"


def _normalize_job(
    record: object, *, source: SourceConfig, tenant: str, first_seen_at: datetime
) -> NormalizedJob:
    if not isinstance(record, list) or len(record) < 13:
        raise UnusableRecord("record is not a positional array of the expected length")
    job_id, title = record[0], record[1]
    if not isinstance(job_id, str) or not _DIGITS.match(job_id):
        raise UnusableRecord("position 0 is not a numeric id")
    if not isinstance(title, str) or not title.strip():
        raise UnusableRecord("position 1 is not a title")
    company = record[7] if isinstance(record[7], str) else None
    locations: list[str] = []
    if isinstance(record[9], list):
        for entry in record[9]:
            name = entry[0].strip() if isinstance(entry, list) and entry and isinstance(
                entry[0], str
            ) else ""
            if name and name not in locations:
                locations.append(name)
    description = _html(record[10]) or _html(record[3])
    posting_url = f"https://www.google.com{RESULTS_PATH}{job_id}-{_slug(title)}"
    try:
        return NormalizedJob(
            company_id=source.company_id,
            source_id=source.id,
            source_job_id=job_id,
            canonical_job_key=build_job_key(
                provider=PROVIDER,
                tenant=tenant,
                source_job_id=job_id,
                company_id=source.company_id,
                title=title,
                locations=locations,
                apply_url=posting_url,
            ),
            dedupe_key=build_dedupe_key(
                company_id=source.company_id,
                requisition_id=None,
                apply_url=posting_url,
                title=title,
                locations=locations,
                department=None,
            ),
            title=title,
            locations=locations,
            description_text=description,
            content_hash=content_hash(title, locations, description),
            posting_url=HttpUrl(posting_url),
            apply_url=HttpUrl(posting_url),
            source_published_at=_seconds(record[12]),
            first_seen_at=first_seen_at,
            raw_metadata={"company": company},
        )
    except ValidationError as exc:
        raise UnusableRecord("record does not normalize into a job") from exc


def _html(value: object) -> str | None:
    if isinstance(value, list) and len(value) > 1 and isinstance(value[1], str):
        return html_to_text(value[1])
    return None


def _seconds(value: object) -> datetime | None:
    if isinstance(value, list) and value and isinstance(value[0], int) and not isinstance(
        value[0], bool
    ):
        try:
            return datetime.fromtimestamp(value[0], tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return None
