"""Avature career site adapter (``jobs.siemens.com``, ``jobs.intralox.com``).

An Avature career portal publishes no JSON API, but its ``SearchJobs`` page is server-rendered
HTML, one page per request. The source's endpoint is that page with its filters in the query
(``/en_US/externaljobs/SearchJobs/?42386=[812209]&42390=[102154]&listFilterMode=1`` is Siemens'
United States student search); the adapter keeps the query and owns the offset.

Avature sites come in two templates, and both are read:

- the article template (Siemens): each result is an ``<article class="article--result">`` whose
  subtitle is ``City , State , Country • Job ID: 526477 • Category``; the page declares ``1 - 6 of
  139 results`` and pages with ``folderOffset``;
- the list template (Intralox): each result is an ``<li class="listSingleColumnItem">`` whose first
  ``listSingleColumnItemMiscDataItem`` is the location; the page declares no total and pages with
  ``jobOffset``.

The offset parameter is whichever of the two the page's own pagination links use. Page sizes are
the site's own (6 on Siemens, 20 on Intralox: the size parameter is ignored), so the offset advances
by the rows read. With a declared total the walk is measured against it; a total written ``999+`` is
Avature's cap and cannot be read whole. Without one the walk ends at the first empty page.

Mapping: the link's trailing ``/JobDetail/[slug/]{id}`` number is the id and identity is
``avature:{host}:{id}``; the link text is the title; the posting link is the link itself.
"""

from __future__ import annotations

import html
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import collect_document, html_text, make_job, utcnow
from job_alert.adapters.paged import UnusableRecord, walk_scoped
from job_alert.fetch import HttpFetcher, HttpPayload, RequestSpec
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "avature"
SEARCH_CAP = 999
MAX_PAGES = 300
OFFSETS = ("folderOffset", "jobOffset")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)"
        " Chrome/141.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}
_ARTICLE = re.compile(r'<article class="article article--result.*?</article>', re.DOTALL)
_ITEM = re.compile(r'<li class="listSingleColumnItem">.*?</li>', re.DOTALL)
_LINK = re.compile(
    r'<a\b[^>]*href="(https://[^"]*/JobDetail/(?:[^"/]+/)?(\d+))"[^>]*>(.*?)</a>', re.DOTALL
)
_TOTAL = re.compile(r"of\s+([\d,]+)(\+?)\s+results", re.IGNORECASE)
_SUBTITLE = re.compile(r'<div class="article__header__text__subtitle[^"]*">(.*?)</div>', re.DOTALL)
_MISC = re.compile(r'<span class="listSingleColumnItemMiscDataItem">(.*?)</span>', re.DOTALL)
_OFFSET_LINK = re.compile(r"[?&;](folderOffset|jobOffset)=\d+")


@dataclass(frozen=True, slots=True)
class Result:
    """One search result as the page renders it."""

    url: str
    job_id: str
    title: str | None
    location: str | None


def parse_search_page(body: str) -> tuple[list[Result], int | None, bool, str | None]:
    """The results, the declared total, whether the total is capped, and the offset parameter."""
    results = [_article(block) for block in _ARTICLE.findall(body)]
    results += [_item(block) for block in _ITEM.findall(body)]
    total = _TOTAL.search(body)
    offset = _OFFSET_LINK.search(body)
    declared = int(total.group(1).replace(",", "")) if total else None
    return (
        [result for result in results if result is not None],
        declared,
        bool(total and total.group(2)),
        offset.group(1) if offset else None,
    )


def _link(block: str) -> tuple[str, str, str | None] | None:
    match = _LINK.search(block)
    if match is None:
        return None
    return html.unescape(match.group(1)), match.group(2), html_text(match.group(3))


def _article(block: str) -> Result | None:
    link = _link(block)
    if link is None:
        return None
    subtitle = _SUBTITLE.search(block)
    text = html_text(subtitle.group(1)) if subtitle else None
    place = text.split("•", 1)[0] if text else None
    location = (
        ", ".join(part.strip() for part in place.split(",") if part.strip()) if place else None
    )
    return Result(url=link[0], job_id=link[1], title=link[2], location=location or None)


