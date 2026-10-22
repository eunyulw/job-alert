"""Teamtailor career site adapter.

``GET https://{company}.teamtailor.com/jobs.rss`` answers every published posting as an RSS item,
with Teamtailor's ``tt:`` extension for locations and department. The ``/jobs`` page itself is
built from the same list; the feed is read because it is one stable document.

Mapping: the posting number at the start of the link's last segment (``/jobs/8447974-robot-
technician``) is the id and identity is ``teamtailor:{company}:{id}``; ``title`` is the title and
``link`` (which must be on the company's ``teamtailor.com`` host) the posting link; each
``tt:location`` reads as city, country; ``tt:department`` is the department; ``remoteStatus``
(``onsite``, ``hybrid``, ``remote``, ``fully``) the workplace type; ``pubDate`` the publication
date.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import collect_document, joined, make_job, unique, utcnow
from job_alert.adapters.common import optional_text
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "teamtailor"
TT = "{https://teamtailor.com/locations}"
_ENDPOINT = re.compile(r"^https://([a-z0-9-]+)\.teamtailor\.com/jobs\.rss$")
_JOB_ID = re.compile(r"/jobs/(\d+)(?:-[^/]*)?/?$")
_WORKPLACES = {"onsite": "onsite", "hybrid": "hybrid", "remote": "remote", "fully": "remote"}


def parse_feed(body: bytes) -> list[dict[str, Any]]:
    """The feed's items as plain records."""
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise AdapterError(f"teamtailor feed is not XML: {exc}") from exc
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise AdapterError("teamtailor feed is not an RSS channel")
    items = []
    for item in channel.findall("item"):
        locations = [
            joined(place.findtext(f"{TT}city"), place.findtext(f"{TT}country"))
            or optional_text(place.findtext(f"{TT}name"))
            for place in item.iter(f"{TT}location")
        ]
        items.append(
            {
                "title": item.findtext("title"),
                "link": item.findtext("link"),
                "published": item.findtext("pubDate"),
                "remote": item.findtext("remoteStatus"),
                "department": item.findtext(f"{TT}department"),
                "locations": unique(locations),
            }
        )
    return items


def _published(value: object) -> datetime | None:
    text = optional_text(value)
    if text is None:
        return None
    try:
        return parsedate_to_datetime(text).astimezone(UTC)
    except (TypeError, ValueError):
        return None


class TeamtailorAdapter:
    """Normalize one Teamtailor jobs feed."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        match = _ENDPOINT.match(str(source.endpoint))
        if match is None:
            raise AdapterError(f"teamtailor endpoint {source.endpoint} is not a jobs.rss URL")
        company = match.group(1)
        payload = await fetcher.get(
            RequestSpec(url=str(source.endpoint), headers=dict(source.headers), expected="bytes")
        )
        records = parse_feed(payload.body)
        first_seen_at = self._now()

        def normalize(record: Mapping[str, Any]) -> NormalizedJob:
            link = optional_text(record.get("link"))
            if link is not None and not link.startswith(f"https://{company}.teamtailor.com/"):
                link = None
            number = _JOB_ID.search(link) if link else None
            return make_job(
                provider=PROVIDER,
                tenant=company,
                source=source,
                job_id=number.group(1) if number else None,
                title=record.get("title"),
                posting_url=link,
                first_seen_at=first_seen_at,
                locations=record.get("locations") or [],
                department=record.get("department"),
                workplace_type=_WORKPLACES.get((optional_text(record.get("remote")) or "").lower()),
                published_at=_published(record.get("published")),
            )

        return collect_document(source=source, records=records, normalize=normalize)
