"""Breezy HR career board adapter.

``GET https://{company}.breezy.hr/json`` answers the whole public board as one JSON array.

Mapping: ``id`` is the id and identity is ``breezy:{company}:{id}``; ``name`` the title; ``url``
(on the company's ``breezy.hr`` host) the posting link; each ``locations`` entry (or the single
``location``) read as city, state, country, falling back to its own ``name``; a remote location
the workplace type ``remote``; ``department`` the department; ``type.name`` the employment type;
``published_date`` the publication date.
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

PROVIDER = "breezy"
_ENDPOINT = re.compile(r"^https://([a-z0-9-]+)\.breezy\.hr/json$")


class BreezyAdapter:
    """Normalize one Breezy HR board's JSON listing."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        match = _ENDPOINT.match(str(source.endpoint))
        if match is None:
            raise AdapterError(f"breezy endpoint {source.endpoint} is not a breezy.hr/json URL")
        company = match.group(1)
        payload = await fetcher.get(
            RequestSpec(url=str(source.endpoint), headers=dict(source.headers), expected="json")
        )
        records = json_document(payload, PROVIDER)
        if not isinstance(records, list):
            raise AdapterError("breezy payload is not a JSON array")
        first_seen_at = self._now()
        on_site = f"https://{company}.breezy.hr/"

        def normalize(record: Mapping[str, Any]) -> NormalizedJob:
            entries = record.get("locations")
            listed = entries if isinstance(entries, list) and entries else [record.get("location")]
            places: list[Mapping[str, Any]] = [
                place for place in listed if isinstance(place, Mapping)
            ]
            url = optional_text(record.get("url"))
            kind = record.get("type")
            return make_job(
                provider=PROVIDER,
                tenant=company,
                source=source,
                job_id=record.get("id"),
                title=record.get("name"),
                posting_url=url if url and url.startswith(on_site) else None,
                first_seen_at=first_seen_at,
                locations=unique(_location(place) for place in places),
                department=record.get("department"),
                employment_type=kind.get("name") if isinstance(kind, Mapping) else None,
                workplace_type=(
                    "remote" if any(place.get("is_remote") is True for place in places) else None
                ),
                published_at=parse_date(record.get("published_date")),
            )

        return collect_document(source=source, records=records, normalize=normalize)


def _location(place: Mapping[str, Any]) -> str | None:
    state, country = place.get("state"), place.get("country")
    return joined(
        place.get("city"),
        state.get("name") if isinstance(state, Mapping) else None,
        country.get("name") if isinstance(country, Mapping) else None,
    ) or optional_text(place.get("name"))
