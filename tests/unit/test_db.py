import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from job_alert.db import IncompleteRunError, Repository
from job_alert.models import JobEvent, JobEventType, NormalizedJob, RunOutcome


def make_job() -> NormalizedJob:
    return NormalizedJob(
        company_id="openai",
        source_id="openai-greenhouse",
        source_job_id="123",
        canonical_job_key="greenhouse:openai:123",
        dedupe_key="requisition:openai:research-123",
        title="Research Engineer",
        locations=["San Francisco, CA"],
        posting_url="https://example.com/jobs/123",
        apply_url="https://example.com/jobs/123/apply",
        first_seen_at=datetime(2026, 9, 18, tzinfo=UTC),
    )


def test_repository_persists_job_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    with Repository.open(path) as repo:
        repo.initialize()
        run_id = repo.start_run("openai-greenhouse", datetime.now(UTC))
        repo.upsert_observations(run_id, [make_job()])
        repo.finish_run(run_id, RunOutcome.COMPLETE, datetime.now(UTC), jobs_seen=1)
    with Repository.open(path) as repo:
        jobs = repo.load_active_jobs("openai-greenhouse")
        assert [job.canonical_job_key for job in jobs] == ["greenhouse:openai:123"]


def test_failed_run_does_not_change_last_complete_snapshot(tmp_path: Path) -> None:
    with Repository.open(tmp_path / "state.sqlite3") as repo:
        repo.initialize()
        first = repo.start_run("openai-greenhouse", datetime.now(UTC))
        repo.upsert_observations(first, [make_job()])
        repo.finish_run(first, RunOutcome.COMPLETE, datetime.now(UTC), jobs_seen=1)
        failed = repo.start_run("openai-greenhouse", datetime.now(UTC))
        repo.finish_run(failed, RunOutcome.FAILED, datetime.now(UTC), jobs_seen=0)
        assert repo.last_complete_job_keys("openai-greenhouse") == {
            "greenhouse:openai:123"
        }


def open_seeded_repo(tmp_path: Path) -> Repository:
    repo = Repository.open(tmp_path / "state.sqlite3")
    repo.initialize()
    run_id = repo.start_run("openai-greenhouse", datetime(2026, 9, 18, tzinfo=UTC))
    repo.upsert_observations(run_id, [make_job()])
    repo.finish_run(run_id, RunOutcome.COMPLETE, datetime(2026, 9, 18, tzinfo=UTC), jobs_seen=1)
    return repo


def test_incomplete_run_cannot_change_absence_state(tmp_path: Path) -> None:
    with open_seeded_repo(tmp_path) as repo:
        for outcome in (
            RunOutcome.PARTIAL,
            RunOutcome.FAILED,
            RunOutcome.CHALLENGED,
            RunOutcome.QUARANTINED,
        ):
            run_id = repo.start_run("openai-greenhouse", datetime(2026, 9, 19, tzinfo=UTC))
            repo.finish_run(run_id, outcome, datetime(2026, 9, 19, tzinfo=UTC), jobs_seen=0)
            with pytest.raises(IncompleteRunError, match="cannot change job absence state"):
                repo.apply_absent_jobs(run_id, ["greenhouse:openai:123"])
        assert [job.canonical_job_key for job in repo.load_active_jobs("openai-greenhouse")] == [
            "greenhouse:openai:123"
        ]
        assert repo.source_health("openai-greenhouse").consecutive_failures == 4


def test_complete_runs_close_job_only_after_absence_threshold(tmp_path: Path) -> None:
    with open_seeded_repo(tmp_path) as repo:
        first_absence = repo.start_run("openai-greenhouse", datetime(2026, 9, 19, tzinfo=UTC))
        repo.finish_run(
            first_absence, RunOutcome.COMPLETE, datetime(2026, 9, 19, tzinfo=UTC), jobs_seen=0
        )
        assert repo.apply_absent_jobs(first_absence, ["greenhouse:openai:123"]) == []
        assert repo.load_active_jobs("openai-greenhouse") != []

        second_absence = repo.start_run("openai-greenhouse", datetime(2026, 9, 20, tzinfo=UTC))
        repo.finish_run(
            second_absence, RunOutcome.COMPLETE, datetime(2026, 9, 20, tzinfo=UTC), jobs_seen=0
        )
        assert repo.apply_absent_jobs(second_absence, ["greenhouse:openai:123"]) == [
            "greenhouse:openai:123"
        ]
        assert repo.load_active_jobs("openai-greenhouse") == []


