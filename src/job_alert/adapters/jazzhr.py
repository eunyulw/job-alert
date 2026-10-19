"""JazzHR career page adapter.

``GET https://{company}.applytojob.com/apply`` server-renders the whole board as one list, with no
pagination: each opening is an ``li.list-group-item`` holding an ``h3`` link to
``/apply/{code}/{slug}`` and a ``fa-map-marker`` item with its location. The page declares no
count, so the collection is complete when every item parses; a page without the job list is an
error, not an empty board (an empty board renders the list container with no items).

Mapping: the link's ``{code}`` is the id and identity is ``jazzhr:{company}:{code}``; the link text
the title; the map-marker item the location; a ``fa-sitemap`` item the department. The posting
link is the item's link. The list carries no description and no date.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import collect_document, html_text, make_job, unique, utcnow
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "jazzhr"
_ENDPOINT = re.compile(r"^https://([a-z0-9-]+)\.applytojob\.com/apply/?$")
_ITEM = re.compile(r'<li class="list-group-item">(.*?)</ul>', re.DOTALL)
_LINK = re.compile(
    r'<a\s+href="(https://[a-z0-9-]+\.applytojob\.com/apply/([A-Za-z0-9]+)/[^"]*)"\s*>(.*?)</a>',
    re.DOTALL,
)
_ICON_ITEM = re.compile(r"<li><i class='fa fa-([a-z-]+)'></i>(.*?)</li>", re.DOTALL)


class JazzHrAdapter:
    """Normalize one JazzHR board, read from its single listing page."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        match = _ENDPOINT.match(str(source.endpoint))
        if match is None:
            raise AdapterError(
                f"jazzhr endpoint {source.endpoint} is not an applytojob.com/apply URL"
            )
        company = match.group(1)
        payload = await fetcher.get(
            RequestSpec(url=str(source.endpoint), headers=dict(source.headers), expected="html")
        )
        if payload.not_modified:
            raise AdapterError(f"jazzhr returned 304 Not Modified for {payload.final_url}")
        body = payload.body.decode("utf-8", errors="replace")
        if "list-group" not in body:
            raise AdapterError(f"jazzhr page for {company} has no job list")
        first_seen_at = self._now()
        on_site = f"https://{company}.applytojob.com/apply/"

        def normalize(record: Mapping[str, Any]) -> NormalizedJob:
            url = record.get("href")
            return make_job(
                provider=PROVIDER,
                tenant=company,
                source=source,
                job_id=record.get("code"),
                title=record.get("title"),
                posting_url=url if isinstance(url, str) and url.startswith(on_site) else None,
                first_seen_at=first_seen_at,
                locations=unique([record.get("location")]),
                department=record.get("department"),
            )

        return collect_document(source=source, records=parse_items(body), normalize=normalize)


def parse_items(body: str) -> list[dict[str, Any]]:
    """Each listed opening's link, code, title, location and department."""
    items = []
    for item in _ITEM.findall(body):
        link = _LINK.search(item)
        icons = {name: html_text(value) for name, value in _ICON_ITEM.findall(item)}
        items.append(
            {
                "href": html.unescape(link.group(1)) if link else None,
                "code": link.group(2) if link else None,
                "title": html_text(link.group(3)) if link else None,
                "location": icons.get("map-marker"),
                "department": icons.get("sitemap"),
            }
        )
    return items
