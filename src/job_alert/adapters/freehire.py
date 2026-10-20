"""Read one company through Freehire's public job-search API.

Freehire aggregates public ATS feeds and returns each posting's original provider and URL. This
adapter is deliberately a fallback: it gives a company whose own careers site refuses ordinary
HTTP a machine-readable route without copying browser, proxy, or challenge-evasion transports.

The source endpoint must be ``https://freehire.me/api/v1/jobs/search`` and must name exactly one
``company_slug``. Other search parameters are preserved. The adapter owns ``limit`` and
``offset`` and walks through Freehire's 100-row pages until the declared total is reached.

Mapping decisions:

- ``public_slug`` is the stable source job id. A result for another company is unusable rather
  than reassigned, so a broad or ambiguous API response cannot silently attach jobs to the
  wrong canonical company.
- ``url`` is both posting and application URL: Freehire publishes the original board's direct
  link. Its ``utm_source=freehire.me`` decoration is removed by the shared URL canonicalizer.
- ``location`` is the display label. When it is blank, nonblank cities and country codes are
  joined so the geography filter still has evidence.
- Freehire's ``internship`` employment type is normalized to the monitor's ``intern`` vocabulary.
- The original Freehire source and the company name are retained in raw metadata for audit.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import HttpUrl, ValidationError

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import parse_date, unique
from job_alert.adapters.common import html_to_text, optional_text
from job_alert.adapters.paged import UnusableRecord, walk_scoped
from job_alert.fetch import HttpFetcher, HttpPayload, RequestSpec
from job_alert.identity import build_dedupe_key, build_job_key, content_hash
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "freehire"
API_HOST = "freehire.me"
API_PATH = "/api/v1/jobs/search"
PAGE_SIZE = 100
SEARCH_CAP = 10_000
MAX_PAGES = SEARCH_CAP // PAGE_SIZE
OWNED = frozenset({"limit", "offset"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


class FreehireAdapter:
    """Normalize one company-scoped Freehire search into jobs."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        parts = urlsplit(str(source.endpoint))
        if parts.scheme != "https" or parts.netloc != API_HOST or parts.path != API_PATH:
            raise AdapterError(
                f"freehire endpoint {source.endpoint} is not https://{API_HOST}{API_PATH}"
            )
        query = [
            (name, value)
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
            if name not in OWNED
        ]
        company_slug = next(
            (value.strip() for name, value in query if name == "company_slug" and value.strip()),
            None,
        )
        if company_slug is None:
            raise AdapterError(
                f"freehire source {source.id} has no company_slug in its endpoint query"
            )
        first_seen_at = self._now()

        async def fetch_page(_: int, offset: int) -> tuple[Sequence[object], int]:
            page_query = urlencode([*query, ("limit", str(PAGE_SIZE)), ("offset", str(offset))])
            payload = await fetcher.get(
                RequestSpec(
                    url=urlunsplit(("https", API_HOST, API_PATH, page_query, "")),
                    headers=dict(source.headers),
                    expected="json",
                )
            )
            return _parse_page(payload)

        return await walk_scoped(
            source_id=source.id,
            fetch_page=fetch_page,
            normalize=lambda record: _normalize_job(
                record,
                source=source,
                company_slug=company_slug,
                first_seen_at=first_seen_at,
            ),
            cap=SEARCH_CAP,
            max_pages=MAX_PAGES,
            cap_advice="Freehire cannot page past 10,000 results for one company",
        )


def _parse_page(payload: HttpPayload) -> tuple[list[Any], int]:
    if payload.not_modified:
        raise AdapterError(f"freehire returned 304 Not Modified for {payload.final_url}")
    try:
        document = json.loads(payload.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError(f"freehire payload from {payload.final_url} is not JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise AdapterError("freehire payload is not a JSON object")
    records = document.get("data")
    meta = document.get("meta")
    total = meta.get("total") if isinstance(meta, Mapping) else None
    if not isinstance(records, list):
        raise AdapterError("freehire payload has no data array")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AdapterError("freehire payload has no non-negative integer meta.total")
    return records, total


def _normalize_job(
    record: object,
    *,
    source: SourceConfig,
    company_slug: str,
    first_seen_at: datetime,
) -> NormalizedJob:
    if not isinstance(record, Mapping):
        raise UnusableRecord("record is not a JSON object")
    returned_company = optional_text(record.get("company_slug"))
    if returned_company != company_slug:
        raise UnusableRecord(
            f"result belongs to company_slug={returned_company!r}, not {company_slug!r}"
        )
    job_id = optional_text(record.get("public_slug"))
    title = optional_text(record.get("title"))
    posting_url = optional_text(record.get("url"))
    if job_id is None:
        raise UnusableRecord("missing public_slug")
    if title is None:
        raise UnusableRecord("missing title")
    if posting_url is None or not posting_url.startswith("https://"):
        raise UnusableRecord("missing an absolute https URL")
    locations = _locations(record)
    description = _description(record.get("description"))
    enrichment = record.get("enrichment")
    metadata = enrichment if isinstance(enrichment, Mapping) else {}
    employment_type = _employment_type(metadata.get("employment_type"))
    try:
        return NormalizedJob(
            company_id=source.company_id,
            source_id=source.id,
            source_job_id=job_id,
            canonical_job_key=build_job_key(
                provider=PROVIDER,
                tenant=company_slug,
                source_job_id=job_id,
                company_id=source.company_id,
                title=title,
                locations=locations,
                apply_url=posting_url,
            ),
            dedupe_key=build_dedupe_key(
                company_id=source.company_id,
                requisition_id=None,
                apply_url=posting_url,
                title=title,
                locations=locations,
                department=optional_text(metadata.get("category")),
            ),
            title=title,
            locations=locations,
            department=optional_text(metadata.get("category")),
            employment_type=employment_type,
            workplace_type=optional_text(record.get("work_mode")),
            description_text=description,
            content_hash=content_hash(title, locations, description),
            posting_url=HttpUrl(posting_url),
            apply_url=HttpUrl(posting_url),
            source_published_at=parse_date(record.get("posted_at")),
            first_seen_at=first_seen_at,
            raw_metadata={
                "freehire_source": optional_text(record.get("source")),
                "freehire_company": optional_text(record.get("company")),
                "freehire_external_id": optional_text(record.get("external_id")),
            },
        )
    except ValidationError as exc:
        raise UnusableRecord("record does not normalize into a job") from exc


def _locations(record: Mapping[str, Any]) -> list[str]:
    label = optional_text(record.get("location"))
    if label is not None:
        return [label]
    cities = record.get("cities")
    countries = record.get("countries")
    city_values = [optional_text(city) for city in cities] if isinstance(cities, list) else []
    country_values = (
        [optional_text(code) for code in countries] if isinstance(countries, list) else []
    )
    country = next((value.upper() for value in country_values if value is not None), None)
    return unique(
        [
            f"{city}, {country}" if city is not None and country is not None else city
            for city in city_values
        ]
    )


def _description(value: object) -> str | None:
    text = optional_text(value)
    return html_to_text(text) if text is not None else None


def _employment_type(value: object) -> str | None:
    text = optional_text(value)
    return "intern" if text == "internship" else text
