"""Oracle Recruiting Cloud (Fusion HCM Candidate Experience) adapter.

A Candidate Experience site (``…oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1``) loads its
listings from the public REST resource ``recruitingCEJobRequisitions``:

``GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true
&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber=CX_1,…``

It answers ``{"items": [{"TotalJobsCount": N, "requisitionList": [...]}]}``. The ``finder`` value
carries the scope (``siteNumber`` is required; ``keyword=…`` or ``locationId=…`` narrow it) and the
paging; the adapter keeps the configured finder arguments and owns ``limit`` and ``offset``. It
pages 200 at a time (the largest page served) and sorts by posting date unless the finder sets
``sortBy``, because offset paging over the default relevance order shifts between requests.

Even sorted by date, postings that share a date can trade places between two requests, so one walk
may repeat a posting at a page boundary and miss another. No sort key is unique, so the adapter
combines up to ``MAX_WALKS`` walks: the collection is complete only when the union of the postings
the walks read equals the declared total exactly. A malformed record is never retried this way.

Some sites list one requisition twice in a row and count both in ``TotalJobsCount``: the same
``Id`` at two adjacent positions of one page; the board's final position repeating the record
before it; or the first record of a page repeating the last of a previous page that came back one
or more rows short of the requested size (the provider collapsed the pair but counted both). Those
double listings are remembered, and each remembered id counts once more toward the
declared total. A repeat at any other page boundary is what a shifting board looks like, so it
stays a defect for the combined walks to repair.

Mapping: ``Id`` (digits, or letters and digits such as ``req480976``) is the id and identity is
``oracle:{host}/{siteNumber}:{Id}``; ``Title`` the title;
``PrimaryLocation`` and each ``secondaryLocations[].Name`` the locations; ``PostedDate`` the
publication date; ``WorkplaceType`` (``On-site``/``Hybrid``/``Remote``) the workplace type. The
posting link is the site's public job page, ``https://{host}/hcmUI/CandidateExperience/en/sites/
{siteNumber}/job/{Id}``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import json_document, make_job, parse_date, unique, utcnow
from job_alert.adapters.common import optional_text
from job_alert.adapters.paged import UnusableRecord, walk_scoped
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "oracle"
API_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
PAGE_SIZE = 200  # the largest page the resource serves; fewer page boundaries to shift across
SEARCH_CAP = 100_000
MAX_PAGES = 400
MAX_WALKS = 3
OWNED_FINDER = frozenset({"limit", "offset"})
_SITE = re.compile(r"^[A-Za-z0-9_]+$")
_JOB_ID = re.compile(r"^[A-Za-z0-9]+$")
_WORKPLACES = {"on-site": "onsite", "onsite": "onsite", "hybrid": "hybrid", "remote": "remote"}


class OracleAdapter:
    """Normalize one scoped Oracle Recruiting Cloud requisition search."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        parts = urlsplit(str(source.endpoint))
        if parts.scheme != "https" or parts.path != API_PATH:
            raise AdapterError(f"oracle endpoint {source.endpoint} is not a {API_PATH} URL")
        query = parse_qsl(parts.query, keep_blank_values=True)
        finder = next((value for name, value in query if name == "finder"), None)
        if finder is None or not finder.startswith("findReqs;"):
            raise AdapterError(f"oracle source {source.id} has no findReqs finder")
        arguments = [
            (key, value)
            for key, _, value in (item.partition("=") for item in finder[9:].split(",") if item)
            if key not in OWNED_FINDER
        ]
        site = next((value for key, value in arguments if key == "siteNumber"), None)
        if site is None or not _SITE.match(site):
            raise AdapterError(f"oracle source {source.id} has no usable siteNumber")
        if not any(key == "sortBy" for key, _ in arguments):
            arguments.append(("sortBy", "POSTING_DATES_DESC"))
        others = [(name, value) for name, value in query if name != "finder"]
        host = parts.netloc.lower()
        first_seen_at = self._now()
        doubled: set[str] = set()
        last_id: list[str | None] = [None]
        short_page = [False]

        async def fetch_page(_: int, offset: int) -> tuple[Sequence[object], int]:
            page_finder = "findReqs;" + ",".join(
                f"{key}={value}"
                for key, value in [*arguments, ("limit", str(PAGE_SIZE)), ("offset", str(offset))]
            )
            page_query = urlencode([*others, ("finder", page_finder)], safe=";,=", quote_via=quote)
            payload = await fetcher.get(
                RequestSpec(
                    url=urlunsplit(("https", parts.netloc, parts.path, page_query, "")),
                    headers=dict(source.headers),
                    expected="json",
                )
            )
            records, total = _parse_page(json_document(payload, PROVIDER))
            if offset == 0:
                last_id[0], short_page[0] = None, False
            _note_double_listings(
                records, doubled, last_id, offset=offset, total=total, after_short=short_page[0]
            )
            short_page[0] = 0 < len(records) < PAGE_SIZE
            # Records are returned whole: the walk advances its offset by what a page returned.
            return records, total

        def normalize(record: object) -> NormalizedJob:
            if not isinstance(record, Mapping):
                raise UnusableRecord("record is not a JSON object")
            return _normalize(record, source=source, host=host, site=site, now=first_seen_at)

        union: dict[str, NormalizedJob] = {}
        pages = 0
        for _ in range(MAX_WALKS):
            result = await walk_scoped(
                source_id=source.id,
                fetch_page=fetch_page,
                normalize=normalize,
                cap=SEARCH_CAP,
                max_pages=MAX_PAGES,
                cap_advice="narrow the source's finder",
            )
            pages += result.page_count
            if result.complete or any(_structural(warning) for warning in result.warnings):
                return result
            for job in result.jobs:
                union.setdefault(job.source_job_id, job)
            if result.declared_total is not None and (
                len(union) + len(doubled) == result.declared_total
            ):
                return result.model_copy(
                    update={
                        "complete": True,
                        "jobs": list(union.values()),
                        "warnings": [],
                        "page_count": pages,
                        # The distinct postings: the provider's total counts a double listing twice.
                        "declared_total": len(union),
                    }
                )
        return result.model_copy(update={"jobs": list(union.values()), "page_count": pages})


