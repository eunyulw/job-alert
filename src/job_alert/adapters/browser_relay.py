"""Boards relayed from the user's own Chrome through the repository's ``tesla-feed`` branch.

Some career sites answer only a real browser: Parker Hannifin's portal sits behind a Cloudflare
challenge that refuses ordinary HTTP and every GitHub runner, while the user's Chrome, which has
passed it, reads the portal normally. The ``extension/tesla-relay`` Chrome extension (named for its
first board) reads each such board in a background tab every 10 minutes and commits it to
``{board}.json`` on the orphan ``tesla-feed`` branch, beside Tesla's own feed. This adapter reads
one of those files, so the monitor never contacts the career site itself. It solves no challenge:
a challenge the browser has not passed fails the extension's read, and the feed goes stale.

The feed is ``{"schema": 1, "board": name, "generated_at": ISO-8601, "declared_total": N, "jobs":
[{"id", "title", "location", "url", "posted"}]}``. ``declared_total`` is the count the site's own
search declared, and the extension publishes only a walk that reached it, so a feed whose job list
misses it is a broken relay and fails the read. The extension commits only when the list changes,
plus a heartbeat every six hours, so a feed older than ``max_age_hours`` (default 24) means the
browser stopped relaying: that read fails, which opens one health incident, rather than passing an
old board off as current.

Parameters: ``board`` (the feed's name, which must match the file's) and ``site`` (the host every
posting link must be on). Mapping: ``id`` is the site's posting id and identity is
``browser-relay:{board}:{id}``; ``location`` is the one location; ``posted`` (ISO 8601) the
publication date; ``url`` the posting link.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import collect_document, json_document, make_job, parse_date
from job_alert.adapters.common import optional_text
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "browser-relay"
SCHEMA = 1
DEFAULT_MAX_AGE_HOURS = 24
FEED_BASE = "https://raw.githubusercontent.com/eunyulw/job-alert/tesla-feed/"
_BOARD = re.compile(r"^[a-z0-9-]+$")
_HOST = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")
_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class BrowserRelayAdapter:
    """Normalize one relayed board feed."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        board, site, max_age = _parameters(source)
        if str(source.endpoint) != f"{FEED_BASE}{board}.json":
            raise AdapterError(f"browser-relay endpoint {source.endpoint} is not {board}.json")
        payload = await fetcher.get(
            RequestSpec(url=str(source.endpoint), headers=dict(source.headers), expected="json")
        )
        document = json_document(payload, PROVIDER)
        generated_at, declared, records = _parse_feed(document, board)
        now = self._now()
        age = now - generated_at
        if age > max_age:
            raise AdapterError(
                f"the {board} feed was last published {generated_at:%Y-%m-%d %H:%M} UTC,"
                f" {age.total_seconds() / 3600:.0f} hours ago: the Chrome relay has not run since"
            )

        def normalize(record: Mapping[str, Any]) -> NormalizedJob:
            job_id = optional_text(record.get("id"))
            url = optional_text(record.get("url"))
            if url is not None and urlsplit(url).netloc.lower() != site:
                url = None
            location = optional_text(record.get("location"))
            return make_job(
                provider=PROVIDER,
                tenant=board,
                source=source,
                job_id=job_id if job_id and _ID.match(job_id) else None,
                title=record.get("title"),
                posting_url=url,
                first_seen_at=now,
                locations=[location] if location else [],
                published_at=parse_date(record.get("posted")),
            )

        return collect_document(
            source=source, records=records, normalize=normalize, declared=declared
        )


def _parameters(source: SourceConfig) -> tuple[str, str, timedelta]:
    board, site = source.parameters.get("board"), source.parameters.get("site")
    if not isinstance(board, str) or not _BOARD.match(board):
        raise AdapterError(f"browser-relay source {source.id} needs a board name")
    if not isinstance(site, str) or not _HOST.match(site):
        raise AdapterError(f"browser-relay source {source.id} needs the site host")
    hours = source.parameters.get("max_age_hours", DEFAULT_MAX_AGE_HOURS)
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 168:
        raise AdapterError(
            f"browser-relay source {source.id} has an unusable max_age_hours: expected 1 to 168,"
            f" got {hours!r}"
        )
    return board, site, timedelta(hours=hours)


def _parse_feed(document: Any, board: str) -> tuple[datetime, int, list[Any]]:
    if not isinstance(document, Mapping) or document.get("schema") != SCHEMA:
        raise AdapterError(f"the {board} feed is not a schema {SCHEMA} document")
    if document.get("board") != board:
        raise AdapterError(f"the {board} feed names another board: {document.get('board')!r}")
    stamp = optional_text(document.get("generated_at"))
    try:
        generated_at = datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        generated_at = None
    if generated_at is None or generated_at.tzinfo is None:
        raise AdapterError(f"the {board} feed has no timezone-aware generated_at")
    declared = document.get("declared_total")
    if isinstance(declared, bool) or not isinstance(declared, int) or declared < 0:
        raise AdapterError(f"the {board} feed has no non-negative declared_total")
    records = document.get("jobs")
    if not isinstance(records, list):
        raise AdapterError(f"the {board} feed has no jobs list")
    return generated_at.astimezone(UTC), declared, records
