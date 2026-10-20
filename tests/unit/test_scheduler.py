"""Deterministic scheduling: jitter, due selection, concurrency, heartbeat, and the run loop.

Every instant here is an argument and every collection is a stand-in, so the scheduler is tested
without a clock, a network, or a real sleep: the tests drive one wake at a time and assert exactly
which sources were dispatched, which were held back, and what the heartbeat decided.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from job_alert.db import Repository, SourceHealthState
from job_alert.health import (
    HealthEvent,
    HealthEventKind,
    IncidentReason,
    evaluate_source_health,
    staleness_threshold,
)
from job_alert.models import (
    CompanyTarget,
    JobEvent,
    JobEventType,
    NormalizedJob,
    RunOutcome,
    SourceClass,
    SourceConfig,
    SupportStatus,
)
from job_alert.notifications.base import DeliveryResult, NotificationMessage
from job_alert.scheduler import (
    DEFAULT_WAKE_INTERVAL_SECONDS,
    DIGEST_TITLE,
    GLOBAL_CONCURRENCY,
    SHUTDOWN_GRACE_SECONDS,
    STALL_MIN_FAILURES,
    Scheduler,
    deterministic_jitter,
    next_digest_at,
    next_due_at,
    source_host,
)
from job_alert.service import CycleResult, RunMode

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
COMPANY = CompanyTarget(id="acme", name="Acme Robotics", categories=["Robotics/Autonomy"])


# ------------------------------------------------------------------ the brief's first two tests


def test_jitter_is_stable_and_bounded() -> None:
    assert deterministic_jitter("openai-greenhouse") == deterministic_jitter(
        "openai-greenhouse"
    )
    assert 0 <= deterministic_jitter("openai-greenhouse").total_seconds() <= 60


def test_next_due_uses_last_complete_plus_interval_and_jitter() -> None:
    last = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    due = next_due_at("source", last, interval_minutes=5)
    assert last + timedelta(minutes=5) <= due <= last + timedelta(minutes=6)


# ------------------------------------------------------------------ jitter and due arithmetic


def test_jitter_is_the_first_eight_hex_characters_of_the_source_digest() -> None:
    expected = timedelta(seconds=int(sha256(b"openai-greenhouse").hexdigest()[:8], 16) % 61)
    assert deterministic_jitter("openai-greenhouse") == expected
    # Two sources never share a phase by accident of ordering: the digest spreads them out.
    spread = {deterministic_jitter(f"acme-{index}") for index in range(20)}
    assert len(spread) > 1


def test_next_due_rejects_an_interval_below_one_minute() -> None:
    with pytest.raises(ValueError, match="interval_minutes"):
        next_due_at("source", NOW, interval_minutes=0)


def test_the_host_of_a_source_is_its_endpoint_hostname() -> None:
    assert source_host(make_source("acme-a", host="a")) == "a.acme-robotics.com"
    assert source_host(make_source("acme-b", host="a")) == "a.acme-robotics.com"


def test_next_digest_is_eighteen_hundred_local_time_in_chicago() -> None:
    # 2026-09-18 22:59 UTC is 17:59 in Chicago (CDT), so the digest is due one minute later.
    assert next_digest_at(datetime(2026, 9, 18, 22, 59, tzinfo=UTC)) == datetime(
        2026, 9, 18, 23, 0, tzinfo=UTC
    )
    # Past 18:00 the next digest belongs to the following local day.
    assert next_digest_at(datetime(2026, 9, 18, 23, 1, tzinfo=UTC)) == datetime(
        2026, 9, 19, 23, 0, tzinfo=UTC
    )


# ------------------------------------------------------------------ due selection and concurrency


async def test_a_wake_dispatches_due_sources_and_leaves_fresh_ones_alone(tmp_path: Path) -> None:
    repository = open_repository(tmp_path)
    fresh = make_source("acme-fresh", host="a")
    never_polled = make_source("acme-never", host="b")
    record_complete(repository, fresh, at=NOW - timedelta(minutes=1))
    service = RecordingService()
    scheduler = build_scheduler(repository, [fresh, never_polled], service)

    report = await scheduler.wake(now=NOW)

    assert report.dispatched == ("acme-never",)
    assert report.deferred_in_flight == ()
    assert report.deferred_for_backoff == ()
    await scheduler.wait_for_idle()
    assert service.calls == ["acme-never"]


async def test_a_source_inside_its_backoff_is_not_dispatched(tmp_path: Path) -> None:
    repository = open_repository(tmp_path)
    cooling = make_source("acme-cooling", host="a")
    repository.record_backoff(cooling.id, NOW + timedelta(minutes=30))
    service = RecordingService()
    scheduler = build_scheduler(repository, [cooling], service)

    report = await scheduler.wake(now=NOW)

    assert report.dispatched == ()
    assert report.deferred_for_backoff == ("acme-cooling",)
    assert service.calls == []
    # Once the recorded instant passes the source is due again.
    later = await scheduler.wake(now=NOW + timedelta(minutes=31))
    assert later.dispatched == ("acme-cooling",)
    await scheduler.wait_for_idle()


async def test_a_disabled_source_is_never_dispatched(tmp_path: Path) -> None:
    repository = open_repository(tmp_path)
    disabled = make_source("acme-disabled", host="a", enabled=False)
    service = RecordingService()
    scheduler = build_scheduler(repository, [disabled], service)

    report = await scheduler.wake(now=NOW)

    assert report.dispatched == ()
    assert service.calls == []


async def test_concurrent_collections_never_exceed_the_global_limit(tmp_path: Path) -> None:
    repository = open_repository(tmp_path)
    total = GLOBAL_CONCURRENCY + 2
    sources = [make_source(f"acme-{index:02d}", host=f"host{index}") for index in range(total)]
    service = RecordingService(gated=True)
    scheduler = build_scheduler(repository, sources, service)

    report = await scheduler.wake(now=NOW)
    await wait_for(lambda: len(service.calls) == GLOBAL_CONCURRENCY)

    assert len(report.dispatched) == total
    assert service.max_active == GLOBAL_CONCURRENCY == 16
    assert service.calls == [f"acme-{index:02d}" for index in range(GLOBAL_CONCURRENCY)]

    service.release.set()
    await scheduler.wait_for_idle()
    assert sorted(service.finished) == sorted(source.id for source in sources)


async def test_a_single_host_gets_at_most_two_collections_at_once(tmp_path: Path) -> None:
    repository = open_repository(tmp_path)
    sources = [make_source(f"acme-a{index}", host="a") for index in range(3)]
    sources += [make_source(f"acme-b{index}", host="b") for index in range(3)]
    service = RecordingService(gated=True)
    scheduler = build_scheduler(repository, sources, service)

    await scheduler.wake(now=NOW)
    await wait_for(lambda: len(service.calls) == 4)

    assert service.max_per_host["a.acme-robotics.com"] == 2
    assert service.max_per_host["b.acme-robotics.com"] == 2
    assert len(service.calls) == 4

    service.release.set()
    await scheduler.wait_for_idle()
    assert len(service.finished) == 6


async def test_a_source_already_in_flight_is_never_dispatched_twice(tmp_path: Path) -> None:
    repository = open_repository(tmp_path)
    source = make_source("acme-a", host="a")
    service = RecordingService(gated=True)
    scheduler = build_scheduler(repository, [source], service)

    first = await scheduler.wake(now=NOW)
    await wait_for(lambda: service.calls == ["acme-a"])
    second = await scheduler.wake(now=NOW + timedelta(seconds=30))

    assert first.dispatched == ("acme-a",)
    assert second.dispatched == ()
    assert second.deferred_in_flight == ("acme-a",)
    assert service.calls == ["acme-a"]

    service.release.set()
    await scheduler.wait_for_idle()
    assert service.finished == ["acme-a"]


async def test_shutdown_waits_for_in_flight_collections_and_closes_resources(
    tmp_path: Path,
) -> None:
    repository = open_repository(tmp_path)
    source = make_source("acme-a", host="a")
    service = RecordingService(gated=True)
    fetcher = StubFetcher()
    scheduler = build_scheduler(repository, [source], service, fetcher=fetcher)

    running = asyncio.ensure_future(scheduler.run(max_wakes=1))
    await wait_for(lambda: service.calls == ["acme-a"])
    assert not running.done()

    service.release.set()
    report = await running

    assert report.wakes[0].dispatched == ("acme-a",)
    assert service.finished == ["acme-a"]
    assert fetcher.closed is True
    with pytest.raises(sqlite3.ProgrammingError):
        repository.source_health("acme-a")


async def test_the_shutdown_wait_is_bounded_by_thirty_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = open_repository(tmp_path)
    source = make_source("acme-a", host="a")
    service = RecordingService(gated=True)
    scheduler = build_scheduler(repository, [source], service)
    monkeypatch.setattr("job_alert.scheduler.SHUTDOWN_GRACE_SECONDS", 0.05)

    running = asyncio.ensure_future(scheduler.run(max_wakes=1))
    await wait_for(lambda: service.calls == ["acme-a"])
    report = await asyncio.wait_for(running, timeout=5.0)

    assert report.wakes  # the loop gave up waiting rather than hanging on the stalled collection
    service.release.set()
    await scheduler.wait_for_idle()
    assert SHUTDOWN_GRACE_SECONDS == 30.0


async def test_the_loop_wakes_every_thirty_seconds_and_stops_after_its_limit(
    tmp_path: Path,
) -> None:
    repository = open_repository(tmp_path)
    pauses: list[float] = []

    async def record_pause(seconds: float, stop: asyncio.Event) -> None:
        pauses.append(seconds)

    scheduler = build_scheduler(repository, [], RecordingService(), pause=record_pause)

    report = await scheduler.run(max_wakes=3)

    assert len(report.wakes) == 3
    assert pauses == [float(DEFAULT_WAKE_INTERVAL_SECONDS)] * 2


# ------------------------------------------------------------------ the health heartbeat


async def test_the_heartbeat_stays_silent_when_every_source_is_fresh(
    tmp_path: Path,
) -> None:
    repository = open_repository(tmp_path)
    source = make_source("acme-a", host="a")
    record_complete(repository, source, at=NOW - timedelta(minutes=1))
    lines: list[str] = []
    notifier = RecordingNotifier()
    scheduler = build_scheduler(
        repository, [source], RecordingService(), notifier=notifier, logger=lines.append
    )

    outcome = await scheduler.heartbeat(now=NOW)

    assert outcome.silent is True
    assert outcome.events == ()
    assert outcome.pending_deliveries == 0
    assert notifier.messages == []
    assert lines == []
    state = repository.source_health(source.id)
    assert state is not None and state.health_alert_open is False


async def test_the_heartbeat_opens_exactly_one_incident_for_a_stale_source(
    tmp_path: Path,
) -> None:
    repository = open_repository(tmp_path)
    source = make_source("acme-a", host="a", interval=5)
    record_complete(repository, source, at=NOW - staleness_threshold(5) - timedelta(minutes=1))
    notifier = RecordingNotifier()
    scheduler = build_scheduler(repository, [source], RecordingService(), notifier=notifier)

    opened = await scheduler.heartbeat(now=NOW)

    assert opened.silent is False
    assert len(opened.incidents) == 1
    incident = opened.incidents[0]
    assert incident.kind is HealthEventKind.SOURCE_FAILURE
    assert incident.reason is IncidentReason.STALE_WITHOUT_COMPLETE_SUCCESS
    assert incident.source_id == "acme-a"
    assert incident.company_name == "Acme Robotics"
    assert len(notifier.messages) == 1
    assert notifier.messages[0].url == "https://a.acme-robotics.com/jobs"
    state = repository.source_health(source.id)
    assert state is not None and state.health_alert_open is True
    assert state.health_reason == IncidentReason.STALE_WITHOUT_COMPLETE_SUCCESS

    repeated = await scheduler.heartbeat(now=NOW + timedelta(minutes=5))

    assert repeated.events == ()
    assert len(notifier.messages) == 1


async def test_the_heartbeat_emits_the_recovery_a_crashed_cycle_never_recorded(
    tmp_path: Path,
) -> None:
    repository = open_repository(tmp_path)
    source = make_source("acme-a", host="a", interval=5)
    opened_at = NOW - timedelta(hours=1)
    repository.record_health_state(
        source.id,
        SourceHealthState(
            source_id=source.id,
            company_id=COMPANY.id,
            company_name=COMPANY.name,
            consecutive_failures=3,
            health_alert_open=True,
            health_opened_at=opened_at,
            health_reason=IncidentReason.CONSECUTIVE_FAILURES,
            first_failure_at=opened_at,
            last_error="HTTP 503",
            interval_minutes=5,
        ),
    )
    # The run completed after the incident opened, but the process stopped before it recorded the
    # recovery the health policy owed, so the incident is still open.
    record_complete(repository, source, at=NOW - timedelta(minutes=2))
    notifier = RecordingNotifier()
    scheduler = build_scheduler(repository, [source], RecordingService(), notifier=notifier)

    recovered = await scheduler.heartbeat(now=NOW)

    assert recovered.silent is False
    assert [event.kind for event in recovered.events] == [HealthEventKind.SOURCE_RECOVERY]
    assert recovered.recoveries[0].occurred_at == NOW - timedelta(minutes=2)
    assert len(notifier.messages) == 1
    state = repository.source_health(source.id)
    assert state is not None
    assert state.health_alert_open is False
    assert state.consecutive_failures == 0


async def test_the_heartbeat_is_not_silent_while_a_delivery_is_pending(tmp_path: Path) -> None:
    repository = open_repository(tmp_path)
    source = make_source("acme-a", host="a")
    record_complete(repository, source, at=NOW - timedelta(minutes=1))
    seed_pending_event(repository, source, at=NOW - timedelta(minutes=2))
    service = RecordingService()
    scheduler = build_scheduler(repository, [source], service)

    outcome = await scheduler.heartbeat(now=NOW)

    assert outcome.silent is False
    assert outcome.pending_deliveries == 1
    # The retry runs on the heartbeat cadence, and the recorded backoff decides whether it sends.
    assert service.deliveries == ["acme-a"]


async def test_the_heartbeat_leaves_health_writes_to_an_in_flight_collection(
    tmp_path: Path,
) -> None:
    """Important 2: a collection that outlives a heartbeat owns the write, so one incident opens.

    The source has never completed and last ran long enough ago for the heartbeat to call it stale,
    while the in-flight collection's own evaluation is not stale (its first failure is now). Without
    the in-flight guard the heartbeat would open an incident that the run's write then clobbers
    without a recovery push; with it, the incident is opened exactly once, after the run is idle.
    """
    repository = open_repository(tmp_path)
    source = make_source("acme-a", host="a", interval=5)
    record_diagnostic(repository, source, at=NOW - timedelta(minutes=20))
    service = RecordingService(
        gated=True, health_writer=run_writer(repository, RunOutcome.FAILED, at=NOW)
    )
    scheduler = build_scheduler(repository, [source], service)

    await scheduler.wake(now=NOW)
    await wait_for(lambda: service.calls == ["acme-a"])

    for offset in (0, 5, 10):
        in_flight = await scheduler.heartbeat(now=NOW + timedelta(minutes=offset))
        assert in_flight.events == ()
        assert in_flight.deferred_in_flight == ("acme-a",)
        state = repository.source_health(source.id)
        assert state is not None and state.health_alert_open is False

    service.release.set()
    await scheduler.wait_for_idle()
    assert service.health_events == []  # the failing run itself opens nothing

    opened = await scheduler.heartbeat(now=NOW + timedelta(minutes=16))

    assert [event.kind for event in opened.incidents] == [HealthEventKind.SOURCE_FAILURE]
    assert opened.deferred_in_flight == ()
    state = repository.source_health(source.id)
    assert state is not None
    assert state.health_alert_open is True
    assert state.health_reason == IncidentReason.STALE_WITHOUT_COMPLETE_SUCCESS
    # Exactly one failure event in the whole sequence, whoever wrote it.
    assert len(service.health_events) + len(opened.incidents) == 1


async def test_the_heartbeat_does_not_misread_a_source_whose_run_is_in_flight(
    tmp_path: Path,
) -> None:
    """Important 2: a source that looks stale only because its collection has not finished yet.

    The source has not completed for twenty minutes, so a heartbeat that ignores the in-flight set
    opens a staleness incident -- and the collection that is completing right now, which carries no
    incident in the pre-run state it evaluates against, closes it again without ever pushing the
    recovery that would explain the closure. With the guard the heartbeat stays quiet, the run's own
    freshness lands, and no incident is invented at all.
    """
    repository = open_repository(tmp_path)
    source = make_source("acme-a", host="a", interval=5)
    record_diagnostic(repository, source, at=NOW - timedelta(minutes=20))
    service = RecordingService(
        gated=True, health_writer=run_writer(repository, RunOutcome.COMPLETE, at=NOW)
    )
    scheduler = build_scheduler(repository, [source], service)

    await scheduler.wake(now=NOW)
    await wait_for(lambda: service.calls == ["acme-a"])

    skipped = await scheduler.heartbeat(now=NOW)
    assert skipped.events == ()
    assert skipped.deferred_in_flight == ("acme-a",)
    state = repository.source_health(source.id)
    assert state is not None and state.health_alert_open is False

    service.release.set()
    await scheduler.wait_for_idle()

    after = await scheduler.heartbeat(now=NOW + timedelta(minutes=5))

    assert after.events == ()
    assert service.health_events == []
    state = repository.source_health(source.id)
    assert state is not None
    assert state.health_alert_open is False
    assert state.last_complete_at == NOW


# ------------------------------------------------------------------ the daily digest


async def test_the_digest_is_sent_once_at_six_pm_chicago_time(tmp_path: Path) -> None:
    repository = open_repository(tmp_path)
    source = make_source("acme-a", host="a")
    record_complete(repository, source, at=NOW)
    notifier = RecordingNotifier()
    report_path = tmp_path / "reports" / "daily-digest.md"
    scheduler = build_scheduler(
        repository,
        [source],
        RecordingService(),
        notifier=notifier,
        clock=lambda: datetime(2026, 9, 18, 22, 59, tzinfo=UTC),
        digest_report_path=report_path,
    )

    before = await scheduler.wake(now=datetime(2026, 9, 18, 22, 59, tzinfo=UTC))
    assert before.digest is None

    due = await scheduler.wake(now=datetime(2026, 9, 18, 23, 0, tzinfo=UTC))
    assert due.digest is not None
    assert due.digest.delivered is True
    assert due.digest.report_path == report_path
    assert report_path.read_text(encoding="utf-8").startswith("# Fast Job Alert daily digest")

    repeated = await scheduler.wake(now=datetime(2026, 9, 18, 23, 1, tzinfo=UTC))
    assert repeated.digest is None
    assert len(digests_sent(notifier)) == 1

    tomorrow = await scheduler.wake(now=datetime(2026, 9, 19, 23, 0, tzinfo=UTC))
    assert tomorrow.digest is not None
    assert len(digests_sent(notifier)) == 2


# ------------------------------------------------------------------ helpers and stand-ins


def make_source(
    source_id: str,
    *,
    host: str = "a",
    interval: int = 5,
    enabled: bool = True,
) -> SourceConfig:
    return SourceConfig(
        id=source_id,
        company_id=COMPANY.id,
        provider="fixture",
        source_class=SourceClass.PUBLIC_ATS,
        support_status=SupportStatus.SUPPORTED,
        endpoint=f"https://{host}.acme-robotics.com/jobs",
        interval_minutes=interval,
        enabled=enabled,
    )


def make_job(source_id: str) -> NormalizedJob:
    posting_url = "https://boards.acme-robotics.com/jobs/1"
    return NormalizedJob(
        company_id=COMPANY.id,
        source_id=source_id,
        source_job_id="1",
        canonical_job_key=f"fixture:acme:1:{source_id}",
        dedupe_key=f"url:acme:{posting_url}",
        title="Robotics Software Engineer",
        locations=["Palo Alto, CA"],
        posting_url=posting_url,
        apply_url=posting_url,
        first_seen_at=NOW,
    )


def open_repository(tmp_path: Path) -> Repository:
    repository = Repository.open(tmp_path / "state.sqlite3")
    repository.initialize()
    return repository


def record_complete(repository: Repository, source: SourceConfig, *, at: datetime) -> None:
    """One stored complete run, which is what moves a source's freshness instant."""
    run_id = repository.start_run(source.id, at)
    repository.upsert_observations(run_id, [make_job(source.id)])
    repository.finish_run(run_id, RunOutcome.COMPLETE, at, jobs_seen=1)


