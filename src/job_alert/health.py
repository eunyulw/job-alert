"""Source health incidents and the daily digest.

Incidents, not a stream
----------------------
Health is deliberately separate from job alerts. A source that cannot be read produces **one**
incident after the policy below is met and **one** recovery when it next completes; every run in
between is suppressed, so a week-long outage is two pushes rather than one per poll. Nothing here
touches job state: an incident never closes a job, and a recovery never reopens one.

When an incident opens
----------------------
Two branches, whichever comes first, and neither one counts a diagnostic run as evidence:

* :data:`FAILURE_INCIDENT_THRESHOLD` consecutive failed, partial, challenged, or quarantined runs;
* staleness -- no complete success for ``2 * interval_minutes + STALE_GRACE_MINUTES`` minutes,
  measured from the last complete success, or from the first failure of the current streak when the
  source has never completed one.

An incident records the source id, company, first failure time, last error, consecutive count,
last complete time, and the branch that opened it. It closes on the next complete success, which
emits exactly one recovery carrying the same record.

Diagnostic runs
---------------
An operator's dry run is stored as a non-complete run on purpose (a complete run with no staged
observations would become the snapshot absence reasoning trusts). Health therefore exempts it by
error code (:data:`job_alert.db.DIAGNOSTIC_ERROR_CODES`) instead of changing what the run row
means: three dry runs on a healthy source leave the streak and incident fields exactly as they
were, while three genuine failures still open the incident. A dry run is not a complete success
either, so it never recovers an open incident.

Where the evaluation fits
-------------------------
:func:`evaluate_source_health` applies one run's outcome to the state as it stood **before** that
run was finished, which is the state ``JobAlertService.collect_source`` reads at the start of a
cycle. Persist the returned state with ``Repository.record_health_state``: that write is the only
one of the streak, the first-failure instant, the last error, and the incident flag, because
``Repository.finish_run`` no longer counts the run as well. Passing the post-run state instead
would advance the streak twice and open an incident one run early.

Where the heartbeat fits
------------------------
:func:`evaluate_source_heartbeat` is the same policy applied to a source that did *not* run: it
opens the staleness incident no run can open, and closes an incident whose recovery a stopped
process never recorded. It never advances the streak, because no run happened to count.

The daily digest
----------------
:func:`build_daily_digest` turns one window of stored history plus the source registry into a
report of enabled sources, complete and stale sources, open incidents, blocked and manual-review
companies, new jobs, matched alerts, failed deliveries, and the remaining Pushover quota when one
was recorded. It takes every instant as an argument, sorts every list, and reads no clock, so the
same inputs produce byte-identical Markdown and plain text.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from job_alert.db import (
    DEFAULT_MAX_RETRYABLE_ATTEMPTS,
    DIAGNOSTIC_ERROR_CODES,
    NOTIFICATION_ATTEMPT_RETENTION_DAYS,
    RETRY_BACKOFF_BASE_SECONDS,
    RETRY_BACKOFF_MAX_SECONDS,
    QuotaSnapshot,
    SourceHealthState,
    SourceState,
    WindowCounts,
)
from job_alert.models import RunOutcome, SourceConfig, SupportStatus
from job_alert.notifications.base import NotificationMessage

#: Consecutive failed, partial, challenged, or quarantined runs that open one incident.
FAILURE_INCIDENT_THRESHOLD = 3

#: A source is stale when no complete success arrived within this multiple of its own interval,
#: plus the grace period below. Both numbers are the brief's, kept named so the digest and the
#: incident policy can never drift apart.
STALE_INTERVAL_MULTIPLIER = 2
STALE_GRACE_MINUTES = 5

#: The report's timezone, and the zone the scheduler is asked to deliver the digest in.
CHICAGO_TIMEZONE = "America/Chicago"

NONE_LINE = "- none"


class HealthEventKind(StrEnum):
    """The two things source health ever pushes."""

    SOURCE_FAILURE = "source_failure"
    SOURCE_RECOVERY = "source_recovery"


class IncidentReason(StrEnum):
    """Which branch of the policy opened an incident. """

    CONSECUTIVE_FAILURES = "consecutive_failures"
    STALE_WITHOUT_COMPLETE_SUCCESS = "stale_without_complete_success"
    COMPLETE_SUCCESS = "complete_success"


@dataclass(frozen=True, slots=True)
class HealthEvent:
    """One incident opened or closed, carrying the record the brief asks an incident to keep.

    ``occurred_at`` is when this event happened (the opening run, or the run that recovered) and
    ``opened_at`` is when the incident itself opened, which a recovery needs in order to report how
    long the source was down. A failure event has the two equal.
    """

    kind: HealthEventKind
    source_id: str
    company_id: str
    company_name: str
    occurred_at: datetime
    reason: IncidentReason
    consecutive_failures: int
    first_failure_at: datetime | None = None
    last_error: str | None = None
    last_complete_at: datetime | None = None
    opened_at: datetime | None = None

    @property
    def company(self) -> str:
        """The name a reader recognizes, falling back to the registry id."""
        return self.company_name or self.company_id

    @property
    def title(self) -> str:
        """The push title for this event, distinct from any job-alert title."""
        if self.kind is HealthEventKind.SOURCE_RECOVERY:
            return f"Source recovered \u2014 {self.company}"
        return f"Source failing \u2014 {self.company}"

    @property
    def body(self) -> str:
        """The push body: the source, the numbers behind the decision, and the last answer."""
        last_complete = (
            "never" if self.last_complete_at is None else _utc_text(self.last_complete_at)
        )
        detail = self.last_error or "no error recorded"
        if self.kind is HealthEventKind.SOURCE_RECOVERY:
            return (
                f"{self.source_id} completed a collection again after"
                f" {self.consecutive_failures} failed runs. Last error: {detail}."
            )
        return (
            f"{self.source_id} has {self.consecutive_failures} consecutive incomplete runs"
            f" ({self.reason}). Last error: {detail}. Last complete: {last_complete}."
        )


@dataclass(frozen=True, slots=True)
class HealthEvaluation:
    """The state to persist after one run, and the zero or one event it earned.

    ``events`` is a list so an evaluation with nothing to push compares equal to ``[]``; the list
    is built per evaluation, so a frozen record still never shares one.
    """

    state: SourceHealthState
    events: list[HealthEvent] = field(default_factory=list)

    @property
    def opened_incident(self) -> HealthEvent | None:
        """The incident this run opened, when it opened one."""
        return next(
            (event for event in self.events if event.kind is HealthEventKind.SOURCE_FAILURE), None
        )

    @property
    def recovery(self) -> HealthEvent | None:
        """The recovery this run emitted, when it closed an incident."""
        return next(
            (event for event in self.events if event.kind is HealthEventKind.SOURCE_RECOVERY), None
        )


def staleness_threshold(interval_minutes: int) -> timedelta:
    """How long a source may go without a complete success before it counts as stale."""
    if interval_minutes < 1:
        raise ValueError(f"interval_minutes must be positive, got {interval_minutes}")
    return timedelta(
        minutes=STALE_INTERVAL_MULTIPLIER * interval_minutes + STALE_GRACE_MINUTES
    )


def health_message(event: HealthEvent, *, source_url: str) -> NotificationMessage:
    """One health push: the incident or recovery, linking the source it describes.

    The title and body are the event's own, so an operator reads the same words in the digest and on
    the phone, and the link is the source endpoint the event is about -- the page worth opening when
    a source is failing or has just recovered. Nothing here touches job state: a health push never
    reads or writes a posting, an event, or a delivery attempt.
    """
    return NotificationMessage(
        title=event.title,
        body=event.body,
        url=source_url,
        url_title="Open source",
    )


def is_stale(
    *,
    now: datetime,
    last_complete_at: datetime | None,
    first_failure_at: datetime | None,
    last_run_at: datetime | None,
    interval_minutes: int | None,
) -> bool:
    """Whether a source has gone too long without a complete success.

    The clock starts at the last complete success when there is one. A source that has never
    completed is measured from the first failure of the streak it is in, then from its last run, so
    "has never worked" and "stopped working" are judged by the same window. With no history at all,
    or no configured interval to multiply, nothing is stale: there is no evidence either way yet.
    """
    if interval_minutes is None or interval_minutes < 1:
        return False
    anchor = last_complete_at or first_failure_at or last_run_at
    if anchor is None:
        return False
    return _as_utc(now) - _as_utc(anchor) > staleness_threshold(interval_minutes)


def evaluate_source_health(
    state: SourceHealthState,
    outcome: RunOutcome,
    *,
    now: datetime,
    error_code: str | None = None,
    error_message: str | None = None,
) -> HealthEvaluation:
    """Apply one run's outcome to a source's health state and report what it earned.

    ``state`` is the state as it stood before this run finished. A diagnostic run
    (:data:`job_alert.db.DIAGNOSTIC_ERROR_CODES`) is returned untouched with no events; a complete
    success clears the streak, closes any open incident, and emits one recovery; anything else
    extends the streak, records the first failure and the last error, and opens one incident when
    the consecutive count reaches :data:`FAILURE_INCIDENT_THRESHOLD` or the staleness branch
    applies. While an incident is open every further non-complete run is suppressed.
    """
    if error_code is not None and error_code in DIAGNOSTIC_ERROR_CODES:
        return HealthEvaluation(state=state)

    if outcome is RunOutcome.COMPLETE:
        complete_at = _as_utc(now)
        events = (
            [_recovery_event(state, complete_at=complete_at)] if state.health_alert_open else []
        )
        return HealthEvaluation(
            state=replace(_recovered_state(state), last_complete_at=complete_at),
            events=events,
        )

    failed_at = _as_utc(now)
    consecutive_failures = state.consecutive_failures + 1
    first_failure_at = state.first_failure_at or failed_at
    last_error = error_message or error_code or str(outcome)
    stale = is_stale(
        now=failed_at,
        last_complete_at=state.last_complete_at,
        first_failure_at=first_failure_at,
        last_run_at=None,
        interval_minutes=state.interval_minutes,
    )
    threshold_reached = consecutive_failures >= FAILURE_INCIDENT_THRESHOLD
    opens = threshold_reached or stale
    reason = (
        IncidentReason.CONSECUTIVE_FAILURES
        if threshold_reached
        else IncidentReason.STALE_WITHOUT_COMPLETE_SUCCESS
    )
    next_state = replace(
        state,
        consecutive_failures=consecutive_failures,
        first_failure_at=first_failure_at,
        last_error=last_error,
    )
    if not opens or state.health_alert_open:
        return HealthEvaluation(state=next_state)
    return HealthEvaluation(
        state=replace(
            next_state,
            health_alert_open=True,
            health_opened_at=failed_at,
            health_reason=reason,
        ),
        events=[
            HealthEvent(
                kind=HealthEventKind.SOURCE_FAILURE,
                source_id=state.source_id,
                company_id=state.company_id,
                company_name=state.company_name,
                occurred_at=failed_at,
                reason=reason,
                consecutive_failures=consecutive_failures,
                first_failure_at=first_failure_at,
                last_error=last_error,
                last_complete_at=state.last_complete_at,
                opened_at=failed_at,
            ),
        ],
    )


def _recovery_event(state: SourceHealthState, *, complete_at: datetime) -> HealthEvent:
    """The one recovery a complete success owes an open incident, with the record it opened with."""
    return HealthEvent(
        kind=HealthEventKind.SOURCE_RECOVERY,
        source_id=state.source_id,
        company_id=state.company_id,
        company_name=state.company_name,
        occurred_at=complete_at,
        reason=IncidentReason.COMPLETE_SUCCESS,
        consecutive_failures=state.consecutive_failures,
        first_failure_at=state.first_failure_at,
        last_error=state.last_error,
        last_complete_at=complete_at,
        opened_at=state.health_opened_at,
    )


def _recovered_state(state: SourceHealthState) -> SourceHealthState:
    """The incident record a source carries once it has completed again."""
    return replace(
        state,
        consecutive_failures=0,
        health_alert_open=False,
        health_opened_at=None,
        health_reason=None,
        first_failure_at=None,
        last_error=None,
    )


def evaluate_source_heartbeat(
    state: SourceHealthState,
    *,
    now: datetime,
    last_run_at: datetime | None = None,
) -> HealthEvaluation:
    """Apply one heartbeat to a source that may not have run since the last one.

    The heartbeat answers the two questions a run cannot. It opens the staleness incident for a
    source that has simply stopped producing complete successes -- the branch nothing else reaches,
    because no run arrives to be counted -- and it closes an incident whose source did complete
    after the incident opened but whose recovery was never recorded, which is what a process that
    stopped between finishing a run and writing its health leaves behind.

    It never advances the failure streak: no run happened, so inventing a failure would be a lie
    about the source. It never opens a second incident for a source that already has one, and it
    returns the state unchanged while the freshness window still holds. ``last_run_at`` is the
    stored instant of the most recent run whatever its outcome, which is the anchor for a source
    that has never completed one.
    """
    moment = _as_utc(now)
    if state.health_alert_open:
        if state.last_complete_at is None or state.health_opened_at is None:
            return HealthEvaluation(state=state)
        complete_at = _as_utc(state.last_complete_at)
        if complete_at < _as_utc(state.health_opened_at):
            return HealthEvaluation(state=state)
        return HealthEvaluation(
            state=replace(_recovered_state(state), last_complete_at=complete_at),
            events=[_recovery_event(state, complete_at=complete_at)],
        )

    if not is_stale(
        now=moment,
        last_complete_at=state.last_complete_at,
        first_failure_at=state.first_failure_at,
        last_run_at=last_run_at,
        interval_minutes=state.interval_minutes,
    ):
        return HealthEvaluation(state=state)

    reason = IncidentReason.STALE_WITHOUT_COMPLETE_SUCCESS
    return HealthEvaluation(
        state=replace(
            state,
            health_alert_open=True,
            health_opened_at=moment,
            health_reason=reason,
        ),
        events=[
            HealthEvent(
                kind=HealthEventKind.SOURCE_FAILURE,
                source_id=state.source_id,
                company_id=state.company_id,
                company_name=state.company_name,
                occurred_at=moment,
                reason=reason,
                consecutive_failures=state.consecutive_failures,
                first_failure_at=state.first_failure_at,
                last_error=state.last_error,
                last_complete_at=state.last_complete_at,
                opened_at=moment,
            ),
        ],
    )


# ------------------------------------------------------------------ the daily digest


@dataclass(frozen=True, slots=True)
class DigestSource:
    """One registry source plus the stored state the digest reasons about.

    The fields are the two configuration values the policy needs, the freshness instants, and the
    incident record. Build it with :meth:`from_parts` from a ``SourceConfig`` and whatever
    ``Repository.source_health`` returned, which is ``None`` for a source that has never run.
    """

    source_id: str
    company_id: str
    company_name: str
    enabled: bool
    support_status: str
    interval_minutes: int
    last_complete_at: datetime | None = None
    last_run_at: datetime | None = None
    consecutive_failures: int = 0
    health_alert_open: bool = False
    health_opened_at: datetime | None = None
    health_reason: str | None = None
    first_failure_at: datetime | None = None
    last_error: str | None = None

    @classmethod
    def from_parts(
        cls,
        source: SourceConfig,
        *,
        company_name: str,
        state: SourceState | None,
    ) -> DigestSource:
        """Join one configured source with its stored state, or with nothing when it never ran."""
        return cls(
            source_id=source.id,
            company_id=source.company_id,
            company_name=company_name,
            enabled=source.enabled,
            support_status=str(source.support_status),
            interval_minutes=source.interval_minutes,
            last_complete_at=None if state is None else state.last_complete_at,
            last_run_at=None if state is None else state.last_run_at,
            consecutive_failures=0 if state is None else state.consecutive_failures,
            health_alert_open=state is not None and state.health_alert_open,
            health_opened_at=None if state is None else state.health_opened_at,
            health_reason=None if state is None else state.health_reason,
            first_failure_at=None if state is None else state.first_failure_at,
            last_error=None if state is None else state.last_error,
        )


@dataclass(frozen=True, slots=True)
class DailyDigest:
    """One deterministic report of source health and delivery activity.

    ``markdown`` and ``text`` describe the same evidence in two shapes, from the same fields and
    with no clock read of their own, so the same inputs always render the same bytes.
    """

    generated_at: datetime
    window_start: datetime
    timezone: str
    total_enabled_sources: int
    sources_complete: int
    sources_stale: int
    sources_awaiting_success: int
    sources_never_polled: int
    open_incidents: tuple[HealthEvent, ...] = ()
    blocked_companies: tuple[str, ...] = ()
    manual_review_companies: tuple[str, ...] = ()
    counts: WindowCounts = WindowCounts()
    quota: QuotaSnapshot | None = None

    @property
    def new_jobs(self) -> int:
        """New-job events detected inside the window."""
        return self.counts.new_jobs

    @property
    def matched_alerts(self) -> int:
        """Deliveries the provider accepted inside the window."""
        return self.counts.matched_alerts

    @property
    def failed_deliveries(self) -> int:
        """Attempts inside the window that did not deliver."""
        return self.counts.failed_deliveries

    @property
    def abandoned_deliveries(self) -> int:
        """Attempts this window gave up on, which is the retry policy taking effect."""
        return self.counts.abandoned

    @property
    def retry_policy(self) -> str:
        """The retry and retention numbers, so the digest states them instead of hiding them.

        The delay is stated as one the durable layer enforces before a retry may be spent, and the
        retention window as one that cannot delete the attempts an unanswered event is still
        counted against: both are what make the per-event cap mean what it says.
        """
        base = int(RETRY_BACKOFF_BASE_SECONDS)
        cap = int(RETRY_BACKOFF_MAX_SECONDS)
        return (
            f"at most {DEFAULT_MAX_RETRYABLE_ATTEMPTS} unanswered attempts per event, each retry"
            f" waiting the recorded {base}s..{cap}s delay before it may be spent; an unanswered"
            f" attempt is pruned after {NOTIFICATION_ATTEMPT_RETENTION_DAYS} days only once every"
            " event it covers has a final answer, so the cap survives the retention window"
        )

    @property
    def markdown(self) -> str:
        """The digest as Markdown, with the same fields as :attr:`text`."""
        return _render(self, markdown=True)

    @property
    def text(self) -> str:
        """The digest as plain text, byte-identical for the same inputs as :attr:`markdown`."""
        return _render(self, markdown=False)


def build_daily_digest(
    *,
    now: datetime,
    window_start: datetime,
    sources: Sequence[DigestSource],
    counts: WindowCounts | None = None,
    quota: QuotaSnapshot | None = None,
    timezone: str = CHICAGO_TIMEZONE,
) -> DailyDigest:
    """Summarize one window for the registry and stored state it is handed.

    Every instant arrives as an argument and nothing here reads a clock, which is what makes two
    digests over the same inputs byte-identical. Enabled sources are bucketed by the same freshness
    rule the incident policy uses: complete when a complete success is inside the window, stale
    when the freshness anchor is older than the threshold, awaiting a first complete success when
    the source has run recently but never completed, and never polled when it has no history at
    all. The four buckets always add up to the enabled count.
    """
    if _as_utc(window_start) > _as_utc(now):
        raise ValueError("window_start must not be after now")
    try:
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError(f"unknown IANA timezone: {timezone}") from exc

    complete = stale = awaiting = never = 0
    for source in sources:
        if not source.enabled:
            continue
        anchor = source.last_complete_at or source.first_failure_at or source.last_run_at
        if anchor is None:
            never += 1
        elif is_stale(
            now=now,
            last_complete_at=source.last_complete_at,
            first_failure_at=source.first_failure_at,
            last_run_at=source.last_run_at,
            interval_minutes=source.interval_minutes,
        ):
            stale += 1
        elif source.last_complete_at is not None:
            complete += 1
        else:
            awaiting += 1

    incidents = tuple(
        sorted(
            (
                _incident_event(source)
                for source in sources
                if source.enabled and source.health_alert_open
            ),
            key=lambda event: (event.company_id, event.source_id),
        )
    )
    return DailyDigest(
        generated_at=_as_utc(now),
        window_start=_as_utc(window_start),
        timezone=timezone,
        total_enabled_sources=complete + stale + awaiting + never,
        sources_complete=complete,
        sources_stale=stale,
        sources_awaiting_success=awaiting,
        sources_never_polled=never,
        open_incidents=incidents,
        blocked_companies=_company_lines(sources, SupportStatus.BLOCKED.value),
        manual_review_companies=_company_lines(sources, SupportStatus.NEEDS_MANUAL_REVIEW.value),
        counts=WindowCounts() if counts is None else counts,
        quota=quota,
    )


def _incident_event(source: DigestSource) -> HealthEvent:
    """The open incident one digest entry reports, with the record it was opened with."""
    opened_at = source.health_opened_at or source.first_failure_at
    derived = (
        IncidentReason.CONSECUTIVE_FAILURES
        if source.consecutive_failures >= FAILURE_INCIDENT_THRESHOLD
        else IncidentReason.STALE_WITHOUT_COMPLETE_SUCCESS
    )
    try:
        reason = IncidentReason(source.health_reason) if source.health_reason else derived
    except ValueError:
        reason = derived
    return HealthEvent(
        kind=HealthEventKind.SOURCE_FAILURE,
        source_id=source.source_id,
        company_id=source.company_id,
        company_name=source.company_name,
        occurred_at=opened_at or source.last_run_at or source.last_complete_at or _EPOCH,
        reason=reason,
        consecutive_failures=source.consecutive_failures,
        first_failure_at=source.first_failure_at,
        last_error=source.last_error,
        last_complete_at=source.last_complete_at,
        opened_at=opened_at,
    )


def _company_lines(sources: Sequence[DigestSource], status: str) -> tuple[str, ...]:
    """One line per company whose sources carry this support status, deduplicated and sorted."""
    names = {
        source.company_id: source.company_name or source.company_id
        for source in sources
        if source.support_status == status
    }
    return tuple(f"{names[key]} ({key})" for key in sorted(names))


def _render(digest: DailyDigest, *, markdown: bool) -> str:
    """The whole digest in one shape; both shapes are built here so they can never diverge."""
    lines = ["# Fast Job Alert daily digest" if markdown else "Fast Job Alert daily digest", ""]
    lines.append(f"- Generated: {_instant_text(digest.generated_at, digest.timezone)}")
    lines.append(
        f"- Window: {_instant_text(digest.window_start, digest.timezone)} .."
        f" {_instant_text(digest.generated_at, digest.timezone)}"
    )
    lines.extend(
        [
            f"- Enabled sources: {digest.total_enabled_sources}",
            f"- Sources complete: {digest.sources_complete}",
            f"- Sources stale: {digest.sources_stale}",
            f"- Sources awaiting a first complete success: {digest.sources_awaiting_success}",
            f"- Sources never polled: {digest.sources_never_polled}",
            f"- Open incidents: {len(digest.open_incidents)}",
            f"- Blocked companies: {len(digest.blocked_companies)}",
            f"- Manual review companies: {len(digest.manual_review_companies)}",
            f"- New jobs: {digest.new_jobs}",
            f"- Reopened jobs: {digest.counts.reopened_jobs}",
            f"- Matched alerts: {digest.matched_alerts}",
            f"- Failed deliveries: {digest.failed_deliveries}",
            f"- Abandoned deliveries: {digest.abandoned_deliveries}",
            f"- Pushover quota: {_quota_text(digest.quota, digest.timezone)}",
            f"- Retry policy: {digest.retry_policy}",
        ]
    )
    lines.extend(_section("Open incidents", _incident_lines(digest), markdown=markdown))
    lines.extend(
        _section(
            "Blocked companies",
            _entries(digest.blocked_companies),
            markdown=markdown,
        )
    )
    lines.extend(
        _section(
            "Manual review companies",
            _entries(digest.manual_review_companies),
            markdown=markdown,
        )
    )
    return "\n".join(lines) + "\n"


def _section(title: str, body: list[str], *, markdown: bool) -> list[str]:
    heading = f"## {title}" if markdown else f"{title}:"
    return ["", heading, "", *body]


def _line(text: str) -> str:
    return f"- {text}"


def _entries(entries: Sequence[str]) -> list[str]:
    """One bullet per entry, or a single `none` bullet, so an empty section still reads."""
    return [_line(entry) for entry in entries] or [NONE_LINE]


def _incident_lines(digest: DailyDigest) -> list[str]:
    """One line per open incident, in company then source order, or a single `none` line."""
    if not digest.open_incidents:
        return [NONE_LINE]
    return [_line(_incident_line(event, digest.timezone)) for event in digest.open_incidents]


def _incident_line(event: HealthEvent, timezone: str) -> str:
    opened = (
        "an unrecorded time"
        if event.opened_at is None
        else _instant_text(event.opened_at, timezone)
    )
    since = (
        "no complete success yet"
        if event.first_failure_at is None
        else f"failing since {_instant_text(event.first_failure_at, timezone)}"
    )
    last_complete = (
        "never"
        if event.last_complete_at is None
        else _instant_text(event.last_complete_at, timezone)
    )
    return (
        f"{event.company} ({event.source_id}): opened {opened} by {event.reason};"
        f" {event.consecutive_failures} consecutive incomplete runs, {since};"
        f" last error: {event.last_error or 'no error recorded'}; last complete: {last_complete}"
    )


def _quota_text(quota: QuotaSnapshot | None, timezone: str) -> str:
    """Remaining Pushover allowance and its reset, or an honest `unknown` when none was seen."""
    if quota is None or quota.remaining is None:
        return "unknown (no Pushover quota figures recorded)"
    remaining = (
        f"{quota.remaining}"
        if quota.limit is None
        else f"{quota.remaining} of {quota.limit} remaining"
    )
    reset = "" if quota.reset_at is None else f", resets {_instant_text(quota.reset_at, timezone)}"
    return f"{remaining}{reset} (as of {_utc_text(quota.observed_at)})"


def _as_utc(moment: datetime) -> datetime:
    """A timestamp read as UTC when it is naive, the way every other stored instant is read."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _utc_text(moment: datetime) -> str:
    return _as_utc(moment).strftime("%Y-%m-%d %H:%M UTC")


def _zone_text(moment: datetime, timezone: str) -> str:
    local = _as_utc(moment).astimezone(ZoneInfo(timezone))
    return f"{local.strftime('%Y-%m-%d %H:%M')} {timezone}"


def _instant_text(moment: datetime, timezone: str) -> str:
    return f"{_utc_text(moment)} ({_zone_text(moment, timezone)})"


#: Only reached when an incident record has no instant at all, which no code path produces.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
