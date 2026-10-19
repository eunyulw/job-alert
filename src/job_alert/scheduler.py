"""Deterministic scheduling for the fast job alert monitor.

One wake at a time
------------------
:class:`Scheduler` is the loop that makes the monitor runnable. Every wake asks a small set of
questions in a fixed order, so an operator can reason about what the process does next without
reading the whole file:

1. **Which sources are due?** An enabled source is due when it has never completed a collection, or
   when its last complete success plus its interval plus its own jitter has passed, and it is never
   due while its recorded ``backoff_until`` is still in the future.
2. **Dispatch them.** A source already in flight is skipped, so no source is ever collected twice at
   once however long its collection takes. The global limit is eight collections at a time and the
   per-host limit is two, because two boards sharing one provider host are closer to failing
   together than ten boards on ten hosts are.
3. **Evaluate the health heartbeat** every five minutes; see below.
4. **Send the daily digest** at 18:00 in the report's own timezone.
5. Sleep until the next wake, which is :data:`DEFAULT_WAKE_INTERVAL_SECONDS` seconds unless the
   heartbeat or the digest is closer.

Jitter
------
Two sources configured with the same interval must not be polled in lockstep, and the phase must not
move between restarts, so jitter comes from the source id itself: the first eight hex characters of
``sha256(source_id)`` modulo 61 seconds. A source is due at a stable, reproducible instant an
operator can compute without running the process.

The health heartbeat
--------------------
Health stays separate from job alerts: an incident describes a source, never a posting, and it is
opened or recovered by the policy in :mod:`job_alert.health`. The heartbeat is the part of that
policy no run can reach. It stays silent -- no push and no log line -- while every enabled source is
inside its freshness window and nothing is waiting to be delivered. When a source has gone quiet it
opens one incident; when a source completed after an incident opened but the process stopped before
recording the recovery, it closes that incident. A source with undelivered events is not silent
either: the heartbeat re-attempts those deliveries, because the recorded retry delay decides whether
an attempt may be spent and nothing else would come back for them until the board was due again.

What the scheduler owns
-----------------------
The collections themselves belong to :class:`~job_alert.service.JobAlertService`, which records each
run's health before returning. The scheduler adds the two things a running process needs on top:
when to poll, and how many at once. A cycle's own health events are delivered by the service that
earned them; the scheduler pushes the ones the heartbeat produces, through whatever notifier it was
handed. It closes the HTTP fetcher and the database it was given at shutdown.

Shutdown
--------
``run()`` stops taking new work at the first ``stop``, waits up to
:data:`SHUTDOWN_GRACE_SECONDS` seconds for the collections already in flight, and then closes the
HTTP fetcher and the database.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from job_alert.config import FilterConfig
from job_alert.db import (
    DEFAULT_MAX_RETRYABLE_ATTEMPTS,
    DIAGNOSTIC_ERROR_CODES,
    RETRY_BACKOFF_BASE_SECONDS,
    RETRY_BACKOFF_MAX_SECONDS,
    Repository,
    SourceHealthState,
    retry_backoff_seconds,
)
from job_alert.fetch import HttpFetcher
from job_alert.health import (
    CHICAGO_TIMEZONE,
    DailyDigest,
    DigestSource,
    HealthEvent,
    HealthEventKind,
    build_daily_digest,
    evaluate_source_heartbeat,
    health_message,
)
from job_alert.models import CompanyTarget, RunOutcome, SourceConfig
from job_alert.notifications.base import NotificationMessage, Notifier
from job_alert.openings import write_openings_page
from job_alert.recent import RECENT_PAGE_NAME, write_recent_page
from job_alert.service import DELIVERABLE_EVENT_TYPES, CycleResult, JobAlertService, RunMode

#: How long the loop waits between wakes when nothing else is scheduled sooner.
DEFAULT_WAKE_INTERVAL_SECONDS = 30

#: How often the health heartbeat is evaluated.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 300

#: Collections allowed to run at once, and against one provider host. 268 sources, some of them
#: whole-board walks of several minutes (Otis's 69 Workday pages take about 12), kept 8 global
#: slots about three-quarters busy, so 5-minute Greenhouse sources waited 20-30 minutes for a slot
#: (measured 2026-10-19). The per-host limit is what keeps any one provider's load polite.
GLOBAL_CONCURRENCY = 16
PER_HOST_CONCURRENCY = 2

#: How long a graceful shutdown waits for the collections already in flight.
SHUTDOWN_GRACE_SECONDS = 30.0

#: When the daily digest is sent, in the local time of the report's timezone.
DIGEST_HOUR = 18
DIGEST_MINUTE = 0

#: The window one digest covers, counted back from the instant it is generated.
DIGEST_WINDOW = timedelta(days=1)

#: Jitter is drawn from this many seconds, which is one more than a whole minute.
JITTER_MODULUS_SECONDS = 61

#: The link a message with no posting to point at opens, which an operator can override.
DEFAULT_ALERT_URL = "https://pushover.net/"

#: The digest message's title, which is also what an operator greps their notifications for.
DIGEST_TITLE = "Fast Job Alert daily digest"


def deterministic_jitter(source_id: str) -> timedelta:
    """The stable phase one source is polled on, derived from its id.

    Eight hex characters are 32 bits and 61 is not a divisor of 2**32, so the values spread across
    the whole minute instead of clustering on the boundaries a smaller modulus would leave.
    """
    digest = sha256(source_id.encode("utf-8")).hexdigest()
    return timedelta(seconds=int(digest[:8], 16) % JITTER_MODULUS_SECONDS)


def next_due_at(source_id: str, last_complete: datetime, interval_minutes: int) -> datetime:
    """When a source that last completed at this instant becomes due again."""
    if interval_minutes < 1:
        raise ValueError(f"interval_minutes must be positive, got {interval_minutes}")
    return (
        _as_utc(last_complete)
        + timedelta(minutes=interval_minutes)
        + deterministic_jitter(source_id)
    )


def source_host(source: SourceConfig) -> str:
    """The provider host a collection talks to, which is what the per-host limit counts."""
    return (urlsplit(str(source.endpoint)).hostname or "").lower()


def next_digest_at(
    now: datetime,
    *,
    timezone: str = CHICAGO_TIMEZONE,
    hour: int = DIGEST_HOUR,
    minute: int = DIGEST_MINUTE,
) -> datetime:
    """The next digest instant at or after ``now``, in the report's own timezone.

    The candidate is built from local wall-clock fields rather than by adding a fixed offset, so the
    digest stays at 18:00 on the clock across a daylight-saving change instead of drifting an hour.
    """
    zone = ZoneInfo(timezone)
    local = _as_utc(now).astimezone(zone)
    candidate = datetime(local.year, local.month, local.day, hour, minute, tzinfo=zone)
    if candidate < local:
        following = local + timedelta(days=1)
        candidate = datetime(
            following.year, following.month, following.day, hour, minute, tzinfo=zone
        )
    return candidate.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class HeartbeatOutcome:
    """What one heartbeat found, and whether it had anything to say.

    ``silent`` is the whole contract: the heartbeat says nothing while every enabled source is
    inside its freshness window and nothing is waiting to be delivered.
    """

    evaluated_at: datetime
    incidents: tuple[HealthEvent, ...] = ()
    recoveries: tuple[HealthEvent, ...] = ()
    pending_deliveries: int = 0
    #: Sources this heartbeat deliberately left alone because their own collection was still
    #: running: that collection holds the state snapshot it will write from, so the heartbeat must
    #: not write health for the same source underneath it.
    deferred_in_flight: tuple[str, ...] = ()

    @property
    def events(self) -> tuple[HealthEvent, ...]:
        """Every health event this heartbeat produced, incidents first."""
        return self.incidents + self.recoveries

    @property
    def silent(self) -> bool:
        """Whether there was nothing to open, nothing to recover, and nothing undelivered."""
        return not self.events and self.pending_deliveries == 0


@dataclass(frozen=True, slots=True)
class DigestDelivery:
    """One daily digest: the report, the message it became, and whether a provider took it."""

    generated_at: datetime
    window_start: datetime
    digest: DailyDigest
    message: NotificationMessage
    report_path: Path | None = None
    delivered: bool = False

    @property
    def summary(self) -> str:
        """One log line: enough to see the state of the fleet without opening the report."""
        digest = self.digest
        return (
            f"{digest.total_enabled_sources} enabled sources, {digest.sources_complete} complete,"
            f" {digest.sources_stale} stale, {len(digest.open_incidents)} open incident(s),"
            f" {digest.new_jobs} new job(s)"
        )


@dataclass(frozen=True, slots=True)
class WakeReport:
    """What one wake did, in the order it decided."""

    now: datetime
    dispatched: tuple[str, ...] = ()
    deferred_in_flight: tuple[str, ...] = ()
    deferred_for_backoff: tuple[str, ...] = ()
    heartbeat: HeartbeatOutcome | None = None
    digest: DigestDelivery | None = None


@dataclass(frozen=True, slots=True)
class SchedulerReport:
    """The whole run: when it started, when it stopped, and every wake in between."""

    started_at: datetime
    finished_at: datetime
    wakes: tuple[WakeReport, ...] = ()


class Scheduler:
    """Poll the configured sources on their own schedules and deliver what they earn."""

    def __init__(
        self,
        repository: Repository,
        *,
        sources: Sequence[SourceConfig],
        service: JobAlertService,
        companies: Sequence[CompanyTarget] = (),
        mode: RunMode = RunMode.LIVE_SUPPRESSED,
        fetcher: HttpFetcher | None = None,
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        pause: Callable[[float, asyncio.Event], Awaitable[None]] | None = None,
        logger: Callable[[str], None] | None = None,
        alert_url: str = DEFAULT_ALERT_URL,
        digest_report_path: Path | None = None,
        openings_page_path: Path | None = None,
        filters: Sequence[FilterConfig] = (),
        wake_interval_seconds: float = DEFAULT_WAKE_INTERVAL_SECONDS,
        heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        digest_hour: int = DIGEST_HOUR,
        digest_minute: int = DIGEST_MINUTE,
        timezone: str = CHICAGO_TIMEZONE,
    ) -> None:
        if wake_interval_seconds <= 0:
            raise ValueError(f"wake_interval_seconds must be positive, got {wake_interval_seconds}")
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        if not 0 <= digest_hour <= 23 or not 0 <= digest_minute <= 59:
            raise ValueError("the digest must be a valid local clock time")
        ZoneInfo(timezone)  # fail on an unusable timezone now rather than at midnight

        self._repository = repository
        self._service = service
        self._mode = mode
        self._fetcher = fetcher
        self._notifier = notifier
        self._clock = clock
        self._pause_callable = pause
        self._logger = logger
        self._alert_url = alert_url
        self._digest_report_path = digest_report_path
        self._openings_page_path = openings_page_path
        self._filters = tuple(filters)
        self._companies = tuple(companies)
        # The page is written on the first wake, after any cycle that changed the board, and on
        # every heartbeat, so it is never more than five minutes stale.
        self._openings_dirty = True
        self._wake_interval_seconds = float(wake_interval_seconds)
        self._heartbeat_interval_seconds = float(heartbeat_interval_seconds)
        self._digest_hour = digest_hour
        self._digest_minute = digest_minute
        self._timezone = timezone
        self._sources = tuple(sources)
        self._enabled_sources = tuple(
            sorted((source for source in sources if source.enabled), key=lambda s: s.id)
        )
        self._source_urls = {source.id: str(source.endpoint) for source in sources}
        self._company_names = {company.id: company.name for company in companies}
        self._global_semaphore = asyncio.Semaphore(GLOBAL_CONCURRENCY)
        self._host_semaphores: dict[str, asyncio.Semaphore] = {}
        self._in_flight: dict[str, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

        started = _as_utc(clock())
        self._next_heartbeat_at = started + timedelta(seconds=self._heartbeat_interval_seconds)
        self._next_digest_at = next_digest_at(
            started, timezone=timezone, hour=digest_hour, minute=digest_minute
        )

    # ------------------------------------------------------------------ the loop

    async def run(
        self, *, stop: asyncio.Event | None = None, max_wakes: int | None = None
    ) -> SchedulerReport:
        """Wake on the schedule until stopped, then shut down gracefully.

        ``max_wakes`` bounds the loop for an operator who wants a single cycle or a short
        rehearsal; leaving it out runs until ``stop`` is set, which is what SIGINT and SIGTERM do.
        """
        stop = asyncio.Event() if stop is None else stop
        started = _as_utc(self._clock())
        wakes: list[WakeReport] = []
        try:
            while not stop.is_set():
                moment = _as_utc(self._clock())
                wakes.append(await self.wake(now=moment))
                if max_wakes is not None and len(wakes) >= max_wakes:
                    break
                if stop.is_set():
                    break
                await self._pause(self._pause_seconds(moment), stop)
            await self.wait_for_idle()
        finally:
            await self.close()
        return SchedulerReport(
            started_at=started, finished_at=_as_utc(self._clock()), wakes=tuple(wakes)
        )

    async def wake(self, *, now: datetime | None = None) -> WakeReport:
        """One pass of the loop: dispatch what is due, then the heartbeat and the digest."""
        moment = _as_utc(self._clock() if now is None else now)
        due, blocked = self._due_now(moment)
        in_flight = tuple(source.id for source in due if source.id in self._in_flight)
        for source in due:
            if source.id not in self._in_flight:
                self._start_collection(source)
        heartbeat = (
            await self.heartbeat(now=moment) if moment >= self._next_heartbeat_at else None
        )
        digest = await self.send_digest(now=moment) if moment >= self._next_digest_at else None
        if self._openings_dirty or heartbeat is not None:
            self._write_openings(moment)
        return WakeReport(
            now=moment,
            dispatched=tuple(source.id for source in due if source.id not in in_flight),
            deferred_in_flight=in_flight,
            deferred_for_backoff=tuple(blocked),
            heartbeat=heartbeat,
            digest=digest,
        )

    def _write_openings(self, moment: datetime) -> None:
        """Rewrite the openings page; a failure is logged, never fatal to the loop."""
        self._openings_dirty = False
        if self._openings_page_path is None:
            return
        try:
            write_openings_page(
                self._openings_page_path,
                self._repository,
                sources=self._sources,
                filters=self._filters,
                companies=self._companies,
                now=moment,
            )
        except Exception as exc:  # the page is a convenience; collection must go on
            self._log(f"openings page not written: {type(exc).__name__}: {exc}")
        try:
            write_recent_page(
                self._openings_page_path.with_name(RECENT_PAGE_NAME),
                self._repository,
                filters=self._filters,
                companies=self._companies,
                now=moment,
            )
        except Exception as exc:
            self._log(f"last-24-hours page not written: {type(exc).__name__}: {exc}")

    async def heartbeat(self, *, now: datetime | None = None) -> HeartbeatOutcome:
        """Evaluate source health, push what it earned, and retry anything still undelivered."""
        moment = _as_utc(self._clock() if now is None else now)
        self._next_heartbeat_at = moment + timedelta(seconds=self._heartbeat_interval_seconds)
        outcome = evaluate_heartbeat(
            self._repository,
            self._sources,
            companies=self._company_names,
            now=moment,
            in_flight=tuple(self._in_flight),
        )
        if outcome.events:
            await self._push_health(outcome.events)
            for event in outcome.events:
                self._log(f"health: {event.title}: {event.body}")
        if outcome.pending_deliveries:
            self._log(
                f"heartbeat: {outcome.pending_deliveries} event(s) are still undelivered;"
                " re-attempting the ones whose recorded backoff has elapsed"
            )
            await self._retry_pending(moment)
        return outcome

    async def send_digest(self, *, now: datetime | None = None) -> DigestDelivery:
        """Build the digest, write its report, and hand it to the notifier when there is one."""
        moment = _as_utc(self._clock() if now is None else now)
        self._next_digest_at = next_digest_at(
            moment + timedelta(seconds=1),
            timezone=self._timezone,
            hour=self._digest_hour,
            minute=self._digest_minute,
        )
        window_start = moment - DIGEST_WINDOW
        digest = build_digest(
            self._repository,
            self._sources,
            companies=self._company_names,
            now=moment,
            window_start=window_start,
            timezone=self._timezone,
        )
        message = digest_message(digest, alert_url=self._alert_url)
        delivered = False
        if self._notifier is not None:
            result = await self._notifier.send(message)
            delivered = result.delivered
        if self._digest_report_path is not None:
            self._digest_report_path.parent.mkdir(parents=True, exist_ok=True)
            self._digest_report_path.write_text(digest.markdown, encoding="utf-8")
        delivery = DigestDelivery(
            generated_at=moment,
            window_start=window_start,
            digest=digest,
            message=message,
            report_path=self._digest_report_path,
            delivered=delivered,
        )
        self._log(f"daily digest: {delivery.summary}")
        return delivery

    async def wait_for_idle(self, *, timeout: float | None = None) -> None:
        """Wait for the collections already in flight, then give up on whatever is left."""
        allowance = SHUTDOWN_GRACE_SECONDS if timeout is None else timeout
        outstanding = tuple(task for task in self._tasks if not task.done())
        if not outstanding:
            return
        _, pending = await asyncio.wait(outstanding, timeout=allowance)
        if not pending:
            return
        self._log(f"shutdown: {len(pending)} collection(s) did not finish within {allowance:.0f}s")
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def close(self) -> None:
        """Close the HTTP fetcher and the database this scheduler was handed."""
        if self._closed:
            return
        self._closed = True
        if self._fetcher is not None:
            await self._fetcher.close()
        self._repository.close()

    # ------------------------------------------------------------------ dispatch

    def _due_now(self, moment: datetime) -> tuple[list[SourceConfig], list[str]]:
        """The enabled sources due at this instant, and the ones held back by their own backoff."""
        due: list[SourceConfig] = []
        blocked: list[str] = []
        for source in self._enabled_sources:
            state = self._repository.source_health(source.id)
            if (
                state is not None
                and state.backoff_until is not None
                and _as_utc(state.backoff_until) > moment
            ):
                blocked.append(source.id)
                continue
            if state is None or state.last_complete_at is None:
                due.append(source)
                continue
            if next_due_at(source.id, state.last_complete_at, source.interval_minutes) <= moment:
                due.append(source)
        return due, blocked

    def _start_collection(self, source: SourceConfig) -> asyncio.Task[None]:
        """Dispatch one collection, recording it as in flight until its task finishes."""
        task = asyncio.ensure_future(self._collect(source))
        self._track(source.id, task)
        return task

    def _start_delivery(self, source: SourceConfig) -> asyncio.Task[None]:
        """Dispatch one delivery-only pass, under the same in-flight guard as a collection."""
        task = asyncio.ensure_future(self._deliver_pending(source))
        self._track(source.id, task)
        return task

    def _track(self, source_id: str, task: asyncio.Task[None]) -> None:
        """Register a dispatch, and forget it the moment it finishes however it finishes."""
        self._in_flight[source_id] = task
        self._tasks.add(task)

        def forget(finished: asyncio.Task[None], *, source_id: str = source_id) -> None:
            self._tasks.discard(finished)
            if self._in_flight.get(source_id) is finished:
                del self._in_flight[source_id]

        task.add_done_callback(forget)

    def _host_semaphore(self, host: str) -> asyncio.Semaphore:
        """The per-host limit, created the first time a host is seen."""
        semaphore = self._host_semaphores.get(host)
        if semaphore is None:
            semaphore = asyncio.Semaphore(PER_HOST_CONCURRENCY)
            self._host_semaphores[host] = semaphore
        return semaphore

    async def _collect(self, source: SourceConfig) -> None:
        """Collect one source inside both limits, then hold it back if the cycle went badly.

        The per-host limit is taken before the global one on purpose: taking the global slot first
        would let eight sources from one host wait on that host's two slots while holding every slot
        there is, which starves the other hosts instead of bounding them.
        """
        try:
            async with self._host_semaphore(source_host(source)), self._global_semaphore:
                result = await self._service.collect_source(source, mode=self._mode)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # one source can never take the loop down with it
            self._log(f"{source.id}: collection failed: {type(exc).__name__}: {exc}")
            return
        self._log(_cycle_line(result))
        if result.new_count or result.reopened_count or result.closed_count:
            self._openings_dirty = True
        self._record_backoff(source, result)

    async def _deliver_pending(self, source: SourceConfig) -> None:
        """Re-attempt one source's undelivered events without reading its board again."""
        try:
            async with self._global_semaphore:
                sent, would_deliver = await self._service.deliver_pending(source)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log(f"{source.id}: retry failed: {type(exc).__name__}: {exc}")
            return
        if sent or would_deliver:
            self._log(f"{source.id}: retried deliveries: {sent} sent, {len(would_deliver)} due")

    def _record_backoff(self, source: SourceConfig, result: CycleResult) -> None:
        """Hold a failing source back instead of spending a collection on every wake.

        A complete run already cleared the instant, and a diagnostic run is the operator's own
        request rather than evidence about the source, so neither one backs off. Everything else
        waits at least its own interval, and longer as the streak grows, up to the shared cap.
        """
        if result.outcome is RunOutcome.COMPLETE or result.error_code in DIAGNOSTIC_ERROR_CODES:
            return
        state = self._repository.source_health(source.id)
        streak = 1 if state is None else max(1, state.consecutive_failures)
        delay = max(float(source.interval_minutes) * 60.0, retry_backoff_seconds(streak))
        until = _as_utc(self._clock()) + timedelta(seconds=delay)
        self._repository.record_backoff(source.id, until)
        self._log(f"{source.id}: {result.outcome} ({result.error_code}); no poll before {until}")

    # ------------------------------------------------------------------ notices and helpers

    async def _push_health(self, events: Sequence[HealthEvent]) -> None:
        """Push health events through the notifier, when one was configured."""
        if self._notifier is None:
            return
        await send_health_events(
            self._notifier, events, source_urls=self._source_urls, alert_url=self._alert_url
        )

    async def _retry_pending(self, moment: datetime) -> None:
        """Re-attempt every enabled source whose recorded backoff has elapsed."""
        tasks = []
        for source in self._enabled_sources:
            if source.id in self._in_flight:
                continue
            pending = self._repository.pending_events(
                source.id, event_types=DELIVERABLE_EVENT_TYPES, now=moment
            )
            if pending:
                tasks.append(self._start_delivery(source))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _pause_seconds(self, moment: datetime) -> float:
        """How long to sleep: the wake interval, unless a heartbeat or the digest is closer."""
        until_heartbeat = (self._next_heartbeat_at - moment).total_seconds()
        until_digest = (self._next_digest_at - moment).total_seconds()
        return max(0.0, min(self._wake_interval_seconds, until_heartbeat, until_digest))

    async def _pause(self, seconds: float, stop: asyncio.Event) -> None:
        """Sleep until the next wake, or until a stop arrives, whichever comes first."""
        if self._pause_callable is not None:
            await self._pause_callable(seconds, stop)
            return
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except TimeoutError:
            return

    def _log(self, line: str) -> None:
        """Report one operational line, if the caller asked for logging at all."""
        if self._logger is not None:
            self._logger(line)


