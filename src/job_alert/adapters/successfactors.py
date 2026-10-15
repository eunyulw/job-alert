"""SAP SuccessFactors career site (Recruiting Marketing) adapter.

A SuccessFactors career site (``jobs.zf.com``, ``jobs.teradyne.com``, ``careers.keyence.com``)
publishes no JSON API, but its search page is plain server-rendered HTML:
``GET https://{site}/search/?q=&optionsFacetsDD_country=US&startrow={n}`` answers one page of
results as ``<tr class="data-row">`` rows and declares the size of the result set in its pagination
label, ``Results <b>1 – 25</b> of <b>528</b>``. The site's RSS feed (``/services/rss/job/``) is not
used: it returns at most 20 items whatever the query, so it can never be a complete collection.

A source is scoped through its endpoint's query, kept as configured (``q``, a country facet such as
``optionsFacetsDD_country=US``, a sort). The adapter owns ``startrow``. Page sizes differ between
sites (25 on most, 10 on some), so the next ``startrow`` advances by the rows actually read.

Mapping decisions:

- A row's posting link is its ``a.jobTitle-link``. The link's last numeric path segment
  (``/job/FarmingtonHills-Electrical-Engineering-Manager-MI/1415878533/``) is the posting id, and
  identity is ``successfactors:{site host}:{id}``. A row without that link or id is unusable.
- ``title`` is the link text; ``locations`` is the row's first ``span.jobLocation``;
  ``department`` is ``span.jobDepartment`` and ``team`` is ``span.jobFacility`` when published.
- ``posting_url`` and ``apply_url`` are the link resolved against the site origin.
- The search page publishes no description and no reliable date, so ``content_hash`` digests the
  title and locations and ``source_published_at`` stays ``None``.

A page with no pagination label and no rows is an error, not an empty board: an empty search still
renders the label with a total of 0, so a missing label means the page is not a search page.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from pydantic import HttpUrl, ValidationError

from job_alert.adapters.base import AdapterError
from job_alert.adapters.paged import UnusableRecord, walk_scoped
from job_alert.fetch import HttpFetcher, HttpPayload, RequestSpec
from job_alert.identity import build_dedupe_key, build_job_key, content_hash
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "successfactors"
SEARCH_PATH = "/search/"
SEARCH_CAP = 100_000
MAX_PAGES = 400
OWNED = frozenset({"startrow"})
_TOTAL = re.compile(r"Results\s*<b>[^<]*</b>\s*of\s*<b>\s*([\d,.\s]+?)\s*</b>", re.IGNORECASE)
_EMPTY = re.compile(r"There are currently no open|no jobs? (?:were )?found", re.IGNORECASE)
_POSTING_ID = re.compile(r"/(\d+)/?$")
_VOID = frozenset({"br", "img", "hr", "input", "meta", "link", "wbr", "source"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class Row:
    """The fields of one ``data-row``, as the page publishes them."""

    href: str | None = None
    title: str | None = None
    locations: list[str] = field(default_factory=list)
    department: str | None = None
    facility: str | None = None


class _RowParser(HTMLParser):
    """Collect each ``<tr class="data-row">``'s link and labelled spans."""

    _SPANS = {"jobLocation": "locations", "jobDepartment": "department", "jobFacility": "facility"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[Row] = []
        self._row: Row | None = None
        self._depth = 0
        self._capture: str | None = None
        self._buffer: list[str] = []
        self._capture_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = set((dict(attrs).get("class") or "").split())
        if tag == "tr":
            if self._row is None and "data-row" in classes:
                self._row, self._depth = Row(), 0
                return
            if self._row is not None:
                self._depth += 1
        if self._row is None:
            return
        if self._capture is not None:
            if tag in _VOID:
                self._buffer.append(" ")
            else:
                self._capture_depth += 1
            return
        if tag == "a" and "jobTitle-link" in classes and self._row.href is None:
            self._row.href = dict(attrs).get("href")
            self._start("title")
        elif tag == "span":
            for css, name in self._SPANS.items():
                if css in classes:
                    self._start(name)
                    break

    def handle_endtag(self, tag: str) -> None:
        if self._row is None:
            return
        if self._capture is not None:
            if self._capture_depth > 0:
                self._capture_depth -= 1
                return
            self._finish()
            return
        if tag == "tr":
            if self._depth == 0:
                self.rows.append(self._row)
                self._row = None
            else:
                self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._buffer.append(data)

    def _start(self, name: str) -> None:
        self._capture, self._buffer, self._capture_depth = name, [], 0

    def _finish(self) -> None:
        assert self._row is not None and self._capture is not None
        text = " ".join("".join(self._buffer).split()) or None
        name, self._capture = self._capture, None
        if text is None:
            return
        if name == "title":
            self._row.title = self._row.title or text
        elif name == "locations":
            if not self._row.locations:
                self._row.locations.append(text)
        elif name == "department":
            self._row.department = self._row.department or text
        elif name == "facility":
            self._row.facility = self._row.facility or text


def parse_search_page(body: str) -> tuple[list[Row], int]:
    """The rows of one search page and the total the page declares."""
    match = _TOTAL.search(body)
    parser = _RowParser()
    parser.feed(body)
    parser.close()
    if match is None:
        if not parser.rows and _EMPTY.search(body):
            return [], 0
        raise AdapterError("successfactors page declares no result total; it is not a search page")
    digits = re.sub(r"\D", "", match.group(1))
    if not digits:
        raise AdapterError("successfactors page declares an unreadable result total")
    return parser.rows, int(digits)


class SuccessFactorsAdapter:
    """Normalize one scoped SuccessFactors career-site search, one HTML page per request."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        parts = urlsplit(str(source.endpoint))
        if parts.scheme != "https" or parts.path != SEARCH_PATH:
            raise AdapterError(
                f"successfactors endpoint {source.endpoint} is not an https {SEARCH_PATH} URL"
            )
        query = [
            (name, value)
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
            if name not in OWNED
        ]
        host = parts.netloc.lower()
        origin = f"https://{parts.netloc}"
        first_seen_at = self._now()

        async def fetch_page(_: int, offset: int) -> tuple[Sequence[object], int]:
            page_query = urlencode([*query, ("startrow", str(offset))])
            payload = await fetcher.get(
                RequestSpec(
                    url=urlunsplit(("https", parts.netloc, parts.path, page_query, "")),
                    headers=dict(source.headers),
                    expected="html",
                )
            )
            return parse_search_page(_decode(payload))

        return await walk_scoped(
            source_id=source.id,
            fetch_page=fetch_page,
            normalize=lambda row: _normalize_job(
                row, source=source, host=host, origin=origin, first_seen_at=first_seen_at
            ),
            cap=SEARCH_CAP,
            max_pages=MAX_PAGES,
            cap_advice="narrow the source's query",
        )


def _decode(payload: HttpPayload) -> str:
    if payload.not_modified:
        raise AdapterError(f"successfactors returned 304 Not Modified for {payload.final_url}")
    return payload.body.decode("utf-8", errors="replace")


def _normalize_job(
    row: object, *, source: SourceConfig, host: str, origin: str, first_seen_at: datetime
) -> NormalizedJob:
    if not isinstance(row, Row):
        raise UnusableRecord("record is not a search row")
    if row.href is None:
        raise UnusableRecord("row has no posting link")
    posting_url = urljoin(f"{origin}/", html.unescape(row.href))
    link = urlsplit(posting_url)
    if link.scheme != "https" or link.netloc.lower() != host:
        raise UnusableRecord("posting link leaves the career site")
    posting_url = urlunsplit(("https", link.netloc, link.path, "", ""))
    match = _POSTING_ID.search(link.path)
    if match is None:
        raise UnusableRecord("posting link has no numeric id")
    job_id = match.group(1)
    if row.title is None:
        raise UnusableRecord("row has no title")
    title, locations = row.title, list(row.locations)
    try:
        return NormalizedJob(
            company_id=source.company_id,
            source_id=source.id,
            source_job_id=job_id,
            canonical_job_key=build_job_key(
                provider=PROVIDER,
                tenant=host,
                source_job_id=job_id,
                company_id=source.company_id,
                title=title,
                locations=locations,
                apply_url=posting_url,
            ),
            dedupe_key=build_dedupe_key(
                company_id=source.company_id,
                requisition_id=None,
                apply_url=posting_url,
                title=title,
                locations=locations,
                department=row.department,
            ),
            title=title,
            locations=locations,
            department=row.department,
            team=row.facility,
            content_hash=content_hash(title, locations, None),
            posting_url=HttpUrl(posting_url),
            apply_url=HttpUrl(posting_url),
            first_seen_at=first_seen_at,
        )
    except ValidationError as exc:
        raise UnusableRecord("row does not normalize into a job") from exc
