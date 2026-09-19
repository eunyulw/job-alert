from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
import respx

from job_alert.fetch import (
    ChallengeDetected,
    FetchFailure,
    HttpFetcher,
    PayloadTooLarge,
    RequestSpec,
)


@respx.mock
@pytest.mark.asyncio
async def test_fetcher_rejects_html_challenge_returned_as_200() -> None:
    respx.get("https://example.com/jobs").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="<html><title>Just a moment...</title></html>",
        )
    )
    async with HttpFetcher() as fetcher:
        with pytest.raises(ChallengeDetected):
            await fetcher.get(RequestSpec(url="https://example.com/jobs", expected="json"))


@respx.mock
@pytest.mark.asyncio
async def test_fetcher_rejects_oversized_payload() -> None:
    respx.get("https://example.com/jobs").mock(
        return_value=httpx.Response(200, content=b"x" * 101)
    )
    async with HttpFetcher(max_bytes=100) as fetcher:
        with pytest.raises(PayloadTooLarge):
            await fetcher.get(RequestSpec(url="https://example.com/jobs", expected="bytes"))


@respx.mock
@pytest.mark.asyncio
async def test_fetcher_sends_validators_and_returns_explicit_not_modified_payload() -> None:
    route = respx.get("https://example.com/jobs").mock(
        return_value=httpx.Response(
            304,
            headers={"ETag": '"current"', "Last-Modified": "Fri, 18 Sep 2026 12:00:00 GMT"},
            content=b"body that must not be parsed",
        )
    )
    async with HttpFetcher() as fetcher:
        payload = await fetcher.get(
            RequestSpec(
                url="https://example.com/jobs",
                expected="json",
                etag='"saved"',
                last_modified="Thu, 17 Sep 2026 12:00:00 GMT",
            )
        )

    assert route.call_count == 1
    assert route.calls[0].request.headers["If-None-Match"] == '"saved"'
    assert route.calls[0].request.headers["If-Modified-Since"] == "Thu, 17 Sep 2026 12:00:00 GMT"
    assert payload.not_modified is True
    assert payload.body == b""
    assert payload.etag == '"current"'


@respx.mock
@pytest.mark.asyncio
async def test_fetcher_retries_429_with_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    route = respx.get("https://example.com/jobs").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={"jobs": []}),
        ]
    )
    waits: list[float] = []

    async def record_wait(delay_seconds: float) -> None:
        waits.append(delay_seconds)

    monkeypatch.setattr("job_alert.fetch._retry_wait", record_wait)
    async with HttpFetcher() as fetcher:
        payload = await fetcher.get(RequestSpec(url="https://example.com/jobs", expected="json"))

    assert route.call_count == 2
    assert waits == [7.0]
    assert payload.status == 200
    assert payload.body == b'{"jobs":[]}'


@respx.mock
@pytest.mark.asyncio
async def test_fetcher_retries_three_times_with_exponential_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = respx.get("https://example.com/jobs").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(502),
            httpx.Response(503),
            httpx.Response(200, json={"jobs": []}),
        ]
    )
    waits: list[float] = []

    async def record_wait(delay_seconds: float) -> None:
        waits.append(delay_seconds)

    monkeypatch.setattr("job_alert.fetch._retry_wait", record_wait)
    async with HttpFetcher() as fetcher:
        payload = await fetcher.get(RequestSpec(url="https://example.com/jobs", expected="json"))

    assert route.call_count == 4
    assert waits[0] == pytest.approx(1.0, abs=0.25)
    assert waits[1] == pytest.approx(2.0, abs=0.25)
    assert waits[2] == pytest.approx(4.0, abs=0.25)
    assert payload.status == 200


@respx.mock
@pytest.mark.asyncio
async def test_fetcher_honors_http_date_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen_now = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    retry_at = frozen_now + timedelta(seconds=45)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: object | None = None) -> datetime:
            assert tz == UTC
            return frozen_now

    monkeypatch.setattr("job_alert.fetch.datetime", FrozenDatetime)
    route = respx.get("https://example.com/jobs").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": format_datetime(retry_at, usegmt=True)}),
            httpx.Response(200, json={"jobs": []}),
        ]
    )
    waits: list[float] = []

    async def record_wait(delay_seconds: float) -> None:
        waits.append(delay_seconds)

    monkeypatch.setattr("job_alert.fetch._retry_wait", record_wait)
    async with HttpFetcher() as fetcher:
        await fetcher.get(RequestSpec(url="https://example.com/jobs", expected="json"))

    assert route.call_count == 2
    assert waits == [45.0]


