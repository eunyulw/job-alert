"""Source health incidents and the daily digest.

The brief's own health test is the first one below. The rest pin the three obligations Task 22
carries in: a diagnostic run must never look like a failure, the unbounded retry class is bounded
and its numbers are visible, and Pushover's quota figures reach the digest.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from job_alert.db import (
    DEFAULT_MAX_RETRYABLE_ATTEMPTS,
    RETRY_BACKOFF_MAX_SECONDS,
    DeliveryOutcome,
    QuotaSnapshot,
    Repository,
    WindowCounts,
)
from job_alert.health import (
    CHICAGO_TIMEZONE,
    FAILURE_INCIDENT_THRESHOLD,
    STALE_GRACE_MINUTES,
    STALE_INTERVAL_MULTIPLIER,
    DailyDigest,
    DigestSource,
    HealthEventKind,
    IncidentReason,
    SourceHealthState,
    build_daily_digest,
    evaluate_source_health,
    is_stale,
    staleness_threshold,
)
from job_alert.models import JobEvent, JobEventType, NormalizedJob, RunOutcome

fixed_now = datetime(2026, 9, 26, 23, 0, tzinfo=UTC)
SOURCE_ID = "openai-greenhouse"
JOB_KEY = "greenhouse:openai:123"


def health_state(**overrides: object) -> SourceHealthState:
    """A health state for one configured source, with the fields a test cares about overridden."""
    values: dict[str, object] = {
        "source_id": SOURCE_ID,
        "company_id": "openai",
        "company_name": "OpenAI",
        "interval_minutes": 30,
    }
    values.update(overrides)
    return SourceHealthState(**values)


def job() -> NormalizedJob:
    return NormalizedJob(
        company_id="openai",
        source_id=SOURCE_ID,
        source_job_id="123",
        canonical_job_key=JOB_KEY,
        dedupe_key="requisition:openai:research-123",
        title="Research Engineer",
        locations=["San Francisco, CA"],
        posting_url="https://example.com/jobs/123",
        apply_url="https://example.com/jobs/123/apply",
        first_seen_at=fixed_now,
    )


def seed_event(repo: Repository, *, now: datetime = fixed_now) -> int:
    """One stored new-job event, which is what a delivery attempt has to link to."""
    run_id = repo.start_run(SOURCE_ID, now)
    posting = job()
    repo.upsert_observations(run_id, [posting])
    return repo.record_events(
        run_id,
        [JobEvent(event_type=JobEventType.NEW, job=posting, detected_at=now)],
    )[0]


# ------------------------------------------------------------------ health policy


def test_health_alert_opens_once_after_three_failures_and_recovers_once() -> None:
    state = SourceHealthState(consecutive_failures=2, health_alert_open=False)
    failed = evaluate_source_health(state, RunOutcome.FAILED, now=fixed_now)
    assert [event.kind for event in failed.events] == ["source_failure"]
    repeated = evaluate_source_health(failed.state, RunOutcome.FAILED, now=fixed_now)
    assert repeated.events == []
    recovered = evaluate_source_health(repeated.state, RunOutcome.COMPLETE, now=fixed_now)
    assert [event.kind for event in recovered.events] == ["source_recovery"]


@pytest.mark.parametrize(
    "outcome",
    [RunOutcome.FAILED, RunOutcome.PARTIAL, RunOutcome.CHALLENGED, RunOutcome.QUARANTINED],
)
def test_every_non_complete_outcome_counts_toward_the_incident(outcome: RunOutcome) -> None:
    state = health_state(consecutive_failures=FAILURE_INCIDENT_THRESHOLD - 2)
    first = evaluate_source_health(state, outcome, now=fixed_now, error_message="boom")
    assert first.events == []
    second = evaluate_source_health(first.state, outcome, now=fixed_now, error_message="boom")
    assert [event.kind for event in second.events] == [HealthEventKind.SOURCE_FAILURE]
    assert second.events[0].reason == IncidentReason.CONSECUTIVE_FAILURES
    assert second.state.consecutive_failures == FAILURE_INCIDENT_THRESHOLD
    assert second.state.health_opened_at == fixed_now
    assert second.state.first_failure_at == fixed_now
    assert second.state.last_error == "boom"
    assert second.state.health_alert_open is True


def test_the_staleness_branch_opens_an_incident_before_the_third_failure() -> None:
    threshold = staleness_threshold(30)
    assert threshold == timedelta(minutes=STALE_INTERVAL_MULTIPLIER * 30 + STALE_GRACE_MINUTES)

    # Exactly at the threshold the source is not stale yet; a second past it, it is.
    exact = health_state(last_complete_at=fixed_now - threshold)
    assert evaluate_source_health(exact, RunOutcome.FAILED, now=fixed_now).events == []
    stale = health_state(last_complete_at=fixed_now - threshold - timedelta(seconds=1))
    opened = evaluate_source_health(
        stale, RunOutcome.FAILED, now=fixed_now, error_message="HTTP 503"
    )
    assert [event.kind for event in opened.events] == [HealthEventKind.SOURCE_FAILURE]
    assert opened.events[0].reason == IncidentReason.STALE_WITHOUT_COMPLETE_SUCCESS
    assert opened.state.consecutive_failures == 1
    assert opened.state.health_opened_at == fixed_now

    assert is_stale(
        now=fixed_now,
        last_complete_at=fixed_now - threshold - timedelta(seconds=1),
        first_failure_at=None,
        last_run_at=None,
        interval_minutes=30,
    )
    assert not is_stale(
        now=fixed_now,
        last_complete_at=fixed_now - timedelta(minutes=1),
        first_failure_at=None,
        last_run_at=None,
        interval_minutes=30,
    )
    assert not is_stale(
        now=fixed_now,
        last_complete_at=None,
        first_failure_at=None,
        last_run_at=None,
        interval_minutes=30,
    )


def test_a_source_that_never_completed_is_measured_from_its_first_failure() -> None:
    state = health_state(
        first_failure_at=fixed_now - staleness_threshold(30) - timedelta(minutes=1),
        consecutive_failures=1,
    )
    opened = evaluate_source_health(
        state, RunOutcome.FAILED, now=fixed_now, error_message="HTTP 503"
    )
    assert [event.kind for event in opened.events] == [HealthEventKind.SOURCE_FAILURE]
    assert opened.events[0].reason == IncidentReason.STALE_WITHOUT_COMPLETE_SUCCESS


def test_a_recovery_reports_the_incident_it_closes() -> None:
    opened = evaluate_source_health(
        health_state(consecutive_failures=2), RunOutcome.FAILED, now=fixed_now, error_message="boom"
    )
    state = health_state(
        consecutive_failures=5,
        health_alert_open=True,
        health_opened_at=fixed_now - timedelta(hours=2),
        health_reason=IncidentReason.CONSECUTIVE_FAILURES,
        first_failure_at=fixed_now - timedelta(hours=3),
        last_error="boom",
        last_complete_at=fixed_now - timedelta(days=1),
    )
    recovery = evaluate_source_health(state, RunOutcome.COMPLETE, now=fixed_now)
    assert [event.kind for event in recovery.events] == [HealthEventKind.SOURCE_RECOVERY]
    event = recovery.events[0]
    assert event.source_id == SOURCE_ID
    assert event.company_name == "OpenAI"
    assert event.first_failure_at == fixed_now - timedelta(hours=3)
    assert event.last_error == "boom"
    assert event.consecutive_failures == 5
    assert event.last_complete_at == fixed_now
    assert event.opened_at == fixed_now - timedelta(hours=2)
    assert event.reason == IncidentReason.COMPLETE_SUCCESS
    assert recovery.state.health_alert_open is False
    assert recovery.state.health_opened_at is None
    assert recovery.state.health_reason is None
    assert recovery.state.first_failure_at is None
    assert recovery.state.consecutive_failures == 0
    assert recovery.state.last_complete_at == fixed_now
    assert opened.state.health_alert_open is True


def test_a_healthy_source_recovers_without_an_event() -> None:
    evaluation = evaluate_source_health(health_state(), RunOutcome.COMPLETE, now=fixed_now)
    assert evaluation.events == []
    assert evaluation.state.last_complete_at == fixed_now
    assert evaluation.state.consecutive_failures == 0


# ------------------------------------------------------------------ obligation 1: diagnostics


def test_three_dry_runs_open_no_incident_while_three_failures_do() -> None:
    diagnostic = health_state()
    for _ in range(3):
        evaluation = evaluate_source_health(
            diagnostic, RunOutcome.PARTIAL, now=fixed_now, error_code="dry_run"
        )
        assert evaluation.events == []
        diagnostic = evaluation.state
    assert diagnostic == health_state()

    failing = health_state()
    opened: list[list[str]] = []
    for _ in range(3):
        evaluation = evaluate_source_health(
            failing, RunOutcome.FAILED, now=fixed_now, error_code="collection_failed"
        )
        opened.append([str(event.kind) for event in evaluation.events])
        failing = evaluation.state
    assert opened == [[], [], ["source_failure"]]
    assert failing.health_alert_open is True


def test_a_dry_run_neither_fails_a_source_nor_recovers_an_open_incident() -> None:
    state = health_state(
        consecutive_failures=4,
        health_alert_open=True,
        health_opened_at=fixed_now,
        health_reason=IncidentReason.CONSECUTIVE_FAILURES,
        first_failure_at=fixed_now,
        last_error="boom",
    )
    evaluation = evaluate_source_health(
        state, RunOutcome.PARTIAL, now=fixed_now, error_code="dry_run", error_message="dry run: "
    )
    assert evaluation.events == []
    assert evaluation.state == state


def test_a_diagnostic_run_does_not_move_the_persisted_failure_streak(tmp_path: Path) -> None:
    with Repository.open(tmp_path / "state.sqlite3") as repo:
        repo.initialize()
        seed_event(repo)
        for _ in range(3):
            run_id = repo.start_run(SOURCE_ID, fixed_now)
            repo.finish_run(
                run_id,
                RunOutcome.PARTIAL,
                fixed_now,
                error_code="dry_run",
                error_message="dry run: a complete collection was compared without writing state",
            )
        after_diagnostics = repo.source_health(SOURCE_ID)
        assert after_diagnostics is not None
        assert after_diagnostics.consecutive_failures == 0
        assert after_diagnostics.first_failure_at is None
        assert after_diagnostics.last_error is None
        assert after_diagnostics.last_complete_at is None

        for _ in range(3):
            run_id = repo.start_run(SOURCE_ID, fixed_now)
            repo.finish_run(
                run_id,
                RunOutcome.FAILED,
                fixed_now,
                error_code="collection_failed",
                error_message="HTTP 503 from the provider",
            )
        failing = repo.source_health(SOURCE_ID)
        assert failing is not None
        assert failing.consecutive_failures == 3
        assert failing.first_failure_at == fixed_now
        assert failing.last_error == "HTTP 503 from the provider"
        assert failing.health_alert_open is False

        recovered = repo.start_run(SOURCE_ID, fixed_now)
        repo.finish_run(recovered, RunOutcome.COMPLETE, fixed_now, jobs_seen=1)
        cleaned = repo.source_health(SOURCE_ID)
        assert cleaned is not None
        assert cleaned.consecutive_failures == 0
        assert cleaned.first_failure_at is None
        assert cleaned.last_complete_at == fixed_now
        assert cleaned.last_run_at == fixed_now


def test_an_incident_round_trips_through_the_repository(tmp_path: Path) -> None:
    with Repository.open(tmp_path / "state.sqlite3") as repo:
        repo.initialize()
        run_id = seed_event(repo)
        for _ in range(FAILURE_INCIDENT_THRESHOLD - 1):
            run_id = repo.start_run(SOURCE_ID, fixed_now)
            repo.finish_run(
                run_id,
                RunOutcome.FAILED,
                fixed_now,
                error_code="collection_failed",
                error_message="HTTP 503",
            )
        stored = repo.source_health(SOURCE_ID)
        assert stored is not None
        state = SourceHealthState.from_source_state(
            stored, company_id="openai", company_name="OpenAI", interval_minutes=5
        )
        opened = evaluate_source_health(
            state, RunOutcome.FAILED, now=fixed_now, error_message="HTTP 503"
        )
        assert [event.kind for event in opened.events] == [HealthEventKind.SOURCE_FAILURE]

        repo.record_health_state(SOURCE_ID, opened.state)

        incident = repo.source_health(SOURCE_ID)
        assert incident is not None
        assert incident.health_alert_open is True
        assert incident.health_opened_at == fixed_now
        assert incident.health_reason == IncidentReason.CONSECUTIVE_FAILURES
        assert incident.first_failure_at == fixed_now
        assert incident.last_error == "HTTP 503"
        assert incident.consecutive_failures == FAILURE_INCIDENT_THRESHOLD

        recovery_at = fixed_now + timedelta(hours=1)
        recovery_run = repo.start_run(SOURCE_ID, recovery_at)
        recovered = evaluate_source_health(
            SourceHealthState.from_source_state(repo.source_health(SOURCE_ID)),
            RunOutcome.COMPLETE,
            now=recovery_at,
        )
        assert [event.kind for event in recovered.events] == [HealthEventKind.SOURCE_RECOVERY]
        repo.record_health_state(SOURCE_ID, recovered.state)
        # Freshness is the run's business, so the complete run records it and the health record
        # writes back only the incident fields.
        repo.finish_run(recovery_run, RunOutcome.COMPLETE, recovery_at, jobs_seen=1)

        cleared = repo.source_health(SOURCE_ID)
        assert cleared is not None
        assert cleared.health_alert_open is False
        assert cleared.health_opened_at is None
        assert cleared.health_reason is None
        assert cleared.first_failure_at is None
        assert cleared.consecutive_failures == 0
        assert cleared.last_complete_at == recovery_at
        assert cleared.last_run_at == recovery_at


def test_the_incident_record_refuses_an_inconsistent_open_incident(tmp_path: Path) -> None:
    with Repository.open(tmp_path / "state.sqlite3") as repo:
        repo.initialize()
        seed_event(repo)
        with pytest.raises(ValueError, match="opening time"):
            repo.record_health_state(
                SOURCE_ID,
                health_state(health_alert_open=True, health_opened_at=None),
            )


# ------------------------------------------------------------------ obligation 2: bounded retries


def test_the_certain_no_send_retry_class_is_capped_with_a_recorded_backoff(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    with Repository.open(path) as repo:
        repo.initialize()
        event_id = seed_event(repo)

        for _ in range(DEFAULT_MAX_RETRYABLE_ATTEMPTS):
            repo.record_notification(
                "pushover",
                DeliveryOutcome.RETRYABLE.value,
                fixed_now,
                error_message="connect error before the request was sent",
                event_ids=[event_id],
            )

        assert repo.pending_events(SOURCE_ID) == []
        assert repo.attempt_counts([event_id], outcome=DeliveryOutcome.RETRYABLE) == {
            event_id: DEFAULT_MAX_RETRYABLE_ATTEMPTS
        }
        with sqlite3.connect(path) as connection:
            delays = [
                row[0]
                for row in connection.execute(
                    "SELECT retry_after_seconds FROM notification_attempts"
                    " WHERE outcome = 'retryable' ORDER BY id"
                )
            ]
            abandoned = [
                row[0]
                for row in connection.execute(
                    "SELECT error_message FROM notification_attempts"
                    " WHERE outcome = 'abandoned'"
                )
            ]
        assert delays[:3] == [60.0, 120.0, 240.0]
        assert delays[-1] == RETRY_BACKOFF_MAX_SECONDS
        assert len(abandoned) == 1
        assert str(DEFAULT_MAX_RETRYABLE_ATTEMPTS) in abandoned[0]


def test_attempt_retention_prunes_old_unresolved_rows_and_keeps_terminal_ones(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    old = fixed_now - timedelta(days=40)
    with Repository.open(path) as repo:
        repo.initialize()
        event_id = seed_event(repo, now=old)
        repo.record_notification(
            "pushover", DeliveryOutcome.RETRYABLE.value, old, event_ids=[event_id]
        )
        repo.record_notification(
            "pushover", DeliveryOutcome.DELIVERED.value, old, event_ids=[event_id]
        )
        repo.record_notification(
            "pushover", DeliveryOutcome.UNCERTAIN.value, fixed_now, event_ids=[event_id]
        )

        assert repo.prune_notification_attempts(now=fixed_now) == 1
        with sqlite3.connect(path) as connection:
            outcomes = [
                row[0]
                for row in connection.execute(
                    "SELECT outcome FROM notification_attempts ORDER BY id"
                )
            ]
        assert outcomes == ["delivered", "uncertain"]
        # The terminal row survived, so the event it resolved never becomes pending again.
        assert repo.pending_events(SOURCE_ID) == []


def test_quota_figures_are_persisted_with_the_attempt_that_reported_them(tmp_path: Path) -> None:
    with Repository.open(tmp_path / "state.sqlite3") as repo:
        repo.initialize()
        event_id = seed_event(repo)
        reset_at = datetime(2026, 10, 1, tzinfo=UTC)
        repo.record_notification(
            "pushover",
            DeliveryOutcome.RETRYABLE.value,
            fixed_now - timedelta(minutes=5),
            error_message="Pushover is rate limited",
            retry_after_seconds=90.0,
            quota_limit=10000,
            quota_remaining=7,
            quota_reset_at=reset_at,
            event_ids=[event_id],
        )
        repo.record_notification(
            "pushover",
            DeliveryOutcome.DELIVERED.value,
            fixed_now,
            quota_limit=10000,
            quota_remaining=6,
            quota_reset_at=reset_at,
            event_ids=[event_id],
        )

        quota = repo.latest_quota("pushover")
        assert quota is not None
        assert quota.provider == "pushover"
        assert (quota.limit, quota.remaining) == (10000, 6)
        assert quota.reset_at == reset_at
        assert quota.observed_at == fixed_now
        assert repo.latest_quota("ntfy") is None


# ------------------------------------------------------------------ the daily digest


def digest_sources() -> list[DigestSource]:
    """One fresh source, one stale source with an open incident, and two unsupported ones."""
    return [
        DigestSource(
            source_id="acme-fixture",
            company_id="acme",
            company_name="Acme Robotics",
            enabled=True,
            support_status="SUPPORTED",
            interval_minutes=30,
            last_complete_at=fixed_now - timedelta(minutes=10),
            last_run_at=fixed_now - timedelta(minutes=10),
        ),
        DigestSource(
            source_id="globex-greenhouse",
            company_id="globex",
            company_name="Globex",
            enabled=True,
            support_status="SUPPORTED",
            interval_minutes=15,
            last_complete_at=fixed_now - timedelta(hours=3),
            last_run_at=fixed_now - timedelta(minutes=5),
            consecutive_failures=4,
            health_alert_open=True,
            health_opened_at=fixed_now - timedelta(hours=1),
            health_reason=IncidentReason.CONSECUTIVE_FAILURES,
            first_failure_at=fixed_now - timedelta(hours=2),
            last_error="HTTP 503 from the provider",
        ),
        DigestSource(
            source_id="blocked-discovery",
            company_id="blocked",
            company_name="Blocked Corp",
            enabled=False,
            support_status="BLOCKED",
            interval_minutes=30,
        ),
        DigestSource(
            source_id="review-discovery",
            company_id="review",
            company_name="Review Co",
            enabled=False,
            support_status="NEEDS_MANUAL_REVIEW",
            interval_minutes=30,
        ),
    ]


def build_digest(
    sources: list[DigestSource],
    *,
    quota: QuotaSnapshot | None = None,
) -> DailyDigest:
    return build_daily_digest(
        now=fixed_now,
        window_start=fixed_now - timedelta(days=1),
        sources=sources,
        counts=WindowCounts(
            new_jobs=3,
            reopened_jobs=1,
            attempts=4,
            delivered=2,
            retryable=1,
            uncertain=0,
            permanent=0,
            abandoned=1,
        ),
        quota=quota,
    )


def test_the_digest_reports_every_field_it_promises() -> None:
    quota = QuotaSnapshot(
        provider="pushover",
        limit=10000,
        remaining=9876,
        reset_at=datetime(2026, 10, 1, tzinfo=UTC),
        observed_at=fixed_now,
    )
    digest = build_digest(digest_sources(), quota=quota)

    assert (digest.total_enabled_sources, digest.sources_complete, digest.sources_stale) == (
        2,
        1,
        1,
    )
    assert digest.sources_awaiting_success == 0
    assert digest.sources_never_polled == 0
    assert [event.source_id for event in digest.open_incidents] == ["globex-greenhouse"]
    assert digest.blocked_companies == ("Blocked Corp (blocked)",)
    assert digest.manual_review_companies == ("Review Co (review)",)
    assert (digest.new_jobs, digest.matched_alerts) == (3, 2)
    assert digest.failed_deliveries == 2
    assert digest.abandoned_deliveries == 1
    assert digest.quota is quota

    for label in (
        "Generated:",
        "Window:",
        "Enabled sources:",
        "Sources complete:",
        "Sources stale:",
        "Open incidents:",
        "Blocked companies:",
        "Manual review companies:",
        "New jobs:",
        "Matched alerts:",
        "Failed deliveries:",
        "Pushover quota:",
    ):
        assert label in digest.markdown
        assert label in digest.text

    assert "2026-09-26 23:00 UTC" in digest.markdown
    assert f"2026-09-26 18:00 {CHICAGO_TIMEZONE}" in digest.markdown
    assert "9876 of 10000 remaining" in digest.markdown
    assert "2026-10-01 00:00 UTC" in digest.markdown
    assert f"2026-09-30 19:00 {CHICAGO_TIMEZONE}" in digest.markdown
    assert "- Globex (globex-greenhouse)" in digest.markdown
    assert "HTTP 503 from the provider" in digest.markdown
    assert "- Blocked Corp (blocked)" in digest.markdown


def test_the_digest_is_byte_identical_for_the_same_inputs_in_any_order() -> None:
    sources = digest_sources()
    first = build_digest(sources)
    reordered = build_digest(list(reversed(sources)))

    assert first.markdown == reordered.markdown
    assert first.text == reordered.text
    assert first.markdown == build_digest(sources).markdown
    assert first.text == build_digest(sources).text
    # Both variants carry the same evidence, and the plain-text one carries no heading marks.
    assert not any(line.startswith("#") for line in first.text.splitlines())
    assert first.markdown.count("Globex (globex-greenhouse)") == first.text.count(
        "Globex (globex-greenhouse)"
    )


def test_the_digest_counts_reconcile_and_report_an_unknown_quota() -> None:
    sources = digest_sources()
    sources.append(
        DigestSource(
            source_id="never-polled",
            company_id="fresh",
            company_name="Fresh Co",
            enabled=True,
            support_status="SUPPORTED",
            interval_minutes=30,
        )
    )
    digest = build_digest(sources)
    assert digest.total_enabled_sources == 3
    assert digest.sources_never_polled == 1
    assert (
        digest.sources_complete
        + digest.sources_stale
        + digest.sources_awaiting_success
        + digest.sources_never_polled
        == digest.total_enabled_sources
    )
    assert "Pushover quota: unknown" in digest.markdown
    assert "Pushover quota: unknown" in digest.text


def test_a_source_with_a_fresh_failure_and_no_complete_success_is_not_counted_stale() -> None:
    digest = build_digest(
        [
            DigestSource(
                source_id="acme-fixture",
                company_id="acme",
                company_name="Acme Robotics",
                enabled=True,
                support_status="SUPPORTED",
                interval_minutes=30,
                last_run_at=fixed_now - timedelta(minutes=2),
                first_failure_at=fixed_now - timedelta(minutes=2),
                consecutive_failures=1,
            )
        ]
    )
    assert (digest.sources_complete, digest.sources_stale) == (0, 0)
    assert digest.sources_awaiting_success == 1
    # An empty section still says so in both variants.
    assert digest.blocked_companies == ()
    assert digest.open_incidents == ()
    assert digest.markdown.count("- none") == 3
    assert digest.text.count("- none") == 3


def test_the_digest_window_is_counted_from_stored_history(tmp_path: Path) -> None:
    with Repository.open(tmp_path / "state.sqlite3") as repo:
        repo.initialize()
        event_id = seed_event(repo)
        repo.record_notification(
            "pushover", DeliveryOutcome.DELIVERED.value, fixed_now, event_ids=[event_id]
        )
        repo.record_notification(
            "pushover",
            DeliveryOutcome.RETRYABLE.value,
            fixed_now,
            error_message="connect error before the request was sent",
            event_ids=[event_id],
        )

        inside = repo.window_counts(
            fixed_now - timedelta(minutes=1), fixed_now + timedelta(minutes=1)
        )
        assert (inside.new_jobs, inside.reopened_jobs) == (1, 0)
        assert (inside.attempts, inside.delivered) == (2, 1)
        assert (inside.matched_alerts, inside.failed_deliveries) == (1, 1)
        assert inside.retryable == 1

        after = repo.window_counts(
            fixed_now + timedelta(minutes=1), fixed_now + timedelta(minutes=2)
        )
        assert after == WindowCounts()

    digest = build_daily_digest(
        now=fixed_now,
        window_start=fixed_now - timedelta(minutes=1),
        sources=[],
        counts=inside,
    )
    assert (digest.new_jobs, digest.matched_alerts, digest.failed_deliveries) == (1, 1, 1)
