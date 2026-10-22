"""Paycor Recruiting (Newton) career page adapter.

``GET https://recruitingbypaycor.com/career/CareerHome.action?clientId={id}`` answers the company's
whole public board as one server-rendered page: postings are grouped under
``gnewtonCareerGroupHeaderClass`` department headers, one ``gnewtonCareerGroupRowClass`` row per
posting, holding the title link and the location.

Mapping: the link's ``id`` query value is the posting id and identity is ``paycor:{client}:{id}``;
the link text is the title; the row's ``gnewtonCareerGroupJobDescriptionClass`` text is the
location; the header above the row is the department. The posting link is
``https://recruitingbypaycor.com/career/JobIntroduction.action?clientId={client}&id={id}``.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import collect_document, html_text, make_job, utcnow
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "paycor"
ORIGIN = "https://recruitingbypaycor.com"
_CLIENT = re.compile(r"^[0-9a-f]{32}$")
_ID = re.compile(r"^[0-9a-f]{32}$")
_TOKEN = re.compile(
    r'<div class="(gnewtonCareerGroupHeaderClass|gnewtonCareerGroupRowClass)">(.*?)'
    r'(?=<div class="gnewtonCareerGroup(?:HeaderClass|RowClass)">|</body>)',
    re.DOTALL,
)
_LINK = re.compile(
    r'<a\b[^>]*href="([^"]*JobIntroduction\.action\?[^"]+)"[^>]*>(.*?)</a>', re.DOTALL
)
_LOCATION = re.compile(r'<div class="gnewtonCareerGroupJobDescriptionClass">(.*?)</div>', re.DOTALL)


def parse_board(body: str) -> list[dict[str, str | None]]:
    """The board's rows, each with its link, title, location and department header."""
    rows: list[dict[str, str | None]] = []
    department: str | None = None
    for kind, fragment in _TOKEN.findall(body):
        if kind == "gnewtonCareerGroupHeaderClass":
            department = html_text(fragment.split("</div>", 1)[0])
            continue
        link = _LINK.search(fragment)
        place = _LOCATION.search(fragment)
        rows.append(
            {
                "href": html.unescape(link.group(1)) if link else None,
                "title": html_text(link.group(2)) if link else None,
                "location": html_text(place.group(1)) if place else None,
                "department": department,
            }
        )
    return rows


class PaycorAdapter:
    """Normalize one Paycor career page."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        parts = urlsplit(str(source.endpoint))
        client = (parse_qs(parts.query).get("clientId") or [""])[0]
        if (
            f"{parts.scheme}://{parts.netloc}" != ORIGIN
            or parts.path != "/career/CareerHome.action"
            or not _CLIENT.match(client)
        ):
            raise AdapterError(
                f"paycor endpoint {source.endpoint} is not a CareerHome.action?clientId= URL"
            )
        payload = await fetcher.get(
            RequestSpec(url=str(source.endpoint), headers=dict(source.headers), expected="html")
        )
        body = payload.body.decode("utf-8", errors="replace")
        if "gnewtonCareer" not in body:
            raise AdapterError("paycor page is not a Newton career page")
        first_seen_at = self._now()

        def normalize(record: Mapping[str, Any]) -> NormalizedJob:
            href = record.get("href")
            query = parse_qs(urlsplit(href).query) if isinstance(href, str) else {}
            job_id = (query.get("id") or [""])[0]
            identifier = job_id if _ID.match(job_id) else None
            location = record.get("location")
            return make_job(
                provider=PROVIDER,
                tenant=client,
                source=source,
                job_id=identifier,
                title=record.get("title"),
                posting_url=(
                    f"{ORIGIN}/career/JobIntroduction.action?"
                    + urlencode({"clientId": client, "id": identifier})
                    if identifier
                    else None
                ),
                first_seen_at=first_seen_at,
                locations=[location] if isinstance(location, str) else [],
                department=record.get("department"),
            )

        return collect_document(source=source, records=parse_board(body), normalize=normalize)
