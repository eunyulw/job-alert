"""Delivery primitives shared by the notification providers.

Where retries live
------------------
``HttpFetcher`` replays a request only when ``RequestSpec.idempotent`` is true, and a notification
POST is not idempotent: replaying it can publish the same alert twice. Each notifier therefore
does exactly one attempt through ``HttpFetcher.send_once`` and classifies the answer into a
:class:`DeliveryResult`. A retryable result says so and carries the earliest delay the provider's
own signal allows, and the durable layer holds the unsent events and decides when to try again.

Message limits
--------------
Pushover accepts a 250-character title and a 1024-character body, so :class:`NotificationMessage`
fits both as it is built. Counting is in code points, the unit Pushover documents, so a multi-byte
character is never split. Truncation keeps the whole alert usable: a title elides its middle so the
leading event label and the trailing company both survive, a body drops its tail so the job title
that leads it survives, and the direct URL is a separate field that is never shortened.
Descriptions are never part of a message.

Burst grouping
--------------
:func:`group_job_events` turns matched events into the messages one delivery cycle should send.
Normal- and low-priority events for one company detected inside the window become one message
titled ``N new jobs - <Company>``, high-priority events bypass the window and stay individual, and
every batch carries the event ids it covers so a grouped delivery is recorded exactly once.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable
from urllib.parse import quote

from pydantic import Field, field_validator

from job_alert.config import FilterPriority
from job_alert.fetch import retry_after_seconds
from job_alert.models import JobEvent, JobEventType, NormalizedJob, StrictModel

#: Provider message limits, counted in characters the way Pushover documents them.
TITLE_LIMIT = 250
BODY_LIMIT = 1024

#: A grouped body lists at most this many postings, each bounded so the count line always fits.
GROUPED_TITLES_SHOWN = 5
GROUPED_TITLE_LIMIT = 120

#: Default burst window: events for one company detected this close together share one message.
DEFAULT_GROUP_WINDOW_SECONDS = 120

#: Server failures are retryable, but never sooner than this; see the Pushover requirements.
SERVER_ERROR_MINIMUM_RETRY_SECONDS = 5.0

REDACTED = "[redacted]"
ELLIPSIS = "\u2026"


class NotificationConfigurationError(ValueError):
    """A notifier cannot be used as configured; the message never quotes a credential."""


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """What one provider attempt did, and whether another attempt is allowed.

    ``retryable`` is the delivery layer's verdict, not the HTTP client's: a notifier never replays
    a non-idempotent POST on its own. ``retry_after_seconds`` is the earliest delay the provider's
    own signal permits (``Retry-After``, a quota reset, or the five-second floor for a server
    failure), and the durable layer decides how many attempts to spend. The quota fields mirror
    Pushover's ``X-Limit-App-*`` headers: the monthly allowance, what is left of it, and the
    instant it resets. ``error`` is a short, credential-free explanation, and it is only set when
    the message was not delivered.
    """

    provider: str
    delivered: bool
    retryable: bool
    http_status: int | None = None
    provider_request_id: str | None = None
    retry_after_seconds: float | None = None
    error: str | None = None
    quota_limit: int | None = None
    quota_remaining: int | None = None
    quota_reset_at: datetime | None = None


class NotificationMessage(StrictModel):
    """One alert, already inside the provider's message limits.

    ``url`` becomes the provider's clickable link and is carried as its own field, so shortening a
    long title or body can never cost the alert its direct link. ``tags`` is ntfy's tag list and is
    ignored by Pushover.
    """

    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    url: str = Field(min_length=1)
    url_title: str = Field(default="Open and apply", min_length=1)
    priority: FilterPriority = FilterPriority.NORMAL
    tags: tuple[str, ...] = ()

    @field_validator("title")
    @classmethod
    def _fit_title(cls, value: str) -> str:
        return _elide_middle(value, TITLE_LIMIT)

    @field_validator("body")
    @classmethod
    def _fit_body(cls, value: str) -> str:
        return _elide_tail(value, BODY_LIMIT)

    @field_validator("url")
    @classmethod
    def _require_https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("notification URLs must use https")
        return value


@runtime_checkable
class Notifier(Protocol):
    """A delivery provider the service can hand one ready message to."""

    provider: str

    async def send(self, message: NotificationMessage) -> DeliveryResult: ...


@dataclass(frozen=True, slots=True)
class AlertEvent:
    """One matched event that may be delivered, with what grouping and recording need.

    ``event_id`` is the persisted ``job_events`` row id. A grouped batch carries every underlying
    id, so the delivery record can link all of them and the group is sent exactly once.
    ``company_name`` is the display name from the company registry; the normalized job itself only
    carries the company id.
    """

    event_id: int
    event: JobEvent
    priority: FilterPriority = FilterPriority.NORMAL
    company_name: str | None = None

    @property
    def company(self) -> str:
        """The name a reader recognizes, falling back to the registry id."""
        return self.company_name or self.event.job.company_id


@dataclass(frozen=True, slots=True)
class AlertBatch:
    """One provider message and every event id it covers.

    ``immediate`` is true only for a high-priority event, which bypasses the grouping window. A
    batch that is not immediate must wait for the window to close before it is sent, so a later
    event for the same company can still join it.
    """

    message: NotificationMessage
    event_ids: tuple[int, ...]
    priority: FilterPriority = FilterPriority.NORMAL
    immediate: bool = False


def group_job_events(
    events: Iterable[AlertEvent], window_seconds: int = DEFAULT_GROUP_WINDOW_SECONDS
) -> list[AlertBatch]:
    """Turn matched events into the messages one delivery cycle should send.

    High-priority events become one immediate batch each, in detection order. Every other event
    joins a per-company burst: the window is anchored on the first event of a burst and includes
    the boundary, so events detected at most ``window_seconds`` after the anchor travel together.
    Batch order is deterministic — immediate batches first, then bursts by detection time and
    company id — so the same events always produce the same messages in the same order.
    """
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")

    window = timedelta(seconds=window_seconds)
    immediate: list[AlertBatch] = []
    waiting: dict[str, list[AlertEvent]] = {}
    for alert in sorted(events, key=_chronological_order):
        if alert.priority is FilterPriority.HIGH:
            immediate.append(_individual_batch(alert))
        else:
            waiting.setdefault(alert.event.job.company_id, []).append(alert)

    bursts: list[tuple[datetime, str, list[AlertEvent]]] = []
    for company_id in sorted(waiting):
        for burst in _window_bursts(waiting[company_id], window):
            bursts.append((_detected_at(burst[0]), company_id, burst))
    bursts.sort(key=lambda entry: (entry[0], entry[1]))

    return immediate + [_grouped_batch(burst) for _, _, burst in bursts]


def redact_secrets(text: str, secrets: Iterable[str | None]) -> str:
    """Replace every credential with ``[redacted]``, in raw and percent-encoded form.

    A missing credential is accepted so a caller can always pass what it holds.
    """
    redacted = text
    for secret in secrets:
        if not secret or secret == REDACTED:
            continue
        for variant in (secret, quote(secret, safe="")):
            redacted = redacted.replace(variant, REDACTED)
    return redacted


def header_value(headers: Mapping[str, str], name: str) -> str | None:
    """One response header, matched case-insensitively because header names are not canonical."""
    wanted = name.lower()
    return next(
        (value for key, value in headers.items() if key.lower() == wanted),
        None,
    )


def retry_after_delay(headers: Mapping[str, str]) -> float | None:
    """The provider's own ``Retry-After`` answer as a delay in seconds, when it sent one."""
    return retry_after_seconds(header_value(headers, "Retry-After"))


