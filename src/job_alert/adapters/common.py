"""Provider-neutral value predicates and normalizers shared by job-board adapters."""

from __future__ import annotations

from urllib.parse import urlsplit


def https_url(value: object, *, field: str) -> str:
    """Return a nonblank absolute HTTPS URL or describe why the provider value is unusable.

    The generic ``ValueError`` deliberately leaves each provider adapter responsible for turning a
    malformed record into its own error type and collection warning.
    """
    url = optional_text(value)
    if url is None:
        raise ValueError(f"missing {field}")
    if not is_absolute_https(url):
        raise ValueError(f"{field} is not an absolute https URL")
    return url


def is_absolute_https(value: str) -> bool:
    """Whether ``value`` parses as an absolute HTTPS URL without raising for malformed input."""
    try:
        parts = urlsplit(value)
    except ValueError:
        # ``urlsplit`` rejects malformed URLs itself, for example an unmatched IPv6 bracket.
        return False
    return parts.scheme == "https" and bool(parts.netloc)


def optional_text(value: object) -> str | None:
    """Normalize provider scalars to nonblank text, excluding boolean values."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value).strip()
    return text or None
