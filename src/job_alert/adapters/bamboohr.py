"""BambooHR careers list adapter.

``GET https://{company}.bamboohr.com/careers/list`` answers ``{"meta": {"totalCount": N},
"result": [...]}``, the whole public board, and the collection must hold ``totalCount`` records.

Mapping: ``id`` is the id and identity is ``bamboohr:{company}:{id}``; ``jobOpeningName`` is the
title; the posting link is ``https://{company}.bamboohr.com/careers/{id}``, the public page the
board links to; ``location`` (``city``, ``state``) plus ``atsLocation.country`` is the location;
``isRemote`` is the workplace type ``remote``; ``departmentLabel`` and ``employmentStatusLabel``
are the department and employment type.
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

PROVIDER = "bamboohr"
_ENDPOINT = re.compile(r"^https://([a-z0-9-]+)\.bamboohr\.com/careers/list$")
_DIGITS = re.compile(r"^\d+$")


class BambooHrAdapter:
    """Normalize one BambooHR careers list."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        match = _ENDPOINT.match(str(source.endpoint))
        if match is None:
            raise AdapterError(f"bamboohr endpoint {source.endpoint} is not a careers/list URL")
        company = match.group(1)
        payload = await fetcher.get(
            RequestSpec(url=str(source.endpoint), headers=dict(source.headers), expected="json")
        )
        document = json_document(payload, PROVIDER)
        records = document.get("result") if isinstance(document, Mapping) else None
        meta = document.get("meta") if isinstance(document, Mapping) else None
        total = meta.get("totalCount") if isinstance(meta, Mapping) else None
        if not isinstance(records, list):
            raise AdapterError("bamboohr payload has no result array")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise AdapterError("bamboohr payload has no non-negative integer meta.totalCount")
        first_seen_at = self._now()

        def normalize(record: Mapping[str, Any]) -> NormalizedJob:
            job_id = optional_text(record.get("id"))
            url = None
            if job_id is not None and _DIGITS.match(job_id):
                url = f"https://{company}.bamboohr.com/careers/{job_id}"
            place = record.get("location")
            ats = record.get("atsLocation")
            location = joined(
                place.get("city") if isinstance(place, Mapping) else None,
                place.get("state") if isinstance(place, Mapping) else None,
                ats.get("country") if isinstance(ats, Mapping) else None,
            )
            return make_job(
                provider=PROVIDER,
                tenant=company,
                source=source,
                job_id=job_id,
                title=record.get("jobOpeningName"),
                posting_url=url,
                first_seen_at=first_seen_at,
                locations=unique([location]),
                department=record.get("departmentLabel"),
                employment_type=record.get("employmentStatusLabel"),
                workplace_type="remote" if record.get("isRemote") is True else None,
            )

        return collect_document(source=source, records=records, normalize=normalize, declared=total)