def record_diagnostic(repository: Repository, source: SourceConfig, *, at: datetime) -> None:
    """A dry run the operator asked for: it moves the source's last run and nothing else."""
    run_id = repository.start_run(source.id, at)
    repository.finish_run(run_id, RunOutcome.PARTIAL, at, error_code="dry_run")


def run_writer(
    repository: Repository, outcome: RunOutcome, *, at: datetime
) -> Callable[[SourceConfig], tuple[HealthEvent, ...]]:
    """A stand-in for the health write one real cycle makes, for a service that has none.

    It follows the order the real service uses: read the stored state, finish the run, evaluate the
    outcome against the state it read, and persist the result. Reading at write time is equivalent
    here only because the heartbeat leaves an in-flight source alone, which is what the tests that
    use this writer are checking.
    """

    def write(source: SourceConfig) -> tuple[HealthEvent, ...]:
        before = repository.source_health(source.id)
        assert before is not None
        run_id = repository.start_run(source.id, at)
        repository.finish_run(
            run_id,
            outcome,
            at,
            jobs_seen=1,
            error_code=None if outcome is RunOutcome.COMPLETE else "collection_failed",
            error_message=None if outcome is RunOutcome.COMPLETE else "HTTP 503",
        )
        evaluation = evaluate_source_health(
            SourceHealthState.from_source_state(
                before,
                company_id=COMPANY.id,
                company_name=COMPANY.name,
                interval_minutes=source.interval_minutes,
            ),
            outcome,
            now=at,
            error_message=None if outcome is RunOutcome.COMPLETE else "HTTP 503",
        )
        repository.record_health_state(source.id, evaluation.state)
        return tuple(evaluation.events)

    return write


