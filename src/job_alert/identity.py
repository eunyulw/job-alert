from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Only these parameters are stripped from a job URL. Everything else is preserved, because an
# unknown parameter may be the only thing distinguishing two postings on the same path.
TRACKING_PARAMETERS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gh_src",
        "lever-source",
        "source",
    }
)


def _normalize_text(value: str) -> str:
    """Casefold and collapse whitespace so cosmetic edits do not change identity."""
    return " ".join(value.split()).casefold()


def _normalize_locations(locations: list[str]) -> list[str]:
    return sorted(_normalize_text(location) for location in locations)


def _digest(parts: list[str]) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def canonicalize_url(url: str) -> str:
    """Normalize a job URL: lowercased host, sorted query, no fragment, no tracking."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    query = sorted(
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if name not in TRACKING_PARAMETERS
    )
    return urlunsplit((parts.scheme.lower(), host, parts.path, urlencode(query), ""))


def build_job_key(
    *,
    provider: str,
    tenant: str,
    source_job_id: str | None,
    company_id: str,
    title: str,
    locations: list[str],
    apply_url: str,
) -> str:
    """Per-source storage identity: the stable source ID when available, else a fingerprint."""
    if source_job_id:
        return f"{provider}:{tenant}:{source_job_id}"
    fingerprint = _digest(
        [
            company_id,
            _normalize_text(title),
            *_normalize_locations(locations),
            canonicalize_url(apply_url),
        ]
    )
    return f"sha256:{fingerprint}"


def build_dedupe_key(
    *,
    company_id: str,
    requisition_id: str | None,
    apply_url: str,
    title: str,
    locations: list[str],
    department: str | None,
) -> str:
    """Cross-board link for one public posting, kept separate from the per-source job key."""
    if requisition_id:
        return f"requisition:{company_id}:{requisition_id}"
    if apply_url:
        return f"url:{company_id}:{canonicalize_url(apply_url)}"
    fingerprint = _digest(
        [
            company_id,
            _normalize_text(title),
            *_normalize_locations(locations),
            _normalize_text(department) if department is not None else "",
        ]
    )
    return f"sha256:{fingerprint}"


def content_hash(title: str, locations: list[str], description_text: str | None) -> str:
    """Content digest used to detect real edits; absent description hashes as empty text."""
    return _digest(
        [
            _normalize_text(title),
            *_normalize_locations(locations),
            "" if description_text is None else description_text,
        ]
    )
