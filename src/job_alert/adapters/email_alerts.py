"""Job postings read from job-alert emails in the user's own mailbox.

This source exists for companies that refuse automated access to their own careers site (Tesla
answers 403 to an ordinary request). It reads the job-alert emails a service the user subscribed
to sends them (LinkedIn job alerts first), keeps the cards for the named companies, and turns each
into a posting. It is a secondary channel, never a complete board: it knows only what the alerts
said, so "complete" means every alert email in the lookback window was read.

Mailbox access is IMAP over TLS to Gmail with an app password kept in the macOS login keychain
(``security add-generic-password -s job-alert-gmail -a you@gmail.com -w``), never in the repository.
The mailbox is opened read-only and messages are fetched with ``BODY.PEEK[]``, so reading never
marks mail as read or changes it in any way.

Mapping: ``source_job_id`` is the LinkedIn job id; identity is ``linkedin:alerts:{id}``; the
posting link is ``https://www.linkedin.com/jobs/view/{id}/`` with the email's tracking parameters
dropped; ``source_published_at`` is the earliest alert email that carried the job, an upper bound
on when it was posted. A posting stays active while an alert in the lookback window mentions it.
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import email.message
import email.utils
import imaplib
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pydantic import HttpUrl, ValidationError

from job_alert.adapters.base import AdapterError
from job_alert.fetch import HttpFetcher
from job_alert.identity import build_dedupe_key, build_job_key, content_hash
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "email-alerts"
LINKEDIN_SENDER = "jobalerts-noreply@linkedin.com"
DEFAULT_KEYCHAIN_SERVICE = "job-alert-gmail"
DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_MAILBOX = '"[Gmail]/All Mail"'
DEFAULT_LOOKBACK_DAYS = 45
_VIEW_JOB = re.compile(r"https://(?:www\.)?linkedin\.com/(?:comm/)?jobs/view/(\d+)")
_NOISE = re.compile(
    r"^(actively recruiting|promoted|easy apply|be an early applicant"
    r"|\d+ (applicants?|connections?)|\d+ (school )?alumni.*|.*connection works here)$",
    re.IGNORECASE,
)
_RULE = re.compile(r"^[-_=]{5,}$")


@dataclass(frozen=True, slots=True)
class AlertCard:
    """One job card from one alert email."""

    job_id: str
    title: str
    company: str
    location: str
    sent_at: datetime


def parse_linkedin_text(text: str, *, sent_at: datetime) -> list[AlertCard]:
    """Cards from the plain-text part of a LinkedIn job-alert email.

    A card is the run of lines ending in a ``View job:`` link to ``linkedin.com/jobs/view/{id}``.
    Its first three meaningful lines are the title, the company, and the location; status lines
    such as "Actively recruiting" are skipped.
    """
    cards: list[AlertCard] = []
    block: list[str] = []
    for raw in text.splitlines():
        line = " ".join(raw.split())
        match = _VIEW_JOB.search(line)
        if match:
            fields = [entry for entry in block if not _NOISE.match(entry)]
            if len(fields) >= 3:
                title, company, location = fields[-3], fields[-2], fields[-1]
                cards.append(AlertCard(match.group(1), title, company, location, sent_at))
            elif len(fields) == 2:
                cards.append(AlertCard(match.group(1), fields[0], fields[1], "", sent_at))
            block = []
        elif _RULE.match(line):
            block = []
        elif line:
            block.append(line)
    return cards


class MailboxReader(Protocol):
    def read(self, *, senders: list[str], since: datetime) -> list[email.message.Message]: ...


class ImapGmailReader:
    """Read matching messages over IMAP without changing the mailbox."""

    def __init__(
        self,
        host: str,
        account: str,
        password: str,
        *,
        mailbox: str = DEFAULT_MAILBOX,
        imap_factory: Callable[[str], Any] = imaplib.IMAP4_SSL,
    ) -> None:
        self._host = host
        self._account = account
        self._password = password
        self._mailbox = mailbox
        self._imap_factory = imap_factory

    def read(self, *, senders: list[str], since: datetime) -> list[email.message.Message]:
        connection = self._imap_factory(self._host)
        try:
            try:
                connection.login(self._account, self._password)
            except imaplib.IMAP4.error as exc:
                raise AdapterError(f"gmail refused the login for {self._account}: {exc}") from None
            status, _ = connection.select(self._mailbox, readonly=True)
            if status != "OK":
                raise AdapterError(f"cannot open mailbox {self._mailbox} read-only")
            messages: list[email.message.Message] = []
            for sender in senders:
                status, found = connection.search(
                    None, "SINCE", since.strftime("%d-%b-%Y"), "FROM", f'"{sender}"'
                )
                if status != "OK":
                    raise AdapterError(f"mailbox search failed for {sender}")
                ids = b" ".join(found).split()
                for message_id in ids:
                    status, parts = connection.fetch(message_id.decode(), "(BODY.PEEK[])")
                    if status != "OK":
                        raise AdapterError(f"cannot read message {message_id.decode()}")
                    for part in parts:
                        if isinstance(part, tuple) and len(part) == 2:
                            messages.append(email.message_from_bytes(part[1]))
            return messages
        finally:
            # A failed logout must not hide the real result.
            with contextlib.suppress(Exception):
                connection.logout()


def _run(argv: list[str]) -> tuple[int, str]:
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    return completed.returncode, completed.stdout or completed.stderr


def keychain_credentials(
    service: str, *, run: Callable[[list[str]], tuple[int, str]] = _run
) -> tuple[str, str]:
    """The account and app password stored under ``service`` in the macOS login keychain."""
    code, attributes = run(["security", "find-generic-password", "-s", service])
    if code != 0:
        raise AdapterError(
            f"no keychain item {service!r}: create a Gmail app password, then run"
            f" `security add-generic-password -s {service} -a you@gmail.com -w`"
        )
    match = re.search(r'"acct"<blob>="([^"]+)"', attributes)
    if match is None:
        raise AdapterError(f"keychain item {service!r} names no account")
    code, password = run(["security", "find-generic-password", "-s", service, "-w"])
    if code != 0 or not password.strip():
        raise AdapterError(f"keychain item {service!r} holds no password")
    return match.group(1), password.strip()


def _default_reader(source: SourceConfig) -> MailboxReader:
    service = str(source.parameters.get("keychain_service", DEFAULT_KEYCHAIN_SERVICE))
    account, password = keychain_credentials(service)
    host = str(source.parameters.get("imap_host", DEFAULT_IMAP_HOST))
    return ImapGmailReader(host, account, password)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _plain_text(message: email.message.Message) -> str:
    for part in message.walk():
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return ""


def _sent_at(message: email.message.Message, fallback: datetime) -> datetime:
    try:
        parsed = email.utils.parsedate_to_datetime(message.get("Date", ""))
    except (TypeError, ValueError):
        return fallback
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class EmailAlertAdapter:
    """Turn the named companies' cards in recent alert emails into postings."""

    provider = PROVIDER

    def __init__(
        self,
        *,
        reader_factory: Callable[[SourceConfig], MailboxReader] = _default_reader,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._reader_factory = reader_factory
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        companies = source.parameters.get("companies")
        if not isinstance(companies, list) or not companies or not all(
            isinstance(name, str) and name.strip() for name in companies
        ):
            raise AdapterError(f"email-alert source {source.id} must name its companies")
        wanted = {name.strip().casefold() for name in companies}
        senders = source.parameters.get("senders", [LINKEDIN_SENDER])
        if not isinstance(senders, list) or not all(isinstance(s, str) for s in senders):
            raise AdapterError(f"email-alert source {source.id} has unusable senders")
        lookback = source.parameters.get("lookback_days", DEFAULT_LOOKBACK_DAYS)
        if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback < 1:
            raise AdapterError(f"email-alert source {source.id} has an unusable lookback_days")
        now = self._now()
        reader = self._reader_factory(source)
        messages = await asyncio.to_thread(
            reader.read, senders=list(senders), since=now - timedelta(days=lookback)
        )
        earliest: dict[str, AlertCard] = {}
        for message in messages:
            for card in parse_linkedin_text(_plain_text(message), sent_at=_sent_at(message, now)):
                if card.company.casefold() not in wanted:
                    continue
                known = earliest.get(card.job_id)
                if known is None or card.sent_at < known.sent_at:
                    earliest[card.job_id] = card
        jobs = [_job(card, source, now) for card in sorted(earliest.values(), key=_order)]
        return CollectionResult(
            source_id=source.id,
            complete=True,
            jobs=jobs,
            page_count=max(1, len(messages)),
            declared_total=None,
        )


def _order(card: AlertCard) -> tuple[datetime, str]:
    return card.sent_at, card.job_id


def _job(card: AlertCard, source: SourceConfig, first_seen_at: datetime) -> NormalizedJob:
    url = f"https://www.linkedin.com/jobs/view/{card.job_id}/"
    locations: Sequence[str] = [card.location] if card.location else []
    try:
        return NormalizedJob(
            company_id=source.company_id,
            source_id=source.id,
            source_job_id=card.job_id,
            canonical_job_key=build_job_key(
                provider="linkedin",
                tenant="alerts",
                source_job_id=card.job_id,
                company_id=source.company_id,
                title=card.title,
                locations=list(locations),
                apply_url=url,
            ),
            dedupe_key=build_dedupe_key(
                company_id=source.company_id,
                requisition_id=None,
                apply_url=url,
                title=card.title,
                locations=list(locations),
                department=None,
            ),
            title=card.title,
            locations=list(locations),
            content_hash=content_hash(card.title, list(locations), None),
            posting_url=HttpUrl(url),
            apply_url=HttpUrl(url),
            source_published_at=card.sent_at,
            first_seen_at=first_seen_at,
            raw_metadata={"alert_source": "linkedin", "company": card.company},
        )
    except ValidationError as exc:
        raise AdapterError(f"alert card {card.job_id} does not normalize: {exc}") from exc
