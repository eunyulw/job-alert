"""Phenom People career site adapter.

A Phenom career site (``careers.honda.com``, ``careers.zimmerbiomet.com``) loads its search results
from ``POST https://{site}/widgets`` with the body ``{"ddoKey": "refineSearch", "jobs": true,
"from": n, "size": 100, ...}``. It answers ``{"refineSearch": {"totalHits": N, "data": {"jobs":
[...]}}}``; the adapter pages by ``from`` and judges completeness against ``totalHits``. A source
can narrow the search with ``parameters.selected_fields`` (Phenom's facet filter, for example
``{"country": ["United States"]}``), which the adapter sends unchanged, and with
``parameters.keywords``, the site's own keyword search (``"intern"``). That match is loose, the way
an Eightfold ``query`` is, and the filters decide; it narrows boards whose facets have no
internship value, such as Lowe's (4,540 postings, 39 for ``intern`` on 2026-10-22).

Mapping: ``jobSeqNo`` is the id and identity is ``phenom:{site}:{jobSeqNo}``; ``title`` the title;
``multi_location`` (or ``location`` when that is empty) the locations; ``category`` the department;
``type`` the employment type; ``postedDate`` the publication date; ``reqId`` the requisition id.
The posting link is the site's job page, ``https://{site}/{country}/{lang}/job/{jobSeqNo}``, from
the record's ``locale`` (``en_US``). The list carries no description, so none is fetched.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import json_document, make_job, parse_date, unique, utcnow
from job_alert.adapters.common import optional_text
from job_alert.adapters.paged import UnusableRecord, walk_scoped
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "phenom"
PAGE_SIZE = 100
SEARCH_CAP = 10_000  # Phenom's search index will not page past 10,000 hits
MAX_PAGES = 100
_LOCALE = re.compile(r"^([a-z]{2})_([A-Za-z]{2,10})$")
_SEQ = re.compile(r"^[A-Za-z0-9_-]+$")


class PhenomAdapter:
    """Normalize one Phenom career-site search, one ``refineSearch`` page per request."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        parts = urlsplit(str(source.endpoint))
        if parts.scheme != "https" or parts.path != "/widgets" or parts.query:
            raise AdapterError(f"phenom endpoint {source.endpoint} is not an https /widgets URL")
        site = parts.netloc.lower()
        selected = source.parameters.get("selected_fields")
        if selected is not None and not isinstance(selected, Mapping):
            raise AdapterError(f"phenom source {source.id} selected_fields is not an object")
        keywords = source.parameters.get("keywords")
        if keywords is not None and (not isinstance(keywords, str) or not keywords.strip()):
            raise AdapterError(f"phenom source {source.id} keywords is not non-blank text")
        first_seen_at = self._now()

        async def fetch_page(_: int, offset: int) -> tuple[Sequence[object], int]:
            body: dict[str, Any] = {
                "deviceType": "desktop",
                "ddoKey": "refineSearch",
                "jobs": True,
                "from": offset,
                "size": PAGE_SIZE,
            }
            if selected:
                body["selected_fields"] = dict(selected)
            if keywords:
                body["keywords"] = keywords
            payload = await fetcher.post(
                RequestSpec(
                    url=str(source.endpoint),
                    headers=dict(source.headers),
                    expected="json",
                    json_body=body,
                    idempotent=True,
                )
            )
            return _parse_page(json_document(payload, PROVIDER))

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
            cap_advice="narrow the source with parameters.selected_fields or keywords",
        )


def _parse_page(document: Any) -> tuple[list[Any], int]:
    search = document.get("refineSearch") if isinstance(document, Mapping) else None
    data = search.get("data") if isinstance(search, Mapping) else None
    records = data.get("jobs") if isinstance(data, Mapping) else None
    total = search.get("totalHits") if isinstance(search, Mapping) else None
    if not isinstance(records, list):
        raise AdapterError("phenom payload has no refineSearch.data.jobs array")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AdapterError("phenom payload has no non-negative integer refineSearch.totalHits")
    return records, total


def _normalize(
    record: Mapping[str, Any], *, source: SourceConfig, site: str, now: datetime
) -> NormalizedJob:
    seq = optional_text(record.get("jobSeqNo"))
    url = None
    if seq is not None and _SEQ.match(seq):
        locale = _LOCALE.match(optional_text(record.get("locale")) or "")
        path = f"{locale.group(2).lower()}/{locale.group(1)}/job" if locale else "job"
        url = f"https://{site}/{path}/{seq}"
    many = record.get("multi_location")
    places = [optional_text(place) for place in (many if isinstance(many, list) else [])]
    return make_job(
        provider=PROVIDER,
        tenant=site,
        source=source,
        job_id=seq,
        title=record.get("title"),
        posting_url=url,
        first_seen_at=now,
        locations=unique(places) or unique([optional_text(record.get("location"))]),
        department=record.get("category"),
        employment_type=record.get("type"),
        requisition_id=record.get("reqId"),
        published_at=parse_date(record.get("postedDate")),
    )
