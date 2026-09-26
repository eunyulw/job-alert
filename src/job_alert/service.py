"""One-source collection cycles: fetch, validate, compare, commit, and deliver.

A cycle is one source's turn in the monitor. :class:`JobAlertService` resolves that source's
adapter, fetches and validates one collection, classifies it against the state a previous complete
snapshot left behind, commits what it learned, and only then talks to a notification provider.

Run modes
---------

``DRY_RUN``
    Fetch, parse, and compare. The parsed collection is reported, but no job row, observation,
    event, validator, or delivery is written. The run row is the diagnostic the operator keeps.
``BASELINE``
    Persist jobs, observations, absence, and reopening state, and stage no event at all, so seeding
    a source can never flood the phone with the postings that were already there.
``LIVE_SUPPRESSED``
    Persist state and durable events, evaluate filters, and report every event that would be
    delivered without calling a provider.
``LIVE``
    Persist state and durable events, then deliver every matching event that no earlier attempt
    resolved — the ones this cycle just created and any still pending from earlier cycles.

Order of operations
-------------------

One cycle runs strictly in this order: open the poll run; fetch through the adapter; validate the
collection; compare it against stored job state; decide quarantine; **commit** observations, the
run outcome, absence/reopening state, and the durable events in a single transaction; install new
request validators; and finally evaluate filters and deliver. Nothing external happens before the
commit, so a crash between the two loses a notification attempt rather than the event that
describes it: the next cycle reads the unsent event from the database and tries again.

Safety rules this module enforces
---------------------------------

* Only a validated complete collection reaches the commit. A failed, challenged, truncated,
  partial, or quarantined fetch leaves job state exactly as it was, so it can neither close a job
  nor announce a false new one.
* Absence and reopening decisions are handed to ``apply_complete_snapshot()``, which refuses any
  run that is not finished ``complete``; both ``diff.missing`` and ``diff.closed`` are forwarded so
  the stored absence streak sees every absent key.
* A complete candidate that drops more than half of a previous snapshot of at least 20 jobs is
  withheld as ``quarantined`` until a second, independently fetched complete collection reproduces
  the same canonical-key set.
* One detection instant is captured per cycle and reused for the stored event, the filter decision,
  and every retry, so a quiet-hours deferral cannot move when the event is reconsidered.
* A new or reopened posting is announced at most once per public posting: before an event is
  created the service looks up the posting's dedupe key against stored active jobs and delivered
  alerts, while still storing and observing every source record that carries it.
* Delivery retries live here, not in the notifier. A retryable failure leaves its events pending for
  the next cycle; an *uncertain* failure — one where the provider may already have accepted the
  message — is retried at most :data:`DEFAULT_MAX_UNCERTAIN_RETRIES` times and then abandoned, so an
  ambiguous outcome cannot repeat forever. A permanent failure ends the question immediately.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast

from job_alert.adapters.base import (
    AdapterError,
    PartialCollectionError,
    validate_collection,
)
from job_alert.adapters.registry import AdapterRegistry
from job_alert.config import FilterConfig, FilterPriority
from job_alert.db import (
    DEFAULT_CLOSE_AFTER,
    DEFAULT_MAX_UNCERTAIN_RETRIES,
    AppliedSnapshot,
    DeliveryOutcome,
    PendingEvent,
    QuarantinedSnapshot,
    Repository,
)
from job_alert.diff import SnapshotDiff, compare_snapshot
from job_alert.fetch import (
    ChallengeDetected,
    FetchFailure,
    HttpFetcher,
    HttpPayload,
    RequestSpec,
)
from job_alert.filters import evaluate_filters
from job_alert.models import (
    CollectionResult,
    CompanyTarget,
    JobEvent,
    JobEventType,
    NormalizedJob,
    RunOutcome,
    SourceConfig,
)
from job_alert.notifications.base import (
    DEFAULT_GROUP_WINDOW_SECONDS,
    AlertBatch,
    AlertEvent,
    DeliveryResult,
    Notifier,
    group_job_events,
)

#: A previous complete snapshot must hold at least this many jobs before a drop is suspicious.
LARGE_DROP_MINIMUM_JOBS = 20

#: The event types a new cycle may deliver; an update is recorded but never alerted on.
DELIVERABLE_EVENT_TYPES: tuple[JobEventType, ...] = (JobEventType.NEW, JobEventType.REOPENED)

_PRIORITY_ORDER: Mapping[FilterPriority, int] = {
    FilterPriority.HIGH: 0,
    FilterPriority.NORMAL: 1,
    FilterPriority.LOW: 2,
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RunMode(StrEnum):
    """What one collection cycle is allowed to change."""

    DRY_RUN = "dry-run"
    BASELINE = "baseline"
    LIVE_SUPPRESSED = "live-suppressed"
    LIVE = "live"


class ConditionalNotModified(Exception):
    """The provider answered ``304 Not Modified`` to a request that carried our validators.

    It is raised by the service's fetcher wrapper, never by a provider adapter: an adapter builds
    its own ``RequestSpec`` and would report a ``304`` as an unusable body. Catching it lets the
    service treat the run as a complete no-change cycle built from the last applied snapshot.
    """


@dataclass(frozen=True, slots=True)
class WouldDeliver:
    """One event a filter authorized, reported for suppressed runs and operators."""

    event_id: int
    event_type: JobEventType
    canonical_job_key: str
    company_id: str
    title: str
    posting_url: str
    priority: FilterPriority
    reason: str
    defer_until: datetime | None = None
    deferred: bool = False


@dataclass(frozen=True, slots=True)
class CycleResult:
    """What one collection cycle did, and what it deliberately did not do.

    ``outcome`` is the outcome stored on the poll run, while ``complete`` reports whether the
    fetched collection itself validated as complete. The two differ for exactly one mode: a dry run
    validates a complete collection but is stored as a non-complete run, because a run that stages
    no observations must never become the snapshot the absence logic reasons about.
    ``new_count`` and friends describe the candidate snapshot even when it was withheld as
    quarantined, where ``closed_count`` stays 0 because nothing was applied.
    """

    source_id: str
    mode: RunMode
    run_id: int
    outcome: RunOutcome
    complete: bool
    new_count: int = 0
    updated_count: int = 0
    reopened_count: int = 0
    unchanged_count: int = 0
    missing_count: int = 0
    closed_count: int = 0
    notifications_sent: int = 0
    would_deliver: tuple[WouldDeliver, ...] = ()
    suppressed_duplicates: tuple[str, ...] = ()
    quarantined: bool = False
    not_modified: bool = False
    warnings: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None


class ConditionalFetcher:
    """A fetcher that offers the source's stored validators to its first ``GET`` request.

    Provider adapters own their request policy and build their own ``RequestSpec``s, so the stored
    ``ETag``/``Last-Modified`` values are supplied here rather than by changing an adapter. Only the
    first ``GET`` carries them, and only when the last applied snapshot came from a single request:
    a validator for page one of a paginated listing does not describe the whole board, so a ``304``
    on it could not stand for "nothing changed". A ``304`` on the request that *did* carry the
    validators raises :class:`ConditionalNotModified` instead of being handed to an adapter that
    would only report an unusable body.

    The service hands this to ``adapter.collect()`` as an ``HttpFetcher`` cast. It mirrors that
    class's whole public surface (``get``, ``post``, ``request``, ``send_once``, ``close``, and the
    async context manager) and delegates every call to the fetcher the service owns.
    """

    def __init__(
        self,
        fetcher: HttpFetcher,
        *,
        etag: str | None,
        last_modified: str | None,
        enabled: bool,
    ) -> None:
        self._fetcher = fetcher
        self._etag = etag
        self._last_modified = last_modified
        self._enabled = enabled and bool(etag or last_modified)
        self._attached = False
        self.first_payload: HttpPayload | None = None

    async def __aenter__(self) -> ConditionalFetcher:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        """The service owns the wrapped fetcher, so leaving this wrapper closes nothing."""

    async def get(self, spec: RequestSpec) -> HttpPayload:
        return await self.request(replace(spec, method="GET"))

    async def post(self, spec: RequestSpec) -> HttpPayload:
        return await self.request(replace(spec, method="POST"))

    async def request(self, spec: RequestSpec) -> HttpPayload:
        attached = self._should_attach(spec)
        if attached:
            self._attached = True
            spec = replace(spec, etag=self._etag, last_modified=self._last_modified)
        payload = await self._fetcher.request(spec)
        if self.first_payload is None:
            self.first_payload = payload
        if attached and payload.not_modified:
            raise ConditionalNotModified(f"{spec.url} answered 304 Not Modified")
        return payload

    async def send_once(self, spec: RequestSpec) -> HttpPayload:
        return await self._fetcher.send_once(spec)

    async def close(self) -> None:
        """The service owns the wrapped fetcher and closes it itself."""

    def _should_attach(self, spec: RequestSpec) -> bool:
        return self._enabled and not self._attached and spec.method == "GET"


class JobAlertService:
    """Run one source's collection cycle against the durable store.

    The service owns no resources it did not receive: the repository, registry, and fetcher belong
    to the caller, so a scheduler can share one fetcher and one database connection across sources
    and decide their lifetimes itself.
    """

    def __init__(
        self,
        repository: Repository,
        *,
        registry: AdapterRegistry,
        fetcher: HttpFetcher,
        filters: Sequence[FilterConfig] = (),
        companies: Sequence[CompanyTarget] = (),
        notifier: Notifier | None = None,
        close_after: int = DEFAULT_CLOSE_AFTER,
        group_window_seconds: int = DEFAULT_GROUP_WINDOW_SECONDS,
        max_uncertain_retries: int = DEFAULT_MAX_UNCERTAIN_RETRIES,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        if close_after < 1:
            raise ValueError(f"close_after must be at least 1, got {close_after}")
        if group_window_seconds < 1:
            raise ValueError("group_window_seconds must be positive")
        if max_uncertain_retries < 1:
            raise ValueError("max_uncertain_retries must be at least 1")
        self._repository = repository
        self._registry = registry
        self._fetcher = fetcher
        self._filters = tuple(filters)
        self._categories = {
            company.id: tuple(company.categories) for company in companies
        }
        self._company_names = {company.id: company.name for company in companies}
        self._notifier = notifier
        self._close_after = close_after
        self._group_window_seconds = group_window_seconds
        self._max_uncertain_retries = max_uncertain_retries
        self._clock = clock

    async def collect_source(
        self, source: SourceConfig, mode: RunMode = RunMode.LIVE
    ) -> CycleResult:
        """Fetch one source, commit what the snapshot proves, and deliver what it earned."""
        if mode is RunMode.LIVE and self._notifier is None:
            raise ValueError("LIVE mode delivers through a notifier; none was configured")

        started_at = self._clock()
        # One detection instant for the whole cycle: the stored event, its filter decision, and
        # every later retry read this value, so a deferral cannot drift between attempts.
        detected_at = started_at
        run_id = self._repository.start_run(source.id, started_at)

        try:
            adapter = self._registry.get(source.provider)
        except AdapterError as exc:
            return self._finish_unsuccessful(
                run_id,
                source,
                mode,
                RunOutcome.FAILED,
                code="unsupported_provider",
                message=str(exc),
            )

        health = self._repository.source_health(source.id)
        applied = self._repository.last_applied_snapshot(source.id)
        conditional = ConditionalFetcher(
            self._fetcher,
            etag=None if health is None else health.etag,
            last_modified=None if health is None else health.last_modified,
            enabled=applied is not None and applied.page_count == 1,
        )

        collection: CollectionResult | None = None
        try:
            collection = await adapter.collect(source, cast("HttpFetcher", conditional))
            valid = validate_collection(collection)
        except ConditionalNotModified:
            return await self._commit_unchanged(
                run_id, source, mode, applied=applied, conditional=conditional
            )
        except ChallengeDetected as exc:
            return self._finish_unsuccessful(
                run_id,
                source,
                mode,
                RunOutcome.CHALLENGED,
                code="challenge_detected",
                message=str(exc),
                http_status=self._status(conditional),
            )
        except PartialCollectionError as exc:
            return self._finish_unsuccessful(
                run_id,
                source,
                mode,
                RunOutcome.PARTIAL,
                code="partial_collection",
                message=str(exc),
                jobs_seen=0 if collection is None else len(collection.jobs),
                http_status=self._status(conditional),
            )
        except (AdapterError, FetchFailure) as exc:
            return self._finish_unsuccessful(
                run_id,
                source,
                mode,
                RunOutcome.FAILED,
                code="collection_failed",
                message=str(exc),
                jobs_seen=0 if collection is None else len(collection.jobs),
                http_status=self._status(conditional),
            )

        return await self._commit_validated(
            run_id,
            source,
            mode,
            valid=valid,
            conditional=conditional,
            applied=applied,
            detected_at=detected_at,
        )

    # ------------------------------------------------------------------ collection outcomes

    async def _commit_validated(
        self,
        run_id: int,
        source: SourceConfig,
        mode: RunMode,
        *,
        valid: CollectionResult,
        conditional: ConditionalFetcher,
        applied: AppliedSnapshot | None,
        detected_at: datetime,
    ) -> CycleResult:
        """Compare a complete collection with stored state, then commit it as one transaction."""
        jobs = list(valid.jobs)
        jobs_by_key = {job.canonical_job_key: job for job in jobs}
        previous = self._repository.snapshot_state(source.id)
        current = {
            key: {"content_hash": job.content_hash} for key, job in jobs_by_key.items()
        }
        diff = compare_snapshot(previous, current, complete=True, close_after=self._close_after)
        http_status = self._status(conditional)

        previous_count = 0 if applied is None else len(applied.canonical_job_keys)
        pending_quarantine = self._repository.pending_quarantine(source.id)
        large_drop = self._is_large_drop(previous_count, len(jobs))
        confirming = self._confirms_quarantine(pending_quarantine, jobs_by_key)

        if mode is RunMode.DRY_RUN:
            warnings = (self._drop_warning(previous_count, len(jobs)),) if large_drop else ()
            return self._finish_dry_run(
                run_id,
                source,
                mode,
                diff=diff,
                jobs_seen=len(jobs),
                http_status=http_status,
                page_count=valid.page_count,
                declared_total=valid.declared_total,
                warnings=warnings,
            )

        if large_drop and not confirming:
            return self._quarantine(
                run_id,
                source,
                mode,
                diff=diff,
                jobs_by_key=jobs_by_key,
                previous_count=previous_count,
                http_status=http_status,
                page_count=valid.page_count,
                declared_total=valid.declared_total,
            )

        events, suppressed = self._plan_events(
            diff, jobs_by_key, detected_at=detected_at, mode=mode
        )
        with self._repository.transaction():
            self._repository.upsert_observations(run_id, jobs)
            self._repository.finish_run(
                run_id,
                RunOutcome.COMPLETE,
                self._clock(),
                jobs_seen=len(jobs),
                http_status=http_status,
                page_count=valid.page_count,
                declared_total=valid.declared_total,
            )
            closed = self._repository.apply_complete_snapshot(
                run_id,
                missing_keys=[*diff.missing, *diff.closed],
                reopened_keys=diff.reopened,
                close_after=self._close_after,
            )
            self._repository.record_events(run_id, events)
            if pending_quarantine is not None:
                self._repository.clear_quarantine(source.id)

        self._persist_validators(source.id, conditional, page_count=valid.page_count)
        sent, would_deliver = await self._deliver(source, mode=mode, now=self._clock())
        new, updated, reopened, unchanged, missing, closed_count = _counts(diff, closed=len(closed))
        return CycleResult(
            source_id=source.id,
            mode=mode,
            run_id=run_id,
            outcome=RunOutcome.COMPLETE,
            complete=True,
            new_count=new,
            updated_count=updated,
            reopened_count=reopened,
            unchanged_count=unchanged,
            missing_count=missing,
            closed_count=closed_count,
            notifications_sent=sent,
            would_deliver=would_deliver,
            suppressed_duplicates=suppressed,
        )

    async def _commit_unchanged(
        self,
        run_id: int,
        source: SourceConfig,
        mode: RunMode,
        *,
        applied: AppliedSnapshot | None,
        conditional: ConditionalFetcher,
    ) -> CycleResult:
        """Finish a conditional ``304`` as a complete cycle that observed no change.

        The board the provider confirmed is the one the last applied snapshot recorded, so the run
        re-stages exactly those observations. The run is therefore an ordinary complete snapshot:
        it advances absence streaks for anything the applied snapshot no longer holds, and it stages
        nothing else. Because that board is the one already applied, the provider has just shown a
        withheld drop is no longer there, so any pending quarantine is cleared. Without an applied
        snapshot there is no board to reuse and the run fails closed instead of being read as an
        empty one.
        """
        if applied is None:
            return self._finish_unsuccessful(
                run_id,
                source,
                mode,
                RunOutcome.FAILED,
                code="not_modified_without_snapshot",
                message=(
                    "the provider answered 304 Not Modified but no complete snapshot has been"
                    " applied to this source, so there is no board to reuse"
                ),
                http_status=self._status(conditional),
            )

        jobs = [
            job
            for key in sorted(applied.canonical_job_keys)
            if (job := self._repository.load_job(key)) is not None
        ]
        previous = self._repository.snapshot_state(source.id)
        current = {
            key: {"content_hash": previous.get(key, {}).get("content_hash")}
            for key in applied.canonical_job_keys
        }
        diff = compare_snapshot(previous, current, complete=True, close_after=self._close_after)
        pending_quarantine = self._repository.pending_quarantine(source.id)

        if mode is RunMode.DRY_RUN:
            return self._finish_dry_run(
                run_id,
                source,
                mode,
                diff=diff,
                jobs_seen=len(jobs),
                http_status=self._status(conditional),
                page_count=applied.page_count,
                declared_total=None,
                not_modified=True,
            )

        with self._repository.transaction():
            self._repository.upsert_observations(run_id, jobs)
            self._repository.finish_run(
                run_id,
                RunOutcome.COMPLETE,
                self._clock(),
                jobs_seen=len(jobs),
                http_status=self._status(conditional),
                page_count=applied.page_count,
            )
            closed = self._repository.apply_complete_snapshot(
                run_id,
                missing_keys=[*diff.missing, *diff.closed],
                close_after=self._close_after,
            )
            if pending_quarantine is not None:
                self._repository.clear_quarantine(source.id)

        sent, would_deliver = await self._deliver(source, mode=mode, now=self._clock())
        new, updated, reopened, unchanged, missing, closed_count = _counts(diff, closed=len(closed))
        return CycleResult(
            source_id=source.id,
            mode=mode,
            run_id=run_id,
            outcome=RunOutcome.COMPLETE,
            complete=True,
            new_count=new,
            updated_count=updated,
            reopened_count=reopened,
            unchanged_count=unchanged,
            missing_count=missing,
            closed_count=closed_count,
            notifications_sent=sent,
            would_deliver=would_deliver,
            not_modified=True,
        )

    def _finish_dry_run(
        self,
        run_id: int,
        source: SourceConfig,
        mode: RunMode,
        *,
        diff: SnapshotDiff,
        jobs_seen: int,
        http_status: int | None,
        page_count: int,
        declared_total: int | None,
        warnings: tuple[str, ...] = (),
        not_modified: bool = False,
    ) -> CycleResult:
        """Close a dry run as a diagnostic: the collection is complete, the run is not.

        The run is deliberately stored as ``partial``. A complete run with no staged observations
        would become the snapshot the absence logic reads, and an empty snapshot closes jobs; a
        non-complete run can never do that. Nothing else about the source moves either: no job row,
        no observation, no event, no request validator, and no quarantine is created or cleared, so
        a dry run can be repeated freely.
        """
        message = (
            "dry run: a complete collection was fetched and compared without writing job state"
        )
        self._repository.finish_run(
            run_id,
            RunOutcome.PARTIAL,
            self._clock(),
            jobs_seen=jobs_seen,
            http_status=http_status,
            page_count=page_count,
            declared_total=declared_total,
            error_code="dry_run",
            error_message=message,
        )
        new, updated, reopened, unchanged, missing, _ = _counts(diff)
        return CycleResult(
            source_id=source.id,
            mode=mode,
            run_id=run_id,
            outcome=RunOutcome.PARTIAL,
            complete=True,
            new_count=new,
            updated_count=updated,
            reopened_count=reopened,
            unchanged_count=unchanged,
            missing_count=missing,
            warnings=(*warnings, message),
            error_code="dry_run",
            error_message=message,
            not_modified=not_modified,
        )

    def _quarantine(
        self,
        run_id: int,
        source: SourceConfig,
        mode: RunMode,
        *,
        diff: SnapshotDiff,
        jobs_by_key: Mapping[str, NormalizedJob],
        previous_count: int,
        http_status: int | None,
        page_count: int,
        declared_total: int | None,
    ) -> CycleResult:
        """Withhold a candidate that dropped most of the board and persist why.

        Nothing is staged: no job row, no observation, and no event, so a board that half-vanished
        can neither close the jobs the previous snapshot held nor announce the ten postings a
        challenge page happened to show. The candidate's canonical keys are stored so a second,
        independently fetched complete collection can be recognised as confirming it.
        """
        message = self._drop_warning(previous_count, len(jobs_by_key))
        finished_at = self._clock()
        with self._repository.transaction():
            self._repository.finish_run(
                run_id,
                RunOutcome.QUARANTINED,
                finished_at,
                jobs_seen=len(jobs_by_key),
                http_status=http_status,
                page_count=page_count,
                declared_total=declared_total,
                error_code="large_drop_quarantined",
                error_message=message,
            )
            self._repository.record_quarantine(
                run_id,
                source_id=source.id,
                previous_job_count=previous_count,
                candidate_keys=jobs_by_key,
                quarantined_at=finished_at,
            )
        new, updated, reopened, unchanged, missing, _ = _counts(diff)
        return CycleResult(
            source_id=source.id,
            mode=mode,
            run_id=run_id,
            outcome=RunOutcome.QUARANTINED,
            complete=True,
            new_count=new,
            updated_count=updated,
            reopened_count=reopened,
            unchanged_count=unchanged,
            missing_count=missing,
            quarantined=True,
            warnings=(message,),
            error_code="large_drop_quarantined",
            error_message=message,
        )

    def _finish_unsuccessful(
        self,
        run_id: int,
        source: SourceConfig,
        mode: RunMode,
        outcome: RunOutcome,
        *,
        code: str,
        message: str,
        jobs_seen: int = 0,
        http_status: int | None = None,
    ) -> CycleResult:
        """Close a run whose collection never validated, leaving every job row untouched."""
        self._repository.finish_run(
            run_id,
            outcome,
            self._clock(),
            jobs_seen=jobs_seen,
            http_status=http_status,
            error_code=code,
            error_message=message,
        )
        return CycleResult(
            source_id=source.id,
            mode=mode,
            run_id=run_id,
            outcome=outcome,
            complete=False,
            warnings=(message,),
            error_code=code,
            error_message=message,
        )

    # ------------------------------------------------------------------ events and delivery

    def _plan_events(
        self,
        diff: SnapshotDiff,
        jobs_by_key: Mapping[str, NormalizedJob],
        *,
        detected_at: datetime,
        mode: RunMode,
    ) -> tuple[list[JobEvent], tuple[str, ...]]:
        """Choose the durable events one complete snapshot creates, skipping duplicate postings.

        A baseline creates none at all. Every other mode records one event per material change,
        except that a new or reopened posting is only given a *deliverable* event once: the
        posting's dedupe key is looked up against stored active jobs and delivered alerts first, and
        against the keys this run has already claimed, so two boards carrying one public posting
        announce it once. The duplicate is still stored and observed by the caller; only its event
        is skipped. Updates are recorded because the change happened, and are never delivered.
        """
        if mode is RunMode.BASELINE:
            return [], ()

        announced = self._repository.announced_dedupe_keys()
        claimed: set[str] = set()
        events: list[JobEvent] = []
        suppressed: list[str] = []
        candidates = [(key, JobEventType.NEW) for key in diff.new]
        candidates.extend((key, JobEventType.REOPENED) for key in diff.reopened)
        for key, event_type in sorted(candidates):
            job = jobs_by_key[key]
            represented = announced.get(job.dedupe_key, frozenset()) - {key}
            if represented or job.dedupe_key in claimed:
                suppressed.append(key)
                continue
            claimed.add(job.dedupe_key)
            events.append(JobEvent(event_type=event_type, job=job, detected_at=detected_at))
        events.extend(
            JobEvent(
                event_type=JobEventType.UPDATED,
                job=jobs_by_key[key],
                detected_at=detected_at,
            )
            for key in diff.updated
        )
        return events, tuple(suppressed)

    async def _deliver(
        self, source: SourceConfig, *, mode: RunMode, now: datetime
    ) -> tuple[int, tuple[WouldDeliver, ...]]:
        """Report, and in ``LIVE`` send, every pending event a filter authorizes right now.

        The pending set comes from the database rather than from this cycle, which is what makes a
        delivery survive a crash: an event committed before a failed send is simply still pending on
        the next cycle, with the detection instant it was stored with.
        """
        if mode is RunMode.BASELINE or mode is RunMode.DRY_RUN:
            return 0, ()

        reports: list[WouldDeliver] = []
        alerts: list[AlertEvent] = []
        pending_events = self._repository.pending_events(
            source.id, event_types=DELIVERABLE_EVENT_TYPES
        )
        for pending in pending_events:
            report, alert = self._pending_alert(pending, now=now)
            if report is None:
                continue
            reports.append(report)
            if alert is not None:
                alerts.append(alert)

        if mode is RunMode.LIVE_SUPPRESSED:
            return 0, tuple(reports)

        notifier = self._notifier
        if notifier is None:
            raise ValueError("LIVE mode delivers through a notifier; none was configured")
        sent = 0
        for batch in group_job_events(alerts, self._group_window_seconds):
            result = await notifier.send(batch.message)
            self._record_delivery(batch, result, now=now)
            if result.delivered:
                sent += 1
        return sent, tuple(reports)

    def _pending_alert(
        self, pending: PendingEvent, *, now: datetime
    ) -> tuple[WouldDeliver | None, AlertEvent | None]:
        """Filter one pending event at its own detection instant, deferring it when told to."""
        job = self._repository.load_job(pending.canonical_job_key)
        if job is None:
            return None, None
        decisions = evaluate_filters(
            job,
            self._filters,
            company_categories=self._categories.get(job.company_id, ()),
            now=pending.detected_at,
        )
        notifying = [decision for decision in decisions if decision.notify]
        if not notifying:
            return None, None

        best = min(notifying, key=lambda decision: _PRIORITY_ORDER[decision.priority])
        deferrals = [
            decision.defer_until for decision in notifying if decision.defer_until is not None
        ]
        defer_until = max(deferrals) if deferrals else None
        deferred = defer_until is not None and defer_until > now
        report = WouldDeliver(
            event_id=pending.event_id,
            event_type=pending.event_type,
            canonical_job_key=pending.canonical_job_key,
            company_id=job.company_id,
            title=job.title,
            posting_url=str(job.posting_url),
            priority=best.priority,
            reason=best.reason,
            defer_until=defer_until,
            deferred=deferred,
        )
        if deferred:
            return report, None
        return report, AlertEvent(
            event_id=pending.event_id,
            event=JobEvent(
                event_type=pending.event_type,
                job=job,
                detected_at=pending.detected_at,
            ),
            priority=best.priority,
            company_name=self._company_names.get(job.company_id),
        )

    def _record_delivery(
        self, batch: AlertBatch, result: DeliveryResult, *, now: datetime
    ) -> None:
        """Persist one attempt, and abandon the events an ambiguous answer cannot keep retrying."""
        outcome = _delivery_outcome(result)
        self._repository.record_notification(
            result.provider,
            outcome.value,
            now,
            provider_request_id=result.provider_request_id,
            http_status=result.http_status,
            error_message=result.error,
            event_ids=batch.event_ids,
        )
        if outcome is not DeliveryOutcome.UNCERTAIN:
            return
        counts = self._repository.attempt_counts(batch.event_ids, outcome=DeliveryOutcome.UNCERTAIN)
        exhausted = sorted(
            event_id
            for event_id in batch.event_ids
            if counts.get(event_id, 0) > self._max_uncertain_retries
        )
        if not exhausted:
            return
        self._repository.record_notification(
            result.provider,
            DeliveryOutcome.ABANDONED.value,
            now,
            error_message=(
                f"abandoned after {self._max_uncertain_retries + 1} ambiguous attempts: the"
                " provider may already have accepted the message"
            ),
            event_ids=exhausted,
        )

    # ------------------------------------------------------------------ helpers

    def _persist_validators(
        self, source_id: str, conditional: ConditionalFetcher, *, page_count: int
    ) -> None:
        """Store the validators of a trustworthy single-request board, and nothing else.

        A validator is only installed after the response it describes was accepted, and only when
        the whole board arrived in one request. A paginated listing's first page is not the board,
        so storing its validator could later answer ``304`` for a listing that did change.
        """
        payload = conditional.first_payload
        if payload is None or page_count != 1 or payload.not_modified:
            return
        self._repository.record_conditional_state(
            source_id, etag=payload.etag, last_modified=payload.last_modified
        )

    @staticmethod
    def _status(conditional: ConditionalFetcher) -> int | None:
        payload = conditional.first_payload
        return None if payload is None else payload.status

    @staticmethod
    def _is_large_drop(previous_count: int, candidate_count: int) -> bool:
        """Whether a candidate holds less than half of a previous snapshot of at least 20 jobs."""
        return (
            previous_count >= LARGE_DROP_MINIMUM_JOBS and candidate_count * 2 < previous_count
        )

    @staticmethod
    def _confirms_quarantine(
        pending: QuarantinedSnapshot | None, jobs_by_key: Mapping[str, NormalizedJob]
    ) -> bool:
        """Whether this complete collection reproduces the exact board a quarantine withheld."""
        return pending is not None and pending.candidate_keys == frozenset(jobs_by_key)

    @staticmethod
    def _drop_warning(previous_count: int, candidate_count: int) -> str:
        return (
            f"previous complete snapshot held {previous_count} jobs and this collection holds"
            f" {candidate_count}, less than half: the snapshot is quarantined until another"
            " independently fetched complete collection confirms the same canonical job keys"
        )


def _counts(diff: SnapshotDiff, *, closed: int = 0) -> tuple[int, int, int, int, int, int]:
    """``SnapshotDiff`` counts in ``CycleResult`` order, with the closures actually applied."""
    return (
        len(diff.new),
        len(diff.updated),
        len(diff.reopened),
        len(diff.unchanged),
        len(diff.missing) + len(diff.closed),
        closed,
    )


def _delivery_outcome(result: DeliveryResult) -> DeliveryOutcome:
    """Map one provider attempt onto the durable outcome it earns."""
    if result.delivered:
        return DeliveryOutcome.DELIVERED
    if not result.retryable:
        return DeliveryOutcome.PERMANENT
    if result.uncertain:
        return DeliveryOutcome.UNCERTAIN
    return DeliveryOutcome.RETRYABLE