def _item(block: str) -> Result | None:
    link = _link(block)
    if link is None:
        return None
    misc = _MISC.search(block)
    location = html_text(misc.group(1)) if misc else None
    return Result(
        url=link[0],
        job_id=link[1],
        title=link[2],
        location=location.rstrip(". ") if location else None,
    )


class AvatureAdapter:
    """Normalize one Avature ``SearchJobs`` search, one HTML page per request."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        parts = urlsplit(str(source.endpoint))
        if parts.scheme != "https" or not parts.path.rstrip("/").endswith("/SearchJobs"):
            raise AdapterError(f"avature endpoint {source.endpoint} is not an https SearchJobs URL")
        host = parts.netloc.lower()
        query = [
            (name, value)
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
            if name not in OFFSETS
        ]
        first_seen_at = self._now()

        async def read(
            offset_name: str | None, offset: int
        ) -> tuple[list[Result], int | None, bool, str | None]:
            page_query = [*query, (offset_name, str(offset))] if offset_name and offset else query
            payload = await fetcher.get(
                RequestSpec(
                    url=urlunsplit(("https", parts.netloc, parts.path, urlencode(page_query), "")),
                    headers={**HEADERS, **source.headers},
                    expected="html",
                )
            )
            return parse_search_page(_decode(payload))

        def normalize(record: object) -> NormalizedJob:
            if not isinstance(record, Result):
                raise UnusableRecord("record is not a search result")
            if urlsplit(record.url).netloc.lower() != host:
                raise UnusableRecord("posting link leaves the career site")
            return make_job(
                provider=PROVIDER,
                tenant=host,
                source=source,
                job_id=record.job_id,
                title=record.title,
                posting_url=record.url,
                first_seen_at=first_seen_at,
                locations=[record.location] if record.location else [],
            )

        first, declared, capped, offset_name = await read(None, 0)
        if declared is None:
            return await _walk_to_empty(source, first, offset_name, read, normalize)

        reads = 0

        async def fetch_page(_: int, offset: int) -> tuple[Sequence[object], int | None]:
            nonlocal reads
            reads += 1
            if offset == 0 and reads == 1:
                return first, SEARCH_CAP if capped else declared
            if offset and offset_name is None:
                raise AdapterError("avature search page links no next page to walk")
            results, total, more, _name = await read(offset_name, offset)
            return results, SEARCH_CAP if more else total

        return await walk_scoped(
            source_id=source.id,
            fetch_page=fetch_page,
            normalize=normalize,
            cap=SEARCH_CAP,
            max_pages=MAX_PAGES,
            cap_advice="narrow the source's filters",
        )


async def _walk_to_empty(
    source: SourceConfig,
    first: list[Result],
    offset_name: str | None,
    read: Callable[[str | None, int], Awaitable[tuple[list[Result], int | None, bool, str | None]]],
    normalize: Callable[[object], NormalizedJob],
) -> CollectionResult:
    """A search with no declared total, read page by page until a page comes back empty."""
    records: list[object] = []
    page, pages = first, 1
    while page:
        records.extend({"result": result} for result in page)
        if offset_name is None:
            break
        if pages >= MAX_PAGES:
            raise AdapterError(f"avature search did not end within {MAX_PAGES} pages")
        page, _total, _capped, _name = await read(offset_name, len(records))
        pages += 1
    if offset_name is None and len(first) >= 20:
        raise AdapterError("avature search filled a page but links no next page to walk")
    result = collect_document(source=source, records=records, normalize=_by_result(normalize))
    return result.model_copy(update={"page_count": pages})


def _by_result(
    normalize: Callable[[object], NormalizedJob],
) -> Callable[[Mapping[str, Any]], NormalizedJob]:
    # collect_document hands over mappings; a Result is wrapped so the same normalizer reads it.
    return lambda wrapped: normalize(wrapped["result"])


def _decode(payload: HttpPayload) -> str:
    if payload.not_modified:
        raise AdapterError(f"avature returned 304 Not Modified for {payload.final_url}")
    return payload.body.decode("utf-8", errors="replace")
