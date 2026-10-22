"""Dayforce candidate portal adapter (``jobs.dayforcehcm.com``).

A Dayforce job board (``https://jobs.dayforcehcm.com/en-US/{client}/{board}``) is a Next.js page
whose list comes from ``POST /api/geo/{client}/jobposting/search``. The search answers 25 postings
a page with the size of the result set (``maxCount``); ``paginationStart`` is the offset. Like the
page, every POST carries the ``X-CSRF-TOKEN`` that ``GET /api/auth/csrf`` hands out with its
cookie, so a read starts with that one request.

Mapping: ``jobPostingId`` is the id and identity is ``dayforce:{client}:{id}``; ``jobTitle`` is the
title; each ``postingLocations`` entry is its ``formattedAddress`` (``Cambridge, ON, Canada``), and
``hasVirtualLocation`` adds ``Remote``; ``jobReqId`` is the requisition and
``postingStartTimestampUTC`` the publication date. The posting link is the board's own
``/{culture}/{client}/{board}/jobs/{id}``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import json_document, make_job, parse_date, unique, utcnow
from job_alert.adapters.common import optional_text
from job_alert.adapters.paged import UnusableRecord, walk_scoped
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "dayforce"
ORIGIN = "https://jobs.dayforcehcm.com"
SEARCH_CAP = 100_000
MAX_PAGES = 200
_BOARD = re.compile(r"^/([a-z]{2}-[A-Za-z]{2})/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)/?$")


class DayforceAdapter:
    """Normalize one Dayforce job board, one search page per request."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        parts = urlsplit(str(source.endpoint))
        match = _BOARD.match(parts.path)
        if f"{parts.scheme}://{parts.netloc}" != ORIGIN or match is None:
            raise AdapterError(
                f"dayforce endpoint {source.endpoint} is not a jobs.dayforcehcm.com board URL"
            )
        culture, client, board = match.groups()
        first_seen_at = self._now()
        token = await _csrf_token(fetcher, source)

        async def fetch_page(_: int, offset: int) -> tuple[Sequence[object], int]:
            payload = await fetcher.post(
                RequestSpec(
                    url=f"{ORIGIN}/api/geo/{client}/jobposting/search",
                    headers={**source.headers, "X-CSRF-TOKEN": token, "Accept": "application/json"},
                    expected="json",
                    json_body={
                        "clientNamespace": client,
                        "jobBoardCode": board,
                        "cultureCode": culture,
                        "distanceUnit": 0,
                        "paginationStart": offset,
                    },
                    idempotent=True,
                )
            )
            document = json_document(payload, PROVIDER)
            records = document.get("jobPostings") if isinstance(document, Mapping) else None
            total = document.get("maxCount") if isinstance(document, Mapping) else None
            if not isinstance(records, list):
                raise AdapterError("dayforce search answered no jobPostings list")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise AdapterError("dayforce search answered no non-negative maxCount")
            return records, total

        def normalize(record: object) -> NormalizedJob:
            if not isinstance(record, Mapping):
                raise UnusableRecord("record is not a JSON object")
            return _normalize(
                record, source=source, path=f"/{culture}/{client}/{board}", now=first_seen_at
            )

        return await walk_scoped(
            source_id=source.id,
            fetch_page=fetch_page,
            normalize=normalize,
            cap=SEARCH_CAP,
            max_pages=MAX_PAGES,
            cap_advice="narrow the board",
        )


async def _csrf_token(fetcher: HttpFetcher, source: SourceConfig) -> str:
    payload = await fetcher.get(
        RequestSpec(url=f"{ORIGIN}/api/auth/csrf", headers=dict(source.headers), expected="json")
    )
    document = json_document(payload, PROVIDER)
    token = optional_text(document.get("csrfToken")) if isinstance(document, Mapping) else None
    if token is None:
        raise AdapterError("dayforce handed out no CSRF token")
    return token


def _normalize(
    record: Mapping[str, Any], *, source: SourceConfig, path: str, now: datetime
) -> NormalizedJob:
    job_id = record.get("jobPostingId")
    identifier = str(job_id) if isinstance(job_id, int) and job_id > 0 else None
    places = record.get("postingLocations")
    locations = unique(
        optional_text(place.get("formattedAddress"))
        for place in (places if isinstance(places, list) else [])
        if isinstance(place, Mapping)
    )
    if record.get("hasVirtualLocation") is True:
        locations = unique([*locations, "Remote"])
    client = path.split("/")[2]
    return make_job(
        provider=PROVIDER,
        tenant=client,
        source=source,
        job_id=identifier,
        title=record.get("jobTitle"),
        posting_url=f"{ORIGIN}{path}/jobs/{identifier}" if identifier else None,
        first_seen_at=now,
        locations=locations,
        requisition_id=record.get("jobReqId"),
        workplace_type="remote"
        if record.get("hasVirtualLocation") is True and not places
        else None,
        published_at=parse_date(record.get("postingStartTimestampUTC")),
    )
