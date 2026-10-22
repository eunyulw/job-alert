"""Tesla's internships, relayed from a real browser through the repository's ``tesla-feed`` branch.

Tesla's careers API (``www.tesla.com/cua-api/apps/careers/state``) answers only a real browser:
Akamai refuses ordinary HTTP, a Playwright-driven browser (headless or not), and every GitHub
runner. The ``extension/tesla-relay`` Chrome extension therefore reads the board in the user's own
Chrome every 10 minutes and commits the internships to ``tesla.json`` on the orphan ``tesla-feed``
branch.
This adapter reads that file, so the monitor never contacts Tesla itself.

The feed is ``{"schema": 1, "generated_at": ISO-8601, "board_total": N, "internships": [{"id",
"title", "location", "department"}]}``, every listing Tesla types as intern (type 3), with its
lookup labels resolved. The extension commits only when the list changes, plus a heartbeat every
six hours, so a feed older than ``max_age_hours`` (default 24) means the browser stopped relaying:
that read fails, which opens one health incident, rather than passing an old board off as current.

Mapping: ``id`` is Tesla's requisition id and identity is ``tesla-relay:tesla:{id}``; ``location``
is the one location. Only postings ``location_country`` places in the US are kept: Tesla writes
``City, State`` in the US and ``City, Region`` elsewhere (``Grünheide (mark), Brandenburg``,
``Shanghai, Shanghai``), which the geo rules cannot place, so "not placed" means "not US" here.
The posting link is Tesla's public job page,
``https://www.tesla.com/careers/search/job/{title-slug}-{id}``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import json_document, make_job
from job_alert.adapters.common import optional_text
from job_alert.adapters.paged import UnusableRecord
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.geo import location_country
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "tesla-relay"
SCHEMA = 1
DEFAULT_MAX_AGE_HOURS = 24
PUBLIC_JOB_BASE = "https://www.tesla.com/careers/search/job"
_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TeslaRelayAdapter:
    """Normalize the relayed Tesla feed into US internships."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        max_age = _max_age(source)
        payload = await fetcher.get(
            RequestSpec(url=str(source.endpoint), headers=dict(source.headers), expected="json")
        )
        document = json_document(payload, PROVIDER)
        generated_at, records = _parse_feed(document)
        now = self._now()
        age = now - generated_at
        if age > max_age:
            raise AdapterError(
                f"the Tesla feed was last published {generated_at:%Y-%m-%d %H:%M} UTC,"
                f" {age.total_seconds() / 3600:.0f} hours ago: the Chrome relay has not run since"
            )
        jobs: list[NormalizedJob] = []
        warnings: list[str] = []
        seen: set[str] = set()
        for index, record in enumerate(records):
            try:
                job = _normalize(record, source=source, now=now)
            except UnusableRecord as exc:
                warnings.append(f"skipped malformed internship at index {index}: {exc}")
                continue
            if job is None:
                continue
            if job.source_job_id in seen:
                warnings.append(
                    f"skipped duplicate internship at index {index}: {job.source_job_id}"
                )
                continue
            seen.add(job.source_job_id)
            jobs.append(job)
        return CollectionResult(
            source_id=source.id,
            complete=not warnings,
            jobs=jobs,
            warnings=warnings,
            page_count=1,
            declared_total=len(jobs) if not warnings else None,
        )


def _max_age(source: SourceConfig) -> timedelta:
    value = source.parameters.get("max_age_hours", DEFAULT_MAX_AGE_HOURS)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 168:
        raise AdapterError(
            f"tesla-relay source {source.id} has an unusable max_age_hours: expected 1 to 168,"
            f" got {value!r}"
        )
    return timedelta(hours=value)


def _parse_feed(document: Any) -> tuple[datetime, list[Any]]:
    if not isinstance(document, Mapping) or document.get("schema") != SCHEMA:
        raise AdapterError(f"the Tesla feed is not a schema {SCHEMA} document")
    stamp = optional_text(document.get("generated_at"))
    try:
        generated_at = datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        generated_at = None
    if generated_at is None or generated_at.tzinfo is None:
        raise AdapterError("the Tesla feed has no timezone-aware generated_at")
    records = document.get("internships")
    if not isinstance(records, list) or not records:
        # Tesla always lists internships; an empty list is a broken relay, never an empty board.
        raise AdapterError("the Tesla feed lists no internships")
    return generated_at.astimezone(UTC), records


def _normalize(record: object, *, source: SourceConfig, now: datetime) -> NormalizedJob | None:
    """One US internship, or ``None`` for a posting not placed in the US."""
    if not isinstance(record, Mapping):
        raise UnusableRecord("record is not a JSON object")
    job_id = optional_text(record.get("id"))
    title = optional_text(record.get("title"))
    if job_id is None or not _ID.match(job_id):
        raise UnusableRecord("missing a usable id")
    if title is None:
        raise UnusableRecord("missing title")
    location = optional_text(record.get("location"))
    if location is None or location_country(location) != "us":
        return None
    title = " ".join(title.split())
    return make_job(
        provider=PROVIDER,
        tenant="tesla",
        source=source,
        job_id=job_id,
        title=title,
        posting_url=f"{PUBLIC_JOB_BASE}/{_slugify(title)}-{job_id}",
        first_seen_at=now,
        locations=[location],
        department=record.get("department"),
        employment_type="intern",
        workplace_type="remote" if "remote" in location.casefold() else None,
        requisition_id=job_id,
    )


def _slugify(value: str) -> str:
    return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", value.casefold()))