def test_first_seen_at_is_earliest_and_reopen_clears_closed_at(tmp_path: Path) -> None:
    with open_seeded_repo(tmp_path) as repo:
        for day in (19, 20):
            absence = repo.start_run(
                "openai-greenhouse", datetime(2026, 9, day, tzinfo=UTC)
            )
            repo.finish_run(
                absence, RunOutcome.COMPLETE, datetime(2026, 9, day, tzinfo=UTC), jobs_seen=0
            )
            repo.apply_absent_jobs(absence, ["greenhouse:openai:123"])
        assert repo.load_active_jobs("openai-greenhouse") == []

        later = make_job().model_copy(
            update={"first_seen_at": make_job().first_seen_at + timedelta(days=30)}
        )
        reopen = repo.start_run("openai-greenhouse", datetime(2026, 10, 18, tzinfo=UTC))
        repo.upsert_observations(
            reopen, [later], reopened_keys=[later.canonical_job_key]
        )
        repo.finish_run(
            reopen, RunOutcome.COMPLETE, datetime(2026, 10, 18, tzinfo=UTC), jobs_seen=1
        )

        jobs = repo.load_active_jobs("openai-greenhouse")
        assert [job.first_seen_at for job in jobs] == [datetime(2026, 9, 18, tzinfo=UTC)]


def test_observation_without_reopen_classification_keeps_job_closed(tmp_path: Path) -> None:
    with open_seeded_repo(tmp_path) as repo:
        for day in (19, 20):
            absence = repo.start_run(
                "openai-greenhouse", datetime(2026, 9, day, tzinfo=UTC)
            )
            repo.finish_run(
                absence, RunOutcome.COMPLETE, datetime(2026, 9, day, tzinfo=UTC), jobs_seen=0
            )
            repo.apply_absent_jobs(absence, ["greenhouse:openai:123"])
        sighting = repo.start_run("openai-greenhouse", datetime(2026, 9, 21, tzinfo=UTC))
        repo.upsert_observations(sighting, [make_job()])
        repo.finish_run(
            sighting, RunOutcome.COMPLETE, datetime(2026, 9, 21, tzinfo=UTC), jobs_seen=1
        )
        assert repo.load_active_jobs("openai-greenhouse") == []
        assert repo.last_complete_job_keys("openai-greenhouse") == {"greenhouse:openai:123"}


def test_record_notification_links_durable_job_events(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    with Repository.open(path) as repo:
        repo.initialize()
        run_id = repo.start_run("openai-greenhouse", datetime(2026, 9, 18, tzinfo=UTC))
        job = make_job()
        repo.upsert_observations(run_id, [job])
        event_ids = repo.record_events(
            run_id,
            [
                JobEvent(
                    event_type=JobEventType.NEW,
                    job=job,
                    detected_at=datetime(2026, 9, 18, tzinfo=UTC),
                )
            ],
        )
        assert event_ids == [1]
        assert repo.record_events(
            run_id,
            [
                JobEvent(
                    event_type=JobEventType.NEW,
                    job=job,
                    detected_at=datetime(2026, 9, 18, tzinfo=UTC),
                )
            ],
        ) == event_ids
        attempt_id = repo.record_notification(
            "pushover",
            "delivered",
            datetime(2026, 9, 18, tzinfo=UTC),
            provider_request_id="req-123",
            http_status=200,
            event_ids=event_ids,
        )
        repo.finish_run(run_id, RunOutcome.COMPLETE, datetime(2026, 9, 18, tzinfo=UTC), jobs_seen=1)
    assert attempt_id == 1
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT a.provider, a.outcome, l.event_id FROM notification_attempts a "
            "JOIN notification_event_links l ON l.notification_attempt_id = a.id"
        ).fetchall()
    assert rows == [("pushover", "delivered", 1)]


def test_initialize_reports_missing_schema_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "absent.sql"
    monkeypatch.setenv("JOB_ALERT_SCHEMA_PATH", str(missing))
    with (
        Repository.open(tmp_path / "state.sqlite3") as repo,
        pytest.raises(RuntimeError, match=str(missing)),
    ):
        repo.initialize()


def test_initialize_is_idempotent_and_finish_run_rejects_unknown_run(tmp_path: Path) -> None:
    with Repository.open(tmp_path / "state.sqlite3") as repo:
        repo.initialize()
        repo.initialize()
        with pytest.raises(ValueError, match="unknown poll run 99"):
            repo.finish_run(99, RunOutcome.COMPLETE, datetime(2026, 9, 18, tzinfo=UTC))
        run_id = repo.start_run("openai-greenhouse", datetime(2026, 9, 18, tzinfo=UTC))
        repo.finish_run(run_id, RunOutcome.COMPLETE, datetime(2026, 9, 18, tzinfo=UTC))
        with pytest.raises(ValueError, match="already finished"):
            repo.finish_run(run_id, RunOutcome.COMPLETE, datetime(2026, 9, 18, tzinfo=UTC))


def test_upsert_observations_requires_matching_run_source(tmp_path: Path) -> None:
    with Repository.open(tmp_path / "state.sqlite3") as repo:
        repo.initialize()
        run_id = repo.start_run("other-source", datetime(2026, 9, 18, tzinfo=UTC))
        with pytest.raises(ValueError, match="openai-greenhouse"):
            repo.upsert_observations(run_id, [make_job()])
