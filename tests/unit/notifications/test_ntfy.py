"""Optional ntfy delivery: authentication, payload shape, and the public-topic guard."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from job_alert.notifications.base import NotificationConfigurationError, NotificationMessage
from job_alert.notifications.ntfy import NTFY_PRIORITIES, NtfyNotifier

TOPIC = "https://ntfy.sh/job-alerts-7f3a9c"
ACCESS_TOKEN = "secret-access-token"


def message(**overrides: object) -> NotificationMessage:
    values: dict[str, object] = {
        "title": "New job — NVIDIA",
        "body": "Robotics Software Engineer\nSanta Clara, CA · hybrid",
        "url": "https://example.com/jobs/123",
        "priority": "normal",
        "tags": ("robot", "briefcase"),
    }
    values.update(overrides)
    return NotificationMessage(**values)


def build_notifier(**overrides: object) -> NtfyNotifier:
    values: dict[str, object] = {"topic_url": TOPIC, "access_token": ACCESS_TOKEN}
    values.update(overrides)
    return NtfyNotifier(**values)  # type: ignore[arg-type]


def sent_body(route: respx.Route) -> dict[str, object]:
    return json.loads(route.calls[0].request.content.decode())


@respx.mock
@pytest.mark.asyncio
async def test_ntfy_posts_authenticated_alert_with_click_url_and_tags() -> None:
    route = respx.post(TOPIC).mock(
        return_value=httpx.Response(200, json={"id": "msg-1", "topic": "job-alerts"})
    )

    result = await build_notifier().send(message())

    assert result.delivered is True
    assert result.provider_request_id == "msg-1"
    assert route.calls[0].request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    body = sent_body(route)
    assert body["title"] == "New job — NVIDIA"
    assert body["message"].startswith("Robotics Software Engineer")
    assert body["click"] == "https://example.com/jobs/123"
    assert body["tags"] == ["robot", "briefcase"]
    assert "topic" not in body


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(("priority", "expected"), [("low", 2), ("normal", 3), ("high", 4)])
async def test_ntfy_maps_priorities_without_using_max(priority: str, expected: int) -> None:
    route = respx.post(TOPIC).mock(return_value=httpx.Response(200, json={"id": "msg-1"}))

    await build_notifier().send(message(priority=priority))

    assert sent_body(route)["priority"] == expected
    assert 5 not in NTFY_PRIORITIES.values()


@respx.mock
@pytest.mark.asyncio
async def test_ntfy_omits_tags_when_the_message_has_none() -> None:
    route = respx.post(TOPIC).mock(return_value=httpx.Response(200, json={"id": "msg-1"}))

    await build_notifier().send(message(tags=()))

    assert "tags" not in sent_body(route)


@respx.mock
@pytest.mark.asyncio
async def test_ntfy_rate_limit_is_retryable_and_not_replayed() -> None:
    route = respx.post(TOPIC).mock(return_value=httpx.Response(429, headers={"Retry-After": "12"}))

    result = await build_notifier().send(message())

    assert result.delivered is False
    assert result.retryable is True
    assert result.retry_after_seconds == 12.0
    assert result.http_status == 429
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_ntfy_server_failure_is_retryable_no_sooner_than_five_seconds() -> None:
    respx.post(TOPIC).mock(return_value=httpx.Response(502))

    result = await build_notifier().send(message())

    assert result.delivered is False
    assert result.retryable is True
    assert result.retry_after_seconds == 5.0


@respx.mock
@pytest.mark.asyncio
async def test_ntfy_client_error_is_permanent_and_redacted() -> None:
    respx.post(TOPIC).mock(return_value=httpx.Response(403, text=f"forbidden for {ACCESS_TOKEN}"))

    result = await build_notifier().send(message())

    assert result.delivered is False
    assert result.retryable is False
    assert result.http_status == 403
    assert result.error is not None and ACCESS_TOKEN not in result.error


@respx.mock
@pytest.mark.asyncio
async def test_ntfy_unreadable_success_body_is_not_a_delivery() -> None:
    respx.post(TOPIC).mock(return_value=httpx.Response(200, text="ok"))

    result = await build_notifier().send(message())

    assert result.delivered is False
    assert result.provider_request_id is None
    assert result.retryable is True
    assert result.uncertain is True


@respx.mock
@pytest.mark.asyncio
async def test_ntfy_transport_failure_is_uncertain_and_retryable() -> None:
    route = respx.post(TOPIC).mock(side_effect=httpx.ReadTimeout("timed out"))

    result = await build_notifier().send(message())

    assert result.delivered is False
    assert result.retryable is True
    # ntfy may have accepted the message before the response was lost.
    assert result.uncertain is True
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "delivered"),
    [
        (httpx.Response(200, json={"id": "msg-1"}), True),
        (httpx.Response(403, text="forbidden"), False),
        (httpx.Response(429, headers={"Retry-After": "12"}), False),
        (httpx.Response(502), False),
    ],
)
async def test_ntfy_definitive_answers_are_not_uncertain(
    response: httpx.Response, delivered: bool
) -> None:
    respx.post(TOPIC).mock(return_value=response)

    result = await build_notifier().send(message())

    assert result.delivered is delivered
    assert result.uncertain is False


def test_ntfy_rejects_an_unauthenticated_public_topic() -> None:
    with pytest.raises(NotificationConfigurationError):
        NtfyNotifier(topic_url="https://ntfy.sh/job-alerts")


def test_ntfy_treats_a_trailing_dot_public_host_as_public() -> None:
    dotted = "https://ntfy.sh./job-alerts"

    with pytest.raises(NotificationConfigurationError):
        NtfyNotifier(topic_url=dotted)
    assert NtfyNotifier(topic_url=dotted, access_token=ACCESS_TOKEN).provider == "ntfy"


@respx.mock
@pytest.mark.asyncio
async def test_ntfy_redacts_an_opted_in_public_topic_path_from_errors() -> None:
    topic = "https://ntfy.sh/unguessable-topic-name"
    respx.post(topic).mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="<html><title>Just a moment...</title></html>",
        )
    )

    result = await NtfyNotifier(topic, allow_public_topic=True).send(message())

    assert result.delivered is False
    assert result.uncertain is True
    assert result.error is not None
    assert "unguessable-topic-name" not in result.error
    assert "ntfy.sh" not in result.error


def test_ntfy_allows_a_public_topic_only_with_the_explicit_opt_in() -> None:
    notifier = NtfyNotifier(topic_url="https://ntfy.sh/job-alerts", allow_public_topic=True)

    assert notifier.provider == "ntfy"


def test_ntfy_allows_a_self_hosted_topic_without_a_token() -> None:
    notifier = NtfyNotifier(topic_url="https://push.example.com/job-alerts")

    assert notifier.provider == "ntfy"


@pytest.mark.parametrize(
    "topic_url",
    [
        "http://ntfy.sh/job-alerts",
        "https://ntfy.sh",
        "https://ntfy.sh/",
        "not-a-url",
        "",
    ],
)
def test_ntfy_refuses_a_topic_url_it_cannot_publish_to(topic_url: str) -> None:
    with pytest.raises(NotificationConfigurationError) as failure:
        NtfyNotifier(topic_url=topic_url, access_token=ACCESS_TOKEN)

    assert ACCESS_TOKEN not in str(failure.value)


def test_ntfy_reads_topic_and_token_from_the_environment() -> None:
    notifier = NtfyNotifier(environ={"NTFY_TOPIC_URL": TOPIC, "NTFY_ACCESS_TOKEN": ACCESS_TOKEN})

    assert notifier.provider == "ntfy"


def test_ntfy_refuses_to_start_without_a_topic() -> None:
    with pytest.raises(NotificationConfigurationError):
        NtfyNotifier(environ={})
