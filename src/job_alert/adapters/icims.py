"""iCIMS career site adapter.

A classic iCIMS career site (``careers-{company}.icims.com``) has no JSON API, but its search frame
is server-rendered HTML: ``GET https://{site}/jobs/search?ss=1&in_iframe=1&pr={n}`` answers page
``n`` (from 0) as ``<li class="iCIMS_JobCardItem">`` cards, 20 to a page, with a ``Page 1 of N``
label. The search declares no posting count, so completeness comes from the site's
``/sitemap.xml``, which lists every open posting's ``/jobs/{id}/`` page (following a sitemap index
one level). The collection is complete only when the ids the search pages list equal the ids the
sitemap lists. A board can change between the two reads, so a mismatch earns one more attempt; a
second mismatch is an incomplete collection. Because the sitemap always lists the whole board, the
endpoint must be the unscoped search (``?ss=1``).

Mapping, per card: the ``a.iCIMS_Anchor`` link's ``/jobs/{id}/`` segment is the id and identity is
``icims:{site}:{id}``; the card's ``h3`` the title; the ``Job Locations`` value, split on ``|``, the
locations, each ``US-CA-Marina`` label read as ``Marina, CA, US``; a ``Category`` field the
department; the ``Posted Date`` (``10/15/2026 11:49 AM``) the publication date. The posting link is
the card's link without its ``in_iframe`` query. Cards carry no description, so none is fetched.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import dashed_location, html_text, make_job, unique, utcnow
from job_alert.adapters.paged import UnusableRecord
from job_alert.fetch import HttpFetcher, HttpPayload, RequestSpec
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "icims"
MAX_PAGES = 200
MAX_ATTEMPTS = 2
_CARD = re.compile(r'<li class="iCIMS_JobCardItem')
_LINK = re.compile(r'<a\b[^>]*\bhref="(https://[^"]*/jobs/(\d+)/[^"]*)"[^>]*\biCIMS_Anchor')
_TITLE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.DOTALL)
_LABEL = re.compile(
    r'field-label">\s*([^<]+?)\s*</span>\s*<span([^>]*)>(.*?)</span>\s*</div>', re.DOTALL
)
_FIELD = re.compile(
    r'<dt class="iCIMS_JobHeaderField">(.*?)</dt>\s*<dd class="iCIMS_JobHeaderData">(.*?)</dd>',
    re.DOTALL,
)
_PAGES = re.compile(r"Page\s+\d+\s+of\s+(\d+)")
_SITEMAP_JOB = re.compile(r"<loc>\s*https://[^<]*/jobs/(\d+)/[^<]*</loc>")
_SITEMAP_INDEX = re.compile(r"<sitemap>\s*<loc>\s*(https://[^<\s]+)\s*</loc>")
_POSTED = re.compile(r'title="(\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2} [AP]M)"')


@dataclass(slots=True)
class Card:
    """One search card, as the page publishes it."""

    href: str | None = None
    job_id: str | None = None
    title: str | None = None
    locations: list[str] = field(default_factory=list)
    category: str | None = None
    posted: datetime | None = None


def parse_search_page(body: str) -> tuple[list[Card], int]:
    """The cards of one search page and the page count its paginator declares."""
    chunks = _CARD.split(body)[1:]
    cards = [_parse_card(chunk) for chunk in chunks]
    pages = _PAGES.search(body)
    if pages is None and not cards and "iCIMS_JobsTable" not in body:
        raise AdapterError("icims page has no job list; it is not a search page")
    return cards, int(pages.group(1)) if pages else 1


def _parse_card(chunk: str) -> Card:
    card = Card()
    link = _LINK.search(chunk)
    if link is not None:
        card.href, card.job_id = html.unescape(link.group(1)), link.group(2)
    title = _TITLE.search(chunk)
    card.title = html_text(title.group(1)) if title else None
    for label, attrs, value in _LABEL.findall(chunk):
        if label == "Job Locations":
            text = html_text(value) or ""
            card.locations = unique(dashed_location(part) for part in text.split("|"))
        elif label == "Posted Date":
            posted = _POSTED.search(attrs)
            if posted is not None:
                moment = datetime.strptime(posted.group(1), "%m/%d/%Y %I:%M %p")
                card.posted = moment.replace(tzinfo=UTC)
    for name, value in _FIELD.findall(chunk):
        if html_text(name) == "Category":
            card.category = html_text(value)
    return card


class IcimsAdapter:
    """Normalize one iCIMS career site: its search pages, checked against its sitemap."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        parts = urlsplit(str(source.endpoint))
        query = parse_qsl(parts.query, keep_blank_values=True)
        if parts.scheme != "https" or parts.path != "/jobs/search" or query != [("ss", "1")]:
            raise AdapterError(
                f"icims endpoint {source.endpoint} is not the unscoped https /jobs/search?ss=1"
            )
        site = parts.netloc.lower()
        first_seen_at = self._now()
        pages = 0
        for _ in range(MAX_ATTEMPTS):
            expected = await self._sitemap_ids(source, fetcher, site)
            jobs: dict[str, NormalizedJob] = {}
            warnings: list[str] = []
            page, declared_pages = 0, 1
            while page < declared_pages:
                payload = await fetcher.get(
                    RequestSpec(
                        url=f"https://{site}/jobs/search?ss=1&in_iframe=1&pr={page}",
                        headers=dict(source.headers),
                        expected="html",
                    )
                )
                cards, count = parse_search_page(_decode(payload))
                pages += 1
                if page == 0:
                    declared_pages = min(count, MAX_PAGES)
                for index, card in enumerate(cards):
                    try:
                        job = _normalize(card, source=source, site=site, now=first_seen_at)
                    except UnusableRecord as exc:
                        warnings.append(f"skipped malformed card {index} on page {page}: {exc}")
                        continue
                    jobs.setdefault(job.source_job_id, job)
                page += 1
            listed = set(jobs)
            if not warnings and listed == expected:
                return CollectionResult(
                    source_id=source.id,
                    complete=True,
                    jobs=list(jobs.values()),
                    page_count=pages,
                    declared_total=len(expected),
                )
            if warnings:
                break
        missing, extra = len(expected - listed), len(listed - expected)
        warnings.append(
            f"the search listed {len(listed)} postings and the sitemap {len(expected)}"
            f" ({missing} only in the sitemap, {extra} only in the search),"
            " so the collection is incomplete"
        )
        return CollectionResult(
            source_id=source.id,
            complete=False,
            jobs=list(jobs.values()),
            warnings=warnings,
            page_count=pages,
            declared_total=len(expected),
        )

    async def _sitemap_ids(
        self, source: SourceConfig, fetcher: HttpFetcher, site: str
    ) -> set[str]:
        body = await self._xml(source, fetcher, f"https://{site}/sitemap.xml")
        ids = set(_SITEMAP_JOB.findall(body))
        for nested in _SITEMAP_INDEX.findall(body):
            if urlsplit(nested).netloc.lower() != site:
                raise AdapterError(f"icims sitemap index points off the site: {nested}")
            ids |= set(_SITEMAP_JOB.findall(await self._xml(source, fetcher, nested)))
        if "<urlset" not in body and "<sitemapindex" not in body:
            raise AdapterError(f"icims sitemap for {site} is not a sitemap document")
        return ids

    async def _xml(self, source: SourceConfig, fetcher: HttpFetcher, url: str) -> str:
        payload = await fetcher.get(
            RequestSpec(url=url, headers=dict(source.headers), expected="bytes")
        )
        return _decode(payload)


def _decode(payload: HttpPayload) -> str:
    if payload.not_modified:
        raise AdapterError(f"icims returned 304 Not Modified for {payload.final_url}")
    return payload.body.decode("utf-8", errors="replace")


def _normalize(card: Card, *, source: SourceConfig, site: str, now: datetime) -> NormalizedJob:
    url = None
    if card.href is not None:
        link = urlsplit(card.href)
        if link.netloc.lower() == site:
            url = urlunsplit(("https", link.netloc, link.path, "", ""))
    return make_job(
        provider=PROVIDER,
        tenant=site,
        source=source,
        job_id=card.job_id,
        title=card.title,
        posting_url=url,
        first_seen_at=now,
        locations=card.locations,
        department=card.category,
        published_at=card.posted,
    )
