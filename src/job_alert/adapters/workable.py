"""Workable job board adapter.

``GET https://apply.workable.com/api/v1/widget/accounts/{slug}`` is the public widget feed: one JSON
object whose ``jobs`` array is the whole board.

Mapping: ``shortcode`` is the id and identity is ``workable:{slug}:{shortcode}``; ``url`` and
``application_url`` (on ``apply.workable.com``) are the links; each visible ``locations`` entry
reads as city, region, country, falling back to the record's own ``city``/``state``/``country``;
``telecommuting: true`` is the workplace type ``remote``; ``published_on`` is the publication date.
A posting open in several places can appear once per place with the same ``shortcode``; those
records are merged and their locations combined.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import (
    collect_document,
    joined,
    json_document,
    make_job,
    parse_date,
    unique,
    utcnow,
)
from job_alert.adapters.common import optional_text
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "workable"
_ENDPOINT = re.compile(r"^https://apply\.workable\.com/api/v1/widget/accounts/([A-Za-z0-9_-]+)$")


class WorkableAdapter:
    """Normalize one Workable account's widget feed."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        match = _ENDPOINT.match(str(source.endpoint))
        if match is None:
            raise AdapterError(f"workable endpoint {source.endpoint} is not a widget account URL")
        slug = match.group(1)
        payload = await fetcher.get(
            RequestSpec(url=str(source.endpoint), headers=dict(source.headers), expected="json")
        )
        document = json_document(payload, PROVIDER)
        jobs = document.get("jobs") if isinstance(document, Mapping) else None
        if not isinstance(jobs, list):
            raise AdapterError("workable payload has no jobs array")
        merged: dict[str, dict[str, Any]] = {}
        order: list[object] = []
        for record in jobs:
            code = optional_text(record.get("shortcode")) if isinstance(record, Mapping) else None
            if code is None or not isinstance(record, Mapping):
                order.append(record)
            elif code in merged:
                merged[code]["_locations"] += _locations(record)
            else:
                merged[code] = {**record, "_locations": _locations(record)}
                order.append(merged[code])
        first_seen_at = self._now()

        def normalize(record: Mapping[str, Any]) -> NormalizedJob:
            url = optional_text(record.get("url"))
            apply = optional_text(record.get("application_url"))
            on_site = "https://apply.workable.com/"
            return make_job(
                provider=PROVIDER,
                tenant=slug,
                source=source,
                job_id=record.get("shortcode"),
                title=record.get("title"),
                posting_url=url if url and url.startswith(on_site) else None,
                apply_url=apply if apply and apply.startswith(on_site) else None,
                first_seen_at=first_seen_at,
                locations=unique(record.get("_locations") or _locations(record)),
                department=record.get("department"),
                employment_type=record.get("employment_type"),
                workplace_type="remote" if record.get("telecommuting") is True else None,
                published_at=parse_date(record.get("published_on")),
            )

        return collect_document(source=source, records=order, normalize=normalize)


def _locations(record: Mapping[str, Any]) -> list[str]:
    entries = record.get("locations")
    places = [
        joined(entry.get("city"), entry.get("region"), entry.get("country"))
        for entry in (entries if isinstance(entries, list) else [])
        if isinstance(entry, Mapping) and entry.get("hidden") is not True
    ]
    return unique(places) or unique(
        [joined(record.get("city"), record.get("state"), record.get("country"))]
    )
