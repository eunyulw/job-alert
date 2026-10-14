"""Offline tests for the email-alert source: parsing, scoping, mailbox access, credentials."""

from __future__ import annotations

import email
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from job_alert.adapters.base import AdapterError, validate_collection
from job_alert.adapters.email_alerts import (
    EmailAlertAdapter,
    ImapGmailReader,
    keychain_credentials,
    parse_linkedin_text,
)
from job_alert.fetch import HttpFetcher
from job_alert.models import SourceClass, SourceConfig, SupportStatus

FIXTURE = Path("tests/fixtures/email_alerts/linkedin-alert.txt")
SENT = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 14, 16, 0, tzinfo=UTC)


def message(text: str, sent: datetime = SENT) -> email.message.Message:
    built = EmailMessage()
    built["From"] = "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>"
    built["Subject"] = "Tesla intern: new jobs"
    built["Date"] = email.utils.format_datetime(sent)
    built.set_content(text)
    return email.message_from_bytes(built.as_bytes())


class FakeReader:
    def __init__(self, *messages: email.message.Message) -> None:
        self.messages = list(messages)
        self.calls: list[dict[str, Any]] = []

    def read(self, *, senders: list[str], since: datetime) -> list[email.message.Message]:
        self.calls.append({"senders": senders, "since": since})
        return self.messages


def source(**parameters: object) -> SourceConfig:
    return SourceConfig(
        id="tesla-email-alerts",
        company_id="tesla",
        provider="email-alerts",
        source_class=SourceClass.FALLBACK,
        support_status=SupportStatus.FALLBACK_ONLY,
        endpoint="https://mail.google.com/",
        interval_minutes=30,
        parameters={"companies": ["Tesla"], **parameters},
    )


def test_linkedin_cards_parse_into_title_company_location_and_id() -> None:
    cards = parse_linkedin_text(FIXTURE.read_text(encoding="utf-8"), sent_at=SENT)

    assert [(card.job_id, card.company, card.location) for card in cards] == [
        ("4012345678", "Tesla", "Palo Alto, CA"),
        ("4012345679", "Tesla", "Palo Alto, CA"),
        ("4012345680", "Rivian", "Irvine, CA"),
    ]
    assert cards[0].title == (
        "Internship, Mechanical Engineer, Vehicle Engineering (Winter/Spring 2027)"
    )
    assert all(card.sent_at == SENT for card in cards)


@pytest.mark.asyncio
async def test_only_the_named_companies_become_jobs_with_clean_links() -> None:
    reader = FakeReader(message(FIXTURE.read_text(encoding="utf-8")))
    adapter = EmailAlertAdapter(reader_factory=lambda _: reader, now=lambda: NOW)

    async with HttpFetcher() as fetcher:
        result = validate_collection(await adapter.collect(source(), fetcher))

    assert [job.source_job_id for job in result.jobs] == ["4012345678", "4012345679"]
    job = result.jobs[0]
    assert job.company_id == "tesla"
    assert job.canonical_job_key == "linkedin:alerts:4012345678"
    # The tracking parameters in the email link are dropped.
    assert str(job.posting_url) == "https://www.linkedin.com/jobs/view/4012345678/"
    assert job.locations == ["Palo Alto, CA"]
    # The alert's send time bounds the posting time from above.
    assert job.source_published_at == SENT
    assert job.raw_metadata["alert_source"] == "linkedin"
    assert reader.calls[0]["senders"] == ["jobalerts-noreply@linkedin.com"]
    assert reader.calls[0]["since"] == NOW - timedelta(days=45)


@pytest.mark.asyncio
async def test_a_job_seen_in_several_alerts_keeps_its_earliest_send_time() -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    reader = FakeReader(message(text, SENT), message(text, SENT - timedelta(days=2)))
    adapter = EmailAlertAdapter(reader_factory=lambda _: reader, now=lambda: NOW)

    async with HttpFetcher() as fetcher:
        result = validate_collection(await adapter.collect(source(), fetcher))

    assert len(result.jobs) == 2
    assert {job.source_published_at for job in result.jobs} == {SENT - timedelta(days=2)}


@pytest.mark.asyncio
async def test_an_empty_mailbox_is_a_complete_empty_collection() -> None:
    adapter = EmailAlertAdapter(reader_factory=lambda _: FakeReader(), now=lambda: NOW)

    async with HttpFetcher() as fetcher:
        result = await adapter.collect(source(), fetcher)

    assert result.complete is True and result.jobs == []


@pytest.mark.asyncio
async def test_a_source_must_name_its_companies() -> None:
    adapter = EmailAlertAdapter(reader_factory=lambda _: FakeReader(), now=lambda: NOW)
    unnamed = source().model_copy(update={"parameters": {}})

    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="companies"):
            await adapter.collect(unnamed, fetcher)


def test_keychain_credentials_read_account_and_password_without_echoing_it() -> None:
    calls: list[list[str]] = []

    def run(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        if "-w" in argv:
            return 0, "app-password-1234\n"
        return 0, (
            'keychain: "login"\n    "acct"<blob>="me@example.com"\n'
            '    "svce"<blob>="job-alert-gmail"\n'
        )

    account, password = keychain_credentials("job-alert-gmail", run=run)

    assert (account, password) == ("me@example.com", "app-password-1234")
    assert all(argv[:2] == ["security", "find-generic-password"] for argv in calls)


def test_missing_keychain_item_explains_how_to_add_it() -> None:
    def run(argv: list[str]) -> tuple[int, str]:
        return 44, "security: SecKeychainSearchCopyNext: The specified item could not be found.\n"

    with pytest.raises(AdapterError, match="security add-generic-password"):
        keychain_credentials("job-alert-gmail", run=run)


class FakeImap:
    instances: list[FakeImap] = []

    def __init__(self, host: str) -> None:
        self.host = host
        self.calls: list[tuple[Any, ...]] = []
        FakeImap.instances.append(self)

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("login", user))
        return "OK", [b""]

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("select", mailbox, readonly))
        return "OK", [b"2"]

    def search(self, charset: Any, *criteria: str) -> tuple[str, list[bytes]]:
        self.calls.append(("search", *criteria))
        return "OK", [b"7"]

    def fetch(self, ids: str, parts: str) -> tuple[str, list[Any]]:
        self.calls.append(("fetch", ids, parts))
        raw = message("Tesla\nView job: https://www.linkedin.com/comm/jobs/view/1/").as_bytes()
        return "OK", [(b"7 (BODY[] {1})", raw), b")"]

    def logout(self) -> tuple[str, list[bytes]]:
        self.calls.append(("logout",))
        return "BYE", [b""]


def test_the_imap_reader_opens_read_only_and_never_marks_mail_read() -> None:
    FakeImap.instances.clear()
    reader = ImapGmailReader("imap.gmail.com", "me@example.com", "pw", imap_factory=FakeImap)

    messages = reader.read(senders=["jobalerts-noreply@linkedin.com"], since=SENT)

    calls = FakeImap.instances[0].calls
    assert ("select", '"[Gmail]/All Mail"', True) in calls
    assert ("search", "SINCE", "14-Oct-2026", "FROM", '"jobalerts-noreply@linkedin.com"') in calls
    assert ("fetch", "7", "(BODY.PEEK[])") in calls
    assert calls[-1] == ("logout",)
    assert len(messages) == 1
