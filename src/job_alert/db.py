from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from job_alert.models import JobEvent, JobEventType, NormalizedJob, RunOutcome

SCHEMA_ENV_VAR = "JOB_ALERT_SCHEMA_PATH"
MIGRATION_NAME = "001_initial.sql"
DEFAULT_CLOSE_AFTER = 2

#: How many ambiguous delivery attempts one event may collect before the durable layer abandons it.
DEFAULT_MAX_UNCERTAIN_RETRIES = 2


class DeliveryOutcome(StrEnum):
    """What one provider attempt did, as the durable layer records it.

    ``DELIVERED`` links the event ids the message covered, ``RETRYABLE`` and ``UNCERTAIN`` leave
    them pending for a later cycle, ``PERMANENT`` ends the question because the provider refused
    the message for good, and ``ABANDONED`` ends it because an ambiguous outcome has been retried
    as often as the service allows. Only the last two are terminal without a delivery.
    """

    DELIVERED = "delivered"
    RETRYABLE = "retryable"
    UNCERTAIN = "uncertain"
    PERMANENT = "permanent"
    ABANDONED = "abandoned"


#: Outcomes that stop a cycle from ever re-attempting the events an attempt covered.
TERMINAL_DELIVERY_OUTCOMES = (
    DeliveryOutcome.DELIVERED,
    DeliveryOutcome.PERMANENT,
    DeliveryOutcome.ABANDONED,
)


class IncompleteRunError(RuntimeError):
    """Raised when a run that is not `complete` is asked to change job absence state."""


@dataclass(frozen=True)
class SourceState:
    """Persisted polling state for one source, including its failure streak and validators."""

    source_id: str
    etag: str | None
    last_modified: str | None
    consecutive_failures: int
    last_complete_at: datetime | None
    backoff_until: datetime | None
    health_alert_open: bool


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


def _schema_path() -> Path:
    override = os.environ.get(SCHEMA_ENV_VAR)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "migrations" / MIGRATION_NAME


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
        """Apply the initial migration. Safe to call repeatedly; the DDL is idempotent."""
        path = _schema_path()
        if not path.is_file():
            raise RuntimeError(f"schema migration file not found: {path}")
        try:
            schema = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"unable to read schema migration {path}: {exc}") from exc
        self._connection.executescript(schema)

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

        Only `RunOutcome.COMPLETE` records `last_complete_at` and clears the failure streak.
        Every other outcome increments `consecutive_failures` and leaves job state untouched.
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
            if outcome is RunOutcome.COMPLETE:
                connection.execute(
                    "UPDATE source_state SET last_complete_at = ?, consecutive_failures = 0,"
                    " backoff_until = NULL WHERE source_id = ?",
                    (_utc_isoformat(finished_at), source_id),
                )
            else:
                connection.execute(
                    "UPDATE source_state SET consecutive_failures = consecutive_failures + 1"
                    " WHERE source_id = ?",
                    (source_id,),
                )

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
        event_ids: Iterable[int] = (),
    ) -> int:
        """Persist a delivery attempt and link it to the job events it covered."""
        with self._transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO notification_attempts (provider, attempted_at, outcome,"
                " provider_request_id, http_status, error_message) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    provider,
                    _utc_isoformat(attempted_at),
                    outcome,
                    provider_request_id,
                    http_status,
                    error_message,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError(f"failed to insert notification attempt for {provider}")
            attempt_id = int(cursor.lastrowid)
            connection.executemany(
                "INSERT INTO notification_event_links (notification_attempt_id, event_id)"
                " VALUES (?, ?) ON CONFLICT DO NOTHING",
                [(attempt_id, event_id) for event_id in event_ids],
            )
        return attempt_id

    def load_active_jobs(self, source_id: str) -> list[NormalizedJob]:
        """Return every job observed for a source that a complete snapshot has not closed."""
        rows = self._connection.execute(
            "SELECT * FROM jobs WHERE source_id = ? AND closed_at IS NULL"
            " ORDER BY canonical_job_key",
            (source_id,),
        ).fetchall()
        return [_job_from_row(row) for row in rows]

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
        return SourceState(
            source_id=str(row["source_id"]),
            etag=row["etag"],
            last_modified=row["last_modified"],
            consecutive_failures=int(row["consecutive_failures"]),
            last_complete_at=_parse_timestamp(row["last_complete_at"]),
            backoff_until=_parse_timestamp(row["backoff_until"]),
            health_alert_open=bool(row["health_alert_open"]),
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
    ) -> list[PendingEvent]:
        """Return the durable events of one source that still need a delivery decision.

        An event stays pending until an attempt covering it ends the question — that is, until one
        ends as ``delivered``, ``permanent``, or ``abandoned``. Pending events are returned oldest
        first so a retry keeps the order the events were detected in.
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
        return pending

    def attempt_counts(
        self, event_ids: Iterable[int], *, outcome: DeliveryOutcome
    ) -> dict[int, int]:
        """How many recorded attempts with one outcome already cover each of these events."""
        ids = list(dict.fromkeys(event_ids))
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        rows = self._connection.execute(
            "SELECT l.event_id AS event_id, COUNT(*) AS attempts FROM notification_event_links l"
            " JOIN notification_attempts a ON a.id = l.notification_attempt_id"
            f" WHERE a.outcome = ? AND l.event_id IN ({placeholders})"
            " GROUP BY l.event_id",
            (outcome.value, *ids),
        ).fetchall()
        return {int(row["event_id"]): int(row["attempts"]) for row in rows}

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