def evaluate_heartbeat(
    repository: Repository,
    sources: Sequence[SourceConfig],
    *,
    companies: Mapping[str, str] | None = None,
    now: datetime,
    in_flight: Collection[str] = (),
) -> HeartbeatOutcome:
    """Apply the health policy to every enabled source that did not run, and count what is pending.

    The heartbeat is deliberately quiet about sources that are fine:
    :func:`evaluate_source_heartbeat` returns the stored state untouched while the freshness window
    holds, and only the sources whose state actually moves are reported. Nothing here touches job
    state.

    A source in ``in_flight`` is skipped entirely, health and pending deliveries alike. That is
    what keeps one incident opened exactly once under concurrency: a collection that outlives a wake
    already read the state it will write from, so a health write landing underneath it would be
    clobbered by the run's own absolute evaluation -- reopening a recovery, or closing an incident
    that was never announced. Skipping is enough on its own because every read and write below runs
    in one event-loop step with no await between them, and because the scheduler only clears the
    in-flight mark *after* the collection has written its health, the heartbeat's next pass sees the
    settled state. A skipped source's pending events are not counted either: its collection is about
    to deliver them at the end of the cycle it is in the middle of.
    """
    names = {} if companies is None else companies
    busy = set(in_flight)
    incidents: list[HealthEvent] = []
    recoveries: list[HealthEvent] = []
    pending = 0
    deferred: list[str] = []
    for source in sorted((source for source in sources if source.enabled), key=lambda s: s.id):
        if source.id in busy:
            deferred.append(source.id)
            continue
        state = repository.source_health(source.id)
        if state is not None:
            health = SourceHealthState.from_source_state(
                state,
                company_id=source.company_id,
                company_name=names.get(source.company_id, ""),
                interval_minutes=source.interval_minutes,
            )
            evaluation = evaluate_source_heartbeat(health, now=now, last_run_at=state.last_run_at)
            if evaluation.state is not health:
                repository.record_health_state(source.id, evaluation.state)
                for event in evaluation.events:
                    if event.kind is HealthEventKind.SOURCE_FAILURE:
                        incidents.append(event)
                    else:
                        recoveries.append(event)
        pending += len(repository.pending_events(source.id, event_types=DELIVERABLE_EVENT_TYPES))
    return HeartbeatOutcome(
        evaluated_at=_as_utc(now),
        incidents=tuple(incidents),
        recoveries=tuple(recoveries),
        pending_deliveries=pending,
        deferred_in_flight=tuple(deferred),
    )


