"""Jibe career site adapter (the iCIMS career-site product behind ``careers.rivian.com``).

``GET https://{site}/api/jobs?page={n}&limit=100`` answers ``{"totalCount": N, "jobs": [{"data":
{...}}]}``. The adapter keeps any configured query (a ``location`` or ``categories`` filter), owns
``page`` and ``limit``, and judges completeness against ``totalCount``. A query without ``sortBy``
is sorted by ``posted_date``: the default relevance order is not a total order, and measured
2026-10-21 it served Rivian's posting 34390 at both ends of a page boundary and hid another on every
walk, while the date order served all 950 postings once each.

Mapping: ``slug`` (falling back to ``req_id``) is the id and identity is ``jibe:{site}:{slug}``;
``title`` the title; ``full_location`` (``;``-separated when ``multipleLocations``), else ``city``,
``state``, ``country``, the locations; ``location_type`` (``REMOTE``/``HYBRID``/``ONSITE``) the
workplace type; the first ``categories[].name`` the department; ``employment_type`` the
employment type; ``posted_date`` the publication date. The posting link is the site's job page,
``https://{site}/careers-home/jobs/{slug}``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import joined, json_document, make_job, parse_date, unique, utcnow
from job_alert.adapters.common import optional_text
from job_alert.adapters.paged import UnusableRecord, walk_scoped
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "jibe"
PAGE_SIZE = 100
SEARCH_CAP = 100_000
MAX_PAGES = 200
OWNED = frozenset({"page", "limit"})
_SLUG = re.compile(r"^[A-Za-z0-9_-]+$")
_WORKPLACES = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on_site": "onsite"}


class JibeAdapter:
    """Normalize one Jibe ``/api/jobs`` search, one page per request."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        parts = urlsplit(str(source.endpoint))
        if parts.scheme != "https" or parts.path != "/api/jobs":
            raise AdapterError(f"jibe endpoint {source.endpoint} is not an https /api/jobs URL")
        site = parts.netloc.lower()
        query = [
            (name, value)
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
            if name not in OWNED
        ]
        if not any(name == "sortBy" for name, _ in query):
            query += [("sortBy", "posted_date"), ("descending", "true")]
        first_seen_at = self._now()

        async def fetch_page(index: int, _: int) -> tuple[Sequence[object], int]:
            page_query = urlencode([*query, ("page", str(index + 1)), ("limit", str(PAGE_SIZE))])
            payload = await fetcher.get(
                RequestSpec(
                    url=urlunsplit(("https", parts.netloc, parts.path, page_query, "")),
                    headers=dict(source.headers),
                    expected="json",
                )
            )
            document = json_document(payload, PROVIDER)
            records = document.get("jobs") if isinstance(document, Mapping) else None
            total = document.get("totalCount") if isinstance(document, Mapping) else None
            if not isinstance(records, list):
                raise AdapterError("jibe payload has no jobs array")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise AdapterError("jibe payload has no non-negative integer totalCount")
            return [
                record.get("data") if isinstance(record, Mapping) else record
                for record in records
            ], total

        def normalize(record: object) -> NormalizedJob:
            if not isinstance(record, Mapping):
                raise UnusableRecord("record is not a JSON object")
            return _normalize(record, source=source, site=site, now=first_seen_at)

        return await walk_scoped(
            source_id=source.id,
            fetch_page=fetch_page,
            normalize=normalize,
            cap=SEARCH_CAP,
            max_pages=MAX_PAGES,
            cap_advice="narrow the source's query",
        )


def _normalize(
    record: Mapping[str, Any], *, source: SourceConfig, site: str, now: datetime
) -> NormalizedJob:
    slug = optional_text(record.get("slug")) or optional_text(record.get("req_id"))
    url = f"https://{site}/careers-home/jobs/{slug}" if slug and _SLUG.match(slug) else None
    full = optional_text(record.get("full_location"))
    places = [place.strip() for place in full.split(";")] if full else []
    categories = record.get("categories")
    first = categories[0] if isinstance(categories, list) and categories else None
    kind = (optional_text(record.get("location_type")) or "").lower()
    return make_job(
        provider=PROVIDER,
        tenant=site,
        source=source,
        job_id=slug,
        title=record.get("title"),
        posting_url=url,
        first_seen_at=now,
        locations=unique(places)
        or unique([joined(record.get("city"), record.get("state"), record.get("country"))]),
        department=first.get("name") if isinstance(first, Mapping) else None,
        employment_type=record.get("employment_type"),
        workplace_type=_WORKPLACES.get(kind),
        requisition_id=record.get("req_id"),
        published_at=parse_date(record.get("posted_date")),
    )
