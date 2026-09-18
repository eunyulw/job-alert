from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from job_alert.models import JobEvent, NormalizedJob, RunOutcome

SCHEMA_ENV_VAR = "JOB_ALERT_SCHEMA_PATH"
MIGRATION_NAME = "001_initial.sql"
DEFAULT_CLOSE_AFTER = 2


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


# A later sighting refreshes the mutable fields, keeps the earliest first_seen_at, and leaves
# closed_at alone: reopening is a diff decision, not an upsert side effect.
UPSERT_JOB_SQL = """
INSERT INTO jobs (
  canonical_job_key, dedupe_key, company_id, source_id, source_job_id, requisition_id,
  title, locations_json, department, team, employment_type, workplace_type, description_text,
  content_hash, posting_url, apply_url, source_published_at, source_updated_at, first_seen_at,
  last_seen_at, missing_complete_runs, raw_metadata_json
) VALUES (
  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?
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
  missing_complete_runs = 0,
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
WHERE source_id = ? AND outcome = ?
ORDER BY COALESCE(finished_at, started_at) DESC, id DESC
LIMIT 1
"""


class Repository:
    """Transactional SQLite state store for poll runs, jobs, events, and notifications."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

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
        """Run a state change as one `BEGIN IMMEDIATE` transaction."""
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")

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
        outcome: RunOutcome = RunOutcome.PARTIAL,
    ) -> int:
        """Open a poll run and return its id.

        A run that is never finished stays non-complete, so a crashed or abandoned run can
        never close jobs.
        """
        with self._transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO poll_runs (source_id, started_at, outcome) VALUES (?, ?, ?)",
                (source_id, _utc_isoformat(started_at), str(outcome)),
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
        reopened_keys: Iterable[str] = (),
        observed_at: datetime | None = None,
    ) -> None:
        """Record one observation per job for a poll run.

        The earliest `first_seen_at` is preserved. `closed_at` is cleared only for keys the
        caller classified as reopened.
        """
        observations = list(jobs)
        reopening = list(dict.fromkeys(reopened_keys))
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
            connection.executemany(
                "UPDATE jobs SET closed_at = NULL, missing_complete_runs = 0"
                " WHERE canonical_job_key = ? AND source_id = ?",
                [(key, source_id) for key in reopening],
            )

    def apply_absent_jobs(
        self,
        run_id: int,
        missing_keys: Iterable[str],
        *,
        close_after: int = DEFAULT_CLOSE_AFTER,
    ) -> list[str]:
        """Count jobs absent from a complete snapshot and close them at the threshold.

        The referenced poll run must have finished as `complete`; any other outcome raises
        `IncompleteRunError` so that a partial, failed, challenged, or quarantined run can
        never change absence state or close a job.
        """
        if close_after < 1:
            raise ValueError(f"close_after must be at least 1, got {close_after}")
        with self._transaction() as connection:
            run = connection.execute(
                "SELECT source_id, outcome, finished_at, started_at FROM poll_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise ValueError(f"unknown poll run {run_id}")
            outcome = str(run["outcome"])
            if outcome != RunOutcome.COMPLETE.value:
                raise IncompleteRunError(
                    f"poll run {run_id} finished as {outcome}; an incomplete run cannot change"
                    " job absence state"
                )
            source_id = str(run["source_id"])
            detected_at = _utc_isoformat(
                _parse_timestamp(run["finished_at"])
                or _parse_timestamp(run["started_at"])
                or datetime.now(UTC)
            )
            closed: list[str] = []
            for key in dict.fromkeys(missing_keys):
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
                    (missing_complete_runs, detected_at if closing else None, key),
                )
            return closed

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
