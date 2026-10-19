"""Radancy (TalentBrew) career site adapter.

A Radancy career site (``careers.l3harris.com``, ``careers.irobot.com``) fills its search results
from ``GET https://{site}/search-jobs/results?CurrentPage={n}&RecordsPerPage=100&...`` with the
header ``X-Requested-With: XMLHttpRequest``. It answers JSON whose ``results`` member is an HTML
fragment: a ``<section>`` carrying ``data-total-results="N"`` and one ``<li>`` per posting. The
adapter keeps the configured query (any facet filters), owns ``CurrentPage`` and
``RecordsPerPage``, and judges completeness against ``data-total-job-results``. Some sites also
count content pages in ``data-total-results``, so that is read only when the job count is absent.

Mapping, per ``<li>``: the link's ``data-job-id`` is the id and identity is
``radancy:{site}:{id}``; its ``<h2>`` the title; the ``job-location`` span the location; a
``job-category`` span the department; a ``job-date-posted`` span (``10/09/2026``) the publication
date. The posting link is the link's path on the site. A posting labelled ``Multiple Locations`` or
``Remote`` (or not at all) also gets the place its link names, ``/job/united-states/...`` read as
``United States``, so a US-only filter can still place it. The fragment carries no description.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import html_text, json_document, make_job, unique, utcnow
from job_alert.adapters.paged import UnusableRecord, walk_scoped
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "radancy"
PAGE_SIZE = 100
SEARCH_CAP = 100_000
MAX_PAGES = 200
OWNED = frozenset({"CurrentPage", "RecordsPerPage"})
_TOTAL = re.compile(r'data-total-results="(\d+)"')
_JOB_TOTAL = re.compile(r'data-total-job-results="(\d+)"')
_ITEM = re.compile(r"<li\b[^>]*>(.*?)</li>", re.DOTALL)
_LINK = re.compile(r'<a\b[^>]*\bhref="(/[^"]+)"[^>]*\bdata-job-id="(\d+)"')
_TITLE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.DOTALL)
_PLACE_SLUG = re.compile(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?job/([a-z-]+)/")
_VAGUE = frozenset({"multiple locations", "remote"})
_SPAN = re.compile(r'<span\b[^>]*\bclass="([^"]*)"[^>]*>(.*?)</span>', re.DOTALL)


def parse_results(fragment: str) -> tuple[list[dict[str, Any]], int]:
    """The posting items of one results fragment and the total it declares."""
    total = _JOB_TOTAL.search(fragment) or _TOTAL.search(fragment)
    if total is None:
        raise AdapterError("radancy results declare no data-total-results")
    items = []
    for item in _ITEM.findall(fragment):
        link = _LINK.search(item)
        if link is None:
            continue
        spans: dict[str, str | None] = {}
        for classes, value in _SPAN.findall(item):
            for name in ("job-location", "job-category", "job-date-posted"):
                if name in classes.split():
                    spans.setdefault(name, html_text(value))
        title = _TITLE.search(item)
        items.append(
            {
                "href": html.unescape(link.group(1)),
                "id": link.group(2),
                "title": html_text(title.group(1)) if title else None,
                "location": spans.get("job-location"),
                "category": spans.get("job-category"),
                "posted": spans.get("job-date-posted"),
            }
        )
    return items, int(total.group(1))


class RadancyAdapter:
    """Normalize one Radancy career-site search, one results page per request."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        parts = urlsplit(str(source.endpoint))
        if parts.scheme != "https" or parts.path != "/search-jobs/results":
            raise AdapterError(
                f"radancy endpoint {source.endpoint} is not an https /search-jobs/results URL"
            )
        site = parts.netloc.lower()
        query = [
            (name, value)
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
            if name not in OWNED
        ]
        headers = {**source.headers, "X-Requested-With": "XMLHttpRequest"}
        first_seen_at = self._now()

        async def fetch_page(index: int, _: int) -> tuple[Sequence[object], int]:
            page_query = urlencode(
                [*query, ("CurrentPage", str(index + 1)), ("RecordsPerPage", str(PAGE_SIZE))]
            )
            payload = await fetcher.get(
                RequestSpec(
                    url=urlunsplit(("https", parts.netloc, parts.path, page_query, "")),
                    headers=headers,
                    expected="json",
                )
            )
            document = json_document(payload, PROVIDER)
            fragment = document.get("results") if isinstance(document, Mapping) else None
            if not isinstance(fragment, str):
                raise AdapterError("radancy payload has no results fragment")
            return parse_results(fragment)

        def normalize(record: object) -> NormalizedJob:
            if not isinstance(record, Mapping):
                raise UnusableRecord("record is not a parsed item")
            return make_job(
                provider=PROVIDER,
                tenant=site,
                source=source,
                job_id=record.get("id"),
                title=record.get("title"),
                posting_url=f"https://{site}{record['href']}",
                first_seen_at=first_seen_at,
                locations=_locations(record.get("location"), str(record["href"])),
                department=record.get("category"),
                published_at=_date(record.get("posted")),
            )

        return await walk_scoped(
            source_id=source.id,
            fetch_page=fetch_page,
            normalize=normalize,
            cap=SEARCH_CAP,
            max_pages=MAX_PAGES,
            cap_advice="narrow the source's query",
        )


def _locations(label: object, href: str) -> list[str]:
    text = label if isinstance(label, str) and label else None
    if text is not None and text.lower() not in _VAGUE:
        return [text]
    slug = _PLACE_SLUG.match(href)
    return unique([text, slug.group(1).replace("-", " ").title() if slug else None])


def _date(text: object) -> datetime | None:
    try:
        return datetime.strptime(str(text), "%m/%d/%Y").replace(tzinfo=UTC)
    except ValueError:
        return None