def build_digest(
    repository: Repository,
    sources: Sequence[SourceConfig],
    *,
    companies: Mapping[str, str] | None = None,
    now: datetime,
    window_start: datetime,
    provider: str = "pushover",
    timezone: str = CHICAGO_TIMEZONE,
) -> DailyDigest:
    """Join the registry with stored state and one window of history into a daily digest.

    Every source is handed to the digest, enabled or not: the policy counts only the enabled ones
    for freshness, and reports the rest by the support status that explains why they are quiet.
    """
    names = {} if companies is None else companies
    digest_sources = [
        DigestSource.from_parts(
            source,
            company_name=names.get(source.company_id, ""),
            state=repository.source_health(source.id),
        )
        for source in sources
    ]
    return build_daily_digest(
        now=now,
        window_start=window_start,
        sources=digest_sources,
        counts=repository.window_counts(window_start, now),
        quota=repository.latest_quota(provider),
        timezone=timezone,
    )


def digest_message(
    digest: DailyDigest, *, alert_url: str = DEFAULT_ALERT_URL
) -> NotificationMessage:
    """The digest as one push: the report itself is the body, so the counts travel with it."""
    return NotificationMessage(
        title=DIGEST_TITLE,
        body=digest.text,
        url=alert_url,
        url_title="Fast Job Alert",
    )


