"""Shared delivery primitives: message limits, provider protocol, and burst grouping."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from job_alert.config import FilterPriority
from job_alert.models import JobEvent, JobEventType, NormalizedJob
from job_alert.notifications.base import (
    BODY_LIMIT,
    TITLE_LIMIT,
    AlertEvent,
    NotificationMessage,
    Notifier,
    group_job_events,
)
from job_alert.notifications.ntfy import NtfyNotifier
from job_alert.notifications.pushover import PushoverNotifier

JobBuilder = Callable[..., NormalizedJob]

DETECTED_AT = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def posting(engineering_job: JobBuilder, index: int, **overrides: object) -> NormalizedJob:
    """A robotics posting with its own public URL, so links can be told apart."""
    url = f"https://boards.greenhouse.io/nvidia/jobs/{index}"
    return engineering_job(
        source_job_id=str(index),
        canonical_job_key=f"greenhouse:nvidia:{index}",
        posting_url=url,
        apply_url=url,
        **overrides,
    )


def alert(
    event_id: int,
    job: NormalizedJob,
    *,
    priority: FilterPriority = FilterPriority.NORMAL,
    company_name: str = "NVIDIA",
    detected_at: datetime = DETECTED_AT,
    event_type: JobEventType = JobEventType.NEW,
) -> AlertEvent:
    """One matched event as Task 21 hands it to the grouping step."""
    return AlertEvent(
        event_id=event_id,
        event=JobEvent(event_type=event_type, job=job, detected_at=detected_at),
        priority=priority,
        company_name=company_name,
    )


def notifier_secrets() -> tuple[str, str]:
    """Credentials no delivery path may print."""
    return "secret-token", "secret-user"


def test_both_providers_satisfy_the_notifier_protocol() -> None:
    assert isinstance(PushoverNotifier(app_token="token", user_key="user"), Notifier)
    assert isinstance(NtfyNotifier("https://ntfy.sh/job-alerts", access_token="token"), Notifier)


def test_long_multibyte_body_is_truncated_to_the_pushover_limit() -> None:
    message = NotificationMessage(
        title="New job — NVIDIA",
        body="職" * (BODY_LIMIT + 50),
        url="https://example.com/jobs/1",
    )
    assert len(message.body) == BODY_LIMIT
    # Counting is in characters, so the cut never splits a multi-byte character in half.
    assert message.body == "職" * (BODY_LIMIT - 1) + "…"


def test_long_title_keeps_the_event_label_and_the_company() -> None:
    message = NotificationMessage(
        title=f"New job — {'C' * 400}",
        body="Robotics Software Engineer",
        url="https://example.com/jobs/1",
    )
    assert len(message.title) == TITLE_LIMIT
    assert message.title.startswith("New job — ")
    assert message.title.endswith("C" * 40)
    assert "…" in message.title


def test_notification_urls_must_be_https() -> None:
    with pytest.raises(ValueError):
        NotificationMessage(title="t", body="b", url="http://example.com/jobs/1")


def test_individual_alert_preserves_company_job_title_and_url(
    engineering_job: JobBuilder,
) -> None:
    job = posting(
        engineering_job,
        7,
        title="Robotics Software Engineer",
        locations=["Santa Clara, CA"] * 200,
        description_text="preferred qualifications: " * 500,
    )
    batches = group_job_events([alert(11, job)])

    assert len(batches) == 1
    batch = batches[0]
    assert batch.message.title == "New job — NVIDIA"
    assert batch.event_ids == (11,)
    assert batch.immediate is False
    assert batch.message.body.startswith("Robotics Software Engineer")
    assert len(batch.message.body) <= BODY_LIMIT
    assert batch.message.url == "https://boards.greenhouse.io/nvidia/jobs/7"
    assert "preferred qualifications" not in batch.message.body


def test_five_same_company_events_become_one_message(engineering_job: JobBuilder) -> None:
    events = [
        alert(
            index,
            posting(engineering_job, index, title=f"Robotics Engineer {index}"),
            detected_at=DETECTED_AT + timedelta(seconds=10 * index),
        )
        for index in range(1, 6)
    ]

    batches = group_job_events(events)

    assert len(batches) == 1
    batch = batches[0]
    assert batch.message.title == "5 new jobs — NVIDIA"
    assert batch.event_ids == (1, 2, 3, 4, 5)
    assert batch.immediate is False
    for index in range(1, 6):
        assert f"Robotics Engineer {index}" in batch.message.body
    assert "more" not in batch.message.body


def test_grouped_message_links_the_highest_priority_posting(
    engineering_job: JobBuilder,
) -> None:
    events = [
        alert(1, posting(engineering_job, 1, title="Low Match"), priority=FilterPriority.LOW),
        alert(2, posting(engineering_job, 2, title="Top Match"), priority=FilterPriority.NORMAL),
        alert(3, posting(engineering_job, 3, title="Other Low"), priority=FilterPriority.LOW),
    ]

    batch = group_job_events(events)[0]

    assert batch.priority is FilterPriority.NORMAL
    assert batch.message.priority is FilterPriority.NORMAL
    assert batch.message.url == "https://boards.greenhouse.io/nvidia/jobs/2"
    assert batch.message.body.splitlines()[0] == "• Top Match — Santa Clara, CA · hybrid"


def test_grouped_bullets_keep_a_long_job_title_intact(engineering_job: JobBuilder) -> None:
    long_title = "Robotics " * 25 + "Engineer"
    events = [
        alert(1, posting(engineering_job, 1, title=long_title)),
        alert(2, posting(engineering_job, 2, title="Perception Engineer")),
    ]

    body = group_job_events(events)[0].message.body

    assert len(long_title) > 120
    assert f"• {long_title}" in body
    assert len(body) <= BODY_LIMIT


def test_a_grouped_body_too_long_for_every_bullet_drops_lines(engineering_job: JobBuilder) -> None:
    title = "R" * 400
    events = [
        alert(index, posting(engineering_job, index, title=f"{title} {index}"))
        for index in range(1, 6)
    ]

    body = group_job_events(events)[0].message.body

    bullets = [line for line in body.splitlines() if line.startswith("• ")]
    assert len(bullets) < 5
    assert len(body) <= BODY_LIMIT
    for index in range(1, len(bullets) + 1):
        # Every listed title is present in full; only whole lines are dropped.
        assert f"{title} {index}" in body
    assert f"…and {5 - len(bullets)} more" in body


def test_a_single_huge_bullet_drops_its_location_tail_but_keeps_its_title(
    engineering_job: JobBuilder,
) -> None:
    title = "R" * 1000
    events = [alert(index, posting(engineering_job, index, title=title)) for index in range(1, 6)]

    body = group_job_events(events)[0].message.body

    assert len(body) <= BODY_LIMIT
    assert body.startswith(f"• {title}")
    assert "Santa Clara" not in body
    assert "…and 4 more" in body


def test_overflowing_group_states_the_remaining_count(engineering_job: JobBuilder) -> None:
    events = [
        alert(
            index,
            posting(engineering_job, index, title=f"Robotics Engineer {index}"),
            detected_at=DETECTED_AT + timedelta(seconds=index),
        )
        for index in range(1, 9)
    ]

    batch = group_job_events(events)[0]

    assert batch.message.title == "8 new jobs — NVIDIA"
    assert batch.message.body.splitlines()[-1] == "…and 3 more"
    assert batch.message.body.count("• ") == 5
    assert batch.event_ids == tuple(range(1, 9))


def test_two_companies_in_one_window_stay_separate_messages(
    engineering_job: JobBuilder,
) -> None:
    events = [
        alert(1, posting(engineering_job, 1, title="Nvidia Role")),
        alert(
            2,
            posting(engineering_job, 2, title="Apple Role", company_id="apple"),
            company_name="Apple",
        ),
    ]

    batches = group_job_events(events)

    # Same detection instant, so the company id orders the two messages deterministically.
    assert [batch.message.title for batch in batches] == ["New job — Apple", "New job — NVIDIA"]
    assert [batch.event_ids for batch in batches] == [(2,), (1,)]


def test_high_priority_events_bypass_the_window_and_deliver_alone(
    engineering_job: JobBuilder,
) -> None:
    events = [
        alert(1, posting(engineering_job, 1, title="First")),
        alert(2, posting(engineering_job, 2, title="Second")),
        alert(3, posting(engineering_job, 3, title="Third")),
        alert(4, posting(engineering_job, 4, title="Urgent"), priority=FilterPriority.HIGH),
    ]

    batches = group_job_events(events)

    assert len(batches) == 2
    urgent, grouped = batches
    assert urgent.immediate is True
    assert urgent.priority is FilterPriority.HIGH
    assert urgent.event_ids == (4,)
    assert urgent.message.title == "New job — NVIDIA"
    assert "Urgent" in urgent.message.body
    assert grouped.immediate is False
    assert grouped.event_ids == (1, 2, 3)
    assert grouped.message.title == "3 new jobs — NVIDIA"


def test_events_outside_the_window_start_a_new_message(engineering_job: JobBuilder) -> None:
    events = [
        alert(1, posting(engineering_job, 1), detected_at=DETECTED_AT),
        alert(2, posting(engineering_job, 2), detected_at=DETECTED_AT + timedelta(seconds=30)),
        alert(3, posting(engineering_job, 3), detected_at=DETECTED_AT + timedelta(seconds=200)),
    ]

    batches = group_job_events(events, window_seconds=120)

    assert [batch.event_ids for batch in batches] == [(1, 2), (3,)]
    assert [batch.message.title for batch in batches] == [
        "2 new jobs — NVIDIA",
        "New job — NVIDIA",
    ]


def test_the_grouping_window_includes_its_boundary(engineering_job: JobBuilder) -> None:
    events = [
        alert(1, posting(engineering_job, 1), detected_at=DETECTED_AT),
        alert(2, posting(engineering_job, 2), detected_at=DETECTED_AT + timedelta(seconds=120)),
    ]

    assert group_job_events(events)[0].event_ids == (1, 2)


def test_grouping_is_deterministic_and_ignores_input_order(
    engineering_job: JobBuilder,
) -> None:
    events = [
        alert(
            index,
            posting(engineering_job, index),
            detected_at=DETECTED_AT + timedelta(seconds=index),
        )
        for index in range(1, 5)
    ]

    expected = group_job_events(events)

    assert group_job_events(list(reversed(events))) == expected
    assert group_job_events(events) == expected


def test_a_non_positive_window_is_refused(engineering_job: JobBuilder) -> None:
    with pytest.raises(ValueError):
        group_job_events([alert(1, posting(engineering_job, 1))], window_seconds=0)


def test_naive_detection_times_are_read_as_utc(engineering_job: JobBuilder) -> None:
    events = [
        alert(1, posting(engineering_job, 1), detected_at=datetime(2026, 9, 19, 12, 0)),
        alert(2, posting(engineering_job, 2), detected_at=datetime(2026, 9, 19, 12, 1)),
    ]

    assert group_job_events(events)[0].event_ids == (1, 2)


def test_secret_redaction_helper_covers_encoded_credentials() -> None:
    from urllib.parse import quote

    from job_alert.notifications.base import redact_secrets

    token, user_key = notifier_secrets()
    text = f"token={token} encoded={quote(token, safe='')} user={user_key}"

    redacted = redact_secrets(text, (token, user_key))

    assert token not in redacted
    assert user_key not in redacted
    assert quote(token, safe="") not in redacted
