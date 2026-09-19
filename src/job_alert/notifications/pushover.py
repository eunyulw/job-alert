"""Pushover delivery: the primary Android provider.

One call sends one message as form data and classifies the answer. The classification is the whole
point of the module: a ``200`` counts as a delivery only when the body really says ``status: 1``, a
``429`` is retryable at the reset or ``Retry-After`` the server named, a server failure is
retryable but never sooner than five seconds, and every other ``4xx`` is permanent so a bad token
cannot spin forever. Quota headers ride along on the result.

The notifier never replays its own POST, because that call creates the notification; it reports
``retryable`` and the earliest allowed delay, and the durable layer retries unsent events.
Credentials come from the constructor or the environment and never appear in a message, an
exception, or a log line.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from job_alert.config import FilterPriority
from job_alert.fetch import FetchFailure, HttpFetcher, HttpPayload, RequestSpec
from job_alert.notifications.base import (
    SERVER_ERROR_MINIMUM_RETRY_SECONDS,
    DeliveryResult,
    NotificationMessage,
    header_value,
    redact_secrets,
    require_credential,
    retry_after_delay,
)

PUSHOVER_ENDPOINT = "https://api.pushover.net/1/messages.json"
PUSHOVER_APP_TOKEN_ENV = "PUSHOVER_APP_TOKEN"
PUSHOVER_USER_KEY_ENV = "PUSHOVER_USER_KEY"

#: Pushover priorities. Emergency ``2`` is deliberately absent: it demands acknowledgement
#: behavior the design does not approve.
PUSHOVER_PRIORITIES: Mapping[FilterPriority, int] = {
    FilterPriority.LOW: -1,
    FilterPriority.NORMAL: 0,
    FilterPriority.HIGH: 1,
}

QUOTA_LIMIT_HEADER = "X-Limit-App-Limit"
QUOTA_REMAINING_HEADER = "X-Limit-App-Remaining"
QUOTA_RESET_HEADER = "X-Limit-App-Reset"

_ERROR_DETAIL_LIMIT = 300


class PushoverNotifier:
    """Deliver one alert per call through Pushover's messages endpoint.

    ``fetcher`` is optional: passing the service's shared :class:`HttpFetcher` reuses one HTTP
    client, and leaving it out gives each send a short-lived one.
    """

    provider = "pushover"

    def __init__(
        self,
        app_token: str | None = None,
        user_key: str | None = None,
        *,
        fetcher: HttpFetcher | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        environment = os.environ if environ is None else environ
        self._app_token = require_credential(
            app_token if app_token is not None else environment.get(PUSHOVER_APP_TOKEN_ENV),
            PUSHOVER_APP_TOKEN_ENV,
        )
        self._user_key = require_credential(
            user_key if user_key is not None else environment.get(PUSHOVER_USER_KEY_ENV),
            PUSHOVER_USER_KEY_ENV,
        )
        self._fetcher = fetcher

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        """Send one message once and classify what Pushover answered."""
        spec = RequestSpec(
            url=PUSHOVER_ENDPOINT,
            method="POST",
            form_body=self._form(message),
            idempotent=False,
        )
        try:
            payload = await self._send_once(spec)
        except (FetchFailure, httpx.HTTPError, TimeoutError) as exc:
            return self._transport_failure(exc)
        return self._classify(payload)

    def _form(self, message: NotificationMessage) -> dict[str, str]:
        """The message as Pushover form fields, including the clickable link."""
        return {
            "token": self._app_token,
            "user": self._user_key,
            "title": message.title,
            "message": message.body,
            "priority": str(PUSHOVER_PRIORITIES[message.priority]),
            "url": message.url,
            "url_title": message.url_title,
        }

    async def _send_once(self, spec: RequestSpec) -> HttpPayload:
        if self._fetcher is not None:
            return await self._fetcher.send_once(spec)
        async with HttpFetcher() as fetcher:
            return await fetcher.send_once(spec)

    def _classify(self, payload: HttpPayload) -> DeliveryResult:
        if payload.status == 200:
            request_id = _provider_request_id(payload.body)
            if request_id is not None:
                return self._result(payload, delivered=True, retryable=False, request_id=request_id)
            # A success status with an unreadable body proves nothing, so it is not a delivery.
            return self._result(
                payload,
                delivered=False,
                retryable=True,
                error="Pushover returned an unreadable success response",
            )
        if payload.status == 429:
            delay = retry_after_delay(payload.headers)
            if delay is None:
                delay = _quota_reset_delay(payload.headers)
            return self._result(
                payload,
                delivered=False,
                retryable=True,
                error=self._error("Pushover is rate limited", payload),
                retry_after_seconds=delay,
            )
        if 500 <= payload.status <= 599:
            delay = max(
                SERVER_ERROR_MINIMUM_RETRY_SECONDS, retry_after_delay(payload.headers) or 0.0
            )
            return self._result(
                payload,
                delivered=False,
                retryable=True,
                error=self._error("Pushover server error", payload),
                retry_after_seconds=delay,
            )
        if 400 <= payload.status <= 499:
            return self._result(
                payload,
                delivered=False,
                retryable=False,
                error=self._error("Pushover rejected the alert", payload),
            )
        return self._result(
            payload,
            delivered=False,
            retryable=False,
            error=f"unexpected HTTP {payload.status} from Pushover",
        )

    def _result(
        self,
        payload: HttpPayload,
        *,
        delivered: bool,
        retryable: bool,
        request_id: str | None = None,
        retry_after_seconds: float | None = None,
        error: str | None = None,
    ) -> DeliveryResult:
        return DeliveryResult(
            provider=self.provider,
            delivered=delivered,
            retryable=retryable,
            http_status=payload.status,
            provider_request_id=request_id,
            retry_after_seconds=retry_after_seconds,
            error=error,
            quota_limit=_int_header(payload.headers, QUOTA_LIMIT_HEADER),
            quota_remaining=_int_header(payload.headers, QUOTA_REMAINING_HEADER),
            quota_reset_at=_quota_reset(payload.headers),
        )

    def _transport_failure(self, exc: BaseException) -> DeliveryResult:
        """A request that never produced an answer is worth retrying, and never leaks a secret."""
        return DeliveryResult(
            provider=self.provider,
            delivered=False,
            retryable=True,
            error=redact_secrets(f"{type(exc).__name__}: {exc}", self._secrets()),
        )

    def _error(self, prefix: str, payload: HttpPayload) -> str:
        detail = _provider_errors(payload.body)
        text = f"{prefix}: HTTP {payload.status}" + (f": {detail}" if detail else "")
        return redact_secrets(text, self._secrets())

    def _secrets(self) -> tuple[str, str]:
        return (self._app_token, self._user_key)


def _provider_request_id(body: bytes) -> str | None:
    """Pushover's request id, and only when the document really reports ``status: 1``."""
    document = _json_object(body)
    if document is None or document.get("status") != 1:
        return None
    request_id = document.get("request")
    return request_id if isinstance(request_id, str) and request_id.strip() else None


