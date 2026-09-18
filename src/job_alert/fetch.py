from __future__ import annotations

import asyncio
import random
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx

DEFAULT_CONNECT_TIMEOUT_SECONDS = 20.0
DEFAULT_READ_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_MAX_BYTES = 15 * 1024 * 1024
MAX_ATTEMPTS = 3
CHALLENGE_SCAN_BYTES = 64 * 1024
CHALLENGE_MARKERS = (
    b"captcha",
    b"cf-chl-",
    b"just a moment",
    b"access denied",
    b"verify you are human",
)


class FetchFailure(RuntimeError):
    """A request could not produce a usable provider payload."""


class ChallengeDetected(FetchFailure):
    """A response looks like an access-control or bot-challenge page."""


class PayloadTooLarge(FetchFailure):
    """A response exceeded the configured streaming byte limit."""


@dataclass(frozen=True, slots=True)
class RequestSpec:
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    expected: Literal["json", "html", "bytes"] = "bytes"
    timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS
    etag: str | None = None
    last_modified: str | None = None
    method: Literal["GET", "POST"] = "GET"
    json_body: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class HttpPayload:
    final_url: str
    status: int
    headers: Mapping[str, str]
    body: bytes
    fetched_at: datetime
    etag: str | None
    last_modified: str | None
    not_modified: bool = False


async def _retry_wait(delay_seconds: float) -> None:
    await asyncio.sleep(delay_seconds)


class HttpFetcher:
    def __init__(
        self,
        max_bytes: int = DEFAULT_MAX_BYTES,
        *,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be at least one byte")
        self._max_bytes = max_bytes
        self._connect_timeout_seconds = connect_timeout_seconds
        self._max_redirects = max_redirects
        self._client = httpx.AsyncClient(follow_redirects=True, max_redirects=max_redirects)

    async def __aenter__(self) -> HttpFetcher:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def get(self, spec: RequestSpec) -> HttpPayload:
        return await self.request(replace(spec, method="GET"))

    async def post(self, spec: RequestSpec) -> HttpPayload:
        return await self.request(replace(spec, method="POST"))

    async def request(self, spec: RequestSpec) -> HttpPayload:
        for attempt in range(MAX_ATTEMPTS):
            response: httpx.Response | None = None
            try:
                response = await self._client.send(self._build_request(spec), stream=True)
                if response.status_code == 304:
                    return await self._not_modified_payload(response)

                if _is_retryable_status(response.status_code):
                    retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                    await response.aclose()
                    if attempt + 1 < MAX_ATTEMPTS:
                        await _retry_wait(_retry_delay(attempt, retry_after))
                        continue
                    raise FetchFailure(
                        f"request failed after {MAX_ATTEMPTS} attempts: "
                        f"HTTP {response.status_code} for {spec.url}"
                    )

                if response.status_code >= 400:
                    await response.aclose()
                    raise FetchFailure(f"non-retryable HTTP {response.status_code} for {spec.url}")

                return await self._payload_from_response(response, spec)
            except (httpx.NetworkError, httpx.TimeoutException) as exc:
                if response is not None:
                    await response.aclose()
                if attempt + 1 < MAX_ATTEMPTS:
                    await _retry_wait(_retry_delay(attempt, None))
                    continue
                raise FetchFailure(
                    f"request failed after {MAX_ATTEMPTS} attempts for {spec.url}"
                ) from exc

        raise AssertionError("unreachable")

    def _build_request(self, spec: RequestSpec) -> httpx.Request:
        headers = dict(spec.headers)
        if spec.method == "GET":
            if spec.etag is not None:
                headers.setdefault("If-None-Match", spec.etag)
            if spec.last_modified is not None:
                headers.setdefault("If-Modified-Since", spec.last_modified)
        if spec.method == "POST" and spec.json_body is not None:
            headers.setdefault("Content-Type", "application/json")

        timeout = httpx.Timeout(
            timeout=spec.timeout_seconds,
            connect=self._connect_timeout_seconds,
            read=spec.timeout_seconds,
        )
        return self._client.build_request(
            spec.method,
            spec.url,
            headers=headers,
            json=spec.json_body if spec.method == "POST" else None,
            timeout=timeout,
        )

    async def _not_modified_payload(self, response: httpx.Response) -> HttpPayload:
        try:
            return HttpPayload(
                final_url=str(response.url),
                status=response.status_code,
                headers=dict(response.headers),
                body=b"",
                fetched_at=datetime.now(UTC),
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
                not_modified=True,
            )
        finally:
            await response.aclose()

    async def _payload_from_response(
        self, response: httpx.Response, spec: RequestSpec
    ) -> HttpPayload:
        try:
            body = await _read_bounded(response, self._max_bytes)
            if _is_challenge(spec.expected, response.headers.get("Content-Type"), body):
                raise ChallengeDetected(f"challenge page detected for {spec.url}")
            return HttpPayload(
                final_url=str(response.url),
                status=response.status_code,
                headers=dict(response.headers),
                body=body,
                fetched_at=datetime.now(UTC),
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
            )
        finally:
            await response.aclose()


async def _read_bounded(response: httpx.Response, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise PayloadTooLarge(f"response exceeds {max_bytes} byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _is_retryable_status(status: int) -> bool:
    return status in {408, 425, 429} or 500 <= status <= 599


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


def _retry_delay(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return retry_after
    return float(2**attempt) + random.uniform(0.0, 0.25)


def _is_challenge(expected: str, content_type: str | None, body: bytes) -> bool:
    if expected == "bytes":
        return False
    media_type = (content_type or "").split(";", maxsplit=1)[0].strip().lower()
    if expected != "json" and "html" not in media_type:
        return False
    sample = body[:CHALLENGE_SCAN_BYTES].lower()
    return any(marker in sample for marker in CHALLENGE_MARKERS)
