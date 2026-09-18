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
        return_value=httpx.Response(200, text="<html><title>Just a moment...</title></html>")
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
async def test_post_sends_json_without_conditional_headers() -> None:
    route = respx.post("https://example.com/search").mock(return_value=httpx.Response(200, json={}))
    async with HttpFetcher() as fetcher:
        await fetcher.post(
            RequestSpec(
                url="https://example.com/search",
                expected="json",
                etag='"saved"',
                last_modified="Thu, 17 Sep 2026 12:00:00 GMT",
                json_body={"limit": 20},
            )
        )

    request = route.calls[0].request
    assert route.call_count == 1
    assert request.headers["Content-Type"] == "application/json"
    assert "If-None-Match" not in request.headers
    assert "If-Modified-Since" not in request.headers
    assert request.content == b'{"limit":20}'