def require_credential(value: str | None, name: str) -> str:
    """A credential that must be present and non-blank, or a configuration error without it."""
    if value is None or not value.strip():
        raise NotificationConfigurationError(f"{name} must be set and non-blank")
    return value


def optional_credential(value: str | None) -> str | None:
    """An optional credential; a blank value counts as absent rather than as a real token."""
    return value if value is not None and value.strip() else None


def _window_bursts(events: Sequence[AlertEvent], window: timedelta) -> list[list[AlertEvent]]:
    """Split one company's chronological events into bursts anchored on each burst's first event."""
    bursts: list[list[AlertEvent]] = []
    anchor: datetime | None = None
    for alert in events:
        moment = _detected_at(alert)
        if anchor is None or moment - anchor > window:
            bursts.append([])
            anchor = moment
        bursts[-1].append(alert)
    return bursts


def _grouped_batch(burst: Sequence[AlertEvent]) -> AlertBatch:
    """One company burst as a single message, or the single event's own message."""
    if len(burst) == 1:
        return _individual_batch(burst[0])

    ordered = sorted(burst, key=_priority_order)
    top = ordered[0]
    priority = top.priority
    lines = [
        f"\u2022 {_elide_tail(_job_line(alert), GROUPED_TITLE_LIMIT)}"
        for alert in ordered[:GROUPED_TITLES_SHOWN]
    ]
    remaining = len(ordered) - len(lines)
    if remaining:
        lines.append(f"\u2026and {remaining} more")
    message = NotificationMessage(
        title=f"{len(ordered)} new jobs \u2014 {top.company}",
        body="\n".join(lines),
        url=str(top.event.job.posting_url),
        url_title="Open top match",
        priority=priority,
    )
    return AlertBatch(
        message=message,
        event_ids=tuple(sorted(alert.event_id for alert in ordered)),
        priority=priority,
        immediate=False,
    )


