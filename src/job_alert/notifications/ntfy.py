"""Optional ntfy delivery, for a self-hosted topic or an authenticated public one.

ntfy publishes to a topic, so the topic itself travels in the request. That makes an
unauthenticated public ``ntfy.sh`` topic a shared secret that leaks through any proxy log, which
is why constructing a notifier for one refuses to work without an explicit opt-in. The notifier
sends one JSON body with the topic, a title, priority, tags, and a click URL to the server's root
URL (ntfy reads a JSON body as message fields only there; posted to the topic URL itself, the whole
document arrives as the message text), attaches a bearer token when it has one, and classifies
the answer the same way the Pushover notifier does: ``429`` and server failures are retryable,
other ``4xx`` responses are permanent, and an unreadable success body is not a delivery.

ntfy has no idempotency key on this endpoint either, so delivery is at-least-once. A transport
failure that provably never reached the server (a connect, pool, or protocol-setup failure) stays
certain and retryable, because a retry cannot duplicate anything. A failure that might have arrived
first (a read or write timeout or error, a remote protocol error, a challenge page, an oversized
body) is reported as ``uncertain`` and stays retryable: a rare duplicate alert is the accepted trade
for never silently dropping a posting, and the durable layer caps how many attempts such a result
may consume.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from job_alert.config import FilterPriority
from job_alert.fetch import FetchFailure, HttpFetcher, HttpPayload, RequestSpec
from job_alert.notifications.base import (
    SERVER_ERROR_MINIMUM_RETRY_SECONDS,
    DeliveryResult,
    NotificationConfigurationError,
    NotificationMessage,
    optional_credential,
    redact_secrets,
    retry_after_delay,
    transport_is_uncertain,
)

NTFY_TOPIC_URL_ENV = "NTFY_TOPIC_URL"
NTFY_ACCESS_TOKEN_ENV = "NTFY_ACCESS_TOKEN"

#: Hosts whose topics are world-readable unless the publisher authenticates.
PUBLIC_NTFY_HOSTS = ("ntfy.sh",)

#: ntfy priorities; ``5`` (max) is reserved for the same reason Pushover's emergency level is.
NTFY_PRIORITIES: Mapping[FilterPriority, int] = {
    FilterPriority.LOW: 2,
    FilterPriority.NORMAL: 3,
    FilterPriority.HIGH: 4,
}

_ERROR_DETAIL_LIMIT = 300


class NtfyNotifier:
    """Deliver one alert per call to one configured ntfy topic.

    The topic and token come from the constructor or the environment, never from committed
    configuration. ``allow_public_topic`` is a deliberate opt-in, not an environment default: it
    is the one place where publishing a job alert on a public topic can be authorized.
    """

    provider = "ntfy"

    def __init__(
        self,
        topic_url: str | None = None,
        access_token: str | None = None,
        *,
        allow_public_topic: bool = False,
        fetcher: HttpFetcher | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        environment = os.environ if environ is None else environ
        self._topic_url = validate_topic_url(
            topic_url if topic_url is not None else environment.get(NTFY_TOPIC_URL_ENV)
        )
        self._access_token = optional_credential(
            access_token if access_token is not None else environment.get(NTFY_ACCESS_TOKEN_ENV)
        )
        public_and_open = self._access_token is None and is_public_topic(self._topic_url)
        if public_and_open and not allow_public_topic:
            raise NotificationConfigurationError(
                "refusing to publish to an unauthenticated public ntfy.sh topic; send an access"
                " token or set allow_public_topic=True deliberately"
            )
        self._public_topic = is_public_topic(self._topic_url)
        self._root_url, self._topic = split_topic_url(self._topic_url)
        self._fetcher = fetcher

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        """Send one message once and classify what the ntfy server answered."""
        spec = RequestSpec(
            url=self._root_url,
            method="POST",
            headers=self._headers(),
            json_body=self._body(message),
            idempotent=False,
        )
        try:
            payload = await self._send_once(spec)
        except (FetchFailure, httpx.HTTPError, TimeoutError, httpx.InvalidURL) as exc:
            return self._transport_failure(exc)
        return self._classify(payload)

    def _body(self, message: NotificationMessage) -> dict[str, Any]:
        body: dict[str, Any] = {
            "topic": self._topic,
            "title": message.title,
            "message": message.body,
            "priority": NTFY_PRIORITIES[message.priority],
            "click": message.url,
        }
        if message.tags:
            body["tags"] = list(message.tags)
        return body

    def _headers(self) -> dict[str, str]:
        if self._access_token is None:
            return {}
        return {"Authorization": f"Bearer {self._access_token}"}

    async def _send_once(self, spec: RequestSpec) -> HttpPayload:
        if self._fetcher is not None:
            return await self._fetcher.send_once(spec)
        async with HttpFetcher() as fetcher:
            return await fetcher.send_once(spec)

    def _classify(self, payload: HttpPayload) -> DeliveryResult:
        if payload.status == 200:
            message_id = _provider_message_id(payload.body)
            if message_id is not None:
                return self._result(payload, delivered=True, retryable=False, request_id=message_id)
            return self._result(
                payload,
                delivered=False,
                retryable=True,
                uncertain=True,
                error="ntfy returned an unreadable success response",
            )
        if payload.status == 429:
            return self._result(
                payload,
                delivered=False,
                retryable=True,
                error=self._error("ntfy is rate limited", payload),
                retry_after_seconds=retry_after_delay(payload.headers),
            )
        if 500 <= payload.status <= 599:
            delay = max(
                SERVER_ERROR_MINIMUM_RETRY_SECONDS, retry_after_delay(payload.headers) or 0.0
            )
            return self._result(
                payload,
                delivered=False,
                retryable=True,
                error=self._error("ntfy server error", payload),
                retry_after_seconds=delay,
            )
        if 400 <= payload.status <= 499:
            return self._result(
                payload,
                delivered=False,
                retryable=False,
                error=self._error("ntfy rejected the alert", payload),
            )
        return self._result(
            payload,
            delivered=False,
            retryable=False,
            error=f"unexpected HTTP {payload.status} from ntfy",
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
        uncertain: bool = False,
    ) -> DeliveryResult:
        return DeliveryResult(
            provider=self.provider,
            delivered=delivered,
            retryable=retryable,
            uncertain=uncertain,
            http_status=payload.status,
            provider_request_id=request_id,
            retry_after_seconds=retry_after_seconds,
            error=error,
        )

    def _transport_failure(self, exc: BaseException) -> DeliveryResult:
        """A request that produced no answer is worth retrying, and never leaks a secret.

        The result is uncertain only when the message may already have been accepted; a failure that
        could not have reached the server is certain and retryable, because a retry cannot duplicate
        anything.
        """
        return DeliveryResult(
            provider=self.provider,
            delivered=False,
            retryable=True,
            uncertain=transport_is_uncertain(exc),
            error=self._redact(f"{type(exc).__name__}: {exc}"),
        )

    def _error(self, prefix: str, payload: HttpPayload) -> str:
        detail = payload.body.decode("utf-8", errors="replace").strip()[:_ERROR_DETAIL_LIMIT]
        text = f"{prefix}: HTTP {payload.status}" + (f": {detail}" if detail else "")
        return self._redact(text)

    def _redact(self, text: str) -> str:
        return redact_secrets(text, self._sensitive())

    def _sensitive(self) -> tuple[str | None, ...]:
        """The access token, plus the topic URL and name when the topic is public.

        On a public host the topic name is the only thing protecting the subscription, so it belongs
        in the redaction set even though the notifier was allowed to publish there.
        """
        if not self._public_topic:
            return (self._access_token,)
        return (self._access_token, self._topic_url, self._topic)


def validate_topic_url(value: str | None) -> str:
    """A topic URL the notifier can publish to: https, a host, and a topic path.

    The value itself is never echoed in the error, because a public topic name is the only secret
    that protects it.
    """
    if value is None or not value.strip():
        raise NotificationConfigurationError(f"{NTFY_TOPIC_URL_ENV} must be set and non-blank")
    topic = value.strip()
    parts = urlsplit(topic)
    if parts.scheme != "https":
        raise NotificationConfigurationError("the ntfy topic URL must use https")
    if not parts.hostname:
        raise NotificationConfigurationError("the ntfy topic URL must name a host")
    if not parts.path.strip("/"):
        raise NotificationConfigurationError("the ntfy topic URL must name a topic")
    return topic


def split_topic_url(value: str) -> tuple[str, str]:
    """The server root a JSON publish goes to, and the topic name it carries.

    The topic is the last path segment, so a server behind a path prefix keeps that prefix:
    ``https://host/ntfy/alerts`` publishes to ``https://host/ntfy/`` with topic ``alerts``.
    """
    parts = urlsplit(value)
    prefix, _, topic = parts.path.strip("/").rpartition("/")
    path = f"/{prefix}/" if prefix else "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", "")), topic


def is_public_topic(value: str) -> bool:
    """Whether a topic URL publishes to a host whose topics are readable by anyone.

    A trailing dot is a legal way to write the same DNS name, so it is stripped before the host is
    compared; ``ntfy.sh.`` is the same public host as ``ntfy.sh``.
    """
    host = (urlsplit(value).hostname or "").lower().rstrip(".")
    return any(host == public or host.endswith(f".{public}") for public in PUBLIC_NTFY_HOSTS)


def _provider_message_id(body: bytes) -> str | None:
    """ntfy's message id from a success document, or ``None`` when the body cannot be trusted."""
    try:
        document = json.loads(body)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    message_id = document.get("id")
    return message_id if isinstance(message_id, str) and message_id.strip() else None
