"""Pinpoint job board adapter.

``GET https://{company}.pinpointhq.com/postings.json`` answers ``{"data": [...]}``, the whole public
board, descriptions included.

Mapping: ``id`` is the id and identity is ``pinpoint:{company}:{id}``; ``url`` (which must be on the
company's own ``pinpointhq.com`` host) is the posting and application link; ``location`` reads as
city, province; ``job.department.name`` and ``job.requisition_id`` are the department and
requisition; ``employment_type_text`` and ``workplace_type`` (``remote``/``hybrid``/``onsite``) are
copied. The description fields are not used, so ``content_hash`` digests title and locations.
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
    unique,
    utcnow,
)
from job_alert.adapters.common import optional_text
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "pinpoint"
_ENDPOINT = re.compile(r"^https://([a-z0-9-]+)\.pinpointhq\.com/postings\.json$")
_WORKPLACES = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}


class PinpointAdapter:
    """Normalize one Pinpoint postings feed."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        match = _ENDPOINT.match(str(source.endpoint))
        if match is None:
            raise AdapterError(f"pinpoint endpoint {source.endpoint} is not a postings.json URL")
        company = match.group(1)
        payload = await fetcher.get(
            RequestSpec(url=str(source.endpoint), headers=dict(source.headers), expected="json")
        )
        document = json_document(payload, PROVIDER)
        records = document.get("data") if isinstance(document, Mapping) else None
        if not isinstance(records, list):
            raise AdapterError("pinpoint payload has no data array")
        first_seen_at = self._now()

        def normalize(record: Mapping[str, Any]) -> NormalizedJob:
            url = optional_text(record.get("url"))
            if url is not None and not url.startswith(f"https://{company}.pinpointhq.com/"):
                url = None
            place = record.get("location")
            job = record.get("job") if isinstance(record.get("job"), Mapping) else {}
            department = job.get("department") if isinstance(job, Mapping) else None
            workplace = optional_text(record.get("workplace_type"))
            return make_job(
                provider=PROVIDER,
                tenant=company,
                source=source,
                job_id=record.get("id"),
                title=record.get("title"),
                posting_url=url,
                first_seen_at=first_seen_at,
                locations=unique(
                    [
                        joined(place.get("city"), place.get("province"))
                        if isinstance(place, Mapping)
                        else None
                    ]
                ),
                department=department.get("name") if isinstance(department, Mapping) else None,
                requisition_id=job.get("requisition_id") if isinstance(job, Mapping) else None,
                employment_type=record.get("employment_type_text"),
                workplace_type=_WORKPLACES.get((workplace or "").lower()),
            )

        return collect_document(source=source, records=records, normalize=normalize)
