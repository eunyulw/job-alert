"""Harness for one-source collection-cycle integration tests.

The harness wires a real :class:`~job_alert.service.JobAlertService` to a real SQLite repository, a
real ``HttpFetcher``, and a fake adapter whose whole board can be swapped between cycles. The
adapter issues one genuine ``GET`` through the service's fetcher, so the mocked endpoint exercises
the service's conditional-request handling, its challenge classification, and its validator policy
for real; only the record-to-job mapping belongs to the test. Notifications go to a notifier that
records every message and answers with queued results until the queue runs out.

Nothing here reaches the network: ``respx`` owns the endpoint and refuses any other request.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import respx

from job_alert.adapters.base import AdapterError
from job_alert.adapters.common import html_to_text
from job_alert.adapters.registry import AdapterRegistry
from job_alert.config import FilterConfig
from job_alert.db import Repository
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.identity import build_dedupe_key, build_job_key, content_hash
from job_alert.models import (
    CollectionResult,
    CompanyTarget,
    HttpUrl,
    NormalizedJob,
    SourceClass,
    SourceConfig,
    SupportStatus,
)
from job_alert.notifications.base import DeliveryResult, NotificationMessage
from job_alert.service import JobAlertService

#: One fixed instant for every cycle unless a test moves the clock, so detection times, event ids,
#: and deferrals are all deterministic.
FIXED_NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
SOURCE_ID = "acme-fixture"
ENDPOINT = "https://api.acme-robotics.com/v1/postings"


def _record(
    index: int, *, requisition: str | None = None, title: str | None = None
) -> dict[str, object]:
    """One provider record, shaped like the boards the real adapters read."""
    return {
        "id": 1000 + index,
        "title": title if title is not None else f"Robotics Software Engineer {index}",
        "absolute_url": f"https://jobs.acme-robotics.com/postings/{1000 + index}",
        "requisition_id": requisition if requisition is not None else f"req-{index}",
        "location": {"name": "Palo Alto, CA"},
        "content": f"<p>Build robot arms and legs ({index}).</p>",
    }


def _board(count: int) -> tuple[dict[str, object], ...]:
    return tuple(_record(index) for index in range(1, count + 1))


@dataclass(frozen=True)
class Fixture:
    """One board the adapter can serve, and how the mocked endpoint answers for it."""

    records: tuple[dict[str, object], ...]
    etag: str
    complete: bool = True
    warnings: tuple[str, ...] = ()
    #: ``json`` serves the board, ``challenge`` answers with a bot-check page, and ``not-modified``
    #: answers 304 to every request however it is asked, validators or not.
    response: str = "json"


FIXTURES: dict[str, Fixture] = {
    "two-jobs": Fixture(records=_board(2), etag='"acme-two"'),
    "three-jobs": Fixture(records=_board(3), etag='"acme-three"'),
    "seven-jobs": Fixture(records=_board(7), etag='"acme-seven"'),
    "one-job": Fixture(records=_board(1), etag='"acme-one"'),
    "one-job-edited": Fixture(
        records=(_record(1, title="Robotics Software Engineer 1 (updated)"),),
        etag='"acme-one-edited"',
    ),
    "ten-jobs": Fixture(records=_board(10), etag='"acme-ten"'),
    "hundred-jobs": Fixture(records=_board(100), etag='"acme-hundred"'),
    "edited-first-job": Fixture(
        records=(
            _record(1, title="Robotics Software Engineer 1 (updated)"),
            _record(2),
        ),
        etag='"acme-edited"',
    ),
    "repost-of-job-one": Fixture(
        records=(
            *_board(2),
            _record(901, requisition="req-1", title="Robotics Software Engineer (repost)"),
        ),
        etag='"acme-repost-one"',
    ),
    "moved-requisition-three": Fixture(
        records=(
            *_board(2),
            _record(903, requisition="req-3", title="Robotics Software Engineer (repost)"),
        ),
        etag='"acme-moved-three"',
    ),
    "partial": Fixture(
        records=_board(2),
        etag='"acme-partial"',
        complete=False,
        warnings=("page 2 failed: the collection is incomplete",),
    ),
    "challenge": Fixture(records=(), etag='"acme-challenge"', response="challenge"),
    "always-not-modified": Fixture(
        records=(), etag='"acme-not-modified"', response="not-modified"
    ),
}


def _job(record: Mapping[str, object], source: SourceConfig) -> NormalizedJob:
    """Map one provider record the way a provider adapter would."""
    source_job_id = str(record["id"])
    title = str(record["title"])
    position = record.get("location")
    locations = [str(position["name"])] if isinstance(position, Mapping) else []
    requisition = record.get("requisition_id")
    requisition_id = None if requisition is None else str(requisition)
    apply_url = str(record["absolute_url"])
    description = html_to_text(record.get("content"))
    return NormalizedJob(
        company_id=source.company_id,
        source_id=source.id,
        source_job_id=source_job_id,
        canonical_job_key=build_job_key(
            provider="fixture",
            tenant="acme",
            source_job_id=source_job_id,
            company_id=source.company_id,
            title=title,
            locations=locations,
            apply_url=apply_url,
        ),
        dedupe_key=build_dedupe_key(
            company_id=source.company_id,
            requisition_id=requisition_id,
            apply_url=apply_url,
            title=title,
            locations=locations,
            department=None,
        ),
        requisition_id=requisition_id,
        title=title,
        locations=locations,
        description_text=description,
        content_hash=content_hash(title, locations, description),
        posting_url=HttpUrl(apply_url),
        apply_url=HttpUrl(apply_url),
        first_seen_at=FIXED_NOW,
        raw_metadata={"fixture": source_job_id},
    )


class FixtureAdapter:
    """A single-request adapter whose board the test can swap between cycles."""

    provider = "fixture"

    def __init__(self) -> None:
        self.fixture = "two-jobs"
        self.requests: list[RequestSpec] = []
        self.headers: list[dict[str, str]] = []

    def set_fixture(self, name: str) -> None:
        if name not in FIXTURES:
            raise KeyError(name)
        self.fixture = name

    @property
    def current(self) -> Fixture:
        return FIXTURES[self.fixture]

    def respond(self, request: httpx.Request) -> httpx.Response:
        """Answer one request from the current fixture, honouring a matching validator."""
        self.headers.append(dict(request.headers))
        fixture = self.current
        if fixture.response == "challenge":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                text="<html><title>Just a moment...</title></html>",
            )
        if fixture.response == "not-modified":
            return httpx.Response(304, headers={"ETag": fixture.etag}, content=b"")
        if request.headers.get("If-None-Match") == fixture.etag:
            return httpx.Response(304, headers={"ETag": fixture.etag}, content=b"")
        return httpx.Response(
            200,
            json={"jobs": list(fixture.records), "meta": {"total": len(fixture.records)}},
            headers={"ETag": fixture.etag},
        )

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        request = RequestSpec(url=str(source.endpoint), expected="json")
        self.requests.append(request)
        payload = await fetcher.get(request)
        if payload.not_modified:
            raise AdapterError(f"fixture answered 304 Not Modified for {payload.final_url}")
        board = json.loads(payload.body)
        jobs = [_job(record, source) for record in board["jobs"]]
        fixture = self.current
        return CollectionResult(
            source_id=source.id,
            complete=fixture.complete,
            jobs=jobs,
            warnings=list(fixture.warnings),
            page_count=1,
            declared_total=len(jobs),
        )


class RecordingNotifier:
    """A notifier that records every message and answers with queued results until they run out."""

    provider = "recording"

    def __init__(self) -> None:
        self.messages: list[NotificationMessage] = []
        self.queued: list[DeliveryResult] = []

    def queue(self, *results: DeliveryResult) -> None:
        self.queued.extend(results)

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        self.messages.append(message)
        if self.queued:
            return self.queued.pop(0)
        return DeliveryResult(
            provider=self.provider,
            delivered=True,
            retryable=False,
            http_status=200,
            provider_request_id=f"req-{len(self.messages)}",
        )


class MutableClock:
    """A deterministic clock a test can move between cycles."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