@respx.mock
@pytest.mark.asyncio
async def test_fetcher_does_not_retry_permanent_403(monkeypatch: pytest.MonkeyPatch) -> None:
    route = respx.get("https://example.com/jobs").mock(return_value=httpx.Response(403))

    async def fail_if_waited(delay_seconds: float) -> None:
        raise AssertionError(f"unexpected retry delay: {delay_seconds}")

    monkeypatch.setattr("job_alert.fetch._retry_wait", fail_if_waited)
    async with HttpFetcher() as fetcher:
        with pytest.raises(FetchFailure, match="non-retryable HTTP 403"):
            await fetcher.get(RequestSpec(url="https://example.com/jobs", expected="json"))

    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_fetcher_does_not_scan_json_markers_as_challenges() -> None:
    respx.get("https://example.com/jobs").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={"description": "Captcha access denied discussion"},
        )
    )
    async with HttpFetcher() as fetcher:
        payload = await fetcher.get(RequestSpec(url="https://example.com/jobs", expected="json"))

    assert payload.status == 200


@respx.mock
@pytest.mark.asyncio
async def test_fetcher_detects_html_challenge_for_bytes_request() -> None:
    respx.get("https://example.com/jobs").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "application/xhtml+xml"},
            text="<html>Verify you are human</html>",
        )
    )
    async with HttpFetcher() as fetcher:
        with pytest.raises(ChallengeDetected):
            await fetcher.get(RequestSpec(url="https://example.com/jobs", expected="bytes"))


class _ExpiringDeadline:
    def __init__(self) -> None:
        self.expired = False

    async def __aenter__(self) -> _ExpiringDeadline:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        if self.expired:
            raise TimeoutError


class _DeadlineExpiringStream(httpx.AsyncByteStream):
    def __init__(self, deadline: _ExpiringDeadline) -> None:
        self._deadline = deadline

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self._deadline.expired = True
        yield b"{}"

    async def aclose(self) -> None:
        return None


@respx.mock
@pytest.mark.asyncio
async def test_fetcher_total_deadline_includes_body_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    deadline = _ExpiringDeadline()
    respx.get("https://example.com/jobs").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=_DeadlineExpiringStream(deadline),
        )
    )
    monkeypatch.setattr("job_alert.fetch.asyncio.timeout", lambda seconds: deadline)

    async with HttpFetcher() as fetcher:
        with pytest.raises(FetchFailure, match="request timed out"):
            await fetcher.get(
                RequestSpec(
                    url="https://example.com/jobs",
                    expected="json",
                    idempotent=False,
                )
            )


@respx.mock
@pytest.mark.asyncio
async def test_fetcher_does_not_retry_ordinary_post_after_ambiguous_failure() -> None:
    route = respx.post("https://example.com/search").mock(
        side_effect=httpx.ConnectError("connection reset")
    )
    async with HttpFetcher() as fetcher:
        with pytest.raises(FetchFailure, match="non-idempotent request failed"):
            await fetcher.post(
                RequestSpec(url="https://example.com/search", expected="json", json_body={})
            )

    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_post_sends_json_without_conditional_headers() -> None:
    route = respx.post("https://example.com/search").mock(return_value=httpx.Response(200, json={}))
    async with HttpFetcher() as fetcher:
        await fetcher.post(
            RequestSpec(
                url="https://example.com/search",
                expected="json",
                etag='"saved"',
                last_modified="Thu, 17 Sep 2026 12:00:00 GMT",
                headers={
                    "Content-Type": "text/plain",
                    "If-None-Match": '"conflicting"',
                    "If-Modified-Since": "Fri, 18 Sep 2026 12:00:00 GMT",
                },
                json_body={"limit": 20},
            )
        )

    request = route.calls[0].request
    assert route.call_count == 1
    assert request.headers["Content-Type"] == "application/json"
    assert "If-None-Match" not in request.headers
    assert "If-Modified-Since" not in request.headers
    assert request.content == b'{"limit":20}'


@respx.mock
@pytest.mark.asyncio
async def test_post_sends_a_form_body_when_asked_for_one() -> None:
    route = respx.post("https://api.example.com/messages").mock(
        return_value=httpx.Response(200, json={"status": 1})
    )
    async with HttpFetcher() as fetcher:
        await fetcher.post(
            RequestSpec(
                url="https://api.example.com/messages",
                expected="json",
                form_body={"title": "New job", "url_title": "Open and apply"},
            )
        )

    request = route.calls[0].request
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert request.content == b"title=New+job&url_title=Open+and+apply"


@respx.mock
@pytest.mark.asyncio
async def test_send_once_returns_a_retryable_status_without_replaying_it() -> None:
    route = respx.post("https://api.example.com/messages").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "7", "X-Limit-App-Reset": "99"})
    )
    async with HttpFetcher() as fetcher:
        payload = await fetcher.send_once(
            RequestSpec(
                url="https://api.example.com/messages",
                method="POST",
                expected="json",
                form_body={"token": "secret"},
                idempotent=False,
            )
        )

    assert route.call_count == 1
    assert payload.status == 429
    # httpx lower-cases header names, so callers match them case-insensitively.
    assert payload.headers["retry-after"] == "7"
    assert payload.headers["x-limit-app-reset"] == "99"
    assert payload.body == b""
