"""Gem job board adapter.

``GET https://api.gem.com/job_board/v0/{board}/job_posts/`` answers the board's whole public list
as a JSON array, the shape of Greenhouse's job board API.

Mapping: ``id`` is the id and identity is ``gem:{board}:{id}``; ``absolute_url`` (which must be on
``jobs.gem.com/{board}/``) is the posting link; ``offices[].location.name``, or else
``location.name``, are the locations (``Seal Beach, United States``); the first department is the
department; ``requisition_id``, ``employment_type`` and ``location_type`` (``remote``, ``hybrid``,
``in_office``) are copied; ``first_published_at`` is the publication date.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import (
    collect_document,
    json_document,
    make_job,
    parse_date,
    unique,
    utcnow,
)
from job_alert.adapters.common import optional_text
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "gem"
_ENDPOINT = re.compile(r"^https://api\.gem\.com/job_board/v0/([a-z0-9-]+)/job_posts/$")
_WORKPLACES = {"remote": "remote", "hybrid": "hybrid", "in_office": "onsite"}


def _name(value: object) -> str | None:
    return optional_text(value.get("name")) if isinstance(value, Mapping) else None


class GemAdapter:
    """Normalize one Gem job board."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        match = _ENDPOINT.match(str(source.endpoint))
        if match is None:
            raise AdapterError(f"gem endpoint {source.endpoint} is not a job_posts/ URL")
        board = match.group(1)
        payload = await fetcher.get(
            RequestSpec(url=str(source.endpoint), headers=dict(source.headers), expected="json")
        )
        records = json_document(payload, PROVIDER)
        if not isinstance(records, list):
            raise AdapterError("gem payload is not a list of job posts")
        first_seen_at = self._now()

        def normalize(record: Mapping[str, Any]) -> NormalizedJob:
            url = optional_text(record.get("absolute_url"))
            if url is not None and not url.startswith(f"https://jobs.gem.com/{board}/"):
                url = None
            offices = record.get("offices")
            places = [
                _name(office.get("location"))
                for office in (offices if isinstance(offices, list) else [])
                if isinstance(office, Mapping)
            ]
            departments = record.get("departments")
            first = departments[0] if isinstance(departments, list) and departments else None
            kind = optional_text(record.get("location_type")) or ""
            return make_job(
                provider=PROVIDER,
                tenant=board,
                source=source,
                job_id=record.get("id"),
                title=record.get("title"),
                posting_url=url,
                first_seen_at=first_seen_at,
                locations=unique(places) or unique([_name(record.get("location"))]),
                department=_name(first),
                employment_type=record.get("employment_type"),
                workplace_type=_WORKPLACES.get(kind),
                requisition_id=record.get("requisition_id"),
                published_at=parse_date(record.get("first_published_at")),
            )

        return collect_document(source=source, records=records, normalize=normalize)
