"""Exactly-once delivery across process restarts, over an on-disk SQLite database.

``test_service_cycle.py`` already retries a failed delivery after one restart by running another
collection cycle. This module covers the paths that test does not: a restart between the baseline
and the first new posting, a retry spent through ``deliver_pending`` (what ``retry-notifications``
and the heartbeat call, without fetching the board), and a final restart and cycle that must send
nothing.
"""

from __future__ import annotations

import pytest

from job_alert.notifications.base import DeliveryResult
from job_alert.service import RunMode
from tests.integration.harness import ServiceHarness

BASELINE_TITLES = ("Robotics Software Engineer 1", "Robotics Software Engineer 2")
NEW_TITLE = "Robotics Software Engineer 3"


def _mentions_baseline(body: str) -> bool:
    # The boards here hold at most three postings, so no title is a prefix of another one.
    return any(title in body for title in BASELINE_TITLES)


@pytest.mark.asyncio
async def test_a_new_posting_is_delivered_exactly_once_across_restarts(
    service_harness: ServiceHarness,
) -> None:
    harness = service_harness
    assert harness.db_path.parent.exists()  # durable state lives in a real file, not :memory:

    seeded = await harness.service.collect_source(harness.source, mode=RunMode.BASELINE)
    assert seeded.new_count == 2
    assert seeded.notifications_sent == 0

    # Process 2: the board gains one posting, and the provider fails the first send transiently.
    harness.restart()
    harness.adapter.set_fixture("three-jobs")
    harness.notifier.queue(
        DeliveryResult(
            provider="recording", delivered=False, retryable=True, error="connection reset"
        )
    )
    failed = await harness.service.collect_source(harness.source, mode=RunMode.LIVE)
    assert failed.new_count == 1
    assert failed.notifications_sent == 0
    assert len(harness.repo.pending_events(harness.source.id)) == 1

    # Process 3: the retry path delivers the committed event without reading the board again.
    harness.restart()
    requests_before = len(harness.adapter.requests)
    harness.clock.advance(minutes=2)  # past the recorded retry delay
    sent, _ = await harness.service.deliver_pending(harness.source)
    assert sent == 1
    assert len(harness.adapter.requests) == requests_before
    assert harness.repo.pending_events(harness.source.id) == []

    # Process 4: another cycle over the same board must neither re-announce nor re-stage anything.
    harness.restart()
    harness.clock.advance(minutes=10)
    repeat = await harness.service.collect_source(harness.source, mode=RunMode.LIVE)
    assert repeat.new_count == 0
    assert repeat.notifications_sent == 0
    again, _ = await harness.service.deliver_pending(harness.source)
    assert again == 0

    # Two attempts reached the provider: the failure and the one success. Only the success counts,
    # and neither attempt ever mentioned a baseline posting.
    bodies = [message.body for message in harness.notifier.messages]
    assert len(bodies) == 2
    assert all(NEW_TITLE in body for body in bodies)
    assert not any(_mentions_baseline(body) for body in bodies)
    assert len(harness.active_keys()) == 3
