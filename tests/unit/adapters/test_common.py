"""Provider-neutral adapter value normalization."""

from __future__ import annotations

import pytest

from job_alert.adapters.common import https_url, is_absolute_https, optional_text


def test_https_url_strips_an_absolute_https_url() -> None:
    assert https_url(" https://jobs.example.com/postings/1 ", field="posting_url") == (
        "https://jobs.example.com/postings/1"
    )


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (None, "missing posting_url"),
        (True, "missing posting_url"),
        ("", "missing posting_url"),
        ("http://jobs.example.com/postings/1", "posting_url is not an absolute https URL"),
        ("https://[::1", "posting_url is not an absolute https URL"),
    ],
)
def test_https_url_rejects_missing_or_non_https_values(value: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        https_url(value, field="posting_url")


def test_is_absolute_https_handles_malformed_urls_without_raising() -> None:
    assert is_absolute_https("https://jobs.example.com/postings/1") is True
    assert is_absolute_https("https://[::1") is False


def test_optional_text_keeps_non_boolean_scalars_and_discards_blank_values() -> None:
    assert optional_text(42) == "42"
    assert optional_text(True) is None
    assert optional_text("  ") is None
