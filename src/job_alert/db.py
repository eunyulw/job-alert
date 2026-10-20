from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from job_alert.models import JobEvent, JobEventType, NormalizedJob, RunOutcome

SCHEMA_ENV_VAR = "JOB_ALERT_SCHEMA_PATH"
MIGRATION_NAME = "001_initial.sql"
DEFAULT_CLOSE_AFTER = 2

#: How many ambiguous delivery attempts one event may collect before the durable layer abandons it.
DEFAULT_MAX_UNCERTAIN_RETRIES = 2

#: Error codes that mark a run as a diagnostic an operator asked for rather than evidence about the
#: source. A diagnostic neither advances the failure streak nor counts as a complete success.
DIAGNOSTIC_ERROR_CODES: frozenset[str] = frozenset({"dry_run"})

#: How many attempts one event may collect from a *certain* no-send failure (``RETRYABLE``) before
#: the durable layer abandons it. The uncertain class is capped separately by the service's
#: :data:`DEFAULT_MAX_UNCERTAIN_RETRIES`; this is the cap for the class nothing else bounded.
DEFAULT_MAX_RETRYABLE_ATTEMPTS = 8

#: Wait before the next attempt after a no-send failure: ``base * 2 ** (attempt - 1)``, capped.
RETRY_BACKOFF_BASE_SECONDS = 60.0
RETRY_BACKOFF_MAX_SECONDS = 3600.0

#: The longest delay a retry may be held back by, whoever asked for it. A provider's own
#: ``Retry-After`` is advisory, so it is honoured inside this ceiling rather than trusted: a
#: pathological header, or a quota reset months away, must not park an alert indefinitely.
RETRY_AFTER_MAX_SECONDS = RETRY_BACKOFF_MAX_SECONDS

#: Attempt rows this old with no terminal outcome are pruned; terminal rows are never pruned.
NOTIFICATION_ATTEMPT_RETENTION_DAYS = 30

