"""Provider-neutral adapter value normalization."""

from __future__ import annotations

import pytest

from job_alert.adapters.common import html_to_text, https_url, is_absolute_https, optional_text


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


def test_html_to_text_flattens_blocks_and_lists_deterministically() -> None:
    document = "<h2>About</h2><p>Build<br />&amp; ship</p><ul><li>One</li><li>Two</li></ul>"

    assert html_to_text(document) == "About\nBuild\n& ship\n- One\n- Two"


def test_html_to_text_recovers_an_escaped_html_document() -> None:
    assert html_to_text("&lt;p&gt;Build &amp;amp; ship&lt;/p&gt;") == "Build & ship"


@pytest.mark.parametrize("value", [None, 42, True, "", "   "])
def test_html_to_text_returns_none_when_there_is_no_text(value: object) -> None:
    assert html_to_text(value) is None
