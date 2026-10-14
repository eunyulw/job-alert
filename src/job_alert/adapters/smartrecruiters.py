"""SmartRecruiters Postings adapter.

``https://api.smartrecruiters.com/v1/companies/{companyIdentifier}/postings`` answers with one JSON
page of postings plus the board's own accounting: ``offset``, ``limit`` and ``totalFound``. One
collection walks the board with ``offset`` and ``limit`` until the unique posting ids it has read
reach ``totalFound``, the size the provider declares for the board.

Mapping decisions the payload does not spell out:

- ``source_job_id`` is the posting ``id`` and per-source identity is
  ``smartrecruiters:{tenant}:{id}``, where the tenant is the company identifier that also names the
  company in the endpoint path.
- ``requisition_id`` is ``refNumber``, which is what the board uses to name a posting internally.
- ``locations`` is one string built from the structured ``location`` object: ``city``, ``region``
  and ``country`` joined with a comma and a space, skipping the parts the board left blank. A
  two-letter country code is upper-cased because SmartRecruiters publishes ISO 3166-1 alpha-2 codes
  in lower case (``"ca"``, ``"gb"``). The board's ``location.fullLocation`` is display text that
  follows the posting's language, so the structured fields are used instead of it. A posting whose
  location object carries none of the three has no location rather than an empty string.
- ``department`` is ``department.label`` and ``employment_type`` is ``typeOfEmployment.label``.
  ``team`` stays empty: the list endpoint publishes no team field.
- ``workplace_type`` is ``"remote"`` or ``"hybrid"`` taken from the ``location.remote`` and
  ``location.hybrid`` flags. Two false flags mean the board did not say, not that the posting is
  on-site, so the field stays ``None`` instead of inventing a workplace type.
- ``description_text`` stays ``None``: the list endpoint publishes no job ad. The board serves the
  description from ``/postings/{id}``, and the adapter does not request one document per posting,
  so ``content_hash`` digests the title and locations, which is what the list endpoint can support.
- ``posting_url`` and ``apply_url`` are the payload's ``postingUrl`` and ``applyUrl`` when the board
  publishes them; those are the names the provider's own detail response uses. The captured list
  endpoint publishes neither, so both normally fall back to the derived public posting URL
  ``https://jobs.smartrecruiters.com/{companyIdentifier}/{postingId}``. That shape is the one the
  provider's detail response documents: for posting ``744000150454779`` the ``Ubisoft2`` board
  publishes ``https://jobs.smartrecruiters.com/Ubisoft2/744000150454779-team-lead-level-designer-splinter-cell-remake-``,
  whose ``{companyIdentifier}/{postingId}`` prefix is what the adapter derives and whose trailing
  slug changes whenever the posting is retitled. Both path segments are percent-encoded, so a
  provider-supplied posting id can never change the structure of the derived URL. The list
  endpoint's ``ref`` field is the *API* resource URL
  (``https://api.smartrecruiters.com/v1/companies/{companyIdentifier}/postings/{postingId}``) and
  is deliberately not used as a public link.
- ``releasedDate`` is the publication timestamp, an ISO 8601 value with a UTC offset, normalized to
  UTC. ``source_updated_at`` stays ``None`` because the list endpoint publishes no update time.

Pagination rules:

- Every request asks for the current ``offset`` and the ``limit`` taken from the source's
  ``page_size`` parameter (default ``100``, the largest page the provider serves: a request for
  ``limit=1000`` is answered with 100 records). Other parameters of the configured endpoint, such
  as a location filter, are preserved; ``offset`` and ``limit`` belong to the adapter.
- ``offset`` advances by the number of records the provider actually returned, never by the
  requested ``limit``, so a capped page cannot skip records.
- The walk ends when a page is empty or when the unique posting ids read so far reach
  ``totalFound``. A page shorter than the requested ``limit`` is never treated as the end: the
  provider caps a page at 100 records whatever it was asked for, so a short page would otherwise
  truncate every board larger than one page.
- ``totalFound`` is required on every page, and the newest declaration is the one the walk and the
  final count are measured against. The collection is complete only when the number of unique
  posting ids read equals that declaration; any disagreement is reported as an incomplete
  collection with a warning. A board that grew while it was being walked therefore follows its own
  new size instead of stopping at the stale one, and one that shrank ends at the size it now
  declares. A walk that ended in a pagination loop or at the page cap disagrees with the declared
  total too, so that warning is reported next to the more specific one.
- A page whose posting ids are exactly a page already seen is a loop: the adapter keeps the jobs it
  already has and reports ``complete=False`` with a warning instead of following the loop or
  truncating the collection silently.
- The board lists postings newest first, so a posting published while the board is walked lands
  ahead of the offset: every later record shifts one place, one record repeats at a page boundary,
  and the new posting sits on the head page the walk already left. A walk that ends short of
  ``totalFound`` therefore re-reads the head page once. If that recovers exactly the missing
  postings, the repeats it explains are dropped and the walk is complete.
- A walk whose only remaining defects are a repeated posting id or a count that disagrees with
  ``totalFound`` is still what a changing board looks like, so the adapter walks the board once
  more and returns that second walk, complete or not. A loop, the page cap, or an unusable record
  is never re-walked, because a second walk would only repeat it.
- ``MAX_PAGES`` is a last-resort cap. Reaching it is an incomplete collection, never a complete one,
  so a runaway walk cannot look like a finished board.

Challenge pages and HTTP failures are classified by ``HttpFetcher`` before this adapter sees the
body, so a challenge is never mistaken for an empty board. A document that is not a JSON object
with a ``content`` array and a non-negative integer ``totalFound`` is an error, not an empty board.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from pydantic import HttpUrl, ValidationError

from job_alert.adapters.base import AdapterError
from job_alert.adapters.common import https_url, optional_text
from job_alert.fetch import HttpFetcher, HttpPayload, RequestSpec
from job_alert.identity import build_dedupe_key, build_job_key, content_hash
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "smartrecruiters"

PAGE_SIZE_PARAMETER = "page_size"
DEFAULT_PAGE_SIZE = 100
MAX_PAGES = 1000

# Query parameters the adapter sets for every page request, whatever the configured endpoint says.
OWNED_QUERY_PARAMETERS = frozenset({"offset", "limit"})

# The documented public posting URL shape. The provider's own detail response publishes this prefix
# followed by a slug that changes when the posting is retitled.
PUBLIC_POSTING_URL = "https://jobs.smartrecruiters.com/{company}/{posting_id}"

_POSTINGS_PATH = re.compile(r"/v1/companies/(?P<company>[^/]+)/postings/?$")
_COUNTRY_CODE = re.compile(r"[a-zA-Z]{2}\Z")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _UnusableJob(ValueError):
    """One provider record cannot be normalized, so the collection is incomplete."""


class SmartRecruitersAdapter:
    """Normalize one SmartRecruiters board into jobs, one JSON page per request."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        tenant = _resolve_tenant(source)
        page_size = _page_size(source)
        list_url = _list_url(str(source.endpoint))
        first_seen_at = self._now()

        result, shifted = await self._walk(
            source, fetcher, tenant=tenant, page_size=page_size, list_url=list_url,
            first_seen_at=first_seen_at,
        )
        if result.complete or not shifted:
            return result
        # The board moved under the offsets while it was read. One fresh walk is the whole budget:
        # its verdict stands, complete or not, so a board that keeps disagreeing still fails closed.
        retried, _ = await self._walk(
            source, fetcher, tenant=tenant, page_size=page_size, list_url=list_url,
            first_seen_at=first_seen_at,
        )
        return retried

    async def _walk(
        self,
        source: SourceConfig,
        fetcher: HttpFetcher,
        *,
        tenant: str,
        page_size: int,
        list_url: str,
        first_seen_at: datetime,
    ) -> tuple[CollectionResult, bool]:
        """One pass over every page, and whether its only defects are those of a shifting board.

        A repeated posting id and a count that disagrees with ``totalFound`` are what an insertion
        or a removal ahead of the offset produces mid-walk. A pagination loop, the page cap, and an
        unusable record are not, and a second walk would only repeat them.
        """
        structural = False
        jobs: list[NormalizedJob] = []
        warnings: list[str] = []
        # Duplicates a shifting board explains; dropped only if the head re-read closes the gap.
        shift_notes: list[str] = []
        seen_job_keys: set[str] = set()
        seen_posting_ids: set[str] = set()
        seen_page_id_sets: set[frozenset[str]] = set()
        records_read = 0
        page_count = 0
        offset = 0
        # Every page declares the size of the board the walk has to cover; the newest declaration is
        # the reference for the next step and for the final count comparison.
        declared_total: int | None = None

        while True:
            payload = await fetcher.get(
                RequestSpec(
                    url=_page_url(list_url, offset=offset, limit=page_size),
                    headers=dict(source.headers),
                    expected="json",
                )
            )
            records, page_total = _parse_page(payload)
            page_count += 1
            declared_total = page_total

            page_ids = _page_ids(records)
            if page_ids in seen_page_id_sets:
                structural = True
                warnings.append(
                    f"pagination loop detected at page {page_count}: the page repeated an earlier "
                    "page's job ids, so the collection is incomplete"
                )
                break
            seen_page_id_sets.add(page_ids)

            for index_in_page, record in enumerate(records):
                index = records_read + index_in_page
                try:
                    job = _normalize_job(
                        record, source=source, tenant=tenant, first_seen_at=first_seen_at
                    )
                except _UnusableJob as exc:
                    structural = True
                    warnings.append(f"skipped malformed job record at index {index}: {exc}")
                    continue
                if job.canonical_job_key in seen_job_keys:
                    note = (
                        f"skipped duplicate job record at index {index}: "
                        "the source job id already appeared"
                    )
                    warnings.append(note)
                    shift_notes.append(note)
                    continue
                seen_job_keys.add(job.canonical_job_key)
                jobs.append(job)
            records_read += len(records)
            seen_posting_ids.update(page_ids)

            if not records:
                break
            if len(seen_posting_ids) >= declared_total:
                break
            if page_count >= MAX_PAGES:
                structural = True
                warnings.append(
                    f"stopped after {page_count} pages: the page cap is reached, so the collection "
                    "may be incomplete"
                )
                break
            offset += len(records)

        if (
            not structural
            and declared_total is not None
            and len(seen_posting_ids) < declared_total
        ):
            # The board lists newest first, so a posting published mid-walk lands ahead of the
            # offset: it shifts every later record (one repeats at a page boundary) and sits on the
            # head page the walk already left. One more read of that page recovers it.
            payload = await fetcher.get(
                RequestSpec(
                    url=_page_url(list_url, offset=0, limit=page_size),
                    headers=dict(source.headers),
                    expected="json",
                )
            )
            records, declared_total = _parse_page(payload)
            page_count += 1
            recovered = 0
            for index, record in enumerate(records):
                try:
                    job = _normalize_job(
                        record, source=source, tenant=tenant, first_seen_at=first_seen_at
                    )
                except _UnusableJob as exc:
                    structural = True
                    warnings.append(f"skipped malformed head record at index {index}: {exc}")
                    continue
                if job.canonical_job_key in seen_job_keys:
                    continue
                seen_job_keys.add(job.canonical_job_key)
                jobs.append(job)
                recovered += 1
            seen_posting_ids.update(_page_ids(records))
            if recovered and len(seen_posting_ids) == declared_total:
                warnings = [note for note in warnings if note not in shift_notes]

        if len(seen_posting_ids) != declared_total:
            warnings.append(
                f"collected {len(seen_posting_ids)} unique postings but the board declared "
                f"totalFound={declared_total}, so the collection is incomplete"
            )

        result = CollectionResult(
            source_id=source.id,
            complete=not warnings,
            jobs=jobs,
            warnings=warnings,
            page_count=page_count,
            declared_total=declared_total,
        )
        return result, not structural


