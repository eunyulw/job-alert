import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from job_alert.db import DeliveryOutcome, IncompleteRunError, Repository
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


JOB_KEY = "greenhouse:openai:123"


def stored_missing_complete_runs(path: Path, canonical_job_key: str) -> int:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT missing_complete_runs FROM jobs WHERE canonical_job_key = ?",
            (canonical_job_key,),
        ).fetchone()
    assert row is not None, f"{canonical_job_key} is not stored"
    return int(row[0])


def stored_run(path: Path, run_id: int) -> tuple[str, str | None]:
    """Return (outcome, finished_at) exactly as stored."""
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT outcome, finished_at FROM poll_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    assert row is not None, f"poll run {run_id} is not stored"
    return str(row[0]), row[1]


def snapshot_applied_at(path: Path, run_id: int) -> str | None:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT applied_at FROM snapshot_applications WHERE run_id = ?", (run_id,)
        ).fetchone()
    return None if row is None else str(row[0])


def open_seeded_repo(tmp_path: Path) -> Repository:
    repo = Repository.open(tmp_path / "state.sqlite3")
    repo.initialize()
    run_id = repo.start_run("openai-greenhouse", datetime(2026, 9, 18, tzinfo=UTC))
    repo.upsert_observations(run_id, [make_job()])
    repo.finish_run(run_id, RunOutcome.COMPLETE, datetime(2026, 9, 18, tzinfo=UTC), jobs_seen=1)
    return repo


def test_incomplete_run_cannot_change_absence_state(tmp_path: Path) -> None:
    with open_seeded_repo(tmp_path) as repo:
        in_flight = repo.start_run("openai-greenhouse", datetime(2026, 9, 18, 1, tzinfo=UTC))
        with pytest.raises(IncompleteRunError, match="cannot change job absence state"):
            repo.apply_absent_jobs(in_flight, [JOB_KEY])
        with pytest.raises(IncompleteRunError, match="cannot change job absence state"):
            repo.apply_complete_snapshot(in_flight, reopened_keys=[JOB_KEY])
        for outcome in (
            RunOutcome.PARTIAL,
            RunOutcome.FAILED,
            RunOutcome.CHALLENGED,
            RunOutcome.QUARANTINED,
        ):
            run_id = repo.start_run("openai-greenhouse", datetime(2026, 9, 19, tzinfo=UTC))
            repo.finish_run(run_id, outcome, datetime(2026, 9, 19, tzinfo=UTC), jobs_seen=0)
            with pytest.raises(IncompleteRunError, match="cannot change job absence state"):
                repo.apply_absent_jobs(run_id, [JOB_KEY])
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
    path = tmp_path / "state.sqlite3"
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
        repo.upsert_observations(reopen, [later])
        repo.finish_run(
            reopen, RunOutcome.COMPLETE, datetime(2026, 10, 18, tzinfo=UTC), jobs_seen=1
        )
        assert repo.apply_complete_snapshot(reopen, reopened_keys=[later.canonical_job_key]) == []

        jobs = repo.load_active_jobs("openai-greenhouse")
        assert [job.first_seen_at for job in jobs] == [datetime(2026, 9, 18, tzinfo=UTC)]
        assert stored_missing_complete_runs(path, JOB_KEY) == 0


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


def test_unfinished_run_can_never_close_a_job_or_become_the_last_snapshot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    with Repository.open(path) as repo:
        repo.initialize()
        run_id = repo.start_run("openai-greenhouse", datetime(2026, 9, 18, tzinfo=UTC))
        repo.upsert_observations(run_id, [make_job()])
        assert stored_run(path, run_id) == ("partial", None)
        assert snapshot_applied_at(path, run_id) is None
        assert repo.last_complete_job_keys("openai-greenhouse") == set()
        with pytest.raises(IncompleteRunError, match="cannot change job absence state"):
            repo.apply_absent_jobs(run_id, [JOB_KEY])

        repo.finish_run(
            run_id, RunOutcome.FAILED, datetime(2026, 9, 18, 1, tzinfo=UTC), jobs_seen=1
        )
        with pytest.raises(IncompleteRunError, match="cannot change job absence state"):
            repo.apply_absent_jobs(run_id, [JOB_KEY])
        assert repo.last_complete_job_keys("openai-greenhouse") == set()
        assert [job.canonical_job_key for job in repo.load_active_jobs("openai-greenhouse")] == [
            JOB_KEY
        ]
        assert stored_missing_complete_runs(path, JOB_KEY) == 0


