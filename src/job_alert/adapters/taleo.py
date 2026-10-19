"""Oracle Taleo career section adapter.

A Taleo career section (``https://{host}/careersection/{section}/jobsearch.ftl``) renders its
search with JavaScript from a public REST call. The section page names its portal
(``portalNo: '8140753014'``), and ``POST https://{host}/careersection/rest/jobboard/searchjobs
?lang=en&portal={portal}`` with a search body answers ``{"requisitionList": [...], "pagingData":
{"totalCount": N, "pageSize": 25}}`` one page (``pageNo``, from 1) at a time, newest first. The
adapter reads the portal number on every collection and pages until a page comes back empty.

``totalCount`` is not always the number of postings served: some sections count requisitions they
then leave out of the pages (Textron counted 717 and served 656, its pages coming back 24, 23 or 20
records short without any repeat). So a walk is complete when it reads exactly ``totalCount``
postings; failing that, when it ends on an empty page with no repeated or malformed record and a
second walk reads exactly the same postings. A posting published or removed mid-walk makes the two
walks differ, and the collection is then incomplete.

Mapping: ``contestNo`` (the public job number) is the id and identity is
``taleo:{host}/{section}:{contestNo}``; ``column[0]`` the title; the ``column`` entries that
``locationsColumns`` points at (each a JSON list such as ``["US-California-Point Mugu"]``) the
locations, each ``US-California-Point Mugu`` label read as ``Point Mugu, California, US``; the
first column holding a date (``10/17/2026`` or ``Oct 17, 2026``) the publication date. The posting
link is the section's job page, ``https://{host}/careersection/{section}/jobdetail.ftl?job=
{contestNo}&lang=en``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import dashed_location, json_document, make_job, unique, utcnow
from job_alert.adapters.common import optional_text
from job_alert.adapters.paged import UnusableRecord
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "taleo"
MAX_PAGES = 400
_SECTION = re.compile(r"^/careersection/([A-Za-z0-9_.-]+)/jobsearch\.ftl$")
_PORTAL = re.compile(r"portalNo:\s*'(\d+)'")
_CONTEST = re.compile(r"^[A-Za-z0-9_-]+$")
_DATE_FORMATS = ("%m/%d/%Y", "%b %d, %Y", "%d-%b-%Y")


class TaleoAdapter:
    """Normalize one Taleo career section, one ``searchjobs`` page per request."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        parts = urlsplit(str(source.endpoint))
        section_match = _SECTION.match(parts.path)
        if parts.scheme != "https" or section_match is None:
            raise AdapterError(
                f"taleo endpoint {source.endpoint} is not a careersection/{{section}}/jobsearch.ftl"
                " URL"
            )
        host, section = parts.netloc.lower(), section_match.group(1)
        page = await fetcher.get(
            RequestSpec(
                url=f"https://{parts.netloc}{parts.path}?lang=en",
                headers=dict(source.headers),
                expected="html",
            )
        )
        portal = _PORTAL.search(page.body.decode("utf-8", errors="replace"))
        if portal is None:
            raise AdapterError(f"taleo section {host}/{section} names no portal number")
        search_url = (
            f"https://{parts.netloc}/careersection/rest/jobboard/searchjobs"
            f"?lang=en&portal={portal.group(1)}"
        )
        headers = {**source.headers, "tz": "GMT+00:00", "tzname": "UTC"}
        first_seen_at = self._now()

        async def fetch_page(page: int) -> tuple[list[object], int]:
            payload = await fetcher.post(
                RequestSpec(
                    url=search_url,
                    headers=headers,
                    expected="json",
                    json_body=_search_body(page),
                    idempotent=True,
                )
            )
            document = json_document(payload, PROVIDER)
            records = document.get("requisitionList") if isinstance(document, Mapping) else None
            paging = document.get("pagingData") if isinstance(document, Mapping) else None
            total = paging.get("totalCount") if isinstance(paging, Mapping) else None
            if not isinstance(records, list):
                raise AdapterError("taleo payload has no requisitionList")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise AdapterError("taleo payload has no non-negative integer totalCount")
            return records, total

        async def walk() -> Walk:
            result = Walk()
            for page in range(1, MAX_PAGES + 1):
                records, total = await fetch_page(page)
                result.pages += 1
                if result.total is None:
                    result.total = total
                if not records:
                    break
                known = len(result.jobs)
                for index, record in enumerate(records):
                    where = f"page {page} record {index}"
                    if not isinstance(record, Mapping):
                        result.warnings.append(f"skipped malformed record at {where}")
                        continue
                    try:
                        job = _normalize(
                            record, source=source, host=host, section=section, now=first_seen_at
                        )
                    except UnusableRecord as exc:
                        result.warnings.append(f"skipped malformed record at {where}: {exc}")
                        continue
                    if job.source_job_id in result.jobs:
                        result.warnings.append(f"skipped duplicate record at {where}")
                        continue
                    result.jobs[job.source_job_id] = job
                if len(result.jobs) == known:
                    # A section that ignores pageNo past its end serves the same page forever.
                    result.warnings.append(f"page {page} repeated earlier records only; stopped")
                    break
            else:
                result.warnings.append(f"stopped after {MAX_PAGES} pages: the page cap is reached")
            return result

        first = await walk()
        pages = first.pages
        verdict = first.verdict()
        if verdict is None and first.total is not None and len(first.jobs) != first.total:
            second = await walk()
            pages += second.pages
            if second.verdict() is not None or set(second.jobs) != set(first.jobs):
                verdict = (
                    f"two walks disagreed ({len(first.jobs)} and {len(second.jobs)} postings,"
                    f" against a declared {first.total}), so the collection is incomplete"
                )
        warnings = first.warnings if first.warnings else ([verdict] if verdict else [])
        return CollectionResult(
            source_id=source.id,
            complete=not warnings,
            jobs=list(first.jobs.values()),
            warnings=warnings,
            page_count=pages,
            # The postings served: the section's own total can count some it never serves.
            declared_total=len(first.jobs) if not warnings else first.total,
        )