def _individual_batch(alert: AlertEvent) -> AlertBatch:
    """One event on its own message, which is also how a high-priority match is delivered."""
    job = alert.event.job
    message = NotificationMessage(
        title=f"{_EVENT_LABELS[alert.event.event_type]} \u2014 {alert.company}",
        body=_individual_body(job),
        url=str(job.posting_url),
        url_title="Open and apply",
        priority=alert.priority,
    )
    return AlertBatch(
        message=message,
        event_ids=(alert.event_id,),
        priority=alert.priority,
        immediate=alert.priority is FilterPriority.HIGH,
    )


def _individual_body(job: NormalizedJob) -> str:
    """The job title first, so a body that has to be shortened still names the posting."""
    lines = [job.title]
    detail = _location_summary(job)
    if detail:
        lines.append(detail)
    lines.append(f"First seen {_format_moment(job.first_seen_at)}")
    return "\n".join(lines)


def _job_line(alert: AlertEvent) -> str:
    detail = _location_summary(alert.event.job)
    return f"{alert.event.job.title} \u2014 {detail}" if detail else alert.event.job.title


def _location_summary(job: NormalizedJob) -> str:
    parts = [", ".join(job.locations)] if job.locations else []
    if job.workplace_type:
        parts.append(job.workplace_type)
    return " \u00b7 ".join(parts)


def _format_moment(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _elide_tail(value: str, limit: int) -> str:
    """Keep the head of a value, so leading detail survives and only the tail is lost."""
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + ELLIPSIS


def _elide_middle(value: str, limit: int) -> str:
    """Keep both ends of a value, so a trailing company survives a shortened title."""
    if len(value) <= limit:
        return value
    keep = limit - 1
    head = value[: keep // 2].rstrip()
    tail = value[-(keep - keep // 2) :].lstrip()
    return f"{head}{ELLIPSIS}{tail}"


def _detected_at(alert: AlertEvent) -> datetime:
    """The moment grouping compares; a naive timestamp is read as UTC, like storage does."""
    moment = alert.event.detected_at
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _chronological_order(alert: AlertEvent) -> tuple[datetime, str, int]:
    return (_detected_at(alert), alert.event.job.company_id, alert.event_id)


def _priority_order(alert: AlertEvent) -> tuple[int, datetime, int]:
    """Highest priority first, then earliest, then event id: bursts order the same way twice."""
    return (_PRIORITY_RANK[alert.priority], _detected_at(alert), alert.event_id)


_PRIORITY_RANK: Mapping[FilterPriority, int] = {
    FilterPriority.HIGH: 0,
    FilterPriority.NORMAL: 1,
    FilterPriority.LOW: 2,
}

_EVENT_LABELS: Mapping[JobEventType, str] = {
    JobEventType.NEW: "New job",
    JobEventType.UPDATED: "Updated job",
    JobEventType.REOPENED: "Reopened job",
    JobEventType.CLOSED: "Closed job",
}
