"""Pushover delivery: form payloads, response classification, and credential redaction."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from urllib.parse import parse_qsl, quote

import httpx
import pytest
import respx

from job_alert.notifications.base import (
    BODY_LIMIT,
    NotificationConfigurationError,
    NotificationMessage,
)
from job_alert.notifications.pushover import (
    PUSHOVER_ENDPOINT,
    PUSHOVER_PRIORITIES,
    PushoverNotifier,
)

TOKEN = "secret-token"
USER_KEY = "secret-user"


def message(**overrides: object) -> NotificationMessage:
    values: dict[str, object] = {
        "title": "New job — NVIDIA",
        "body": "Robotics Software Engineer\nSanta Clara, CA · hybrid",
        "url": "https://example.com/jobs/123",
        "url_title": "Open and apply",
        "priority": "normal",
    }
    values.update(overrides)
    return NotificationMessage(**values)


def build_notifier(**overrides: object) -> PushoverNotifier:
    values: dict[str, object] = {"app_token": TOKEN, "user_key": USER_KEY}
    values.update(overrides)
    return PushoverNotifier(**values)  # type: ignore[arg-type]


def sent_form(route: respx.Route) -> dict[str, str]:
    return dict(parse_qsl(route.calls[0].request.content.decode()))


@respx.mock
@pytest.mark.asyncio
async def test_pushover_sends_clickable_job_alert_without_secret_in_logs() -> None:
    route = respx.post("https://api.pushover.net/1/messages.json").mock(
        return_value=httpx.Response(200, json={"status": 1, "request": "req-123"})
    )
    notifier = PushoverNotifier(app_token="secret-token", user_key="secret-user")
    result = await notifier.send(
        NotificationMessage(
            title="New Robotics Job — NVIDIA",
            body="Robotics Software Engineer · Santa Clara, CA",
            url="https://example.com/jobs/123",
            url_title="Open and apply",
            priority="normal",
        )
    )
    assert result.delivered is True
    assert result.provider_request_id == "req-123"
    request_body = route.calls[0].request.content.decode()
    assert "Open+and+apply" in request_body


@respx.mock
@pytest.mark.asyncio
async def test_pushover_posts_form_data_with_the_clickable_url() -> None:
    route = respx.post(PUSHOVER_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"status": 1, "request": "req-1"})
    )

    result = await build_notifier().send(message())

    assert result.delivered is True
    request = route.calls[0].request
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    form = sent_form(route)
    assert form["token"] == TOKEN
    assert form["user"] == USER_KEY
    assert form["url"] == "https://example.com/jobs/123"
    assert form["url_title"] == "Open and apply"


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(("priority", "expected"), [("low", "-1"), ("normal", "0"), ("high", "1")])
async def test_pushover_maps_only_the_approved_priorities(priority: str, expected: str) -> None:
    route = respx.post(PUSHOVER_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"status": 1, "request": "req-1"})
    )

    await build_notifier().send(message(priority=priority))

    assert sent_form(route)["priority"] == expected
    # Emergency priority 2 demands acknowledgement handling the design does not approve.
    assert 2 not in PUSHOVER_PRIORITIES.values()


@respx.mock
@pytest.mark.asyncio
async def test_pushover_captures_quota_headers_on_success() -> None:
    reset = 1_800_000_000
    respx.post(PUSHOVER_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={"status": 1, "request": "req-9"},
            headers={
                "X-Limit-App-Limit": "10000",
                "X-Limit-App-Remaining": "9987",
                "X-Limit-App-Reset": str(reset),
            },
        )
    )

    result = await build_notifier().send(message())

    assert result.quota_limit == 10000
    assert result.quota_remaining == 9987
    assert result.quota_reset_at == datetime.fromtimestamp(reset, tz=UTC)


@respx.mock
@pytest.mark.asyncio
async def test_pushover_treats_unreadable_success_body_as_undelivered() -> None:
    respx.post(PUSHOVER_ENDPOINT).mock(
        return_value=httpx.Response(200, content=b"<html>not json</html>")
    )

    result = await build_notifier().send(message())

    assert result.delivered is False
    # The answer is unreadable, so the delivery is unconfirmed: the durable layer decides whether
    # one more attempt is worth the duplicate risk.
    assert result.retryable is True
    assert result.provider_request_id is None
    assert result.error is not None


@respx.mock
@pytest.mark.asyncio
async def test_pushover_rejects_a_success_status_that_is_not_one() -> None:
    respx.post(PUSHOVER_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"status": 0, "request": "req-1"})
    )

    result = await build_notifier().send(message())

    assert result.delivered is False
    assert result.provider_request_id is None


@respx.mock
@pytest.mark.asyncio
async def test_pushover_client_error_is_permanent() -> None:
    route = respx.post(PUSHOVER_ENDPOINT).mock(
        return_value=httpx.Response(
            400, json={"status": 0, "errors": ["application token is invalid"]}
        )
    )

    result = await build_notifier().send(message())

    assert result.delivered is False
    assert result.retryable is False
    assert result.http_status == 400
    assert result.error is not None and "application token is invalid" in result.error
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_pushover_rate_limit_honours_retry_after_without_replaying() -> None:
    route = respx.post(PUSHOVER_ENDPOINT).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "7"})
    )

    result = await build_notifier().send(message())

    assert result.delivered is False
    assert result.retryable is True
    assert result.retry_after_seconds == 7.0
    # A notification POST creates a message, so the notifier must never replay it itself.
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_pushover_rate_limit_falls_back_to_the_quota_reset() -> None:
    reset = int(datetime.now(UTC).timestamp()) + 90
    respx.post(PUSHOVER_ENDPOINT).mock(
        return_value=httpx.Response(429, headers={"X-Limit-App-Reset": str(reset)})
    )

    result = await build_notifier().send(message())

    assert result.retryable is True
    assert result.retry_after_seconds is not None
    assert 0 < result.retry_after_seconds <= 91
    assert result.quota_reset_at == datetime.fromtimestamp(reset, tz=UTC)


@respx.mock
@pytest.mark.asyncio
async def test_pushover_accepts_an_http_date_retry_after() -> None:
    retry_at = datetime.now(UTC) + timedelta(seconds=45)
    respx.post(PUSHOVER_ENDPOINT).mock(
        return_value=httpx.Response(429, headers={"Retry-After": format_datetime(retry_at)})
    )

    result = await build_notifier().send(message())

    assert result.retryable is True
    assert result.retry_after_seconds is not None
    assert 0 < result.retry_after_seconds <= 45


@respx.mock
@pytest.mark.asyncio
async def test_pushover_treats_a_challenge_page_as_a_retryable_non_delivery() -> None:
    respx.post(PUSHOVER_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="<html><title>Just a moment...</title></html>",
        )
    )

    result = await build_notifier().send(message())

    assert result.delivered is False
    assert result.retryable is True
    assert result.error is not None and "ChallengeDetected" in result.error


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "expected"),
    [({}, 5.0), ({"Retry-After": "2"}, 5.0), ({"Retry-After": "45"}, 45.0)],
)
async def test_pushover_server_errors_wait_at_least_five_seconds(
    headers: dict[str, str], expected: float
) -> None:
    respx.post(PUSHOVER_ENDPOINT).mock(return_value=httpx.Response(503, headers=headers))

    result = await build_notifier().send(message())

    assert result.delivered is False
    assert result.retryable is True
    assert result.retry_after_seconds == expected


@respx.mock
@pytest.mark.asyncio
async def test_pushover_redacts_credentials_from_failure_text() -> None:
    respx.post(PUSHOVER_ENDPOINT).mock(
        return_value=httpx.Response(
            400,
            json={
                "status": 0,
                "errors": [
                    f"token {TOKEN} is invalid for user {USER_KEY}",
                    f"encoded {quote(TOKEN, safe='')}",
                ],
            },
        )
    )

    result = await build_notifier().send(message())

    assert result.error is not None
    assert TOKEN not in result.error
    assert USER_KEY not in result.error
    assert quote(TOKEN, safe="") not in result.error


@respx.mock
@pytest.mark.asyncio
async def test_pushover_transport_failure_is_retryable_and_redacted() -> None:
    respx.post(PUSHOVER_ENDPOINT).mock(
        side_effect=httpx.ConnectError(f"connection refused for {TOKEN}")
    )

    result = await build_notifier().send(message())

    assert result.delivered is False
    assert result.retryable is True
    assert result.error is not None and TOKEN not in result.error


@respx.mock
@pytest.mark.asyncio
async def test_pushover_truncates_a_long_multibyte_body_and_keeps_the_url() -> None:
    route = respx.post(PUSHOVER_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"status": 1, "request": "req-1"})
    )

    await build_notifier().send(message(body="職" * (BODY_LIMIT + 400)))

    form = sent_form(route)
    assert len(form["message"]) == BODY_LIMIT
    assert form["url"] == "https://example.com/jobs/123"


@respx.mock
@pytest.mark.asyncio
async def test_pushover_never_logs_credentials(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    route = respx.post(PUSHOVER_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"status": 1, "request": "req-1"})
    )

    await build_notifier().send(message())

    assert route.call_count == 1
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert TOKEN not in logged
    assert USER_KEY not in logged


def test_pushover_reads_credentials_from_the_environment() -> None:
    notifier = PushoverNotifier(
        environ={"PUSHOVER_APP_TOKEN": TOKEN, "PUSHOVER_USER_KEY": USER_KEY}
    )

    assert notifier.provider == "pushover"


def test_pushover_refuses_to_start_without_credentials() -> None:
    with pytest.raises(NotificationConfigurationError):
        PushoverNotifier(environ={})
    with pytest.raises(NotificationConfigurationError):
        PushoverNotifier(app_token=TOKEN, user_key="   ", environ={})