async def send_health_events(
    notifier: Notifier,
    events: Sequence[HealthEvent],
    *,
    source_urls: Mapping[str, str] | None = None,
    alert_url: str = DEFAULT_ALERT_URL,
) -> int:
    """Push every health event once, returning how many the provider accepted.

    Health pushes are never job alerts: nothing here reads or writes a posting, an event, or a
    delivery attempt, so an incident cannot mark a job as announced or consume a retry budget.
    """
    urls = {} if source_urls is None else source_urls
    delivered = 0
    for event in events:
        message = health_message(event, source_url=urls.get(event.source_id, alert_url))
        result = await notifier.send(message)
        if result.delivered:
            delivered += 1
    return delivered


def digest_payload(digest: DailyDigest) -> dict[str, Any]:
    """The digest as JSON-ready data, which is what ``report health --json`` prints.

    The retry policy is reported as an object as well as prose because it is the part an operator
    reads to answer "what is the monitor allowed to do next": which limit applies, and whether the
    recorded delay before a retry is enforced.
    """
    quota = digest.quota
    return {
        "generated_at": digest.generated_at.isoformat(),
        "window_start": digest.window_start.isoformat(),
        "timezone": digest.timezone,
        "total_enabled_sources": digest.total_enabled_sources,
        "sources_complete": digest.sources_complete,
        "sources_stale": digest.sources_stale,
        "sources_awaiting_success": digest.sources_awaiting_success,
        "sources_never_polled": digest.sources_never_polled,
        "open_incidents": [
            {
                "source_id": event.source_id,
                "company_id": event.company_id,
                "company_name": event.company_name,
                "kind": str(event.kind),
                "reason": str(event.reason),
                "opened_at": None if event.opened_at is None else event.opened_at.isoformat(),
                "first_failure_at": (
                    None if event.first_failure_at is None else event.first_failure_at.isoformat()
                ),
                "last_complete_at": (
                    None if event.last_complete_at is None else event.last_complete_at.isoformat()
                ),
                "consecutive_failures": event.consecutive_failures,
                "last_error": event.last_error,
            }
            for event in digest.open_incidents
        ],
        "blocked_companies": list(digest.blocked_companies),
        "manual_review_companies": list(digest.manual_review_companies),
        "counts": asdict(digest.counts),
        "quota": (
            None
            if quota is None
            else {
                "provider": quota.provider,
                "limit": quota.limit,
                "remaining": quota.remaining,
                "reset_at": None if quota.reset_at is None else quota.reset_at.isoformat(),
                "observed_at": quota.observed_at.isoformat(),
            }
        ),
        "retry_policy": {
            "enforced": True,
            "max_attempts_per_event": DEFAULT_MAX_RETRYABLE_ATTEMPTS,
            "backoff_base_seconds": RETRY_BACKOFF_BASE_SECONDS,
            "backoff_max_seconds": RETRY_BACKOFF_MAX_SECONDS,
            "summary": digest.retry_policy,
        },
        "markdown": digest.markdown,
    }


def _cycle_line(result: CycleResult) -> str:
    """One collection as an operator log line."""
    return (
        f"{result.source_id}: {result.outcome} new={result.new_count}"
        f" updated={result.updated_count} reopened={result.reopened_count}"
        f" missing={result.missing_count} closed={result.closed_count}"
        f" notifications={result.notifications_sent}"
        + (f" error={result.error_code}" if result.error_code else "")
        + (" seeded (first complete snapshot, no events)" if result.seeded else "")
    )


def _as_utc(moment: datetime) -> datetime:
    """A timestamp read as UTC when it is naive, the way every stored instant is read."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
