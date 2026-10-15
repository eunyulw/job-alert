"""UKG Pro (UltiPro) Recruiting job board adapter.

A UKG board (``https://recruiting.ultipro.com/{tenant}/JobBoard/{board}/``) loads its listings by
``POST …/JobBoard/{board}/JobBoardView/LoadSearchResults`` with an ``opportunitySearch`` document
(``Top``, ``Skip``, ``QueryString``, ``Filters``) and answers ``{"opportunities": [...],
"totalCount": N}``. The endpoint is that ``LoadSearchResults`` URL. A source may narrow the search
with ``parameters.query`` (the ``QueryString``); the adapter owns ``Top`` and ``Skip``.

Mapping: ``Id`` (a GUID) is the id and identity is ``ukg:{tenant}/{board}:{Id}``; ``Title`` the
title; ``RequisitionNumber`` the requisition; ``JobCategoryName`` the department; each
``Locations[].Address`` reads as city, state code, country code, falling back to the location's
description; ``FullTime`` is the employment type; ``PostedDate`` the publication time. The posting
link is the board's public page, ``…/JobBoard/{board}/OpportunityDetail?opportunityId={Id}``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import joined, json_document, make_job, parse_date, unique, utcnow
from job_alert.adapters.common import optional_text
from job_alert.adapters.paged import UnusableRecord, walk_scoped
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "ukg"
PAGE_SIZE = 50
SEARCH_CAP = 100_000
MAX_PAGES = 200
_ENDPOINT = re.compile(
    r"^https://(recruiting\d*\.ultipro\.com)/([A-Za-z0-9]+)/JobBoard/([0-9a-f-]{36})"
    r"/JobBoardView/LoadSearchResults$"
)
_GUID = re.compile(r"^[0-9a-fA-F-]{36}$")
_EMPLOYMENT = {True: "Full-time", False: "Part-time"}


class UkgAdapter:
    """Normalize one UKG Pro Recruiting job board search."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        match = _ENDPOINT.match(str(source.endpoint))
        if match is None:
            raise AdapterError(f"ukg endpoint {source.endpoint} is not a LoadSearchResults URL")
        host, tenant, board = match.groups()
        query = optional_text(source.parameters.get("query")) or ""
        base = f"https://{host}/{tenant}/JobBoard/{board}"
        first_seen_at = self._now()

        async def fetch_page(_: int, offset: int) -> tuple[Sequence[object], int]:
            payload = await fetcher.post(
                RequestSpec(
                    url=str(source.endpoint),
                    headers=dict(source.headers),
                    expected="json",
                    json_body={
                        "opportunitySearch": {
                            "Top": PAGE_SIZE,
                            "Skip": offset,
                            "QueryString": query,
                            "OrderBy": [
                                {
                                    "Value": "postedDateDesc",
                                    "PropertyName": "PostedDate",
                                    "Ascending": False,
                                }
                            ],
                            "Filters": [],
                        },
                        "matchCriteria": {
                            "PreferredJobs": [],
                            "Educations": [],
                            "LicenseAndCertifications": [],
                            "Skills": [],
                            "hasNoLicenses": False,
                            "SkippedSkills": [],
                        },
                    },
                    # A search is read-only, so retrying it can never duplicate anything.
                    idempotent=True,
                )
            )
            return _parse_page(json_document(payload, PROVIDER))

        def normalize(record: object) -> NormalizedJob:
            if not isinstance(record, Mapping):
                raise UnusableRecord("record is not a JSON object")
            job_id = optional_text(record.get("Id"))
            url = None
            if job_id is not None and _GUID.match(job_id):
                url = f"{base}/OpportunityDetail?opportunityId={job_id}"
            full_time = record.get("FullTime")
            return make_job(
                provider=PROVIDER,
                tenant=f"{tenant}/{board}",
                source=source,
                job_id=job_id,
                title=record.get("Title"),
                posting_url=url,
                first_seen_at=first_seen_at,
                locations=_locations(record.get("Locations")),
                department=record.get("JobCategoryName"),
                requisition_id=record.get("RequisitionNumber"),
                employment_type=_EMPLOYMENT.get(full_time) if isinstance(full_time, bool) else None,
                published_at=parse_date(record.get("PostedDate")),
            )

        return await walk_scoped(
            source_id=source.id,
            fetch_page=fetch_page,
            normalize=normalize,
            cap=SEARCH_CAP,
            max_pages=MAX_PAGES,
            cap_advice="narrow the source's query",
        )


def _parse_page(document: Any) -> tuple[list[Any], int]:
    if not isinstance(document, Mapping):
        raise AdapterError("ukg payload is not a JSON object")
    records, total = document.get("opportunities"), document.get("totalCount")
    if not isinstance(records, list):
        raise AdapterError("ukg payload has no opportunities array")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AdapterError("ukg payload has no non-negative integer totalCount")
    return records, total


def _locations(entries: object) -> list[str]:
    places: list[str | None] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, Mapping):
            continue
        address = entry.get("Address")
        place = None
        if isinstance(address, Mapping):
            state, country = address.get("State"), address.get("Country")
            place = joined(
                address.get("City"),
                state.get("Code") if isinstance(state, Mapping) else None,
                country.get("Code") if isinstance(country, Mapping) else None,
            )
        places.append(place or optional_text(entry.get("LocalizedDescription")))
    return unique(places)
