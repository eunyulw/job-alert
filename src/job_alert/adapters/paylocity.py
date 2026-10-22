"""Paylocity recruiting board adapter.

``GET https://recruiting.paylocity.com/recruiting/jobs/All/{board guid}`` answers the company's
whole public board as one HTML page that carries every posting in ``window.pageData.Jobs``. The
board's JSON feed (``/recruiting/v2/api/feed/jobs/{guid}``) is not used: it answers an empty list
for boards that have not enabled it, which would pass for an empty board.

Mapping: ``JobId`` is the id and identity is ``paylocity:{guid}:{id}``; the title is ``JobTitle``;
the location is ``JobLocation``'s city, state and country (``Wheeling, IL, USA``), or
``LocationName`` when the structured location is missing, and ``Remote`` for a remote posting;
``HiringDepartment`` is the department and ``PublishedDate`` the publication date. The posting link
is ``https://recruiting.paylocity.com/recruiting/jobs/Details/{id}``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import (
    collect_document,
    joined,
    make_job,
    parse_date,
    unique,
    utcnow,
)
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "paylocity"
ORIGIN = "https://recruiting.paylocity.com"
_ENDPOINT = re.compile(
    r"^https://recruiting\.paylocity\.com/recruiting/jobs/All/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$"
)
_PAGE_DATA = re.compile(r"window\.pageData\s*=\s*(\{.*?\});\s*\n", re.DOTALL)


class PaylocityAdapter:
    """Normalize one Paylocity board page."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        match = _ENDPOINT.match(str(source.endpoint))
        if match is None:
            raise AdapterError(f"paylocity endpoint {source.endpoint} is not a jobs/All board URL")
        board = match.group(1)
        payload = await fetcher.get(
            RequestSpec(url=str(source.endpoint), headers=dict(source.headers), expected="html")
        )
        page = _PAGE_DATA.search(payload.body.decode("utf-8", errors="replace"))
        if page is None:
            raise AdapterError("paylocity board page carries no window.pageData")
        try:
            data = json.loads(page.group(1))
        except json.JSONDecodeError as exc:
            raise AdapterError(f"paylocity pageData is not JSON: {exc}") from exc
        records = data.get("Jobs") if isinstance(data, Mapping) else None
        if not isinstance(records, list):
            raise AdapterError("paylocity pageData has no Jobs list")
        first_seen_at = self._now()

        def normalize(record: Mapping[str, Any]) -> NormalizedJob:
            job_id = record.get("JobId")
            identifier = str(job_id) if isinstance(job_id, int) and job_id > 0 else None
            place = record.get("JobLocation")
            structured = (
                joined(place.get("City"), place.get("State"), place.get("Country"))
                if isinstance(place, Mapping)
                else None
            )
            return make_job(
                provider=PROVIDER,
                tenant=board,
                source=source,
                job_id=identifier,
                title=record.get("JobTitle"),
                posting_url=f"{ORIGIN}/recruiting/jobs/Details/{identifier}"
                if identifier
                else None,
                first_seen_at=first_seen_at,
                locations=unique(
                    [structured or joined(record.get("LocationName"))]
                    + (["Remote"] if record.get("IsRemote") is True else [])
                ),
                department=record.get("HiringDepartment"),
                workplace_type="remote" if record.get("IsRemote") is True else None,
                published_at=parse_date(record.get("PublishedDate")),
            )

        return collect_document(source=source, records=records, normalize=normalize)
