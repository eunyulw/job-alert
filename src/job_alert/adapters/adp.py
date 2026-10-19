"""ADP Workforce Now "MyJobs" career site adapter.

A career site at ``https://myjobs.adp.com/{site}/cx`` is a JavaScript app over two public calls:

- ``GET https://myjobs.adp.com/public/staffing/v1/career-site/{site}`` (the source's endpoint)
  answers the site's configuration, whose ``orgoid`` names the client organization;
- ``GET https://my.adp.com/myadp_prefix/mycareer/public/staffing/v1/job-requisitions
  ?$top=100&$skip={n}`` with the header ``orgoid: {orgoid}`` answers ``{"count": N,
  "jobRequisitions": [...]}``, one page at a time.

The adapter reads the configuration on every collection, pages the requisitions, and judges
completeness against ``count``.

Mapping: ``reqId`` is the id and identity is ``adp:{site}:{reqId}``; ``jobTitle`` the title; each
``requisitionLocations[].address`` (city, state, country) a location; ``clientRequisitionID`` the
requisition id. The posting link is the site's job page, ``https://myjobs.adp.com/{site}/cx/job/
{reqId}``. The list carries no description and no date.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import joined, json_document, make_job, unique, utcnow
from job_alert.adapters.common import optional_text
from job_alert.adapters.paged import UnusableRecord, walk_scoped
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "adp"
PAGE_SIZE = 100
SEARCH_CAP = 100_000
MAX_PAGES = 200
LIST_URL = "https://my.adp.com/myadp_prefix/mycareer/public/staffing/v1/job-requisitions"
_ENDPOINT = re.compile(
    r"^https://myjobs\.adp\.com/public/staffing/v1/career-site/([A-Za-z0-9_-]+)$"
)
_ORGOID = re.compile(r"^[A-Za-z0-9_-]+$")
_REQ = re.compile(r"^\d+$")


class AdpAdapter:
    """Normalize one ADP MyJobs career site, one requisition page per request."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        match = _ENDPOINT.match(str(source.endpoint))
        if match is None:
            raise AdapterError(f"adp endpoint {source.endpoint} is not a MyJobs career-site URL")
        site = match.group(1)
        config = json_document(
            await fetcher.get(
                RequestSpec(url=str(source.endpoint), headers=dict(source.headers), expected="json")
            ),
            PROVIDER,
        )
        orgoid = optional_text(config.get("orgoid")) if isinstance(config, Mapping) else None
        if orgoid is None or not _ORGOID.match(orgoid):
            raise AdapterError(f"adp career site {site} publishes no orgoid")
        headers = {**source.headers, "orgoid": orgoid}
        first_seen_at = self._now()

        async def fetch_page(_: int, offset: int) -> tuple[Sequence[object], int]:
            payload = await fetcher.get(
                RequestSpec(
                    url=f"{LIST_URL}?$top={PAGE_SIZE}&$skip={offset}",
                    headers=headers,
                    expected="json",
                )
            )
            document = json_document(payload, PROVIDER)
            records = document.get("jobRequisitions") if isinstance(document, Mapping) else None
            total = document.get("count") if isinstance(document, Mapping) else None
            if records is None and total == 0:
                records = []
            if not isinstance(records, list):
                raise AdapterError("adp payload has no jobRequisitions array")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise AdapterError("adp payload has no non-negative integer count")
            return records, total

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
            cap_advice="the board is too large to read whole",
        )


def _normalize(
    record: Mapping[str, Any], *, source: SourceConfig, site: str, now: datetime
) -> NormalizedJob:
    req = optional_text(record.get("reqId"))
    url = f"https://myjobs.adp.com/{site}/cx/job/{req}" if req and _REQ.match(req) else None
    entries = record.get("requisitionLocations")
    places = []
    for entry in entries if isinstance(entries, list) else []:
        address = entry.get("address") if isinstance(entry, Mapping) else None
        if not isinstance(address, Mapping):
            continue
        state, country = address.get("countrySubdivisionLevel1"), address.get("country")
        places.append(
            joined(
                address.get("cityName"),
                state.get("longName") if isinstance(state, Mapping) else None,
                country.get("longName") if isinstance(country, Mapping) else None,
            )
        )
    return make_job(
        provider=PROVIDER,
        tenant=site,
        source=source,
        job_id=req,
        title=record.get("jobTitle"),
        posting_url=url,
        first_seen_at=now,
        locations=unique(places),
        requisition_id=record.get("clientRequisitionID"),
    )