def _note_double_listings(
    records: list[Any],
    doubled: set[str],
    last_id: list[str | None],
    *,
    offset: int,
    total: int,
    after_short: bool,
) -> None:
    """Remember a record that repeats the previous one where only a double listing explains it.

    ``last_id`` carries the previous record's id across the pages of one walk; ``after_short`` says
    the previous page came back short of the requested size.
    """
    for position, record in enumerate(records):
        job_id = optional_text(record.get("Id")) if isinstance(record, Mapping) else None
        tail = offset + position == total - 1
        explained = position > 0 or tail or after_short
        if job_id is not None and job_id == last_id[0] and explained:
            doubled.add(job_id)
        last_id[0] = job_id


def _structural(warning: str) -> bool:
    """A defect another walk cannot repair: a malformed record, the cap, or a missing total."""
    return not (warning.startswith("skipped duplicate") or warning.startswith("collected "))


def _parse_page(document: Any) -> tuple[list[Any], int]:
    items = document.get("items") if isinstance(document, Mapping) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], Mapping):
        raise AdapterError("oracle payload has no search result item")
    result = items[0]
    records, total = result.get("requisitionList"), result.get("TotalJobsCount")
    if not isinstance(records, list):
        raise AdapterError("oracle payload has no requisitionList")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AdapterError("oracle payload has no non-negative integer TotalJobsCount")
    return records, total


def _normalize(
    record: Mapping[str, Any], *, source: SourceConfig, host: str, site: str, now: datetime
) -> NormalizedJob:
    job_id = optional_text(record.get("Id"))
    url = None
    if job_id is not None and _JOB_ID.match(job_id):
        url = f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{job_id}"
    secondary = record.get("secondaryLocations")
    places = [optional_text(record.get("PrimaryLocation"))] + [
        optional_text(entry.get("Name"))
        for entry in (secondary if isinstance(secondary, list) else [])
        if isinstance(entry, Mapping)
    ]
    workplace = (optional_text(record.get("WorkplaceType")) or "").lower()
    return make_job(
        provider=PROVIDER,
        tenant=f"{host}/{site}",
        source=source,
        job_id=job_id,
        title=record.get("Title"),
        posting_url=url,
        first_seen_at=now,
        locations=unique(places),
        department=record.get("Department"),
        team=record.get("JobFamily"),
        employment_type=record.get("WorkerType") or record.get("JobType"),
        workplace_type=_WORKPLACES.get(workplace),
        published_at=parse_date(record.get("PostedDate")),
    )