def _list_url(endpoint: str) -> str:
    """The configured endpoint with the parameters the adapter sets itself removed.

    A configured ``offset`` or ``limit`` would otherwise decide where the walk starts or how long a
    page is. Every other query parameter is kept, so a registry entry can still filter a board, for
    example by posting location. The query string is rebuilt through ``urlencode``, so an already
    clean endpoint is preserved semantically rather than byte for byte (parameter order and
    percent-encoding may differ).
    """
    parts = urlsplit(endpoint)
    query = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if name not in OWNED_QUERY_PARAMETERS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _page_url(list_url: str, *, offset: int, limit: int) -> str:
    parts = urlsplit(list_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend([("offset", str(offset)), ("limit", str(limit))])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _resolve_tenant(source: SourceConfig) -> str:
    """The company identifier that makes a source job ID unique and names the public board."""
    if source.tenant is not None and source.tenant.strip():
        return source.tenant.strip()
    match = _POSTINGS_PATH.search(urlsplit(str(source.endpoint)).path)
    if match is not None:
        return match.group("company")
    raise AdapterError(
        f"smartrecruiters source {source.id} has no tenant: set tenant or point the endpoint at "
        "/v1/companies/{companyIdentifier}/postings"
    )


def _page_size(source: SourceConfig) -> int:
    value = source.parameters.get(PAGE_SIZE_PARAMETER)
    if value is None:
        return DEFAULT_PAGE_SIZE
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AdapterError(
            f"smartrecruiters source {source.id} has an unusable {PAGE_SIZE_PARAMETER}: expected a "
            f"positive integer, got {value!r}"
        )
    # ``SourceConfig.parameters`` is an untyped registry dictionary; the checks above make this a
    # positive ``int``, so the conversion only tells the type checker what is already true.
    return int(value)


def _parse_page(payload: HttpPayload) -> tuple[list[object], int]:
    """Validate one page envelope and return its postings and declared board size."""
    if payload.not_modified:
        raise AdapterError(f"smartrecruiters returned 304 Not Modified for {payload.final_url}")
    media_type = payload.headers.get("Content-Type")
    try:
        document = json.loads(payload.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError(
            f"smartrecruiters payload from {payload.final_url} is not JSON ({media_type}): {exc}"
        ) from exc
    if not isinstance(document, Mapping):
        raise AdapterError("smartrecruiters payload is not a JSON object")
    records = document.get("content")
    if not isinstance(records, list):
        raise AdapterError("smartrecruiters payload has no 'content' array")
    total = document.get("totalFound")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AdapterError("smartrecruiters payload has no non-negative integer totalFound")
    return records, total


def _page_ids(records: list[object]) -> frozenset[str]:
    """The posting ids of one page, ignoring records that cannot even name themselves."""
    ids: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            continue
        try:
            ids.add(_source_job_id(record))
        except _UnusableJob:
            continue
    return frozenset(ids)


def _normalize_job(
    record: object,
    *,
    source: SourceConfig,
    tenant: str,
    first_seen_at: datetime,
) -> NormalizedJob:
    if not isinstance(record, Mapping):
        raise _UnusableJob("record is not a JSON object")
    source_job_id = _source_job_id(record)
    title = _title(record)
    location = record.get("location")
    locations = _locations(location)
    department = _label(record.get("department"))
    requisition_id = optional_text(record.get("refNumber"))
    posting_url, apply_url = _posting_urls(record, tenant=tenant, source_job_id=source_job_id)
    try:
        return NormalizedJob(
            company_id=source.company_id,
            source_id=source.id,
            source_job_id=source_job_id,
            canonical_job_key=build_job_key(
                provider=PROVIDER,
                tenant=tenant,
                source_job_id=source_job_id,
                company_id=source.company_id,
                title=title,
                locations=locations,
                apply_url=apply_url,
            ),
            dedupe_key=build_dedupe_key(
                company_id=source.company_id,
                requisition_id=requisition_id,
                apply_url=apply_url,
                title=title,
                locations=locations,
                department=department,
            ),
            requisition_id=requisition_id,
            title=title,
            locations=locations,
            department=department,
            employment_type=_label(record.get("typeOfEmployment")),
            workplace_type=_workplace_type(location),
            # The list endpoint publishes no job ad, so the description stays empty and the hash
            # covers the fields the list endpoint does publish.
            description_text=None,
            content_hash=content_hash(title, locations, None),
            # ``NormalizedJob`` types both URLs as ``HttpUrl``, so validate them here and let the
            # surrounding handler turn an unusable URL into an incomplete collection.
            posting_url=HttpUrl(posting_url),
            apply_url=HttpUrl(apply_url),
            source_published_at=_timestamp(record.get("releasedDate"), "releasedDate"),
            first_seen_at=first_seen_at,
            raw_metadata=_raw_metadata(record, location),
        )
    except ValidationError as exc:
        raise _UnusableJob("record does not normalize into a job") from exc


def _source_job_id(record: Mapping[str, object]) -> str:
    value = record.get("id")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise _UnusableJob("missing id")
    source_job_id = str(value).strip()
    if not source_job_id:
        raise _UnusableJob("missing id")
    return source_job_id


def _title(record: Mapping[str, object]) -> str:
    value = record.get("name")
    if not isinstance(value, str) or not value.strip():
        raise _UnusableJob("missing title")
    return value


def _label(value: object) -> str | None:
    """The display label of a provider object such as ``department`` or ``typeOfEmployment``."""
    if not isinstance(value, Mapping):
        return None
    return optional_text(value.get("label"))


def _locations(location: object) -> list[str]:
    """One location string built from the structured city, region and country parts."""
    if not isinstance(location, Mapping):
        return []
    parts = [
        optional_text(location.get("city")),
        optional_text(location.get("region")),
        _country(location.get("country")),
    ]
    text = ", ".join(part for part in parts if part is not None)
    return [text] if text else []


def _country(value: object) -> str | None:
    """An ISO 3166-1 alpha-2 code as SmartRecruiters publishes it (lower case) is upper-cased."""
    country = optional_text(value)
    if country is None:
        return None
    return country.upper() if _COUNTRY_CODE.fullmatch(country) else country


def _workplace_type(location: object) -> str | None:
    """The provider's remote/hybrid flag, or nothing when the board did not say."""
    if not isinstance(location, Mapping):
        return None
    if location.get("remote") is True:
        return "remote"
    if location.get("hybrid") is True:
        return "hybrid"
    return None


def _posting_urls(
    record: Mapping[str, object], *, tenant: str, source_job_id: str
) -> tuple[str, str]:
    """The public posting and application URLs, derived from the posting when absent."""
    published_posting = _published_url(record.get("postingUrl"), "postingUrl")
    posting_url = published_posting or _derived_posting_url(tenant, source_job_id)
    published_apply = _published_url(record.get("applyUrl"), "applyUrl")
    return posting_url, published_apply or posting_url


def _published_url(value: object, field: str) -> str | None:
    """A URL the payload published, ``None`` when it published none, else unusable record."""
    if value is None:
        return None
    try:
        return https_url(value, field=field)
    except ValueError as exc:
        raise _UnusableJob(str(exc)) from exc


def _derived_posting_url(tenant: str, source_job_id: str) -> str:
    """The documented public posting URL, with both path segments percent-encoded."""
    return PUBLIC_POSTING_URL.format(
        company=quote(tenant, safe=""), posting_id=quote(source_job_id, safe="")
    )


def _timestamp(value: object, field: str) -> datetime | None:
    """Parse an ISO 8601 publication timestamp into an aware UTC datetime."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _UnusableJob(f"{field} is not an ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _UnusableJob(f"{field} is not an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise _UnusableJob(f"{field} has no UTC offset")
    return parsed.astimezone(UTC)


def _raw_metadata(record: Mapping[str, object], location: object) -> dict[str, object]:
    """Provider bookkeeping kept for diagnosis; not part of job identity."""
    language = record.get("language")
    return {
        "uuid": optional_text(record.get("uuid")),
        "job_ad_id": optional_text(record.get("jobAdId")),
        "location": dict(location) if isinstance(location, Mapping) else {},
        "industry": _label(record.get("industry")),
        "function": _label(record.get("function")),
        "experience_level": _label(record.get("experienceLevel")),
        "visibility": optional_text(record.get("visibility")),
        "language_code": _code(language),
    }


def _code(value: object) -> str | None:
    """The language code of a SmartRecruiters language object."""
    if not isinstance(value, Mapping):
        return None
    return optional_text(value.get("code"))