DEFAULT_FILTERS: tuple[FilterConfig, ...] = (
    FilterConfig(
        id="harness-engineer",
        notify=True,
        include_companies=["acme"],
        include_title_keywords=["engineer"],
    ),
)


class ServiceHarness:
    """Everything one collection-cycle test needs, plus a way to restart the process."""

    def __init__(self, tmp_path: Path, router: respx.Router) -> None:
        self.db_path = tmp_path / "state.sqlite3"
        self.repo = Repository.open(self.db_path)
        self.repo.initialize()
        self.adapter = FixtureAdapter()
        self.notifier = RecordingNotifier()
        self.fetcher = HttpFetcher()
        self.clock = MutableClock(FIXED_NOW)
        self.company = CompanyTarget(
            id="acme", name="Acme Robotics", categories=["Robotics/Autonomy"]
        )
        self.source = SourceConfig(
            id=SOURCE_ID,
            company_id="acme",
            provider="fixture",
            source_class=SourceClass.PUBLIC_ATS,
            support_status=SupportStatus.SUPPORTED,
            endpoint=ENDPOINT,
            interval_minutes=5,
        )
        self.filters: tuple[FilterConfig, ...] = DEFAULT_FILTERS
        self.service = self.build()
        router.get(ENDPOINT).mock(side_effect=self.adapter.respond)

    def build(self) -> JobAlertService:
        """A service over the harness's durable state, adapter, notifier, and clock."""
        return JobAlertService(
            self.repo,
            registry=AdapterRegistry([self.adapter]),
            fetcher=self.fetcher,
            filters=self.filters,
            companies=[self.company],
            notifier=self.notifier,
            clock=self.clock,
        )

    def set_filters(self, filters: Sequence[FilterConfig]) -> None:
        """Rebuild the service around a different filter set, keeping all durable state."""
        self.filters = tuple(filters)
        self.service = self.build()

    def restart(self) -> None:
        """Close and reopen the database, as a process restart would."""
        self.repo.close()
        self.repo = Repository.open(self.db_path)
        self.repo.initialize()
        self.service = self.build()

    def active_keys(self) -> list[str]:
        return [job.canonical_job_key for job in self.repo.load_active_jobs(self.source.id)]

    def missing_counts(self) -> dict[str, int]:
        """Every stored absence streak, read straight out of SQLite the way a reviewer would."""
        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT canonical_job_key, missing_complete_runs FROM jobs WHERE source_id = ?",
                (self.source.id,),
            ).fetchall()
        return {str(key): int(count) for key, count in rows}
