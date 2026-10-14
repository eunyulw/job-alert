"""jobs.apple.com adapter.

Apple's careers site renders each search page on the server and embeds the result as
``window.__staticRouterHydrationData = JSON.parse("…")``: a JSON string holding
``loaderData.search.searchResults`` (20 per page) and ``totalRecords``. The adapter reads that
object; it never runs page script. A source is scoped through its endpoint's query (for example
``team=internships-STDNT-INTRN&sort=newest``); the adapter owns only ``page`` (1-based).

Mapping decisions:

- ``source_job_id`` is ``id`` (``200688776-2611``, or ``PIPE-…`` for pipeline roles), identity
  ``apple:{tenant}:{id}``; ``requisition_id`` is ``positionId``.
- ``posting_url`` is ``https://jobs.apple.com/{locale}/details/{id}/{transformedPostingTitle}``,
  with both segments restricted to letters, digits, and ``-`` so a payload cannot reshape the link.
- ``locations`` are ``"{name}, {countryName}"`` (just one when they match); ``team`` is
  ``team.teamName``; ``description_text`` is ``jobSummary``; ``source_published_at`` is
  ``postDateInGMT``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import HttpUrl, ValidationError

from job_alert.adapters.base import AdapterError
from job_alert.adapters.common import optional_text
from job_alert.adapters.paged import UnusableRecord, walk_scoped
from job_alert.fetch import HttpFetcher, HttpPayload, RequestSpec
from job_alert.identity import build_dedupe_key, build_job_key, content_hash
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "apple"
DEFAULT_TENANT = "apple"
ORIGIN = "https://jobs.apple.com"
SEARCH_CAP = 100_000
MAX_PAGES = 500
_HYDRATION = re.compile(r"__staticRouterHydrationData\s*=\s*JSON\.parse\((\"(?:[^\"\\]|\\.)*\")\)")
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9-]+$")
_LOCALE = re.compile(r"^/([a-z]{2}-[a-z]{2})/search/?$")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AppleAdapter:
    """Normalize one scoped jobs.apple.com search into jobs, one server-rendered page at a time."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        parts = urlsplit(str(source.endpoint))
        locale_match = _LOCALE.match(parts.path)
        if parts.netloc != "jobs.apple.com" or locale_match is None:
            raise AdapterError(
                f"apple endpoint {source.endpoint} is not https://jobs.apple.com/{{locale}}/search"
            )
        locale = locale_match.group(1)
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "page"]
        tenant = (source.tenant or DEFAULT_TENANT).strip() or DEFAULT_TENANT
        first_seen_at = self._now()

        async def fetch_page(page_index: int, _: int) -> tuple[Sequence[object], int]:
            page_query = urlencode([*query, ("page", str(page_index + 1))])
            url = urlunsplit(("https", parts.netloc, parts.path, page_query, ""))
            payload = await fetcher.get(
                RequestSpec(url=url, headers=dict(source.headers), expected="html")
            )
            return _parse_page(payload)

        return await walk_scoped(
            source_id=source.id,
            fetch_page=fetch_page,
            normalize=lambda record: _normalize_job(
                record, source=source, tenant=tenant, locale=locale, first_seen_at=first_seen_at
            ),
            cap=SEARCH_CAP,
            max_pages=MAX_PAGES,
            cap_advice="narrow the source's query",
        )


def _parse_page(payload: HttpPayload) -> tuple[list[Any], int]:
    if payload.not_modified:
        raise AdapterError(f"apple returned 304 Not Modified for {payload.final_url}")
    text = payload.body.decode("utf-8", errors="replace")
    match = _HYDRATION.search(text)
    if match is None:
        raise AdapterError(f"apple page {payload.final_url} carries no hydration data")
    try:
        data = json.loads(json.loads(match.group(1)))
        search = data["loaderData"]["search"]
        results = search["searchResults"]
        total = search["totalRecords"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise AdapterError(f"apple hydration data has an unexpected shape: {exc}") from exc
    if not isinstance(results, list):
        raise AdapterError("apple hydration data has no searchResults array")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AdapterError("apple hydration data has no non-negative integer totalRecords")
    return results, total


def _normalize_job(
    record: object, *, source: SourceConfig, tenant: str, locale: str, first_seen_at: datetime
) -> NormalizedJob:
    if not isinstance(record, Mapping):
        raise UnusableRecord("record is not a JSON object")
    job_id = optional_text(record.get("id"))
    slug = optional_text(record.get("transformedPostingTitle"))
    title = record.get("postingTitle")
    if job_id is None or not _SAFE_SEGMENT.match(job_id):
        raise UnusableRecord("id is missing or not a safe path segment")
    if slug is None or not _SAFE_SEGMENT.match(slug):
        raise UnusableRecord("transformedPostingTitle is missing or not a safe path segment")
    if not isinstance(title, str) or not title.strip():
        raise UnusableRecord("missing postingTitle")
    posting_url = f"{ORIGIN}/{locale}/details/{job_id}/{slug}"
    locations = _locations(record.get("locations"))
    team_value = record.get("team")
    team = optional_text(team_value.get("teamName")) if isinstance(team_value, Mapping) else None
    description = optional_text(record.get("jobSummary"))
    requisition = optional_text(record.get("positionId"))
    try:
        return NormalizedJob(
            company_id=source.company_id,
            source_id=source.id,
            source_job_id=job_id,
            canonical_job_key=build_job_key(
                provider=PROVIDER,
                tenant=tenant,
                source_job_id=job_id,
                company_id=source.company_id,
                title=title,
                locations=locations,
                apply_url=posting_url,
            ),
            dedupe_key=build_dedupe_key(
                company_id=source.company_id,
                requisition_id=requisition,
                apply_url=posting_url,
                title=title,
                locations=locations,
                department=team,
            ),
            requisition_id=requisition,
            title=title,
            locations=locations,
            team=team,
            description_text=description,
            content_hash=content_hash(title, locations, description),
            posting_url=HttpUrl(posting_url),
            apply_url=HttpUrl(posting_url),
            source_published_at=_timestamp(record.get("postDateInGMT")),
            first_seen_at=first_seen_at,
            raw_metadata={"type": optional_text(record.get("type"))},
        )
    except ValidationError as exc:
        raise UnusableRecord("record does not normalize into a job") from exc


def _locations(value: object) -> list[str]:
    names: list[str] = []
    for entry in value if isinstance(value, list) else []:
        if not isinstance(entry, Mapping):
            continue
        name = optional_text(entry.get("name"))
        country = optional_text(entry.get("countryName"))
        if name and country and name != country:
            label: str | None = f"{name}, {country}"
        else:
            label = name or country
        if label and label not in names:
            names.append(label)
    return names


def _timestamp(value: object) -> datetime | None:
    text = optional_text(value)
    if text is None:
        return None
    try:
        # Apple writes up to nine fractional digits; Python parses at most six.
        trimmed = re.sub(r"(\.\d{6})\d+", r"\1", text.replace("Z", "+00:00"))
        return datetime.fromisoformat(trimmed).astimezone(UTC)
    except ValueError:
        return None
