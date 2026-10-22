"""ADP Workforce Now recruitment page adapter (``workforcenow.adp.com``).

Smaller employers on Workforce Now publish their board at
``https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={client id}``
(the source's endpoint), a JavaScript page over one public call:
``GET .../careercenter/public/events/staffing/v1/job-requisitions?cid={cid}&$top=20&$skip={n}``,
which answers ``{"jobRequisitions": [...], "meta": {"totalNumber": N}}``. The MyJobs sites larger
clients use are read by the ``adp`` adapter instead.

Mapping: ``itemID`` is the id and identity is ``adp-wfn:{cid}:{id}``; ``requisitionTitle`` the
title; each ``requisitionLocations[].nameCode.shortName`` a location (``San Jose, CA, US``);
``clientRequisitionID`` the requisition; ``workLevelCode.shortName`` the employment type;
``postDate`` the publication date. The posting link is the recruitment page with ``jobId``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import json_document, make_job, parse_date, unique, utcnow
from job_alert.adapters.common import optional_text
from job_alert.adapters.paged import UnusableRecord, walk_scoped
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "adp-wfn"
ORIGIN = "https://workforcenow.adp.com"
PAGE_PATH = "/mascsr/default/mdf/recruitment/recruitment.html"
LIST_PATH = "/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
PAGE_SIZE = 20
SEARCH_CAP = 100_000
MAX_PAGES = 100
_CID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
_ITEM = re.compile(r"^[0-9A-Za-z_]+$")


class AdpWorkforceNowAdapter:
    """Normalize one Workforce Now recruitment board, one page per request."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        parts = urlsplit(str(source.endpoint))
        cid = (parse_qs(parts.query).get("cid") or [""])[0]
        if (
            f"{parts.scheme}://{parts.netloc}" != ORIGIN
            or parts.path != PAGE_PATH
            or not _CID.match(cid)
        ):
            raise AdapterError(
                f"adp-wfn endpoint {source.endpoint} is not a recruitment.html?cid= page"
            )
        first_seen_at = self._now()

        async def fetch_page(_: int, offset: int) -> tuple[Sequence[object], int]:
            query = urlencode(
                {"cid": cid, "lang": "en_US", "locale": "en_US", "$top": PAGE_SIZE, "$skip": offset}
            )
            payload = await fetcher.get(
                RequestSpec(
                    url=f"{ORIGIN}{LIST_PATH}?{query}",
                    headers={**source.headers, "Accept": "application/json"},
                    expected="json",
                )
            )
            document = json_document(payload, PROVIDER)
            records = document.get("jobRequisitions") if isinstance(document, Mapping) else None
            meta = document.get("meta") if isinstance(document, Mapping) else None
            total = meta.get("totalNumber") if isinstance(meta, Mapping) else None
            if not isinstance(records, list):
                raise AdapterError("adp-wfn answered no jobRequisitions list")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise AdapterError("adp-wfn answered no non-negative meta.totalNumber")
            return records, total

        def normalize(record: object) -> NormalizedJob:
            if not isinstance(record, Mapping):
                raise UnusableRecord("record is not a JSON object")
            return _normalize(record, source=source, cid=cid, now=first_seen_at)

        return await walk_scoped(
            source_id=source.id,
            fetch_page=fetch_page,
            normalize=normalize,
            cap=SEARCH_CAP,
            max_pages=MAX_PAGES,
            cap_advice="narrow the board",
        )


def _normalize(
    record: Mapping[str, Any], *, source: SourceConfig, cid: str, now: datetime
) -> NormalizedJob:
    item = optional_text(record.get("itemID"))
    identifier = item if item and _ITEM.match(item) else None
    places = record.get("requisitionLocations")
    level = record.get("workLevelCode")
    return make_job(
        provider=PROVIDER,
        tenant=cid,
        source=source,
        job_id=identifier,
        title=record.get("requisitionTitle"),
        posting_url=f"{ORIGIN}{PAGE_PATH}?" + urlencode({"cid": cid, "jobId": identifier})
        if identifier
        else None,
        first_seen_at=now,
        locations=unique(
            optional_text(place["nameCode"].get("shortName"))
            for place in (places if isinstance(places, list) else [])
            if isinstance(place, Mapping) and isinstance(place.get("nameCode"), Mapping)
        ),
        requisition_id=record.get("clientRequisitionID"),
        employment_type=level.get("shortName") if isinstance(level, Mapping) else None,
        published_at=parse_date(record.get("postDate")),
    )
