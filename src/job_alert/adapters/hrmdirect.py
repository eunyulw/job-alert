"""HRM Direct career site adapter.

``GET https://{tenant}.hrmdirect.com/employment/job-openings.php?search=true`` is the site's own
"all openings" search. It server-renders the whole board as one table, with no pagination: a
header row of ``td.reqhead`` labels, then one ``tr.ReqRowClick`` per opening, whose cells are
classed by column (``posTitle``, ``city``, ``state``, ``offices``, ``departments``, ...). The page
declares no count, so the collection is complete when every row parses.

A requisition open in several places is listed once per place, each row linking
``job-opening.php?req={req}&req_loc={loc}``. Those rows are merged into one posting keyed by
``req``, their locations combined, so one opening alerts once.

Mapping: ``req`` is the id and identity is ``hrmdirect:{tenant}:{req}``; the ``posTitle`` link text
the title; ``city`` (or ``offices``) and ``state`` the location. HRM Direct is a US product whose
``State`` column holds US states, so a location whose state is a US state code or name is given
the country ``US``, which lets the US-only filters read it. ``departments`` (or the first
``custSort`` column) is the department. The posting link is the first row's ``job-opening.php``
page. The table carries no description and no date.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import collect_document, html_text, joined, make_job, utcnow
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.geo import US_STATE_CODES, US_STATE_NAMES
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "hrmdirect"
_ENDPOINT = re.compile(
    r"^https://([a-z0-9-]+)\.hrmdirect\.com/employment/job-openings\.php\?search=true$"
)
_ROW = re.compile(r"<tr\b[^>]*\bReqRowClick\b[^>]*>(.*?)</tr>", re.DOTALL)
_CELL = re.compile(r'<td\b[^>]*\bclass="([a-zA-Z0-9_]+)[^"]*"[^>]*>(.*?)</td>', re.DOTALL)
_LINK = re.compile(r'href="(job-opening\.php\?req=(\d+)&(?:amp;)?req_loc=(\d+))')
_US_STATES = {name.lower() for name in US_STATE_NAMES} | set(US_STATE_CODES)


class HrmDirectAdapter:
    """Normalize one HRM Direct board, read from its single all-openings page."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        match = _ENDPOINT.match(str(source.endpoint))
        if match is None:
            raise AdapterError(
                f"hrmdirect endpoint {source.endpoint} is not a job-openings.php?search=true URL"
            )
        tenant = match.group(1)
        payload = await fetcher.get(
            RequestSpec(url=str(source.endpoint), headers=dict(source.headers), expected="html")
        )
        if payload.not_modified:
            raise AdapterError(f"hrmdirect returned 304 Not Modified for {payload.final_url}")
        body = payload.body.decode("utf-8", errors="replace")
        if "reqhead" not in body:
            raise AdapterError(f"hrmdirect page for {tenant} has no openings table")
        merged: dict[str, dict[str, Any]] = {}
        order: list[object] = []
        for index, row in enumerate(parse_rows(body)):
            req = row.get("req")
            if req is None:
                order.append(f"row {index}")
            elif req in merged:
                merged[req]["places"].append(row["place"])
            else:
                merged[req] = {**row, "places": [row["place"]]}
                order.append(merged[req])
        first_seen_at = self._now()
        base = f"https://{tenant}.hrmdirect.com/employment/"

        def normalize(record: Mapping[str, Any]) -> NormalizedJob:
            places = [place for place in record["places"] if place]
            return make_job(
                provider=PROVIDER,
                tenant=tenant,
                source=source,
                job_id=record.get("req"),
                title=record.get("title"),
                posting_url=base + record["href"] if record.get("href") else None,
                first_seen_at=first_seen_at,
                locations=list(dict.fromkeys(places)),
                department=record.get("department"),
            )

        return collect_document(source=source, records=order, normalize=normalize)


def parse_rows(body: str) -> list[dict[str, Any]]:
    """Each opening row's link, title, location and department."""
    rows = []
    for row in _ROW.findall(body):
        cells = {name: value for name, value in _CELL.findall(row)}
        link = _LINK.search(cells.get("posTitle", ""))
        state = html_text(cells.get("state"))
        city = html_text(cells.get("city")) or html_text(cells.get("offices"))
        country = "US" if state is not None and _is_us_state(state) else None
        department = html_text(cells.get("departments")) or html_text(cells.get("custSort1"))
        rows.append(
            {
                "req": link.group(2) if link else None,
                "href": html.unescape(link.group(1)) if link else None,
                "title": html_text(cells.get("posTitle")),
                "place": joined(city, state, country),
                "department": department,
            }
        )
    return rows


def _is_us_state(text: str) -> bool:
    return text.upper() in _US_STATES or text.lower() in _US_STATES