@dataclass(slots=True)
class Walk:
    """One pass over a section's pages."""

    jobs: dict[str, NormalizedJob] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    pages: int = 0
    total: int | None = None

    def verdict(self) -> str | None:
        """Why this walk alone cannot be complete, or ``None`` when it can be."""
        return self.warnings[0] if self.warnings else None


def _search_body(page: int) -> dict[str, Any]:
    return {
        "multilineEnabled": False,
        # 3 is the posting date; descending puts the newest first.
        "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": False},
        "fieldData": {"fields": {"KEYWORD": "", "LOCATION": ""}, "valid": True},
        "filterSelectionParam": {"searchFilterSelections": []},
        "advancedSearchFiltersSelectionParam": {"searchFilterSelections": []},
        "pageNo": page,
    }


def _normalize(
    record: Mapping[str, Any], *, source: SourceConfig, host: str, section: str, now: datetime
) -> NormalizedJob:
    contest = optional_text(record.get("contestNo"))
    url = None
    if contest is not None and _CONTEST.match(contest):
        url = (
            f"https://{host}/careersection/{quote(section)}/jobdetail.ftl"
            f"?job={contest}&lang=en"
        )
    columns = record.get("column")
    cells = [optional_text(cell) for cell in columns] if isinstance(columns, list) else []
    indexes = record.get("locationsColumns")
    places: list[str | None] = []
    for index in indexes if isinstance(indexes, list) else []:
        if isinstance(index, int) and 0 <= index < len(cells):
            places.extend(_locations(cells[index]))
    return make_job(
        provider=PROVIDER,
        tenant=f"{host}/{section}",
        source=source,
        job_id=contest,
        title=cells[0] if cells else None,
        posting_url=url,
        first_seen_at=now,
        locations=unique(dashed_location(place) for place in places),
        requisition_id=record.get("jobId"),
        published_at=next((date for date in map(_date, cells) if date is not None), None),
    )


def _locations(cell: str | None) -> list[str | None]:
    """A location column: a JSON list of labels, or one plain label."""
    if cell is None:
        return []
    try:
        labels = json.loads(cell)
    except json.JSONDecodeError:
        return [cell]
    if isinstance(labels, list):
        return [optional_text(label) for label in labels]
    return [cell]


def _date(cell: str | None) -> datetime | None:
    for layout in _DATE_FORMATS:
        try:
            return datetime.strptime(cell or "", layout).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None