def seed_pending_event(repository: Repository, source: SourceConfig, *, at: datetime) -> int:
    job = make_job(source.id)
    run_id = repository.start_run(source.id, at)
    repository.upsert_observations(run_id, [job])
    repository.finish_run(run_id, RunOutcome.COMPLETE, at, jobs_seen=1)
    return repository.record_events(
        run_id, [JobEvent(event_type=JobEventType.NEW, job=job, detected_at=at)]
    )[0]


async def wait_for(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    """Yield to the loop until a predicate holds, so a gated stand-in can reach its limit."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for the scheduler")
        await asyncio.sleep(0)


async def no_pause(seconds: float, stop: asyncio.Event) -> None:
    """A pause that returns immediately: the loop's schedule is asserted through its arguments."""
    return None


def digests_sent(notifier: RecordingNotifier) -> list[NotificationMessage]:
    """Only the daily digest among the messages a test notifier recorded.

    A wake can carry both a digest and a health push, so a test about the digest counts its own.
    """
    return [message for message in notifier.messages if message.title == DIGEST_TITLE]


class StubFetcher:
    """The scheduler owns closing the HTTP client, so a test can observe that it did."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class RecordingService:
    """A stand-in collection service that records concurrency and delivery passes."""

    def __init__(
        self,
        *,
        gated: bool = False,
        health_writer: Callable[[SourceConfig], tuple[HealthEvent, ...]] | None = None,
        outcome: RunOutcome = RunOutcome.COMPLETE,
    ) -> None:
        self.outcome = outcome
        self.calls: list[str] = []
        self.finished: list[str] = []
        self.deliveries: list[str] = []
        self.health_events: list[HealthEvent] = []
        self.health_writer = health_writer
        self.active = 0
        self.max_active = 0
        self.per_host_active: dict[str, int] = {}
        self.max_per_host: dict[str, int] = {}
        self.release = asyncio.Event() if gated else None

    async def collect_source(
        self, source: SourceConfig, mode: RunMode = RunMode.LIVE
    ) -> CycleResult:
        host = source_host(source)
        self.calls.append(source.id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.per_host_active[host] = self.per_host_active.get(host, 0) + 1
        self.max_per_host[host] = max(
            self.max_per_host.get(host, 0), self.per_host_active[host]
        )
        try:
            if self.release is not None:
                await self.release.wait()
            events: tuple[HealthEvent, ...] = ()
            if self.health_writer is not None:
                events = tuple(self.health_writer(source))
                self.health_events.extend(events)
            return CycleResult(
                source_id=source.id,
                mode=mode,
                run_id=1,
                outcome=self.outcome,
                complete=self.outcome is RunOutcome.COMPLETE,
                error_code=None if self.outcome is RunOutcome.COMPLETE else "collection_failed",
                health_events=events,
            )
        finally:
            self.active -= 1
            self.per_host_active[host] -= 1
            self.finished.append(source.id)

    async def deliver_pending(
        self, source: SourceConfig, *, limit: int | None = None
    ) -> tuple[int, tuple[object, ...]]:
        self.deliveries.append(source.id)
        return 0, ()


class RecordingNotifier:
    """A notifier that records every message and always reports a delivery."""

    provider = "recording"

    def __init__(self) -> None:
        self.messages: list[NotificationMessage] = []

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        self.messages.append(message)
        return DeliveryResult(
            provider=self.provider,
            delivered=True,
            retryable=False,
            http_status=200,
            provider_request_id=f"req-{len(self.messages)}",
        )


def build_scheduler(
    repository: Repository,
    sources: Sequence[SourceConfig],
    service: RecordingService,
    **overrides: object,
) -> Scheduler:
    values: dict[str, object] = {
        "repository": repository,
        "sources": tuple(sources),
        "companies": (COMPANY,),
        "service": service,
        "mode": RunMode.LIVE_SUPPRESSED,
        "fetcher": None,
        "notifier": None,
        "clock": lambda: NOW,
        "pause": no_pause,
        "logger": None,
    }
    values.update(overrides)
    return Scheduler(**values)  # type: ignore[arg-type]


def test_the_cycle_log_line_says_when_a_snapshot_only_seeded() -> None:
    from job_alert.models import RunOutcome
    from job_alert.scheduler import _cycle_line
    from job_alert.service import CycleResult, RunMode

    seeded = CycleResult(
        source_id="acme", mode=RunMode.LIVE, run_id=1, outcome=RunOutcome.COMPLETE,
        complete=True, new_count=5, seeded=True,
    )
    assert _cycle_line(seeded).endswith("seeded (first complete snapshot, no events)")
    assert "seeded" not in _cycle_line(
        CycleResult(
            source_id="acme", mode=RunMode.LIVE, run_id=2, outcome=RunOutcome.COMPLETE,
            complete=True,
        )
    )


async def test_a_wake_writes_the_openings_page_and_only_again_when_due(tmp_path: Path) -> None:
    repository = open_repository(tmp_path)
    page = tmp_path / "reports" / "openings.html"
    source = make_source("acme-fresh", host="a")
    record_complete(repository, source, at=NOW - timedelta(minutes=1))
    scheduler = build_scheduler(
        repository, [source], RecordingService(), openings_page_path=page
    )

    await scheduler.wake(now=NOW)

    assert page.read_text(encoding="utf-8").startswith("<!doctype html>")
    recent = page.with_name("recent.html")
    assert "<title>Last 24 Hours</title>" in recent.read_text(encoding="utf-8")
    page.unlink()
    # Nothing changed and no heartbeat is due, so the next wake leaves the page alone.
    await scheduler.wake(now=NOW + timedelta(seconds=30))
    assert not page.exists()
    await scheduler.wait_for_idle()


# ------------------------------------------------------------------ the stall watchdog


def ten_minute_steps(clock: list[datetime]) -> Callable[[float, asyncio.Event], object]:
    """A pause that lets the dispatched collections finish, then moves the clock ten minutes."""

    async def pause(seconds: float, stop: asyncio.Event) -> None:
        for _ in range(5):
            await asyncio.sleep(0)
        clock[0] += timedelta(minutes=10)

    return pause


async def test_the_watchdog_stops_the_loop_once_nothing_completes_for_thirty_minutes(
    tmp_path: Path,
) -> None:
    repository = open_repository(tmp_path)
    sources = [make_source(f"acme-{index:02d}", host=f"h{index}") for index in range(20)]
    assert len(sources) >= STALL_MIN_FAILURES
    clock = [NOW]
    scheduler = build_scheduler(
        repository,
        sources,
        RecordingService(outcome=RunOutcome.PARTIAL),
        clock=lambda: clock[0],
        pause=ten_minute_steps(clock),
    )

    report = await scheduler.run(max_wakes=10)

    assert report.stalled
    # Wakes at 0, 10, 20 and 30 minutes: the fourth is the first a full half hour after the start.
    assert len(report.wakes) == 4


async def test_the_watchdog_leaves_a_loop_alone_while_collections_complete(
    tmp_path: Path,
) -> None:
    repository = open_repository(tmp_path)
    sources = [make_source(f"acme-{index:02d}", host=f"h{index}") for index in range(20)]
    clock = [NOW]
    scheduler = build_scheduler(
        repository,
        sources,
        RecordingService(),
        clock=lambda: clock[0],
        pause=ten_minute_steps(clock),
    )

    report = await scheduler.run(max_wakes=6)

    assert not report.stalled
    assert len(report.wakes) == 6