def test_complete_outcome_without_finished_at_is_not_a_complete_snapshot(tmp_path: Path) -> None:
    """Defense in depth: a legacy or buggy writer must not be able to close jobs either."""
    path = tmp_path / "state.sqlite3"
    with Repository.open(path) as repo:
        repo.initialize()
        run_id = repo.start_run("openai-greenhouse", datetime(2026, 9, 18, tzinfo=UTC))
        repo.upsert_observations(run_id, [make_job()])
        with sqlite3.connect(path) as raw:
            raw.execute(
                "UPDATE poll_runs SET outcome = ? WHERE id = ?",
                (RunOutcome.COMPLETE.value, run_id),
            )
        assert repo.last_complete_job_keys("openai-greenhouse") == set()
        with pytest.raises(IncompleteRunError, match="cannot change job absence state"):
            repo.apply_absent_jobs(run_id, [JOB_KEY])
        assert repo.source_health("openai-greenhouse").last_complete_at is None


def test_failed_run_sighting_does_not_reset_a_prior_absence(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    with open_seeded_repo(tmp_path) as repo:
        absence = repo.start_run("openai-greenhouse", datetime(2026, 9, 19, tzinfo=UTC))
        repo.finish_run(
            absence, RunOutcome.COMPLETE, datetime(2026, 9, 19, tzinfo=UTC), jobs_seen=0
        )
        assert repo.apply_absent_jobs(absence, [JOB_KEY]) == []
        assert stored_missing_complete_runs(path, JOB_KEY) == 1

        failed = repo.start_run("openai-greenhouse", datetime(2026, 9, 20, tzinfo=UTC))
        repo.upsert_observations(failed, [make_job()])
        assert stored_missing_complete_runs(path, JOB_KEY) == 1
        repo.finish_run(
            failed, RunOutcome.FAILED, datetime(2026, 9, 20, tzinfo=UTC), jobs_seen=1
        )
        assert stored_missing_complete_runs(path, JOB_KEY) == 1
        assert repo.load_active_jobs("openai-greenhouse") != []

        second = repo.start_run("openai-greenhouse", datetime(2026, 9, 21, tzinfo=UTC))
        repo.finish_run(
            second, RunOutcome.COMPLETE, datetime(2026, 9, 21, tzinfo=UTC), jobs_seen=0
        )
        assert repo.apply_absent_jobs(second, [JOB_KEY]) == [JOB_KEY]
        assert repo.load_active_jobs("openai-greenhouse") == []


@pytest.mark.parametrize("close_after", [1, 2])
def test_repeated_snapshot_application_is_single_use(tmp_path: Path, close_after: int) -> None:
    path = tmp_path / "state.sqlite3"
    with open_seeded_repo(tmp_path) as repo:
        absence = repo.start_run("openai-greenhouse", datetime(2026, 9, 19, tzinfo=UTC))
        repo.finish_run(
            absence, RunOutcome.COMPLETE, datetime(2026, 9, 19, tzinfo=UTC), jobs_seen=0
        )
        first = repo.apply_complete_snapshot(
            absence, missing_keys=[JOB_KEY], close_after=close_after
        )
        repeat = repo.apply_complete_snapshot(
            absence, missing_keys=[JOB_KEY], close_after=close_after
        )
        via_wrapper = repo.apply_absent_jobs(absence, [JOB_KEY], close_after=close_after)

        assert repeat == []
        assert via_wrapper == []
        assert first == ([JOB_KEY] if close_after == 1 else [])
        assert snapshot_applied_at(path, absence) == "2026-09-19T00:00:00+00:00"
        assert stored_missing_complete_runs(path, JOB_KEY) == 1
        if close_after == 1:
            assert repo.load_active_jobs("openai-greenhouse") == []
        else:
            assert repo.load_active_jobs("openai-greenhouse") != []
            second = repo.start_run("openai-greenhouse", datetime(2026, 9, 20, tzinfo=UTC))
            repo.finish_run(
                second, RunOutcome.COMPLETE, datetime(2026, 9, 20, tzinfo=UTC), jobs_seen=0
            )
            assert repo.apply_absent_jobs(second, [JOB_KEY]) == [JOB_KEY]


def test_closed_job_reopens_only_through_a_complete_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    with open_seeded_repo(tmp_path) as repo:
        for day in (19, 20):
            absence = repo.start_run("openai-greenhouse", datetime(2026, 9, day, tzinfo=UTC))
            repo.finish_run(
                absence, RunOutcome.COMPLETE, datetime(2026, 9, day, tzinfo=UTC), jobs_seen=0
            )
            repo.apply_absent_jobs(absence, [JOB_KEY])
        assert repo.load_active_jobs("openai-greenhouse") == []

        failed = repo.start_run("openai-greenhouse", datetime(2026, 9, 21, tzinfo=UTC))
        repo.upsert_observations(failed, [make_job()])
        repo.finish_run(
            failed, RunOutcome.FAILED, datetime(2026, 9, 21, tzinfo=UTC), jobs_seen=1
        )
        assert repo.load_active_jobs("openai-greenhouse") == []

        reopened = repo.start_run("openai-greenhouse", datetime(2026, 9, 22, tzinfo=UTC))
        repo.upsert_observations(reopened, [make_job()])
        repo.finish_run(
            reopened, RunOutcome.COMPLETE, datetime(2026, 9, 22, tzinfo=UTC), jobs_seen=1
        )
        assert repo.apply_complete_snapshot(reopened, reopened_keys=[JOB_KEY]) == []
        assert [job.canonical_job_key for job in repo.load_active_jobs("openai-greenhouse")] == [
            JOB_KEY
        ]
        assert stored_missing_complete_runs(path, JOB_KEY) == 0


def test_reopen_requires_the_job_to_be_observed_by_that_run(tmp_path: Path) -> None:
    with open_seeded_repo(tmp_path) as repo:
        for day in (19, 20):
            absence = repo.start_run("openai-greenhouse", datetime(2026, 9, day, tzinfo=UTC))
            repo.finish_run(
                absence, RunOutcome.COMPLETE, datetime(2026, 9, day, tzinfo=UTC), jobs_seen=0
            )
            repo.apply_absent_jobs(absence, [JOB_KEY])
        assert repo.load_active_jobs("openai-greenhouse") == []

        empty = repo.start_run("openai-greenhouse", datetime(2026, 9, 21, tzinfo=UTC))
        repo.finish_run(
            empty, RunOutcome.COMPLETE, datetime(2026, 9, 21, tzinfo=UTC), jobs_seen=0
        )
        assert repo.apply_complete_snapshot(empty, reopened_keys=[JOB_KEY]) == []
        assert repo.load_active_jobs("openai-greenhouse") == []


def test_snapshot_cannot_report_a_key_as_both_observed_and_missing(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    with open_seeded_repo(tmp_path) as repo:
        run_id = repo.start_run("openai-greenhouse", datetime(2026, 9, 19, tzinfo=UTC))
        repo.upsert_observations(run_id, [make_job()])
        repo.finish_run(
            run_id, RunOutcome.COMPLETE, datetime(2026, 9, 19, tzinfo=UTC), jobs_seen=1
        )
        with pytest.raises(ValueError, match="both observed and missing"):
            repo.apply_complete_snapshot(run_id, missing_keys=[JOB_KEY])
        assert stored_missing_complete_runs(path, JOB_KEY) == 0


def test_transaction_commits_staged_state_and_events_as_one_unit(tmp_path: Path) -> None:
    with Repository.open(tmp_path / "state.sqlite3") as repo:
        repo.initialize()
        run_id = repo.start_run("openai-greenhouse", datetime(2026, 9, 18, tzinfo=UTC))
        job = make_job()
        with repo.transaction():
            repo.upsert_observations(run_id, [job])
            repo.finish_run(
                run_id, RunOutcome.COMPLETE, datetime(2026, 9, 18, tzinfo=UTC), jobs_seen=1
            )
            repo.apply_complete_snapshot(run_id)
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
        assert repo.last_complete_job_keys("openai-greenhouse") == {JOB_KEY}
        applied = repo.last_applied_snapshot("openai-greenhouse")
        assert applied is not None
        assert applied.run_id == run_id
        assert applied.canonical_job_keys == frozenset({JOB_KEY})
        assert [event.event_id for event in repo.pending_events("openai-greenhouse")] == [1]


def test_transaction_rolls_back_every_call_in_the_unit(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    with Repository.open(path) as repo:
        repo.initialize()
        run_id = repo.start_run("openai-greenhouse", datetime(2026, 9, 18, tzinfo=UTC))
        with pytest.raises(ValueError, match="unknown poll run 999"), repo.transaction():
            repo.upsert_observations(run_id, [make_job()])
            # A nested repository call reuses the same unit rather than opening its own.
            repo.finish_run(
                999, RunOutcome.COMPLETE, datetime(2026, 9, 18, tzinfo=UTC), jobs_seen=1
            )

        assert repo.load_active_jobs("openai-greenhouse") == []
        assert stored_run(path, run_id) == ("partial", None)
        # The connection is still usable once the unit has been rolled back.
        repo.finish_run(
            run_id, RunOutcome.COMPLETE, datetime(2026, 9, 18, 1, tzinfo=UTC), jobs_seen=0
        )
        assert repo.source_health("openai-greenhouse").last_complete_at == datetime(
            2026, 9, 18, 1, tzinfo=UTC
        )


def test_validators_are_replaced_and_can_be_cleared(tmp_path: Path) -> None:
    with Repository.open(tmp_path / "state.sqlite3") as repo:
        repo.initialize()
        repo.start_run("openai-greenhouse", datetime(2026, 9, 18, tzinfo=UTC))
        repo.record_conditional_state(
            "openai-greenhouse", etag='"v1"', last_modified="Thu, 17 Sep 2026 12:00:00 GMT"
        )
        assert repo.source_health("openai-greenhouse").etag == '"v1"'

        repo.record_conditional_state("openai-greenhouse", etag=None, last_modified=None)

        health = repo.source_health("openai-greenhouse")
        assert health.etag is None
        assert health.last_modified is None


def test_quarantine_records_one_pending_candidate_per_source(tmp_path: Path) -> None:
    with Repository.open(tmp_path / "state.sqlite3") as repo:
        repo.initialize()
        first = repo.start_run("openai-greenhouse", datetime(2026, 9, 18, tzinfo=UTC))
        second = repo.start_run("openai-greenhouse", datetime(2026, 9, 19, tzinfo=UTC))
        repo.record_quarantine(
            first,
            source_id="openai-greenhouse",
            previous_job_count=100,
            candidate_keys=["b", "a", "a"],
            quarantined_at=datetime(2026, 9, 18, tzinfo=UTC),
        )
        repo.record_quarantine(
            second,
            source_id="openai-greenhouse",
            previous_job_count=100,
            candidate_keys=["c"],
            quarantined_at=datetime(2026, 9, 19, tzinfo=UTC),
        )

        pending = repo.pending_quarantine("openai-greenhouse")

        assert pending is not None
        assert pending.run_id == second
        assert pending.candidate_keys == frozenset({"c"})
        assert (pending.previous_job_count, pending.candidate_job_count) == (100, 1)
        assert repo.pending_quarantine("other-source") is None

        repo.clear_quarantine("openai-greenhouse")
        assert repo.pending_quarantine("openai-greenhouse") is None


def test_pending_events_stop_at_a_terminal_delivery_outcome(tmp_path: Path) -> None:
    with Repository.open(tmp_path / "state.sqlite3") as repo:
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
                ),
                JobEvent(
                    event_type=JobEventType.REOPENED,
                    job=job,
                    detected_at=datetime(2026, 9, 18, tzinfo=UTC),
                ),
                JobEvent(
                    event_type=JobEventType.CLOSED,
                    job=job,
                    detected_at=datetime(2026, 9, 18, tzinfo=UTC),
                ),
            ],
        )
        deliverable = event_ids[:2]
        # A closure event is durable history, never a delivery candidate.
        assert (
            [event.event_id for event in repo.pending_events("openai-greenhouse")] == deliverable
        )
        # A retryable failure leaves both events outstanding for the next cycle.
        repo.record_notification(
            "pushover", DeliveryOutcome.RETRYABLE.value, datetime(2026, 9, 18, tzinfo=UTC),
            event_ids=deliverable,
        )
        assert len(repo.pending_events("openai-greenhouse")) == 2
        assert repo.attempt_counts(deliverable, outcome=DeliveryOutcome.RETRYABLE) == {
            deliverable[0]: 1,
            deliverable[1]: 1,
        }

        repo.record_notification(
            "pushover", DeliveryOutcome.DELIVERED.value, datetime(2026, 9, 18, tzinfo=UTC),
            event_ids=[deliverable[0]],
        )
        repo.record_notification(
            "pushover", DeliveryOutcome.ABANDONED.value, datetime(2026, 9, 18, tzinfo=UTC),
            event_ids=[deliverable[1]],
        )

        assert repo.pending_events("openai-greenhouse") == []