#: An ``ALTER TABLE ... ADD COLUMN`` addition, recognized so an existing database only misses the
#: columns it does not have. SQLite has no ``ADD COLUMN IF NOT EXISTS``.
_ADD_COLUMN = re.compile(
    r"^\s*ALTER\s+TABLE\s+(?P<table>[A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+COLUMN\s+"
    r"(?P<column>[A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


class DeliveryOutcome(StrEnum):
    """What one provider attempt did, as the durable layer records it.

    ``DELIVERED`` links the event ids the message covered, ``RETRYABLE`` and ``UNCERTAIN`` leave
    them pending for a later cycle, ``PERMANENT`` ends the question because the provider refused
    the message for good, and ``ABANDONED`` ends it because an ambiguous outcome has been retried
    as often as the service allows. ``FILTERED`` ends it without contacting any provider, because
    no enabled filter authorized a notification when the event was evaluated; it is a decision, not
    a provider attempt, so delivery reports leave it out. The last three are terminal without a
    delivery.
    """

    DELIVERED = "delivered"
    RETRYABLE = "retryable"
    UNCERTAIN = "uncertain"
    PERMANENT = "permanent"
    ABANDONED = "abandoned"
    FILTERED = "filtered"


#: Outcomes that stop a cycle from ever re-attempting the events an attempt covered.
TERMINAL_DELIVERY_OUTCOMES = (
    DeliveryOutcome.DELIVERED,
    DeliveryOutcome.PERMANENT,
    DeliveryOutcome.ABANDONED,
    DeliveryOutcome.FILTERED,
)

#: Outcomes that leave the events they cover pending for a later cycle.
NON_TERMINAL_DELIVERY_OUTCOMES = (
    DeliveryOutcome.RETRYABLE,
    DeliveryOutcome.UNCERTAIN,
)


class IncompleteRunError(RuntimeError):
    """Raised when a run that is not `complete` is asked to change job absence state."""


@dataclass(frozen=True)
class SourceState:
    """Persisted polling state for one source: validators, freshness, and health incident.

    ``last_run_at`` is the finish time of the most recent run whatever its outcome, while
    ``last_complete_at`` only moves for a complete success. ``consecutive_failures``,
    ``first_failure_at``, ``last_error``, and the incident fields have one writer: the health
    layer that :meth:`Repository.record_health_state` persists. :meth:`Repository.finish_run`
    records the run and the freshness it proves; letting it count the failure as well would count
    one failed run twice and open an incident a run early. ``backoff_until`` is the instant
    before which the scheduler must not poll the source again.
    """

    source_id: str
    etag: str | None
    last_modified: str | None
    consecutive_failures: int
    last_complete_at: datetime | None
    backoff_until: datetime | None
    health_alert_open: bool
    last_run_at: datetime | None = None
    first_failure_at: datetime | None = None
    last_error: str | None = None
    health_opened_at: datetime | None = None
    health_reason: str | None = None


@dataclass(frozen=True)
class SourceHealthState:
    """The health policy's input record for one source: persisted incident fields plus config.

    Build it with :meth:`from_source_state`, evaluate one run with
    :func:`job_alert.health.evaluate_source_health`, and persist the result with
    :meth:`Repository.record_health_state`. ``company_id``, ``company_name``, and
    ``interval_minutes`` come from configuration and are carried here because the incident record
    and the staleness branch both need them; persistence writes back only the rest.
    """

    source_id: str = ""
    company_id: str = ""
    company_name: str = ""
    consecutive_failures: int = 0
    health_alert_open: bool = False
    health_opened_at: datetime | None = None
    health_reason: str | None = None
    first_failure_at: datetime | None = None
    last_error: str | None = None
    last_complete_at: datetime | None = None
    interval_minutes: int | None = None

    @classmethod
    def from_source_state(
        cls,
        state: SourceState,
        *,
        company_id: str = "",
        company_name: str = "",
        interval_minutes: int | None = None,
    ) -> SourceHealthState:
        """Join one stored source state with the configuration values health reasoning needs."""
        return cls(
            source_id=state.source_id,
            company_id=company_id,
            company_name=company_name,
            consecutive_failures=state.consecutive_failures,
            health_alert_open=state.health_alert_open,
            health_opened_at=state.health_opened_at,
            health_reason=state.health_reason,
            first_failure_at=state.first_failure_at,
            last_error=state.last_error,
            last_complete_at=state.last_complete_at,
            interval_minutes=interval_minutes,
        )


@dataclass(frozen=True)
class QuotaSnapshot:
    """The provider quota figures the most recent attempt reported, when it reported any.

    Pushover sends ``X-Limit-App-Limit``, ``X-Limit-App-Remaining``, and ``X-Limit-App-Reset`` on
    every answer; ``limit`` or ``reset_at`` may still be missing if the provider omitted one.
    """

    provider: str
    limit: int | None
    remaining: int | None
    reset_at: datetime | None
    observed_at: datetime


@dataclass(frozen=True)
class WindowCounts:
    """What one reporting window contains, counted from durable history rather than memory."""

    new_jobs: int = 0
    reopened_jobs: int = 0
    attempts: int = 0
    delivered: int = 0
    retryable: int = 0
    uncertain: int = 0
    permanent: int = 0
    abandoned: int = 0

    @property
    def matched_alerts(self) -> int:
        """Alerts the provider accepted inside the window."""
        return self.delivered

    @property
    def failed_deliveries(self) -> int:
        """Recorded attempts that did not deliver, whatever their reason."""
        return self.attempts - self.delivered


def retry_backoff_seconds(attempt_number: int) -> float:
    """How long to wait before the next attempt after this many certain no-send failures.

    The wait doubles from :data:`RETRY_BACKOFF_BASE_SECONDS` and never exceeds
    :data:`RETRY_BACKOFF_MAX_SECONDS`, so one unreachable provider costs a bounded number of sends
    spread over a bounded schedule instead of one attempt at every poll forever. The recording
    layer stores this value on the attempt row it belongs to, which is what makes the schedule
    visible to an operator instead of implicit in the poll cadence.
    """
    if attempt_number < 1:
        raise ValueError(f"attempt_number must be at least 1, got {attempt_number}")
    return min(RETRY_BACKOFF_BASE_SECONDS * 2.0 ** (attempt_number - 1), RETRY_BACKOFF_MAX_SECONDS)


def bounded_retry_after(seconds: float | None) -> float | None:
    """A recorded retry delay, floored at zero and capped at :data:`RETRY_AFTER_MAX_SECONDS`.

    The value comes from a provider's own answer -- ``Retry-After``, or a quota reset -- so it is
    treated as advice rather than as an instruction: a delay the provider asks for is honoured
    inside the same ceiling the durable layer's own backoff uses, and a negative or absurd one
    cannot make an alert either send immediately on every pass or never come back at all.
    ``None`` stays ``None``, which means "no recorded delay" rather than "due now".
    """
    if seconds is None:
        return None
    return min(max(float(seconds), 0.0), RETRY_AFTER_MAX_SECONDS)


@dataclass(frozen=True)
class AppliedSnapshot:
    """The last complete snapshot whose decisions actually reached job state.

    This is not simply the last run that finished as ``complete``: a dry run finishes complete
    without staging a single observation, so the snapshot absence inference, the quarantine
    baseline, and ``304`` reconstruction have to reason about is the one job state reflects.
    """

    run_id: int
    applied_at: datetime
    page_count: int
    canonical_job_keys: frozenset[str]


@dataclass(frozen=True)
class PendingEvent:
    """One durable job event that no terminal delivery attempt has covered yet."""

    event_id: int
    event_type: JobEventType
    canonical_job_key: str
    detected_at: datetime
    run_id: int


@dataclass(frozen=True)
class QuarantinedSnapshot:
    """A complete candidate withheld because it dropped most of the previous complete snapshot."""

    run_id: int
    source_id: str
    quarantined_at: datetime
    previous_job_count: int
    candidate_job_count: int
    candidate_keys: frozenset[str]


def _utc_isoformat(moment: datetime) -> str:
    """Serialize a timestamp as a UTC ISO-8601 string; naive input is read as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def _parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(str(value))


def _optional_isoformat(moment: datetime | None) -> str | None:
    """A timestamp serialized the same way as every other one, or ``None`` when there is none."""
    return None if moment is None else _utc_isoformat(moment)


def _source_state_from_row(row: sqlite3.Row) -> SourceState:
    """One stored source state, read the way both `source_health` and `source_states` need it."""
    return SourceState(
        source_id=str(row["source_id"]),
        etag=row["etag"],
        last_modified=row["last_modified"],
        consecutive_failures=int(row["consecutive_failures"]),
        last_complete_at=_parse_timestamp(row["last_complete_at"]),
        backoff_until=_parse_timestamp(row["backoff_until"]),
        health_alert_open=bool(row["health_alert_open"]),
        last_run_at=_parse_timestamp(row["last_run_at"]),
        first_failure_at=_parse_timestamp(row["first_failure_at"]),
        last_error=row["last_error"],
        health_opened_at=_parse_timestamp(row["health_opened_at"]),
        health_reason=row["health_reason"],
    )


def _attempt_counts(
    connection: sqlite3.Connection, event_ids: Iterable[int], outcomes: Sequence[str]
) -> dict[int, int]:
    """How many recorded attempts with any of these outcomes cover each of these events."""
    ids = list(dict.fromkeys(event_ids))
    if not ids or not outcomes:
        return {}
    id_placeholders = ", ".join("?" for _ in ids)
    outcome_placeholders = ", ".join("?" for _ in outcomes)
    rows = connection.execute(
        "SELECT l.event_id AS event_id, COUNT(*) AS attempts FROM notification_event_links l"
        " JOIN notification_attempts a ON a.id = l.notification_attempt_id"
        f" WHERE a.outcome IN ({outcome_placeholders}) AND l.event_id IN ({id_placeholders})"
        " GROUP BY l.event_id",
        (*outcomes, *ids),
    ).fetchall()
    return {int(row["event_id"]): int(row["attempts"]) for row in rows}


def _attempts_every_event_has_answered(
    connection: sqlite3.Connection, candidates: Sequence[int]
) -> list[int]:
    """Which candidate attempts cover nothing an operator is still waiting on.

    An attempt is safe to forget once every event it covers has a terminal answer, because the
    per-event cap only counts the attempts of events that are still unanswered. An attempt that
    covers no event at all is safe to forget for the same reason: nothing reads it back.
    """
    if not candidates:
        return []
    attempt_placeholders = ", ".join("?" for _ in candidates)
    rows = connection.execute(
        "SELECT notification_attempt_id, event_id FROM notification_event_links"
        f" WHERE notification_attempt_id IN ({attempt_placeholders})",
        candidates,
    ).fetchall()
    linked: dict[int, set[int]] = {}
    for row in rows:
        linked.setdefault(int(row["notification_attempt_id"]), set()).add(int(row["event_id"]))
    resolved: set[int] = set()
    event_ids = sorted({event_id for events in linked.values() for event_id in events})
    if event_ids:
        event_placeholders = ", ".join("?" for _ in event_ids)
        outcome_placeholders = ", ".join("?" for _ in TERMINAL_DELIVERY_OUTCOMES)
        resolved = {
            int(row["event_id"])
            for row in connection.execute(
                "SELECT DISTINCT l.event_id AS event_id FROM notification_event_links l"
                " JOIN notification_attempts a ON a.id = l.notification_attempt_id"
                f" WHERE l.event_id IN ({event_placeholders})"
                f" AND a.outcome IN ({outcome_placeholders})",
                (*event_ids, *[outcome.value for outcome in TERMINAL_DELIVERY_OUTCOMES]),
            )
        }
    return [
        attempt_id
        for attempt_id in candidates
        if not (linked.get(attempt_id, set()) - resolved)
    ]


def _schema_path() -> Path:
    override = os.environ.get(SCHEMA_ENV_VAR)
    if override:
        return Path(override)
    # Inside the package, so an installed wheel carries the schema it applies.
    return Path(__file__).resolve().parent / "migrations" / MIGRATION_NAME


def _schema_statements(schema: str) -> list[str]:
    """The migration's statements, with full-line comments removed and empty pieces dropped.

    The repository applies them one at a time so an ``ALTER TABLE ... ADD COLUMN`` whose column an
    existing database already has can be skipped; SQLite offers no ``ADD COLUMN IF NOT EXISTS``.
    """
    without_comments = "\n".join(
        line for line in schema.splitlines() if not line.lstrip().startswith("--")
    )
    pieces = (piece.strip() for piece in without_comments.split(";"))
    return [statement for statement in pieces if statement]


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    """Whether a table already has a column, read from the schema rather than guessed."""
    return any(
        str(row["name"]) == column for row in connection.execute(f"PRAGMA table_info({table})")
    )


def _job_parameters(job: NormalizedJob, observed_at: str) -> tuple[object, ...]:
    return (
        job.canonical_job_key,
        job.dedupe_key,
        job.company_id,
        job.source_id,
        job.source_job_id,
        job.requisition_id,
        job.title,
        json.dumps(list(job.locations), sort_keys=True),
        job.department,
        job.team,
        job.employment_type,
        job.workplace_type,
        job.description_text,
        job.content_hash,
        str(job.posting_url),
        str(job.apply_url),
        _utc_isoformat(job.source_published_at) if job.source_published_at is not None else None,
        _utc_isoformat(job.source_updated_at) if job.source_updated_at is not None else None,
        _utc_isoformat(job.first_seen_at),
        observed_at,
        json.dumps(job.raw_metadata, sort_keys=True),
    )


def _job_from_row(row: sqlite3.Row) -> NormalizedJob:
    first_seen_at = _parse_timestamp(row["first_seen_at"])
    if first_seen_at is None:
        raise ValueError(f"stored job {row['canonical_job_key']} has no first_seen_at")
    return NormalizedJob(
        company_id=row["company_id"],
        source_id=row["source_id"],
        source_job_id=row["source_job_id"],
        canonical_job_key=row["canonical_job_key"],
        dedupe_key=row["dedupe_key"],
        requisition_id=row["requisition_id"],
        title=row["title"],
        locations=list(json.loads(row["locations_json"])),
        department=row["department"],
        team=row["team"],
        employment_type=row["employment_type"],
        workplace_type=row["workplace_type"],
        description_text=row["description_text"],
        content_hash=row["content_hash"],
        posting_url=row["posting_url"],
        apply_url=row["apply_url"],
        source_published_at=_parse_timestamp(row["source_published_at"]),
        source_updated_at=_parse_timestamp(row["source_updated_at"]),
        first_seen_at=first_seen_at,
        raw_metadata=dict(json.loads(row["raw_metadata_json"])),
    )


# A later sighting refreshes the mutable descriptive fields and keeps the earliest first_seen_at.
# It touches neither missing_complete_runs nor closed_at: a sighting is staged evidence, and only
# a finalized complete run may change absence counts or reopen a job.
UPSERT_JOB_SQL = """
INSERT INTO jobs (
  canonical_job_key, dedupe_key, company_id, source_id, source_job_id, requisition_id,
  title, locations_json, department, team, employment_type, workplace_type, description_text,
  content_hash, posting_url, apply_url, source_published_at, source_updated_at, first_seen_at,
  last_seen_at, raw_metadata_json
) VALUES (
  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
)
ON CONFLICT(canonical_job_key) DO UPDATE SET
  dedupe_key = excluded.dedupe_key,
  company_id = excluded.company_id,
  source_id = excluded.source_id,
  source_job_id = excluded.source_job_id,
  requisition_id = excluded.requisition_id,
  title = excluded.title,
  locations_json = excluded.locations_json,
  department = excluded.department,
  team = excluded.team,
  employment_type = excluded.employment_type,
  workplace_type = excluded.workplace_type,
  description_text = excluded.description_text,
  content_hash = excluded.content_hash,
  posting_url = excluded.posting_url,
  apply_url = excluded.apply_url,
  source_published_at = excluded.source_published_at,
  source_updated_at = excluded.source_updated_at,
  first_seen_at = MIN(jobs.first_seen_at, excluded.first_seen_at),
  last_seen_at = excluded.last_seen_at,
  raw_metadata_json = excluded.raw_metadata_json
"""

UPSERT_OBSERVATION_SQL = """
INSERT INTO job_observations (run_id, canonical_job_key, observed_at, content_hash)
VALUES (?, ?, ?, ?)
ON CONFLICT(run_id, canonical_job_key) DO UPDATE SET
  observed_at = excluded.observed_at,
  content_hash = excluded.content_hash
"""

UPSERT_SOURCE_STATE_SQL = (
    "INSERT INTO source_state (source_id) VALUES (?) ON CONFLICT(source_id) DO NOTHING"
)

LAST_COMPLETE_RUN_SQL = """
SELECT id FROM poll_runs
WHERE source_id = ? AND outcome = ? AND finished_at IS NOT NULL
ORDER BY COALESCE(finished_at, started_at) DESC, id DESC
LIMIT 1
"""

SNAPSHOT_STATE_SQL = """
SELECT canonical_job_key, content_hash, closed_at, missing_complete_runs
FROM jobs WHERE source_id = ?
"""

LAST_APPLIED_RUN_SQL = """
SELECT a.run_id, a.applied_at, r.page_count
FROM snapshot_applications a JOIN poll_runs r ON r.id = a.run_id
WHERE a.source_id = ?
ORDER BY a.applied_at DESC, a.run_id DESC
LIMIT 1
"""

RUN_OBSERVATIONS_SQL = "SELECT canonical_job_key FROM job_observations WHERE run_id = ?"

#: How long an unfinished run's observations survive the prune.
UNFINISHED_RUN_RETENTION = timedelta(days=1)

# Observations are read only for the run being applied, for each source's latest complete run, and
# for its latest applied snapshot. Every other run's rows are dead weight -- one row per posting per
# poll, about 2.6 million a day across the registry -- so the prune keeps exactly those runs. An
# unfinished run is kept for a day, which covers a laptop that slept mid-walk; a run a shutdown
# abandoned never finishes, so its rows go after that.
PRUNE_OBSERVATIONS_SQL = """
DELETE FROM job_observations
WHERE observed_at < ?
AND run_id NOT IN (SELECT id FROM poll_runs WHERE finished_at IS NULL AND started_at >= ?)
AND run_id NOT IN (
    SELECT id FROM (
        SELECT id, ROW_NUMBER() OVER (
            PARTITION BY source_id ORDER BY COALESCE(finished_at, started_at) DESC, id DESC
        ) AS rank
        FROM poll_runs WHERE outcome = ? AND finished_at IS NOT NULL
    ) WHERE rank = 1
)
AND run_id NOT IN (
    SELECT run_id FROM (
        SELECT run_id, ROW_NUMBER() OVER (
            PARTITION BY source_id ORDER BY applied_at DESC, run_id DESC
        ) AS rank
        FROM snapshot_applications
    ) WHERE rank = 1
)
"""

# Every dedupe key storage already holds as an active job, plus every dedupe key a delivered
# alert already covered. The service reads this before creating a deliverable event so one public
# posting is announced once even when two boards publish it.
ANNOUNCED_DEDUPE_SQL = """
SELECT j.dedupe_key, j.canonical_job_key FROM jobs j WHERE j.closed_at IS NULL
UNION
SELECT j.dedupe_key, j.canonical_job_key FROM job_events e
JOIN notification_event_links l ON l.event_id = e.id
JOIN notification_attempts a ON a.id = l.notification_attempt_id
JOIN jobs j ON j.canonical_job_key = e.canonical_job_key
WHERE a.outcome = ?
"""

PENDING_QUARANTINE_SQL = """
SELECT * FROM quarantined_snapshots WHERE source_id = ?
ORDER BY quarantined_at DESC, run_id DESC
LIMIT 1
"""


class Repository:
    """Transactional SQLite state store for poll runs, jobs, events, and notifications."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._depth = 0

    @classmethod
    def open(cls, path: str | Path) -> Repository:
        database = Path(path)
        database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database, isolation_level=None, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return cls(connection)

    @classmethod
    def open_read_only(cls, path: str | Path) -> Repository:
        """Open an existing database for reading, without creating or migrating it.

        A report reads state that some other command produced, so it must not create the file it was
        pointed at and must not apply a migration to it: an operator who mistypes a path should be
        told, not handed an empty report and a new database. SQLite's ``mode=ro`` URI makes that
        explicit, and a missing file is refused here rather than created by the connect call.
        """
        database = Path(path)
        if not database.is_file():
            raise FileNotFoundError(f"no state database at {database}")
        connection = sqlite3.connect(
            f"{database.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None, timeout=30.0
        )
        connection.row_factory = sqlite3.Row
        return cls(connection)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Repository:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a state change as one transaction; see :meth:`transaction`."""
        with self.transaction():
            yield self._connection

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run several repository calls as one atomic unit.

        The outermost call opens a `BEGIN IMMEDIATE` transaction; a call nested inside one only
        marks a savepoint, so a caller can compose the reviewed state-changing methods (stage
        observations, finish the run, apply the snapshot, record events) into the single commit a
        collection cycle needs before it attempts any external delivery. An error rolls the whole
        unit back, whichever depth it happened at.
        """
        connection = self._connection
        depth = self._depth
        savepoint = f"job_alert_{depth}"
        if depth == 0:
            connection.execute("BEGIN IMMEDIATE")
        else:
            connection.execute(f"SAVEPOINT {savepoint}")
        self._depth = depth + 1
        try:
            yield
        except BaseException:
            self._depth = depth
            if depth == 0:
                connection.execute("ROLLBACK")
            else:
                connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        self._depth = depth
        if depth == 0:
            connection.execute("COMMIT")
        else:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")

    def initialize(self) -> None:
        """Apply the initial migration to a fresh or an existing database.

        Safe to call repeatedly. Statements run one at a time so an ``ALTER TABLE ... ADD COLUMN``
        this file adds for a database created before the column existed is applied once and then
        skipped, which is how an existing file upgrades on ``initialize()`` without losing rows.
        """
        path = _schema_path()
        if not path.is_file():
            raise RuntimeError(f"schema migration file not found: {path}")
        try:
            schema = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"unable to read schema migration {path}: {exc}") from exc
        for statement in _schema_statements(schema):
            addition = _ADD_COLUMN.match(statement)
            if addition is not None and _column_exists(
                self._connection, addition["table"], addition["column"]
            ):
                continue
            self._connection.execute(statement)

    def start_run(
        self,
        source_id: str,
        started_at: datetime,
    ) -> int:
        """Open a poll run and return its id.

        Every run starts as `partial`, the only outcome an in-flight run may hold. A run that is
        never finished therefore stays non-complete and can never close jobs or become the last
        complete snapshot.
        """
        with self._transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO poll_runs (source_id, started_at, outcome) VALUES (?, ?, ?)",
                (source_id, _utc_isoformat(started_at), RunOutcome.PARTIAL.value),
            )
            if cursor.lastrowid is None:
                raise RuntimeError(f"failed to insert poll run for source {source_id}")
            run_id = int(cursor.lastrowid)
            connection.execute(UPSERT_SOURCE_STATE_SQL, (source_id,))
        return run_id

    def finish_run(
        self,
        run_id: int,
        outcome: RunOutcome,
        finished_at: datetime,
        *,
        jobs_seen: int = 0,
        http_status: int | None = None,
        page_count: int = 0,
        declared_total: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """Close a poll run and update source state.

        Every finished run moves `last_run_at`. Only `RunOutcome.COMPLETE` records
        `last_complete_at` and clears `backoff_until`. Nothing here touches `consecutive_failures`,
        `first_failure_at`, `last_error`, or the incident fields: the health layer is their single
        writer, :func:`job_alert.health.evaluate_source_health`, and :meth:`record_health_state`
        persists what it says. The health layer evaluates the state as it stood *before* this run,
        so a run recorded here and counted there counts once, whether it succeeded or failed. A
        deployment that wires only the repository still records what happened and when. No outcome
        changes job state.
        """
        with self._transaction() as connection:
            run = connection.execute(
                "SELECT source_id, finished_at FROM poll_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError(f"unknown poll run {run_id}")
            if run["finished_at"] is not None:
                raise ValueError(f"poll run {run_id} is already finished")
            source_id = str(run["source_id"])
            connection.execute(
                "UPDATE poll_runs SET finished_at = ?, outcome = ?, http_status = ?,"
                " jobs_seen = ?, page_count = ?, declared_total = ?, error_code = ?,"
                " error_message = ? WHERE id = ?",
                (
                    _utc_isoformat(finished_at),
                    str(outcome),
                    http_status,
                    jobs_seen,
                    page_count,
                    declared_total,
                    error_code,
                    error_message,
                    run_id,
                ),
            )
            connection.execute(UPSERT_SOURCE_STATE_SQL, (source_id,))
            finished = _utc_isoformat(finished_at)
            if outcome is RunOutcome.COMPLETE:
                connection.execute(
                    "UPDATE source_state SET last_complete_at = ?, backoff_until = NULL,"
                    " last_run_at = ? WHERE source_id = ?",
                    (finished, finished, source_id),
                )
            else:
                connection.execute(
                    "UPDATE source_state SET last_run_at = ? WHERE source_id = ?",
                    (finished, source_id),
                )

    def record_backoff(self, source_id: str, until: datetime | None) -> None:
        """Record the instant before which this source must not be polled again.

        The scheduler sets it after an incomplete cycle so a failing source costs one collection per
        backoff step instead of one per wake; a complete run clears it. `None` clears it too, so
        an operator can release a source deliberately.
        """
        with self._transaction() as connection:
            connection.execute(UPSERT_SOURCE_STATE_SQL, (source_id,))
            connection.execute(
                "UPDATE source_state SET backoff_until = ? WHERE source_id = ?",
                (_optional_isoformat(until), source_id),
            )

    def prune_observations(self, before: datetime) -> int:
        """Delete observations no later read can reach, and return how many went.

        Rows newer than ``before`` stay, and so do the rows of each source's latest complete run, of
        its latest applied snapshot, and of a run started within a day of ``before`` that has not
        finished yet, which are all a cycle ever reads.
        """
        with self._transaction() as connection:
            deleted = connection.execute(
                PRUNE_OBSERVATIONS_SQL,
                (
                    _utc_isoformat(before),
                    _utc_isoformat(before - UNFINISHED_RUN_RETENTION),
                    RunOutcome.COMPLETE.value,
                ),
            )
            return int(deleted.rowcount)

    def upsert_observations(
        self,
        run_id: int,
        jobs: Sequence[NormalizedJob],
        *,
        observed_at: datetime | None = None,
    ) -> None:
        """Stage one observation per job for a poll run.

        The earliest `first_seen_at` is preserved and the mutable descriptive fields are
        refreshed. `missing_complete_runs` and `closed_at` are left untouched, because a run that
        may still fail must not change absence or closure state; a complete run applies those
        decisions later through `apply_complete_snapshot()`.
        """
        observations = list(jobs)
        with self._transaction() as connection:
            run = connection.execute(
                "SELECT source_id, started_at FROM poll_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError(f"unknown poll run {run_id}")
            source_id = str(run["source_id"])
            foreign = sorted({job.source_id for job in observations if job.source_id != source_id})
            if foreign:
                raise ValueError(
                    f"observations for {foreign} do not belong to poll run {run_id}"
                    f" of source {source_id}"
                )
            moment = str(run["started_at"]) if observed_at is None else _utc_isoformat(observed_at)
            connection.executemany(
                UPSERT_JOB_SQL, [_job_parameters(job, moment) for job in observations]
            )
            connection.executemany(
                UPSERT_OBSERVATION_SQL,
                [
                    (run_id, job.canonical_job_key, moment, job.content_hash)
                    for job in observations
                ],
            )

    @staticmethod
    def _require_finished_complete_run(
        connection: sqlite3.Connection, run_id: int
    ) -> tuple[str, str]:
        """Return `(source_id, finished_at)` for a finalized complete run, else fail closed."""
        run = connection.execute(
            "SELECT source_id, outcome, finished_at FROM poll_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise ValueError(f"unknown poll run {run_id}")
        outcome = str(run["outcome"])
        finished_at = run["finished_at"]
        if outcome != RunOutcome.COMPLETE.value or finished_at is None:
            raise IncompleteRunError(
                f"poll run {run_id} is {outcome} with finished_at={finished_at}; an"
                " unfinished or non-complete run cannot change job absence state"
            )
        return str(run["source_id"]), str(finished_at)

    def apply_complete_snapshot(
        self,
        run_id: int,
        *,
        missing_keys: Iterable[str] = (),
        reopened_keys: Iterable[str] = (),
        close_after: int = DEFAULT_CLOSE_AFTER,
    ) -> list[str]:
        """Apply the absence and reopening decisions of one finalized complete snapshot.

        The referenced run must have finished as `complete` and must not have been applied
        before: the run is claimed in `snapshot_applications` inside this transaction, so a
        repeated call for the same run is a no-op that increments nothing. Jobs observed by the
        run start a fresh absence streak, `reopened_keys` clear `closed_at` for jobs this run
        actually observed, and `missing_keys` increment `missing_complete_runs` and close the job
        once the streak reaches `close_after`. Returns the keys closed by this application.
        """
        if close_after < 1:
            raise ValueError(f"close_after must be at least 1, got {close_after}")
        missing = list(dict.fromkeys(missing_keys))
        reopening = list(dict.fromkeys(reopened_keys))
        with self._transaction() as connection:
            source_id, finished_at = self._require_finished_complete_run(connection, run_id)
            applied_at = _utc_isoformat(_parse_timestamp(finished_at) or datetime.now(UTC))
            observed = {
                str(row["canonical_job_key"])
                for row in connection.execute(
                    "SELECT canonical_job_key FROM job_observations WHERE run_id = ?", (run_id,)
                )
            }
            contradictory = sorted(observed.intersection(missing))
            if contradictory:
                raise ValueError(
                    f"poll run {run_id} reports {contradictory} as both observed and missing"
                )
            claim = connection.execute(
                "INSERT OR IGNORE INTO snapshot_applications (run_id, source_id, applied_at)"
                " VALUES (?, ?, ?)",
                (run_id, source_id, applied_at),
            )
            if claim.rowcount == 0:
                return []
            connection.execute(
                "UPDATE jobs SET missing_complete_runs = 0 WHERE source_id = ?"
                " AND canonical_job_key IN (SELECT canonical_job_key FROM job_observations"
                " WHERE run_id = ?)",
                (source_id, run_id),
            )
            connection.executemany(
                "UPDATE jobs SET closed_at = NULL, missing_complete_runs = 0"
                " WHERE canonical_job_key = ? AND source_id = ? AND canonical_job_key IN"
                " (SELECT canonical_job_key FROM job_observations WHERE run_id = ?)",
                [(key, source_id, run_id) for key in reopening],
            )
            closed: list[str] = []
            for key in missing:
                job = connection.execute(
                    "SELECT missing_complete_runs FROM jobs WHERE canonical_job_key = ?"
                    " AND source_id = ? AND closed_at IS NULL",
                    (key, source_id),
                ).fetchone()
                if job is None:
                    continue
                missing_complete_runs = int(job["missing_complete_runs"]) + 1
                closing = missing_complete_runs >= close_after
                if closing:
                    closed.append(key)
                connection.execute(
                    "UPDATE jobs SET missing_complete_runs = ?, closed_at = ?"
                    " WHERE canonical_job_key = ?",
                    (missing_complete_runs, applied_at if closing else None, key),
                )
            return closed

    def apply_absent_jobs(
        self,
        run_id: int,
        missing_keys: Iterable[str],
        *,
        close_after: int = DEFAULT_CLOSE_AFTER,
    ) -> list[str]:
        """Absence-only convenience wrapper around `apply_complete_snapshot()`."""
        return self.apply_complete_snapshot(
            run_id, missing_keys=missing_keys, close_after=close_after
        )

    def record_events(self, run_id: int, events: Sequence[JobEvent]) -> list[int]:
        """Persist durable job events for a run and return their ids, newest call wins."""
        with self._transaction() as connection:
            run = connection.execute(
                "SELECT source_id FROM poll_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError(f"unknown poll run {run_id}")
            source_id = str(run["source_id"])
            foreign = sorted(
                {event.job.source_id for event in events if event.job.source_id != source_id}
            )
            if foreign:
                raise ValueError(
                    f"events for {foreign} do not belong to poll run {run_id} of source {source_id}"
                )
            event_ids: list[int] = []
            for event in events:
                event_type = str(event.event_type)
                connection.execute(
                    "INSERT INTO job_events (canonical_job_key, event_type, detected_at, run_id)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(canonical_job_key, event_type, run_id) DO NOTHING",
                    (
                        event.job.canonical_job_key,
                        event_type,
                        _utc_isoformat(event.detected_at),
                        run_id,
                    ),
                )
                stored = connection.execute(
                    "SELECT id FROM job_events WHERE canonical_job_key = ? AND event_type = ?"
                    " AND run_id = ?",
                    (event.job.canonical_job_key, event_type, run_id),
                ).fetchone()
                if stored is None:
                    raise RuntimeError(f"failed to persist event for {event.job.canonical_job_key}")
                event_ids.append(int(stored["id"]))
            return event_ids

    def record_notification(
        self,
        provider: str,
        outcome: str,
        attempted_at: datetime,
        *,
        provider_request_id: str | None = None,
        http_status: int | None = None,
        error_message: str | None = None,
        retry_after_seconds: float | None = None,
        quota_limit: int | None = None,
        quota_remaining: int | None = None,
        quota_reset_at: datetime | None = None,
        event_ids: Iterable[int] = (),
    ) -> int:
        """Persist a delivery attempt and link it to the job events it covered.

        The provider's own retry signal, when it sent one, is stored as given. A certain no-send
        failure that carries no signal gets the durable layer's own backoff for this attempt
        number, so the schedule an unreachable provider is kept on is visible in the table instead
        of being implied by the poll cadence. Pushover's ``X-Limit-App-*`` figures ride along on
        the attempt that reported them, which is where the digest reads them.

        Recording the attempt that reaches :data:`DEFAULT_MAX_RETRYABLE_ATTEMPTS` also writes one
        ``abandoned`` row for each event that hit the cap, which ends the question: the events stop
        being pending, and the row's message says why. An ambiguous outcome is capped separately by
        the service's :data:`DEFAULT_MAX_UNCERTAIN_RETRIES`, which is tighter and fires first.
        """
        covered = list(dict.fromkeys(event_ids))
        # A provider's own delay is advice: recorded inside the durable ceiling, so a pathological
        # header cannot hold an alert back indefinitely, and never negative.
        retry_after_seconds = bounded_retry_after(retry_after_seconds)
        with self._transaction() as connection:
            if (
                retry_after_seconds is None
                and outcome == DeliveryOutcome.RETRYABLE.value
                and covered
            ):
                already = _attempt_counts(connection, covered, NON_TERMINAL_DELIVERY_OUTCOMES)
                attempt_number = max(already.values(), default=0) + 1
                retry_after_seconds = retry_backoff_seconds(attempt_number)
            cursor = connection.execute(
                "INSERT INTO notification_attempts (provider, attempted_at, outcome,"
                " provider_request_id, http_status, error_message, retry_after_seconds,"
                " quota_limit, quota_remaining, quota_reset_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    provider,
                    _utc_isoformat(attempted_at),
                    outcome,
                    provider_request_id,
                    http_status,
                    error_message,
                    retry_after_seconds,
                    quota_limit,
                    quota_remaining,
                    _optional_isoformat(quota_reset_at),
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError(f"failed to insert notification attempt for {provider}")
            attempt_id = int(cursor.lastrowid)
            connection.executemany(
                "INSERT INTO notification_event_links (notification_attempt_id, event_id)"
                " VALUES (?, ?) ON CONFLICT DO NOTHING",
                [(attempt_id, event_id) for event_id in covered],
            )
            if outcome == DeliveryOutcome.RETRYABLE.value and covered:
                self._abandon_exhausted_retryable(
                    connection,
                    provider=provider,
                    attempted_at=attempted_at,
                    event_ids=covered,
                )
        return attempt_id

    @staticmethod
    def _abandon_exhausted_retryable(
        connection: sqlite3.Connection,
        *,
        provider: str,
        attempted_at: datetime,
        event_ids: Sequence[int],
    ) -> None:
        """End the question for events that have spent the no-send retry budget.

        This is the cap the unbounded class needed: without it an unreachable provider would keep
        collecting one ``retryable`` row per batch per cycle forever. The row written here is
        terminal, so the events leave ``pending_events()`` and the attempt count stops growing.
        """
        counts = _attempt_counts(connection, event_ids, NON_TERMINAL_DELIVERY_OUTCOMES)
        exhausted = sorted(
            event_id
            for event_id in event_ids
            if counts.get(event_id, 0) >= DEFAULT_MAX_RETRYABLE_ATTEMPTS
        )
        if not exhausted:
            return
        cursor = connection.execute(
            "INSERT INTO notification_attempts (provider, attempted_at, outcome, error_message)"
            " VALUES (?, ?, ?, ?)",
            (
                provider,
                _utc_isoformat(attempted_at),
                DeliveryOutcome.ABANDONED.value,
                f"abandoned after {DEFAULT_MAX_RETRYABLE_ATTEMPTS} unanswered attempts with a"
                f" {retry_backoff_seconds(DEFAULT_MAX_RETRYABLE_ATTEMPTS):.0f}s final backoff:"
                " the provider never accepted the message",
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError(f"failed to record the abandoned attempt for {provider}")
        abandoned_id = int(cursor.lastrowid)
        connection.executemany(
            "INSERT INTO notification_event_links (notification_attempt_id, event_id)"
            " VALUES (?, ?) ON CONFLICT DO NOTHING",
            [(abandoned_id, event_id) for event_id in exhausted],
        )

    def load_active_jobs(self, source_id: str) -> list[NormalizedJob]:
        """Return every job observed for a source that a complete snapshot has not closed."""
        rows = self._connection.execute(
            "SELECT * FROM jobs WHERE source_id = ? AND closed_at IS NULL"
            " ORDER BY canonical_job_key",
            (source_id,),
        ).fetchall()
        return [_job_from_row(row) for row in rows]

    def first_detections(self) -> dict[str, datetime]:
        """When each posting was first detected as new or reopened, by canonical job key.

        A posting a baseline seeded has no such event: it was already open when monitoring began,
        so it is absent here rather than given the seeding instant.
        """
        rows = self._connection.execute(
            "SELECT canonical_job_key, MIN(detected_at) AS detected_at FROM job_events"
            " WHERE event_type IN (?, ?) GROUP BY canonical_job_key",
            (JobEventType.NEW.value, JobEventType.REOPENED.value),
        ).fetchall()
        detections: dict[str, datetime] = {}
        for row in rows:
            moment = _parse_timestamp(row["detected_at"])
            if moment is not None:
                detections[str(row["canonical_job_key"])] = moment
        return detections

    def detections_since(self, since: datetime) -> list[tuple[NormalizedJob, datetime, bool]]:
        """Postings detected as new or reopened at or after ``since``, newest first.

        Each is returned with its latest such detection and whether it has closed since.
        """
        rows = self._connection.execute(
            "SELECT jobs.*, latest.detected_at AS detected_at FROM jobs JOIN ("
            " SELECT canonical_job_key, MAX(detected_at) AS detected_at FROM job_events"
            " WHERE event_type IN (?, ?) AND detected_at >= ? GROUP BY canonical_job_key"
            ") AS latest USING (canonical_job_key) ORDER BY latest.detected_at DESC",
            (JobEventType.NEW.value, JobEventType.REOPENED.value, _utc_isoformat(since)),
        ).fetchall()
        detections: list[tuple[NormalizedJob, datetime, bool]] = []
        for row in rows:
            moment = _parse_timestamp(row["detected_at"])
            if moment is not None:
                detections.append((_job_from_row(row), moment, row["closed_at"] is not None))
        return detections

    def last_complete_job_keys(self, source_id: str) -> set[str]:
        """Return the canonical keys observed by the most recent complete run of a source.

        Later failed, partial, challenged, or quarantined runs cannot change this set.
        """
        row = self._connection.execute(
            LAST_COMPLETE_RUN_SQL, (source_id, RunOutcome.COMPLETE.value)
        ).fetchone()
        if row is None:
            return set()
        observed = self._connection.execute(
            "SELECT canonical_job_key FROM job_observations WHERE run_id = ?", (int(row["id"]),)
        ).fetchall()
        return {str(observation["canonical_job_key"]) for observation in observed}

    def source_health(self, source_id: str) -> SourceState | None:
        """Return persisted source state, or `None` when the source has never been polled."""
        row = self._connection.execute(
            "SELECT * FROM source_state WHERE source_id = ?", (source_id,)
        ).fetchone()
        if row is None:
            return None
        return _source_state_from_row(row)

    def source_states(self) -> list[SourceState]:
        """Every persisted source state, ordered by source id so callers can diff two reads."""
        rows = self._connection.execute(
            "SELECT * FROM source_state ORDER BY source_id"
        ).fetchall()
        return [_source_state_from_row(row) for row in rows]

    def record_health_state(self, source_id: str, state: SourceHealthState) -> None:
        """Persist the incident record one health evaluation produced.

        ``consecutive_failures`` is written too, so the streak the health policy reasoned about and
        the streak a later digest reads are the same number. ``source_id`` on the record is not
        trusted; the argument names the row. An incident that is open must carry the instant it
        opened, which keeps the two fields from drifting apart.

        ``last_complete_at`` is deliberately not written here: freshness belongs to
        :meth:`finish_run`, which sees the run that actually completed, and a health evaluation
        reads the state from before its own run.
        """
        if state.consecutive_failures < 0:
            raise ValueError("consecutive_failures must not be negative")
        if state.health_alert_open and state.health_opened_at is None:
            raise ValueError(
                f"source {source_id} cannot have an open incident without its opening time"
            )
        with self._transaction() as connection:
            connection.execute(UPSERT_SOURCE_STATE_SQL, (source_id,))
            connection.execute(
                "UPDATE source_state SET consecutive_failures = ?, health_alert_open = ?,"
                " health_opened_at = ?, health_reason = ?, first_failure_at = ?, last_error = ?"
                " WHERE source_id = ?",
                (
                    state.consecutive_failures,
                    1 if state.health_alert_open else 0,
                    _optional_isoformat(state.health_opened_at),
                    state.health_reason,
                    _optional_isoformat(state.first_failure_at),
                    state.last_error,
                    source_id,
                ),
            )

    def record_conditional_state(
        self,
        source_id: str,
        *,
        etag: str | None,
        last_modified: str | None,
    ) -> None:
        """Replace the stored validators for a source with the ones a trustworthy body carried.

        A caller writes this only after a valid response, so a validator never describes a body
        the monitor refused to trust: ``None`` clears a validator the provider stopped sending
        rather than leaving a stale one behind for the next conditional request.
        """
        with self._transaction() as connection:
            connection.execute(UPSERT_SOURCE_STATE_SQL, (source_id,))
            connection.execute(
                "UPDATE source_state SET etag = ?, last_modified = ? WHERE source_id = ?",
                (etag, last_modified, source_id),
            )

    def snapshot_state(self, source_id: str) -> dict[str, dict[str, object]]:
        """Return the stored state a comparison needs, keyed by canonical job key.

        Each value carries the ``content_hash``, ``closed_at``, and ``missing_complete_runs`` of
        one job, which is exactly the shape ``compare_snapshot()`` reads. Closed jobs are
        included, because that is how a reappearing posting is classified as reopened rather than
        new.
        """
        rows = self._connection.execute(SNAPSHOT_STATE_SQL, (source_id,)).fetchall()
        return {
            str(row["canonical_job_key"]): {
                "content_hash": row["content_hash"],
                "closed_at": row["closed_at"],
                "missing_complete_runs": int(row["missing_complete_runs"]),
            }
            for row in rows
        }

    def last_applied_snapshot(self, source_id: str) -> AppliedSnapshot | None:
        """Return the most recent complete snapshot that actually changed job state.

        ``None`` means no complete snapshot has ever been applied to this source, which is
        different from an applied snapshot that observed no jobs at all.
        """
        row = self._connection.execute(LAST_APPLIED_RUN_SQL, (source_id,)).fetchone()
        if row is None:
            return None
        applied_at = _parse_timestamp(row["applied_at"])
        if applied_at is None:
            raise ValueError(f"snapshot application for source {source_id} has no applied_at")
        observed = self._connection.execute(
            RUN_OBSERVATIONS_SQL, (int(row["run_id"]),)
        ).fetchall()
        return AppliedSnapshot(
            run_id=int(row["run_id"]),
            applied_at=applied_at,
            page_count=int(row["page_count"]),
            canonical_job_keys=frozenset(
                str(observation["canonical_job_key"]) for observation in observed
            ),
        )

    def load_job(self, canonical_job_key: str) -> NormalizedJob | None:
        """Return one stored job, or `None` when the key is unknown."""
        row = self._connection.execute(
            "SELECT * FROM jobs WHERE canonical_job_key = ?", (canonical_job_key,)
        ).fetchone()
        return None if row is None else _job_from_row(row)

    def announced_dedupe_keys(self) -> dict[str, frozenset[str]]:
        """Dedupe keys a public posting is already represented by, with the job keys that do it.

        A posting counts as represented when an active stored job carries its dedupe key or when a
        delivered alert covered an event for one. Reading this before a cycle stages anything, and
        comparing against the candidate's own canonical key, is what keeps one posting from being
        announced twice across boards while still letting a job that reopened under its own key
        be delivered again.
        """
        rows = self._connection.execute(
            ANNOUNCED_DEDUPE_SQL, (DeliveryOutcome.DELIVERED.value,)
        ).fetchall()
        index: dict[str, set[str]] = {}
        for row in rows:
            index.setdefault(str(row["dedupe_key"]), set()).add(str(row["canonical_job_key"]))
        return {dedupe_key: frozenset(keys) for dedupe_key, keys in index.items()}

    def pending_events(
        self,
        source_id: str,
        *,
        event_types: Sequence[JobEventType] = (JobEventType.NEW, JobEventType.REOPENED),
        now: datetime | None = None,
    ) -> list[PendingEvent]:
        """Return the durable events of one source that still need a delivery decision.

        An event stays pending until an attempt covering it ends the question — that is, until one
        ends as ``delivered``, ``permanent``, or ``abandoned``. Pending events are returned oldest
        first so a retry keeps the order the events were detected in.

        When `now` is given, an event whose newest unanswered attempt recorded a
        `retry_after_seconds` delay that has not elapsed is left out: the recorded backoff decides
        when a retry may be spent rather than the poll cadence deciding for it. An event with no
        recorded delay -- one nothing has attempted yet, or one whose attempt carried no delay -- is
        always due. Passing no `now` returns the unanswered set without that filter, which is what
        an operator's report of outstanding work wants.
        """
        types = [str(event_type) for event_type in event_types]
        if not types:
            return []
        terminals = [outcome.value for outcome in TERMINAL_DELIVERY_OUTCOMES]
        type_placeholders = ", ".join("?" for _ in types)
        terminal_placeholders = ", ".join("?" for _ in terminals)
        rows = self._connection.execute(
            "SELECT e.id, e.event_type, e.detected_at, e.run_id, j.canonical_job_key"
            " FROM job_events e JOIN jobs j ON j.canonical_job_key = e.canonical_job_key"
            f" WHERE j.source_id = ? AND e.event_type IN ({type_placeholders})"
            " AND NOT EXISTS (SELECT 1 FROM notification_event_links l"
            " JOIN notification_attempts a ON a.id = l.notification_attempt_id"
            f" WHERE l.event_id = e.id AND a.outcome IN ({terminal_placeholders}))"
            " ORDER BY e.detected_at, e.id",
            (source_id, *types, *terminals),
        ).fetchall()
        pending: list[PendingEvent] = []
        for row in rows:
            detected_at = _parse_timestamp(row["detected_at"])
            if detected_at is None:
                raise ValueError(f"event {row['id']} has no detected_at")
            pending.append(
                PendingEvent(
                    event_id=int(row["id"]),
                    event_type=JobEventType(str(row["event_type"])),
                    canonical_job_key=str(row["canonical_job_key"]),
                    detected_at=detected_at,
                    run_id=int(row["run_id"]),
                )
            )
        if now is None:
            return pending
        due = self._events_past_backoff(pending, now=now)
        return [event for event in pending if event.event_id in due]

    def _events_past_backoff(
        self, events: Sequence[PendingEvent], *, now: datetime
    ) -> set[int]:
        """Which of these events are past the delay their newest unanswered attempt recorded.

        The newest attempt is the one that matters: a provider's own `Retry-After` replaces the
        delay the durable layer chose, and the later attempt is the more recent answer.
        """
        if not events:
            return set()
        event_ids = [event.event_id for event in events]
        event_placeholders = ", ".join("?" for _ in event_ids)
        outcomes = [outcome.value for outcome in NON_TERMINAL_DELIVERY_OUTCOMES]
        outcome_placeholders = ", ".join("?" for _ in outcomes)
        rows = self._connection.execute(
            "SELECT l.event_id, a.attempted_at, a.retry_after_seconds"
            " FROM notification_event_links l"
            " JOIN notification_attempts a ON a.id = l.notification_attempt_id"
            f" WHERE l.event_id IN ({event_placeholders})"
            f" AND a.outcome IN ({outcome_placeholders})"
            " ORDER BY a.attempted_at, a.id",
            (*event_ids, *outcomes),
        ).fetchall()
        newest: dict[int, tuple[datetime, float | None]] = {}
        for row in rows:
            attempted_at = _parse_timestamp(row["attempted_at"])
            if attempted_at is None:
                continue
            delay = row["retry_after_seconds"]
            newest[int(row["event_id"])] = (
                attempted_at,
                None if delay is None else float(delay),
            )
        moment = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
        due: set[int] = set()
        for event_id in event_ids:
            recorded = newest.get(event_id)
            if recorded is None:
                due.add(event_id)
                continue
            attempted_at, delay = recorded
            if delay is None or attempted_at + timedelta(seconds=delay) <= moment:
                due.add(event_id)
        return due

    def attempt_counts(
        self, event_ids: Iterable[int], *, outcome: DeliveryOutcome
    ) -> dict[int, int]:
        """How many recorded attempts with one outcome already cover each of these events."""
        return _attempt_counts(self._connection, event_ids, (outcome.value,))

    def latest_quota(self, provider: str) -> QuotaSnapshot | None:
        """The newest recorded provider quota for one provider, or `None` when none was reported.

        The digest reports the provider's remaining monthly allowance and reset instant from here,
        so a run of failures cannot quietly discard the last figures the provider sent.
        """
        row = self._connection.execute(
            "SELECT provider, attempted_at, quota_limit, quota_remaining, quota_reset_at"
            " FROM notification_attempts WHERE provider = ? AND quota_remaining IS NOT NULL"
            " ORDER BY attempted_at DESC, id DESC LIMIT 1",
            (provider,),
        ).fetchone()
        if row is None:
            return None
        observed_at = _parse_timestamp(row["attempted_at"])
        if observed_at is None:
            raise ValueError(f"notification attempt for {provider} has no attempted_at")
        return QuotaSnapshot(
            provider=str(row["provider"]),
            limit=None if row["quota_limit"] is None else int(row["quota_limit"]),
            remaining=int(row["quota_remaining"]),
            reset_at=_parse_timestamp(row["quota_reset_at"]),
            observed_at=observed_at,
        )

    def window_counts(self, started_at: datetime, finished_at: datetime) -> WindowCounts:
        """Count one reporting window's job events and delivery attempts from stored history.

        The window is half open -- ``started_at`` inclusive, ``finished_at`` exclusive -- and every
        figure comes from the database, so a digest is reproducible from the same window and never
        depends on what the reporting process remembers.
        """
        start = _utc_isoformat(started_at)
        end = _utc_isoformat(finished_at)
        events = self._connection.execute(
            "SELECT event_type, COUNT(*) AS total FROM job_events"
            " WHERE detected_at >= ? AND detected_at < ? GROUP BY event_type",
            (start, end),
        ).fetchall()
        event_totals = {str(row["event_type"]): int(row["total"]) for row in events}
        attempts = self._connection.execute(
            "SELECT outcome, COUNT(*) AS total FROM notification_attempts"
            " WHERE attempted_at >= ? AND attempted_at < ? GROUP BY outcome",
            (start, end),
        ).fetchall()
        attempt_totals = {str(row["outcome"]): int(row["total"]) for row in attempts}
        return WindowCounts(
            new_jobs=event_totals.get(JobEventType.NEW.value, 0),
            reopened_jobs=event_totals.get(JobEventType.REOPENED.value, 0),
            attempts=sum(
                total
                for outcome, total in attempt_totals.items()
                if outcome != DeliveryOutcome.FILTERED.value
            ),
            delivered=attempt_totals.get(DeliveryOutcome.DELIVERED.value, 0),
            retryable=attempt_totals.get(DeliveryOutcome.RETRYABLE.value, 0),
            uncertain=attempt_totals.get(DeliveryOutcome.UNCERTAIN.value, 0),
            permanent=attempt_totals.get(DeliveryOutcome.PERMANENT.value, 0),
            abandoned=attempt_totals.get(DeliveryOutcome.ABANDONED.value, 0),
        )

    def prune_notification_attempts(
        self,
        *,
        now: datetime,
        retention_days: int = NOTIFICATION_ATTEMPT_RETENTION_DAYS,
    ) -> int:
        """Drop old unanswered attempt rows nothing is waiting on any more, with their links.

        Two kinds of row survive. A terminal row is the record that ended an event's question:
        deleting one would make its events pending again and could resend an alert, so those rows
        stay for good. An *unanswered* row survives while any event it covers is still unanswered,
        because :data:`DEFAULT_MAX_RETRYABLE_ATTEMPTS` is a budget counted in exactly those rows:
        deleting them would hand the same event a fresh budget every retention window, which is how
        "8 attempts per event" quietly became "8 per window". Once every event a row covers has a
        terminal answer, the row is history and the retention window applies to it. Returns the rows
        removed.
        """
        if retention_days < 1:
            raise ValueError(f"retention_days must be at least 1, got {retention_days}")
        cutoff = _utc_isoformat(now - timedelta(days=retention_days))
        with self._transaction() as connection:
            placeholders = ", ".join("?" for _ in NON_TERMINAL_DELIVERY_OUTCOMES)
            stale = connection.execute(
                "SELECT id FROM notification_attempts"
                f" WHERE outcome IN ({placeholders}) AND attempted_at < ?",
                (*[outcome.value for outcome in NON_TERMINAL_DELIVERY_OUTCOMES], cutoff),
            ).fetchall()
            attempt_ids = _attempts_every_event_has_answered(
                connection, [int(row["id"]) for row in stale]
            )
            if not attempt_ids:
                return 0
            ids = ", ".join("?" for _ in attempt_ids)
            connection.execute(
                f"DELETE FROM notification_event_links WHERE notification_attempt_id IN ({ids})",
                attempt_ids,
            )
            connection.execute(
                f"DELETE FROM notification_attempts WHERE id IN ({ids})", attempt_ids
            )
            return len(attempt_ids)

    def record_quarantine(
        self,
        run_id: int,
        *,
        source_id: str,
        previous_job_count: int,
        candidate_keys: Iterable[str],
        quarantined_at: datetime,
    ) -> None:
        """Persist a withheld candidate, replacing any quarantine already pending for the source.

        One source holds at most one pending candidate: a later complete snapshot that drops just
        as far is the new thing to confirm, not a second outstanding question.
        """
        keys = sorted(set(candidate_keys))
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM quarantined_snapshots WHERE source_id = ?", (source_id,)
            )
            connection.execute(
                "INSERT INTO quarantined_snapshots (run_id, source_id, quarantined_at,"
                " previous_job_count, candidate_job_count, candidate_keys_json)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    source_id,
                    _utc_isoformat(quarantined_at),
                    previous_job_count,
                    len(keys),
                    json.dumps(keys),
                ),
            )

    def pending_quarantine(self, source_id: str) -> QuarantinedSnapshot | None:
        """Return the candidate a source is waiting to see confirmed, if any."""
        row = self._connection.execute(PENDING_QUARANTINE_SQL, (source_id,)).fetchone()
        if row is None:
            return None
        quarantined_at = _parse_timestamp(row["quarantined_at"])
        if quarantined_at is None:
            raise ValueError(f"quarantine for source {source_id} has no quarantined_at")
        return QuarantinedSnapshot(
            run_id=int(row["run_id"]),
            source_id=str(row["source_id"]),
            quarantined_at=quarantined_at,
            previous_job_count=int(row["previous_job_count"]),
            candidate_job_count=int(row["candidate_job_count"]),
            candidate_keys=frozenset(json.loads(row["candidate_keys_json"])),
        )

    def clear_quarantine(self, source_id: str) -> None:
        """Drop the pending candidate once a snapshot has been confirmed or superseded."""
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM quarantined_snapshots WHERE source_id = ?", (source_id,)
            )
