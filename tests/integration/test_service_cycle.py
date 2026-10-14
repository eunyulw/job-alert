"""One-source collection cycles: modes, quarantine, deduplication, and durable delivery.

Every test drives :class:`~job_alert.service.JobAlertService` over the harness in
``tests/integration/harness.py``: a real SQLite repository, a real fetcher, and a mocked provider
endpoint whose board can be swapped between cycles.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from job_alert.adapters.registry import AdapterRegistry
from job_alert.config import FilterConfig
from job_alert.db import DeliveryOutcome
from job_alert.health import HealthEventKind
from job_alert.models import JobEventType, RunOutcome
from job_alert.notifications.base import DeliveryResult
from job_alert.service import JobAlertService, RunMode
from tests.integration.harness import DEFAULT_FILTERS, ENDPOINT, FIXED_NOW, ServiceHarness


@pytest.mark.asyncio
async def test_baseline_stores_jobs_without_notifications(service_harness: ServiceHarness) -> None:
    result = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.BASELINE
    )

    assert result.new_count == 2
    assert result.notifications_sent == 0
    assert len(service_harness.repo.load_active_jobs(service_harness.source.id)) == 2
    assert service_harness.notifier.messages == []
    assert service_harness.repo.pending_events(service_harness.source.id) == []


@pytest.mark.asyncio
async def test_live_cycle_sends_exactly_one_alert_for_one_added_job(
    service_harness: ServiceHarness,
) -> None:
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.adapter.set_fixture("three-jobs")
    first = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )
    second = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )

    assert first.notifications_sent == 1
    assert second.notifications_sent == 0
    assert len(service_harness.notifier.messages) == 1
    assert "Robotics Software Engineer 3" in service_harness.notifier.messages[0].body
    # The repeat cycle is answered 304, and the snapshot it reuses keeps the board intact.
    assert second.not_modified is True
    assert len(service_harness.active_keys()) == 3


@pytest.mark.asyncio
async def test_dry_run_reports_the_board_without_writing_state(
    service_harness: ServiceHarness,
) -> None:
    result = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.DRY_RUN
    )

    assert result.complete is True
    assert result.new_count == 2
    # A dry run must never become the snapshot absence is reasoned about, so the run is stored as
    # non-complete while reporting the complete collection it fetched.
    assert result.outcome is RunOutcome.PARTIAL
    assert service_harness.repo.load_active_jobs(service_harness.source.id) == []
    assert service_harness.repo.last_complete_job_keys(service_harness.source.id) == set()
    assert service_harness.repo.last_applied_snapshot(service_harness.source.id) is None
    assert service_harness.repo.source_health(service_harness.source.id).etag is None
    assert service_harness.repo.pending_events(service_harness.source.id) == []
    assert service_harness.notifier.messages == []


@pytest.mark.parametrize("mode", [RunMode.LIVE, RunMode.LIVE_SUPPRESSED])
@pytest.mark.asyncio
async def test_a_source_never_seeded_is_seeded_by_its_first_snapshot_without_events(
    service_harness: ServiceHarness, mode: RunMode
) -> None:
    """A board nobody baselined holds postings that were already public, so none of them is news.

    A failed baseline, or a source enabled after the initial seeding, would otherwise announce its
    whole board on its first complete cycle.
    """
    first = await service_harness.service.collect_source(service_harness.source, mode=mode)

    assert first.complete is True
    assert first.seeded is True
    assert first.notifications_sent == 0
    assert first.would_deliver == ()
    assert any("seeded" in warning for warning in first.warnings)
    assert service_harness.repo.pending_events(service_harness.source.id) == []
    assert len(service_harness.active_keys()) == 2
    assert service_harness.notifier.messages == []

    service_harness.adapter.set_fixture("three-jobs")
    second = await service_harness.service.collect_source(service_harness.source, mode=mode)

    assert second.seeded is False
    assert second.new_count == 1
    assert len(second.would_deliver) == 1
    if mode is RunMode.LIVE:
        assert second.notifications_sent == 1
        assert "Robotics Software Engineer 3" in service_harness.notifier.messages[0].body


NOTHING_MATCHES = (
    FilterConfig(
        id="harness-nothing",
        notify=True,
        include_companies=["acme"],
        include_title_keywords=["astronaut"],
    ),
)


@pytest.mark.parametrize("mode", [RunMode.LIVE, RunMode.LIVE_SUPPRESSED])
@pytest.mark.asyncio
async def test_a_posting_no_filter_approves_is_resolved_and_never_resurrected(
    service_harness: ServiceHarness, mode: RunMode
) -> None:
    """Most new postings match no filter; they must not stay pending and pile up forever.

    Left pending, every cycle and heartbeat would re-filter an ever-growing set, and widening a
    filter later would announce every old posting at once.
    """
    service_harness.set_filters(NOTHING_MATCHES)
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.adapter.set_fixture("three-jobs")

    result = await service_harness.service.collect_source(service_harness.source, mode=mode)

    assert result.new_count == 1
    assert result.would_deliver == ()
    assert service_harness.repo.pending_events(service_harness.source.id) == []
    assert service_harness.notifier.messages == []
    # A filter decision is not a provider attempt, so delivery reports do not count it.
    counts = service_harness.repo.window_counts(FIXED_NOW, FIXED_NOW + timedelta(days=1))
    assert counts.attempts == 0

    # Widening the filter afterwards must not announce the posting that was already decided.
    service_harness.set_filters(DEFAULT_FILTERS)
    service_harness.clock.advance(minutes=10)
    later = await service_harness.service.collect_source(service_harness.source, mode=RunMode.LIVE)
    assert later.notifications_sent == 0
    assert later.would_deliver == ()
    assert service_harness.notifier.messages == []


@pytest.mark.asyncio
async def test_a_cycle_reports_the_collection_it_fetched(service_harness: ServiceHarness) -> None:
    result = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.DRY_RUN
    )

    assert result.metrics is not None
    assert result.metrics.page_count == 1
    assert result.metrics.declared_total == 2
    assert result.metrics.parsed_total == 2
    assert result.metrics.stable_id_percent == 100.0
    assert result.metrics.direct_url_percent == 100.0
    assert result.duration_seconds is not None
    assert result.duration_seconds >= 0


@pytest.mark.asyncio
async def test_an_incomplete_collection_still_reports_what_it_parsed(
    service_harness: ServiceHarness,
) -> None:
    service_harness.adapter.set_fixture("partial")

    result = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.DRY_RUN
    )

    assert result.complete is False
    assert result.metrics is not None
    assert result.metrics.parsed_total == 2
    # The adapter's own reason is what an operator needs, so it survives into the cycle report.
    assert result.error_message is not None
    assert "page 2 failed" in result.error_message


@pytest.mark.asyncio
async def test_a_challenged_cycle_reports_no_collection_metrics(
    service_harness: ServiceHarness,
) -> None:
    service_harness.adapter.set_fixture("challenge")

    result = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.DRY_RUN
    )

    assert result.outcome is RunOutcome.CHALLENGED
    assert result.metrics is None
    assert result.duration_seconds is not None


@pytest.mark.asyncio
async def test_a_304_is_a_complete_no_change_run_over_the_last_applied_snapshot(
    service_harness: ServiceHarness,
) -> None:
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    seeded = service_harness.active_keys()
    assert service_harness.repo.source_health(service_harness.source.id).etag == '"acme-two"'

    result = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )

    assert result.not_modified is True
    assert result.complete is True
    assert result.outcome is RunOutcome.COMPLETE
    assert result.new_count == 0
    assert result.notifications_sent == 0
    assert service_harness.active_keys() == seeded
    # The no-change run re-stages the board it confirmed, so it is a coherent complete snapshot and
    # not an empty one that would close every job.
    assert service_harness.repo.last_complete_job_keys(service_harness.source.id) == set(seeded)
    assert service_harness.repo.source_health(service_harness.source.id).etag == '"acme-two"'


@pytest.mark.asyncio
async def test_a_304_without_any_snapshot_is_never_read_as_an_empty_board(
    service_harness: ServiceHarness,
) -> None:
    """A provider that answers 304 unasked must fail closed, not empty the board."""
    service_harness.adapter.set_fixture("always-not-modified")

    result = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE_SUPPRESSED
    )

    assert result.complete is False
    assert result.outcome is RunOutcome.FAILED
    assert service_harness.repo.load_active_jobs(service_harness.source.id) == []
    assert service_harness.repo.last_complete_job_keys(service_harness.source.id) == set()
    assert service_harness.notifier.messages == []


@pytest.mark.asyncio
async def test_a_delivery_failure_after_the_commit_retries_once_after_a_restart(
    service_harness: ServiceHarness,
) -> None:
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.adapter.set_fixture("three-jobs")
    service_harness.notifier.queue(
        DeliveryResult(
            provider="recording", delivered=False, retryable=True, error="connection refused"
        )
    )

    failed = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )

    assert failed.notifications_sent == 0
    assert [event.event_type for event in service_harness.repo.pending_events(
        service_harness.source.id
    )] == [JobEventType.NEW]
    # The event was committed before the send was attempted, and the job state it describes
    # survived the failed delivery.
    assert len(service_harness.active_keys()) == 3

    service_harness.restart()
    # The recorded 60s backoff is enforced, so the retry waits for it before it may be spent.
    service_harness.clock.advance(minutes=2)
    retried = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )

    assert retried.notifications_sent == 1
    assert len(service_harness.notifier.messages) == 2
    assert "Robotics Software Engineer 3" in service_harness.notifier.messages[1].body
    assert service_harness.repo.pending_events(service_harness.source.id) == []


@pytest.mark.asyncio
async def test_a_job_absent_from_two_complete_snapshots_closes(
    service_harness: ServiceHarness,
) -> None:
    """The absence streak is fed both halves of the diff: missing and threshold-closing keys."""
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    seeded = service_harness.active_keys()
    service_harness.adapter.set_fixture("one-job")

    first_absence = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE_SUPPRESSED
    )

    assert first_absence.missing_count == 1
    assert first_absence.closed_count == 0
    assert sorted(service_harness.missing_counts().values()) == [0, 1]

    # A second complete snapshot that still lacks the job closes it. The board is edited, so the
    # provider answers 200 rather than 304 and the run commits through the normal path.
    service_harness.adapter.set_fixture("one-job-edited")
    second_absence = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE_SUPPRESSED
    )

    assert second_absence.not_modified is False
    assert second_absence.closed_count == 1
    assert service_harness.active_keys() == seeded[:1]
    assert service_harness.repo.last_complete_job_keys(service_harness.source.id) == {seeded[0]}


@pytest.mark.asyncio
async def test_an_absence_streak_advances_through_a_304(service_harness: ServiceHarness) -> None:
    """A 304 run advances an absence the applied board no longer holds."""
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.adapter.set_fixture("one-job")
    await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE_SUPPRESSED
    )

    closing = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE_SUPPRESSED
    )

    assert closing.not_modified is True
    assert closing.closed_count == 1
    assert len(service_harness.active_keys()) == 1


@pytest.mark.asyncio
async def test_a_partial_collection_leaves_existing_jobs_untouched(
    service_harness: ServiceHarness,
) -> None:
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    seeded = service_harness.active_keys()
    service_harness.adapter.set_fixture("partial")

    result = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE_SUPPRESSED
    )

    assert result.complete is False
    assert result.outcome is RunOutcome.PARTIAL
    assert service_harness.active_keys() == seeded
    assert service_harness.repo.last_complete_job_keys(service_harness.source.id) == set(seeded)
    assert set(service_harness.missing_counts().values()) == {0}
    assert service_harness.repo.pending_events(service_harness.source.id) == []
    assert service_harness.notifier.messages == []


@pytest.mark.asyncio
async def test_a_challenge_page_leaves_existing_jobs_untouched(
    service_harness: ServiceHarness,
) -> None:
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    seeded = service_harness.active_keys()
    service_harness.adapter.set_fixture("challenge")

    result = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )

    assert result.complete is False
    assert result.outcome is RunOutcome.CHALLENGED
    assert service_harness.active_keys() == seeded
    assert service_harness.repo.pending_events(service_harness.source.id) == []
    assert service_harness.notifier.messages == []


@pytest.mark.asyncio
async def test_a_large_drop_is_quarantined_until_a_second_identical_snapshot_confirms(
    service_harness: ServiceHarness,
) -> None:
    service_harness.adapter.set_fixture("hundred-jobs")
    seeded = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.BASELINE
    )
    assert seeded.new_count == 100
    service_harness.adapter.set_fixture("ten-jobs")

    dropped = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE_SUPPRESSED
    )

    assert dropped.quarantined is True
    assert dropped.outcome is RunOutcome.QUARANTINED
    assert dropped.complete is True
    assert (dropped.new_count, dropped.missing_count, dropped.closed_count) == (0, 90, 0)
    assert len(service_harness.active_keys()) == 100
    assert set(service_harness.missing_counts().values()) == {0}
    assert service_harness.repo.pending_events(service_harness.source.id) == []
    pending = service_harness.repo.pending_quarantine(service_harness.source.id)
    assert pending is not None
    assert (pending.previous_job_count, pending.candidate_job_count) == (100, 10)

    confirming = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE_SUPPRESSED
    )

    assert confirming.quarantined is False
    assert confirming.outcome is RunOutcome.COMPLETE
    assert service_harness.repo.pending_quarantine(service_harness.source.id) is None
    counts = service_harness.missing_counts()
    assert len(counts) == 100
    assert sorted(counts.values()) == [0] * 10 + [1] * 90
    assert confirming.new_count == 0
    assert confirming.notifications_sent == 0

    # A third identical collection completes the absence streak and closes the ninety jobs.
    settled = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE_SUPPRESSED
    )
    assert settled.closed_count == 90
    assert len(service_harness.active_keys()) == 10


@pytest.mark.asyncio
async def test_a_challenge_page_cannot_confirm_a_quarantine(
    service_harness: ServiceHarness,
) -> None:
    service_harness.adapter.set_fixture("hundred-jobs")
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.adapter.set_fixture("ten-jobs")
    quarantined = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE_SUPPRESSED
    )
    assert quarantined.quarantined is True

    service_harness.adapter.set_fixture("challenge")
    challenged = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE_SUPPRESSED
    )

    assert challenged.outcome is RunOutcome.CHALLENGED
    assert challenged.quarantined is False
    assert service_harness.repo.pending_quarantine(service_harness.source.id) is not None
    assert len(service_harness.active_keys()) == 100
    assert set(service_harness.missing_counts().values()) == {0}
    assert service_harness.repo.pending_events(service_harness.source.id) == []

    service_harness.adapter.set_fixture("ten-jobs")
    confirming = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE_SUPPRESSED
    )
    assert confirming.quarantined is False
    assert confirming.outcome is RunOutcome.COMPLETE
    assert service_harness.repo.pending_quarantine(service_harness.source.id) is None


@pytest.mark.asyncio
async def test_a_board_that_returns_unchanged_clears_a_withheld_candidate(
    service_harness: ServiceHarness,
) -> None:
    """A 304 proves the board is the one already applied, so the drop suspicion is moot."""
    service_harness.adapter.set_fixture("hundred-jobs")
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.adapter.set_fixture("ten-jobs")
    quarantined = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE_SUPPRESSED
    )
    assert quarantined.quarantined is True

    service_harness.adapter.set_fixture("hundred-jobs")
    reverted = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE_SUPPRESSED
    )

    assert reverted.not_modified is True
    assert service_harness.repo.pending_quarantine(service_harness.source.id) is None
    assert len(service_harness.active_keys()) == 100
    assert set(service_harness.missing_counts().values()) == {0}


@pytest.mark.asyncio
async def test_a_posting_already_stored_under_another_key_is_not_alerted(
    service_harness: ServiceHarness,
) -> None:
    """Every source observation is stored; the public posting is announced at most once."""
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.adapter.set_fixture("repost-of-job-one")

    result = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )

    assert result.new_count == 1
    assert result.notifications_sent == 0
    assert len(result.suppressed_duplicates) == 1
    # The duplicate is still linked: its record is stored as an active job.
    assert len(service_harness.active_keys()) == 3
    assert service_harness.repo.pending_events(service_harness.source.id) == []
    assert service_harness.notifier.messages == []


@pytest.mark.asyncio
async def test_a_posting_whose_alert_was_delivered_is_not_alerted_again(
    service_harness: ServiceHarness,
) -> None:
    """The dedupe outlives the announced job closing and being republished."""
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.adapter.set_fixture("three-jobs")
    announced = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )
    assert announced.notifications_sent == 1

    service_harness.adapter.set_fixture("two-jobs")
    closed = None
    for _ in range(2):
        closed = await service_harness.service.collect_source(
            service_harness.source, mode=RunMode.LIVE_SUPPRESSED
        )
    assert closed is not None and closed.closed_count == 1

    # The posting reappears under a new source job id while the announced record is gone, so only
    # the delivered alert's dedupe key stands between the repost and a second alert.
    service_harness.adapter.set_fixture("moved-requisition-three")
    result = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )

    assert result.new_count == 1
    assert result.notifications_sent == 0
    assert len(result.suppressed_duplicates) == 1
    assert len(service_harness.notifier.messages) == 1


@pytest.mark.asyncio
async def test_quiet_hours_defer_an_alert_until_the_window_closes(
    service_harness: ServiceHarness,
) -> None:
    service_harness.set_filters(
        [
            FilterConfig(
                id="quiet-window",
                notify=True,
                include_companies=["acme"],
                quiet_hours_start="11:30",
                quiet_hours_end="13:00",
                timezone="UTC",
            )
        ]
    )
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.adapter.set_fixture("three-jobs")

    deferred = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )

    assert deferred.notifications_sent == 0
    assert service_harness.notifier.messages == []
    assert len(deferred.would_deliver) == 1
    assert deferred.would_deliver[0].deferred is True
    assert deferred.would_deliver[0].defer_until == datetime(2026, 9, 18, 13, 0, tzinfo=UTC)
    assert len(service_harness.repo.pending_events(service_harness.source.id)) == 1

    # The deferral is recomputed from the event's stored detection instant, not from the retry's
    # clock, so moving time past the window releases exactly the same event.
    service_harness.clock.advance(hours=2)
    delivered = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )

    assert delivered.notifications_sent == 1
    assert delivered.would_deliver[0].deferred is False
    assert len(service_harness.notifier.messages) == 1


@pytest.mark.asyncio
async def test_an_uncertain_delivery_is_abandoned_after_the_retry_cap(
    service_harness: ServiceHarness,
) -> None:
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.adapter.set_fixture("three-jobs")
    ambiguous = DeliveryResult(
        provider="recording",
        delivered=False,
        retryable=True,
        uncertain=True,
        error="read timeout after the request was sent",
    )
    service_harness.notifier.queue(ambiguous, ambiguous, ambiguous)

    for _ in range(3):
        attempt = await service_harness.service.collect_source(
            service_harness.source, mode=RunMode.LIVE
        )
        assert attempt.notifications_sent == 0

    assert len(service_harness.notifier.messages) == 3
    assert service_harness.repo.pending_events(service_harness.source.id) == []

    fourth = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )
    assert fourth.notifications_sent == 0
    assert len(service_harness.notifier.messages) == 3


@pytest.mark.asyncio
async def test_one_ambiguous_answer_keeps_the_event_pending(
    service_harness: ServiceHarness,
) -> None:
    """Below the cap an ambiguous delivery is retried, and a certain no-send never abandons."""
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.adapter.set_fixture("three-jobs")
    service_harness.notifier.queue(
        DeliveryResult(
            provider="recording",
            delivered=False,
            retryable=True,
            uncertain=True,
            error="read timeout",
        ),
        DeliveryResult(
            provider="recording",
            delivered=False,
            retryable=True,
            error="connect error before the request was sent",
        ),
    )

    first = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )
    assert first.notifications_sent == 0
    pending = service_harness.repo.pending_events(service_harness.source.id)
    assert len(pending) == 1
    event_id = pending[0].event_id

    second = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )
    assert second.notifications_sent == 0
    assert service_harness.repo.attempt_counts([event_id], outcome=DeliveryOutcome.UNCERTAIN) == {
        event_id: 1
    }
    assert service_harness.repo.attempt_counts([event_id], outcome=DeliveryOutcome.RETRYABLE) == {
        event_id: 1
    }
    # A definitive no-send failure leaves the alert pending rather than abandoning it, and the
    # delay it recorded keeps the next cycle from spending a second attempt too early.
    assert len(service_harness.repo.pending_events(service_harness.source.id)) == 1
    too_soon = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )
    assert too_soon.notifications_sent == 0
    assert len(service_harness.notifier.messages) == 2
    service_harness.clock.advance(minutes=2)

    delivered = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )
    assert delivered.notifications_sent == 1
    assert service_harness.repo.pending_events(service_harness.source.id) == []


@pytest.mark.asyncio
async def test_five_added_jobs_arrive_as_one_grouped_message(
    service_harness: ServiceHarness,
) -> None:
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.adapter.set_fixture("seven-jobs")

    result = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )

    assert result.new_count == 5
    assert result.notifications_sent == 1
    assert len(service_harness.notifier.messages) == 1
    assert service_harness.notifier.messages[0].title == "5 new jobs \u2014 Acme Robotics"


@pytest.mark.asyncio
async def test_a_grouped_delivery_links_every_event_it_covers(
    service_harness: ServiceHarness,
) -> None:
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.adapter.set_fixture("seven-jobs")
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.LIVE)

    with sqlite3.connect(service_harness.db_path) as connection:
        attempts, links = connection.execute(
            "SELECT (SELECT COUNT(*) FROM notification_attempts),"
            " (SELECT COUNT(*) FROM notification_event_links)"
        ).fetchone()

    assert (attempts, links) == (1, 5)
    assert service_harness.repo.pending_events(service_harness.source.id) == []


@pytest.mark.asyncio
async def test_live_suppressed_reports_without_calling_a_provider(
    service_harness: ServiceHarness,
) -> None:
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.adapter.set_fixture("three-jobs")

    result = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE_SUPPRESSED
    )

    assert result.notifications_sent == 0
    assert service_harness.notifier.messages == []
    assert len(result.would_deliver) == 1
    assert result.would_deliver[0].deferred is False
    assert result.would_deliver[0].title == "Robotics Software Engineer 3"
    assert result.would_deliver[0].priority.value == "normal"
    # The event is durable even though nothing was sent, so a later live cycle can still deliver it.
    assert len(service_harness.repo.pending_events(service_harness.source.id)) == 1


@pytest.mark.asyncio
async def test_an_updated_posting_is_recorded_and_never_alerted(
    service_harness: ServiceHarness,
) -> None:
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.adapter.set_fixture("edited-first-job")

    result = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )

    assert result.updated_count == 1
    assert result.new_count == 0
    assert result.notifications_sent == 0
    assert service_harness.notifier.messages == []


@pytest.mark.asyncio
async def test_reappearing_job_reopens_and_alerts_once(service_harness: ServiceHarness) -> None:
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.adapter.set_fixture("three-jobs")
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.adapter.set_fixture("two-jobs")
    closing = None
    for _ in range(2):
        closing = await service_harness.service.collect_source(
            service_harness.source, mode=RunMode.LIVE_SUPPRESSED
        )
    assert closing is not None and closing.closed_count == 1

    service_harness.adapter.set_fixture("three-jobs")
    reopened = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )

    assert reopened.reopened_count == 1
    assert reopened.new_count == 0
    assert reopened.notifications_sent == 1
    assert "Reopened job" in service_harness.notifier.messages[0].title


@pytest.mark.asyncio
async def test_a_baseline_seeded_board_reports_nothing_new_on_the_next_cycle(
    service_harness: ServiceHarness,
) -> None:
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.clock.advance(minutes=10)

    result = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE_SUPPRESSED
    )

    assert result.outcome is RunOutcome.COMPLETE
    assert result.new_count == 0
    assert result.unchanged_count == 2
    assert result.not_modified is True
    assert result.would_deliver == ()
    assert service_harness.repo.pending_events(service_harness.source.id) == []


@pytest.mark.asyncio
async def test_an_unknown_provider_fails_without_touching_state(
    service_harness: ServiceHarness,
) -> None:
    unsupported = service_harness.source.model_copy(
        update={"id": "nope-fixture", "provider": "nope"}
    )

    result = await service_harness.service.collect_source(unsupported, mode=RunMode.LIVE)

    assert result.outcome is RunOutcome.FAILED
    assert result.error_code == "unsupported_provider"
    assert service_harness.repo.load_active_jobs(unsupported.id) == []


@pytest.mark.asyncio
async def test_live_mode_refuses_to_run_without_a_notifier(service_harness: ServiceHarness) -> None:
    service = JobAlertService(
        service_harness.repo,
        registry=AdapterRegistry([service_harness.adapter]),
        fetcher=service_harness.fetcher,
    )

    with pytest.raises(ValueError, match="notifier"):
        await service.collect_source(service_harness.source, mode=RunMode.LIVE)
    # Refusing before the fetch means no poll run and no state was created either.
    assert service_harness.repo.source_health(service_harness.source.id) is None
    assert service_harness.adapter.requests == []
    suppressed = await service.collect_source(service_harness.source, mode=RunMode.LIVE_SUPPRESSED)
    assert suppressed.complete is True


@pytest.mark.asyncio
async def test_stored_validators_are_offered_to_the_endpoint(
    service_harness: ServiceHarness,
) -> None:
    """The saved ETag reaches the provider only after a complete board stored it."""
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    assert [str(request.url) for request in service_harness.adapter.requests] == [ENDPOINT]
    assert service_harness.adapter.headers[-1].get("if-none-match") is None

    service_harness.clock.advance(minutes=5)
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.LIVE)

    assert service_harness.adapter.headers[-1]["if-none-match"] == '"acme-two"'


@pytest.mark.asyncio
async def test_a_permanent_delivery_failure_ends_the_question(
    service_harness: ServiceHarness,
) -> None:
    """A provider that refuses the message for good is not hammered every cycle."""
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.adapter.set_fixture("three-jobs")
    service_harness.notifier.queue(
        DeliveryResult(
            provider="recording",
            delivered=False,
            retryable=False,
            http_status=400,
            error="invalid request",
        )
    )

    refused = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )
    follow_up = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )

    assert refused.notifications_sent == 0
    assert follow_up.notifications_sent == 0
    assert len(service_harness.notifier.messages) == 1
    assert service_harness.repo.pending_events(service_harness.source.id) == []


@pytest.mark.asyncio
async def test_three_dry_runs_leave_the_failure_streak_alone(
    service_harness: ServiceHarness,
) -> None:
    """A diagnostic the operator asked for is not evidence that the source is failing."""
    for _ in range(3):
        diagnostic = await service_harness.service.collect_source(
            service_harness.source, mode=RunMode.DRY_RUN
        )
        assert diagnostic.error_code == "dry_run"
        assert diagnostic.outcome is RunOutcome.PARTIAL

    healthy = service_harness.repo.source_health(service_harness.source.id)
    assert healthy is not None
    assert healthy.consecutive_failures == 0
    assert healthy.first_failure_at is None
    assert healthy.last_error is None
    assert healthy.health_alert_open is False
    # The dry runs are still recorded, so the operator can see what was checked and when.
    assert healthy.last_run_at is not None

    service_harness.adapter.set_fixture("challenge")
    for _ in range(3):
        challenged = await service_harness.service.collect_source(
            service_harness.source, mode=RunMode.LIVE_SUPPRESSED
        )
        assert challenged.outcome is RunOutcome.CHALLENGED

    failing = service_harness.repo.source_health(service_harness.source.id)
    assert failing is not None
    assert failing.consecutive_failures == 3
    assert failing.first_failure_at is not None
    assert failing.last_error is not None


@pytest.mark.asyncio
async def test_a_delivery_answer_carries_its_quota_and_retry_signal(
    service_harness: ServiceHarness,
) -> None:
    """The provider's own quota and retry figures reach the database the digest reads."""
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.adapter.set_fixture("three-jobs")
    reset_at = datetime(2026, 10, 1, tzinfo=UTC)
    service_harness.notifier.queue(
        DeliveryResult(
            provider="recording",
            delivered=False,
            retryable=True,
            uncertain=True,
            http_status=429,
            error="rate limited",
            retry_after_seconds=90.0,
            quota_limit=10000,
            quota_remaining=0,
            quota_reset_at=reset_at,
        )
    )

    throttled = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )

    assert throttled.notifications_sent == 0
    assert len(service_harness.repo.pending_events(service_harness.source.id)) == 1
    quota = service_harness.repo.latest_quota("recording")
    assert quota is not None
    assert (quota.limit, quota.remaining) == (10000, 0)
    assert quota.reset_at == reset_at
    with sqlite3.connect(service_harness.db_path) as connection:
        stored = connection.execute(
            "SELECT retry_after_seconds FROM notification_attempts"
        ).fetchone()
    assert stored == (90.0,)