def _provider_errors(body: bytes) -> str | None:
    """Pushover's human-readable ``errors`` list, bounded and never trusted beyond plain text."""
    document = _json_object(body)
    if document is None:
        return None
    errors = document.get("errors")
    if not isinstance(errors, list):
        return None
    joined = "; ".join(str(error) for error in errors)[:_ERROR_DETAIL_LIMIT]
    return joined or None


def _json_object(body: bytes) -> dict[str, Any] | None:
    try:
        document = json.loads(body)
    except ValueError:
        return None
    return document if isinstance(document, dict) else None


def _int_header(headers: Mapping[str, str], name: str) -> int | None:
    value = header_value(headers, name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _quota_reset(headers: Mapping[str, str]) -> datetime | None:
    """``X-Limit-App-Reset`` epoch seconds as the instant Pushover's monthly allowance resets."""
    seconds = _int_header(headers, QUOTA_RESET_HEADER)
    return None if seconds is None else datetime.fromtimestamp(seconds, tz=UTC)


def _quota_reset_delay(headers: Mapping[str, str]) -> float | None:
    """The same reset as a delay, for a ``429`` that named no ``Retry-After``."""
    reset = _quota_reset(headers)
    if reset is None:
        return None
    return max(0.0, (reset - datetime.now(UTC)).total_seconds())
