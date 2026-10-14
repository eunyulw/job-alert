"""Provenance and sanitization checks for every recorded provider fixture.

The fixtures under ``tests/fixtures/`` are committed provider responses, so they are the one place
in the tree where a live capture can leak credentials or a natural person's name. This module
scans every fixture directory and asserts what a reviewer would otherwise have to check by hand:
the directory carries a provenance README, at least one response file, and no response carries a
credential key or a personal-data key that holds a value.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

FIXTURES_ROOT = Path(__file__).resolve().parents[2] / "fixtures"

# Request credentials and session state that a capture must never carry. A key is compared
# case-insensitively with `-` normalised to `_`, so `Set-Cookie` and `set_cookie` both match.
SECRET_KEYS = frozenset({"authorization", "cookie", "set_cookie", "api_key", "access_token"})

# Employee names that reach a fixture through a provider schema rather than through a description.
# SmartRecruiters published `creator: {"name": "<employee>"}` on 12 of 100 captured records; the
# key is what leaked a real name past a review that only scanned job text, so any value-bearing
# `creator`, `author`, `hiringManager`, `recruiter`, `contact` or `owner` key is a failure here.
PERSONAL_DATA_KEYS = frozenset(
    {"creator", "author", "hiringmanager", "recruiter", "contact", "owner"}
)

HTML_SUFFIXES = frozenset({".html", ".htm"})

# Email-alert fixtures are message text, not provider responses. Email is where a person's address
# leaks, so the only addresses such a file may carry are the alert services' no-reply senders.
EMAIL_SUFFIXES = frozenset({".txt", ".eml"})
ALLOWED_EMAIL_ADDRESSES = frozenset({"jobalerts-noreply@linkedin.com", "alert@indeed.com"})
EMAIL_ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

PROVIDER_DIRECTORIES = sorted(
    path
    for path in FIXTURES_ROOT.iterdir()
    if path.is_dir() and not path.name.startswith(".")
)


def _normalise_key(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_")


def _key_value_pairs(payload: Any) -> Iterator[tuple[str, Any]]:
    """Yield every key in a JSON payload with the value it holds, at any nesting depth."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield str(key), value
            yield from _key_value_pairs(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _key_value_pairs(item)


def _holds_a_value(value: Any) -> bool:
    """Whether a key carries content, as opposed to being absent-equivalent.

    ``null``, ``""``, ``[]`` and ``{}`` are treated as no value: a sanitized response that keeps
    the key shape but drops the name is not a leak, while the captured
    ``creator: {"name": "..."}`` object is.
    """
    if value is None:
        return False
    if isinstance(value, (str, list, dict)):
        return bool(value)
    return True


def _response_files(directory: Path) -> list[Path]:
    """Every committed capture in a directory, so a stray file is scanned rather than skipped."""
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.name != "README.md"
        and not path.name.startswith(".")
    )


def _is_html(path: Path, text: str) -> bool:
    if path.suffix.lower() in HTML_SUFFIXES:
        return True
    return text.lstrip().startswith("<")


def _parse_response(path: Path) -> Any:
    """Return the parsed JSON payload of a response file, or ``None`` when the file is HTML."""
    text = path.read_text(encoding="utf-8")
    if _is_html(path, text):
        assert text.strip(), f"{path.name} is an empty HTML response"
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise AssertionError(
            f"{path.name} is neither valid JSON nor an HTML document, so it is not a response "
            f"capture this test can vouch for: {error}"
        ) from error


def test_fixture_scan_covers_every_provider_directory() -> None:
    """The scan is only meaningful while it really walks the recorded provider directories."""
    assert PROVIDER_DIRECTORIES, f"no fixture directories found under {FIXTURES_ROOT}"
    assert {"greenhouse", "lever", "ashby", "smartrecruiters"} <= {
        path.name for path in PROVIDER_DIRECTORIES
    }


@pytest.mark.parametrize(
    "directory", PROVIDER_DIRECTORIES, ids=[path.name for path in PROVIDER_DIRECTORIES]
)
def test_fixture_directory_records_provenance(directory: Path) -> None:
    readme = directory / "README.md"

    assert readme.is_file(), f"{directory.name} has no README.md"
    assert readme.read_text(encoding="utf-8").strip(), f"{directory.name}/README.md is empty"
    assert _response_files(directory), f"{directory.name} has no JSON or HTML response file"


@pytest.mark.parametrize(
    "directory", PROVIDER_DIRECTORIES, ids=[path.name for path in PROVIDER_DIRECTORIES]
)
def test_fixture_responses_parse_and_carry_no_sensitive_keys(directory: Path) -> None:
    for path in _response_files(directory):
        if path.suffix.lower() in EMAIL_SUFFIXES:
            text = path.read_text(encoding="utf-8")
            assert text.strip(), f"{directory.name}/{path.name} is empty"
            addresses = set(EMAIL_ADDRESS.findall(text)) - ALLOWED_EMAIL_ADDRESSES
            assert not addresses, f"{directory.name}/{path.name} carries addresses {addresses}"
            for secret in sorted(SECRET_KEYS):
                assert secret not in text.lower(), f"{directory.name}/{path.name} has {secret!r}"
            continue
        payload = _parse_response(path)
        if payload is None:
            # An HTML capture has no keys, so the secret check falls back to the raw text: the page
            # must not publish a credential word the browser-scraped markup picked up.
            text = path.read_text(encoding="utf-8").lower()
            for secret in sorted(SECRET_KEYS):
                assert secret not in text, f"{directory.name}/{path.name} contains {secret!r}"
            continue

        for key, value in _key_value_pairs(payload):
            normalised = _normalise_key(key)
            assert normalised not in SECRET_KEYS, (
                f"{directory.name}/{path.name} contains the credential key {key!r}"
            )
            assert not (normalised in PERSONAL_DATA_KEYS and _holds_a_value(value)), (
                f"{directory.name}/{path.name} holds a personal-data value under {key!r}"
            )
