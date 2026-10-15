"""Rippling ATS job board adapter.

``GET https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs`` answers the whole board as
one JSON array. A posting open in several places appears once per place with the same ``uuid``, so
records are merged by ``uuid`` and their ``workLocation`` labels become the posting's locations.

Mapping: ``uuid`` is the id and identity is ``rippling:{slug}:{uuid}``; ``name`` is the title;
``department.label`` the department; ``url`` (which must be on ``ats.rippling.com``) the posting
and application link. The list publishes no description or date.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import collect_document, json_document, make_job, unique, utcnow
from job_alert.adapters.common import optional_text
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "rippling"
_ENDPOINT = re.compile(r"^https://api\.rippling\.com/platform/api/ats/v1/board/([A-Za-z0-9_-]+)/jobs$")


class RipplingAdapter:
    """Normalize one Rippling job board."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        match = _ENDPOINT.match(str(source.endpoint))
        if match is None:
            raise AdapterError(f"rippling endpoint {source.endpoint} is not a board jobs URL")
        slug = match.group(1)
        payload = await fetcher.get(
            RequestSpec(url=str(source.endpoint), headers=dict(source.headers), expected="json")
        )
        document = json_document(payload, PROVIDER)
        if not isinstance(document, list):
            raise AdapterError("rippling payload is not a JSON array")
        merged: dict[str, dict[str, Any]] = {}
        order: list[object] = []
        for record in document:
            uuid = optional_text(record.get("uuid")) if isinstance(record, Mapping) else None
            if uuid is None or not isinstance(record, Mapping):
                order.append(record)
                continue
            label = _location(record)
            if uuid in merged:
                merged[uuid]["_locations"].append(label)
                continue
            merged[uuid] = {**record, "_locations": [label]}
            order.append(merged[uuid])
        first_seen_at = self._now()

        def normalize(record: Mapping[str, Any]) -> NormalizedJob:
            url = optional_text(record.get("url"))
            if url is not None and not url.startswith(f"https://ats.rippling.com/{slug}/"):
                url = None
            department = record.get("department")
            return make_job(
                provider=PROVIDER,
                tenant=slug,
                source=source,
                job_id=record.get("uuid"),
                title=record.get("name"),
                posting_url=url,
                first_seen_at=first_seen_at,
                locations=unique(record.get("_locations", [])),
                department=department.get("label") if isinstance(department, Mapping) else None,
            )

        return collect_document(source=source, records=order, normalize=normalize)


def _location(record: Mapping[str, Any]) -> str | None:
    place = record.get("workLocation")
    return optional_text(place.get("label")) if isinstance(place, Mapping) else None