# ------------------------------------------------------------------ obligation 1: one writer


@pytest.mark.asyncio
async def test_one_failed_cycle_counts_as_exactly_one_failure(
    service_harness: ServiceHarness,
) -> None:
    """A cycle is one failure, not two: only the health evaluation counts the run."""
    service_harness.adapter.set_fixture("challenge")

    failed = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE_SUPPRESSED
    )

    assert failed.outcome is RunOutcome.CHALLENGED
    assert failed.health_events == ()
    state = service_harness.repo.source_health(service_harness.source.id)
    assert state is not None
    assert state.consecutive_failures == 1
    assert state.first_failure_at is not None
    assert state.last_error is not None
    assert state.health_alert_open is False


@pytest.mark.asyncio
async def test_three_failing_cycles_open_one_incident_exactly_once(
    service_harness: ServiceHarness,
) -> None:
    """The streak reaches three once, and only that third run opens an incident."""
    service_harness.adapter.set_fixture("challenge")
    events = []
    streaks = []
    for _ in range(3):
        failed = await service_harness.service.collect_source(
            service_harness.source, mode=RunMode.LIVE_SUPPRESSED
        )
        events.extend(failed.health_events)
        state = service_harness.repo.source_health(service_harness.source.id)
        assert state is not None
        streaks.append(state.consecutive_failures)

    assert streaks == [1, 2, 3]
    assert [event.kind for event in events] == [HealthEventKind.SOURCE_FAILURE]
    assert events[0].consecutive_failures == 3
    assert events[0].source_id == service_harness.source.id
    state = service_harness.repo.source_health(service_harness.source.id)
    assert state is not None and state.health_alert_open is True
    # Suppressed runs report the incident without pushing it, and an open incident stays single.
    assert service_harness.notifier.messages == []

    fourth = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE_SUPPRESSED
    )
    assert fourth.health_events == ()


@pytest.mark.asyncio
async def test_a_live_incident_is_pushed_once_and_its_recovery_too(
    service_harness: ServiceHarness,
) -> None:
    """Health events reach the phone through the same notifier the alerts use, and only in LIVE."""
    service_harness.adapter.set_fixture("challenge")
    for _ in range(3):
        await service_harness.service.collect_source(service_harness.source, mode=RunMode.LIVE)

    titles = [message.title for message in service_harness.notifier.messages]
    assert len([title for title in titles if title.startswith("Source failing")]) == 1

    service_harness.adapter.set_fixture("two-jobs")
    recovered = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )

    assert [event.kind for event in recovered.health_events] == [
        HealthEventKind.SOURCE_RECOVERY
    ]
    titles = [message.title for message in service_harness.notifier.messages]
    assert len([title for title in titles if title.startswith("Source recovered")]) == 1
    state = service_harness.repo.source_health(service_harness.source.id)
    assert state is not None
    assert state.health_alert_open is False
    assert state.consecutive_failures == 0
