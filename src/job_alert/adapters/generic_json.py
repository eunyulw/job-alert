"""Registry-driven generic JSON adapter.

A company that publishes its own JSON search endpoint is a fallback source: the audit verified one
endpoint by hand, and nothing about the payload is reusable, so this adapter holds no provider
knowledge at all. Every field it reads is named by a source's ``parameters``, and every one of
those names is compiled through ``compile_selector()`` before any request is sent.

Selector grammar
----------------
``compile_selector()`` accepts identifiers separated by dots, where a segment may end in one list
expansion token ``[]`` (``results.jobs[]``, ``locations[].name``). An identifier is
``[A-Za-z_][A-Za-z0-9_]*`` and may not contain ``__``. Everything else is refused with
``AdapterError``: index or star access (``jobs[0]``, ``jobs[*]``), a second expansion token in one
segment (``jobs[][]``), call syntax (``jobs()``), recursive descent (``$..jobs``), filters
(``jobs[?(@.name)]``), arbitrary expressions (``jobs[] | length``, ``1+2``), empty or blank
segments (``jobs.``, ``.jobs``), and a selector deeper than ``MAX_SELECTOR_SEGMENTS``. Nothing in a
selector is evaluated: it is walked, key by key, over the parsed document.

Registry parameter contract
---------------------------
Required, each a selector into the parsed page:

- ``collection``: the job records. The selector must resolve on every page; a page that stopped
  publishing it is a shape change, not an empty board. A collection that resolves to an empty list
  is an empty board.
- ``id``: the posting id. Identity is ``generic-json:{tenant}:{id}``.
- ``title``: a nonblank string.
- ``posting_url``: an absolute HTTPS URL, or a relative one resolved against the audited origin.

Optional mappings: ``apply_url`` (defaults to the posting URL), ``locations`` (one or more strings;
``locations[].name`` reads a list of location objects), ``description`` (HTML or text, flattened by
the shared ``html_to_text()``), ``department``, ``published_at`` (ISO 8601 with a UTC offset), and
``total`` (a non-negative integer the walk is measured against).

Pagination parameters, mutually exclusive: ``next_page_url`` (a selector for the next page's URL,
absolute or relative to the audited origin) or ``next_page_token`` (a selector for an opaque cursor)
with ``token_parameter`` naming the query parameter it is sent as (default ``token``). A page whose
next-page reference is absent, ``null``, or blank ends the walk.

Missing mapped fields
---------------------
A record that cannot name itself, lacks a title, or lacks a usable posting URL makes the
collection incomplete with a warning that names the selector; the collection is never quietly
shortened. The same is true for a malformed mapped timestamp or a repeated posting id.

Pagination rules
----------------
The walk follows the configured next-page reference, exactly one request per page, and ends when
the payload publishes no next page. An empty page ends the walk too, which is the rule the provider
adapters use; when that empty page still published a next page the walk reports an incomplete
collection instead of claiming the board ended there, and a short page is never proof of the end in
the first place. A repeated next-page reference and a page of already-seen job ids are both loops:
each is reported as incomplete and the walk stops. ``MAX_PAGES`` is a last-resort ceiling and
reaching it is an incomplete collection, never a complete one. When ``total`` is mapped, the number
of unique posting ids read has to equal the newest declaration or the collection is incomplete.

Challenge pages and HTTP failures are classified by ``HttpFetcher`` before this adapter sees the
body, so a challenge is never mistaken for an empty board. A document that is not a JSON object,
or one that does not publish the mapped collection, is an error rather than an empty board.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import HttpUrl, ValidationError

from job_alert.adapters.base import AdapterError
from job_alert.adapters.common import (
    html_to_text,
    https_origin,
    optional_text,
    resolve_https_url,
)
from job_alert.fetch import HttpFetcher, HttpPayload, RequestSpec
from job_alert.identity import build_dedupe_key, build_job_key, content_hash
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "generic-json"

# Registry parameter names. A reviewer reading a source registry entry sees exactly these keys.
COLLECTION_PARAMETER = "collection"
ID_PARAMETER = "id"
TITLE_PARAMETER = "title"
LOCATIONS_PARAMETER = "locations"
POSTING_URL_PARAMETER = "posting_url"
APPLY_URL_PARAMETER = "apply_url"
DESCRIPTION_PARAMETER = "description"
DEPARTMENT_PARAMETER = "department"
PUBLISHED_AT_PARAMETER = "published_at"
TOTAL_PARAMETER = "total"
NEXT_PAGE_URL_PARAMETER = "next_page_url"
NEXT_PAGE_TOKEN_PARAMETER = "next_page_token"
TOKEN_PARAMETER_PARAMETER = "token_parameter"

REQUIRED_PARAMETERS: tuple[str, ...] = (
    COLLECTION_PARAMETER,
    ID_PARAMETER,
    TITLE_PARAMETER,
    POSTING_URL_PARAMETER,
)

DEFAULT_TOKEN_PARAMETER = "token"

# Registering more key lookups than this is a configuration mistake, so the grammar bounds the walk
# rather than accepting an arbitrarily deep selector.
MAX_SELECTOR_SEGMENTS = 8

# A fallback source is a courtesy read of an endpoint the company does not document for this
# purpose, so its walk is bounded far below the provider-specific adapters' ceilings. Reaching the
# cap is an incomplete collection, never a complete one.
MAX_PAGES = 100

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_TOKEN_PARAMETER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*\Z")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _UnusableJob(ValueError):
    """One record cannot be normalized, so the collection is incomplete."""


@dataclass(frozen=True, slots=True)
class SelectorSegment:
    """One compiled segment: the key to read and whether the value is a list to expand."""

    name: str
    expand: bool = False


@dataclass(frozen=True, slots=True)
class _Resolution:
    """What one selector found: its values, and whether its keys were published at all."""

    present: bool
    values: list[object]


@dataclass(frozen=True, slots=True)
class _Selector:
    """A compiled mapping, kept with the registry text so errors can name the parameter."""

    text: str
    segments: tuple[SelectorSegment, ...]


@dataclass(frozen=True, slots=True)
class _Mapping:
    """Every selector one source's registry entry mapped, compiled once per collection."""

    collection: _Selector
    job_id: _Selector
    title: _Selector
    posting_url: _Selector
    apply_url: _Selector | None
    locations: _Selector | None
    description: _Selector | None
    department: _Selector | None
    published_at: _Selector | None
    total: _Selector | None
    next_page_url: _Selector | None
    next_page_token: _Selector | None
    token_parameter: str


class GenericJsonAdapter:
    """Normalize one registry-described company JSON endpoint into jobs."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        mapping = _mapping(source)
        origin = https_origin(str(source.endpoint))
        tenant = _tenant(source)
        list_url = _list_url(str(source.endpoint), mapping)
        first_seen_at = self._now()

        jobs: list[NormalizedJob] = []
        warnings: list[str] = []
        seen_job_keys: set[str] = set()
        seen_page_ids: set[frozenset[str]] = set()
        seen_ids: set[str] = set()
        seen_targets: set[str] = {list_url}
        records_read = 0
        page_count = 0
        declared_total: int | None = None
        target = list_url

        while True:
            payload = await fetcher.get(
                RequestSpec(url=target, headers=dict(source.headers), expected="json")
            )
            document = _parse_document(payload)
            page_count += 1
            declared_total = _declared_total(document, source=source, mapping=mapping)

            resolution = _resolve(document, mapping.collection.segments)
            if not resolution.present:
                raise AdapterError(
                    f"generic-json payload from {payload.final_url} does not publish the mapped "
                    f"collection {mapping.collection.text!r}"
                )
            records = _flatten(resolution.values)

            page_ids = frozenset(_page_ids(records, mapping))
            if page_ids and page_ids in seen_page_ids:
                warnings.append(
                    f"pagination loop detected at page {page_count}: the page repeated an earlier "
                    "page's job ids, so the collection is incomplete"
                )
                break
            if page_ids:
                seen_page_ids.add(page_ids)

            for index_in_page, record in enumerate(records):
                index = records_read + index_in_page
                try:
                    job = _normalize_job(
                        record,
                        source=source,
                        mapping=mapping,
                        origin=origin,
                        tenant=tenant,
                        first_seen_at=first_seen_at,
                        page=page_count,
                        index=index,
                    )
                except _UnusableJob as exc:
                    warnings.append(f"skipped malformed job record at index {index}: {exc}")
                    continue
                if job.canonical_job_key in seen_job_keys:
                    warnings.append(
                        f"skipped duplicate job record at index {index}: "
                        "the source job id already appeared"
                    )
                    continue
                seen_job_keys.add(job.canonical_job_key)
                jobs.append(job)
            records_read += len(records)
            seen_ids.update(page_ids)

            next_target = _next_target(document, mapping, origin=origin, list_url=list_url)
            if not records:
                # An empty page ends the walk, which is the rule the provider adapters use. When
                # the page still published a next page the walk cannot claim the board ended
                # there, so the collection is reported incomplete instead of quietly stopping.
                if next_target is not None:
                    warnings.append(
                        f"stopped at page {page_count}: the page published no job records while "
                        "it still published a next page, so the collection may be incomplete"
                    )
                break
            if next_target is None:
                break
            if next_target in seen_targets:
                warnings.append(
                    f"pagination loop detected after page {page_count}: the next page reference "
                    "repeated one already read, so the collection is incomplete"
                )
                break
            if page_count >= MAX_PAGES:
                warnings.append(
                    f"stopped after {page_count} pages: the page cap is reached, so the collection "
                    "may be incomplete"
                )
                break
            seen_targets.add(next_target)
            target = next_target

        if declared_total is not None and len(seen_ids) != declared_total:
            warnings.append(
                f"collected {len(seen_ids)} unique postings but the payload declared "
                f"{mapping.total.text if mapping.total else TOTAL_PARAMETER}={declared_total}, so "
                "the collection is incomplete"
            )

        return CollectionResult(
            source_id=source.id,
            complete=not warnings,
            jobs=jobs,
            warnings=warnings,
            page_count=page_count,
            declared_total=declared_total,
        )


# --------------------------------------------------------------------------------------------
# Selector compilation and evaluation
# --------------------------------------------------------------------------------------------


def compile_selector(selector: object) -> tuple[SelectorSegment, ...]:
    """Compile a registry selector, or refuse it as a shape the grammar does not allow.

    The grammar is identifiers separated by dots plus the single list expansion token ``[]``. A
    compiled selector is only ever walked key by key over parsed JSON: nothing is evaluated, called,
    filtered, or searched recursively, so a registry entry cannot name an attribute, an index, or
    an expression on the payload.
    """
    if not isinstance(selector, str):
        raise AdapterError(
            f"selector must be a dotted string of identifiers, got {selector!r}"
        )
    if not selector:
        raise AdapterError("selector is empty")

    segments: list[SelectorSegment] = []
    for part in selector.split("."):
        if not part:
            raise AdapterError(f"selector {selector!r} has an empty segment")
        name = part
        expand = False
        if part.endswith("[]"):
            name = part[:-2]
            expand = True
        if not _IDENTIFIER.match(name):
            raise AdapterError(
                f"selector {selector!r} is not a dotted key: {part!r} is not a plain identifier"
            )
        if "__" in name:
            raise AdapterError(f"selector {selector!r} names the dunder key {name!r}")
        segments.append(SelectorSegment(name=name, expand=expand))

    if len(segments) > MAX_SELECTOR_SEGMENTS:
        raise AdapterError(
            f"selector {selector!r} has {len(segments)} segments, more than the "
            f"{MAX_SELECTOR_SEGMENTS} this adapter walks"
        )
    return tuple(segments)


def _resolve(document: object, selector: tuple[SelectorSegment, ...]) -> _Resolution:
    """Every value a compiled selector names, in document order.

    A key is read from a mapping. A key applied to a list is read from each mapping the list holds,
    which is what a selector such as ``locations[].name`` needs. ``present`` is false only when the
    selector matched nothing: a segment whose containers were all empty lists matched nothing
    because there was nothing to read, which is not the same as a key that disappeared.
    """
    current: list[object] = [document]
    present = True
    for segment in selector:
        found: list[object] = []
        for value in current:
            found.extend(_lookup(value, segment.name))
        present = bool(found) or bool(current) and all(
            isinstance(value, list) and not value for value in current
        )
        if segment.expand:
            found = _flatten(found)
        current = found
        if not current:
            break
    return _Resolution(present=present, values=current)


def _lookup(value: object, name: str) -> list[object]:
    if isinstance(value, Mapping):
        return [value[name]] if name in value else []
    if isinstance(value, list):
        found: list[object] = []
        for item in value:
            found.extend(_lookup(item, name))
        return found
    return []


def _flatten(values: list[object]) -> list[object]:
    """Flatten one level, so a collection that is itself a list still reads as its records."""
    flattened: list[object] = []
    for value in values:
        if isinstance(value, list):
            flattened.extend(value)
        else:
            flattened.append(value)
    return flattened


def extract_records(document: object, selector: str) -> list[object]:
    """The job records a collection selector names.

    A payload that stopped publishing the collection is an ``AdapterError``, because an endpoint
    that renamed its records is a shape change and never an empty board. A collection that is
    published but empty is an empty board.
    """
    resolution = _resolve(document, compile_selector(selector))
    if not resolution.present:
        raise AdapterError(f"payload does not publish the collection {selector!r}")
    return _flatten(resolution.values)


# --------------------------------------------------------------------------------------------
# Registry parameters
# --------------------------------------------------------------------------------------------


def _mapping(source: SourceConfig) -> _Mapping:
    """Compile every selector a source mapped, refusing one the grammar does not allow."""
    for name in REQUIRED_PARAMETERS:
        if source.parameters.get(name) is None:
            raise AdapterError(
                f"generic-json source {source.id} has no {name} parameter: the registry must map "
                "it explicitly"
            )

    next_page_url = _optional_selector(source, NEXT_PAGE_URL_PARAMETER)
    next_page_token = _optional_selector(source, NEXT_PAGE_TOKEN_PARAMETER)
    if next_page_url is not None and next_page_token is not None:
        raise AdapterError(
            f"generic-json source {source.id} maps both {NEXT_PAGE_URL_PARAMETER} and "
            f"{NEXT_PAGE_TOKEN_PARAMETER}: one pagination mode per source"
        )

    return _Mapping(
        collection=_required_selector(source, COLLECTION_PARAMETER),
        job_id=_required_selector(source, ID_PARAMETER),
        title=_required_selector(source, TITLE_PARAMETER),
        posting_url=_required_selector(source, POSTING_URL_PARAMETER),
        apply_url=_optional_selector(source, APPLY_URL_PARAMETER),
        locations=_optional_selector(source, LOCATIONS_PARAMETER),
        description=_optional_selector(source, DESCRIPTION_PARAMETER),
        department=_optional_selector(source, DEPARTMENT_PARAMETER),
        published_at=_optional_selector(source, PUBLISHED_AT_PARAMETER),
        total=_optional_selector(source, TOTAL_PARAMETER),
        next_page_url=next_page_url,
        next_page_token=next_page_token,
        token_parameter=_token_parameter(source),
    )


def _required_selector(source: SourceConfig, name: str) -> _Selector:
    selector = _compile_parameter(source, name, source.parameters.get(name))
    assert selector is not None  # guaranteed by the required-parameter check above
    return selector


def _optional_selector(source: SourceConfig, name: str) -> _Selector | None:
    return _compile_parameter(source, name, source.parameters.get(name))


def _compile_parameter(source: SourceConfig, name: str, value: object) -> _Selector | None:
    if value is None:
        return None
    try:
        return _Selector(text=str(value), segments=compile_selector(value))
    except AdapterError as exc:
        raise AdapterError(
            f"generic-json source {source.id} has an unusable {name} selector: {exc}"
        ) from exc


def _token_parameter(source: SourceConfig) -> str:
    value = source.parameters.get(TOKEN_PARAMETER_PARAMETER)
    if value is None:
        return DEFAULT_TOKEN_PARAMETER
    name = optional_text(value)
    if name is None or not _TOKEN_PARAMETER_NAME.match(name):
        raise AdapterError(
            f"generic-json source {source.id} has an unusable {TOKEN_PARAMETER_PARAMETER}: "
            f"expected a query parameter name, got {value!r}"
        )
    return name


def _tenant(source: SourceConfig) -> str:
    """The board token that makes a source job id unique, or the audited endpoint's host."""
    if source.tenant is not None and source.tenant.strip():
        return source.tenant.strip()
    return https_origin(str(source.endpoint)).split("://", maxsplit=1)[-1]


def _list_url(endpoint: str, mapping: _Mapping) -> str:
    """The configured endpoint without the token parameter the adapter sets itself.

    A configured token would otherwise decide where the walk starts. Every other query parameter is
    kept, so a registry entry can still narrow a company endpoint, for example to one team.
    """
    if mapping.next_page_token is None:
        return endpoint
    parts = urlsplit(endpoint)
    query = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if name != mapping.token_parameter
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _token_url(list_url: str, parameter: str, token: str) -> str:
    parts = urlsplit(list_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append((parameter, token))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


# --------------------------------------------------------------------------------------------
# Response shape and next-page references
# --------------------------------------------------------------------------------------------


def _parse_document(payload: HttpPayload) -> object:
    if payload.not_modified:
        raise AdapterError(f"generic-json returned 304 Not Modified for {payload.final_url}")
    media_type = payload.headers.get("Content-Type")
    try:
        document = json.loads(payload.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError(
            f"generic-json payload from {payload.final_url} is not JSON ({media_type}): {exc}"
        ) from exc
    if not isinstance(document, Mapping):
        raise AdapterError("generic-json payload is not a JSON object")
    return document


def _declared_total(document: object, *, source: SourceConfig, mapping: _Mapping) -> int | None:
    """The board size one page declares, when the registry mapped a total."""
    if mapping.total is None:
        return None
    value = _first(_resolve(document, mapping.total.segments).values)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AdapterError(
            f"generic-json payload has no non-negative integer {mapping.total.text!r}, so the "
            f"source {source.id} collection cannot be measured"
        )
    return int(value)


def _next_target(
    document: object, mapping: _Mapping, *, origin: str, list_url: str
) -> str | None:
    """The next page to request, or ``None`` when the payload publishes no next page."""
    if mapping.next_page_url is not None:
        value = _first(_resolve(document, mapping.next_page_url.segments).values)
        if value is None:
            return None
        try:
            return resolve_https_url(
                value, origin=origin, field=mapping.next_page_url.text
            )
        except ValueError as exc:
            raise AdapterError(f"generic-json next page reference is unusable: {exc}") from exc
    if mapping.next_page_token is not None:
        token = optional_text(_first(_resolve(document, mapping.next_page_token.segments).values))
        if token is None:
            return None
        return _token_url(list_url, mapping.token_parameter, token)
    return None


def _page_ids(records: list[object], mapping: _Mapping) -> set[str]:
    """The posting ids of one page, ignoring records that cannot even name themselves."""
    ids: set[str] = set()
    for record in records:
        value = optional_text(_first(_resolve(record, mapping.job_id.segments).values))
        if value is not None:
            ids.add(value)
    return ids


# --------------------------------------------------------------------------------------------
# Record normalization
# --------------------------------------------------------------------------------------------


def _normalize_job(
    record: object,
    *,
    source: SourceConfig,
    mapping: _Mapping,
    origin: str,
    tenant: str,
    first_seen_at: datetime,
    page: int,
    index: int,
) -> NormalizedJob:
    if not isinstance(record, Mapping):
        raise _UnusableJob("record is not a JSON object")
    source_job_id = _job_id(record, mapping)
    title = _title(record, mapping)
    posting_url = _url(record, mapping.posting_url, origin=origin, required=True)
    assert posting_url is not None  # required=True raises instead of returning None
    apply_url = posting_url
    if mapping.apply_url is not None:
        apply_url = _url(record, mapping.apply_url, origin=origin, required=False) or posting_url
    locations = _locations(record, mapping)
    department = _text(record, mapping.department)
    description_text = _description(record, mapping)
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
                requisition_id=None,
                apply_url=apply_url,
                title=title,
                locations=locations,
                department=department,
            ),
            title=title,
            locations=locations,
            department=department,
            description_text=description_text,
            content_hash=content_hash(title, locations, description_text),
            # ``NormalizedJob`` types both URLs as ``HttpUrl``, so validate them here and let the
            # surrounding handler turn an unusable URL into an incomplete collection.
            posting_url=HttpUrl(posting_url),
            apply_url=HttpUrl(apply_url),
            source_published_at=_timestamp(record, mapping.published_at),
            first_seen_at=first_seen_at,
            raw_metadata={"page": page, "record_index": index},
        )
    except ValidationError as exc:
        raise _UnusableJob("record does not normalize into a job") from exc


def _job_id(record: Mapping[str, object], mapping: _Mapping) -> str:
    source_job_id = optional_text(_first(_resolve(record, mapping.job_id.segments).values))
    if source_job_id is None:
        raise _UnusableJob(f"missing {mapping.job_id.text}")
    return source_job_id


def _title(record: Mapping[str, object], mapping: _Mapping) -> str:
    value = _first(_resolve(record, mapping.title.segments).values)
    if not isinstance(value, str) or not value.strip():
        raise _UnusableJob(f"missing {mapping.title.text}")
    return value


def _url(
    record: Mapping[str, object], selector: _Selector, *, origin: str, required: bool
) -> str | None:
    value = _first(_resolve(record, selector.segments).values)
    if value is None:
        if required:
            raise _UnusableJob(f"missing {selector.text}")
        return None
    try:
        return resolve_https_url(value, origin=origin, field=selector.text)
    except ValueError as exc:
        raise _UnusableJob(str(exc)) from exc


def _locations(record: Mapping[str, object], mapping: _Mapping) -> list[str]:
    """Every mapped location string, deduplicated case-insensitively in provider order."""
    if mapping.locations is None:
        return []
    candidates: list[object] = []
    for value in _resolve(record, mapping.locations.segments).values:
        if isinstance(value, list):
            candidates.extend(value)
        else:
            candidates.append(value)

    locations: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        text = optional_text(candidate)
        if text is None:
            continue
        normalized = " ".join(text.split())
        key = normalized.casefold()
        if key not in seen:
            seen.add(key)
            locations.append(normalized)
    return locations


def _text(record: Mapping[str, object], selector: _Selector | None) -> str | None:
    if selector is None:
        return None
    return optional_text(_first(_resolve(record, selector.segments).values))


def _description(record: Mapping[str, object], mapping: _Mapping) -> str | None:
    """The mapped description as plain text, whether the provider published HTML or text."""
    if mapping.description is None:
        return None
    return html_to_text(_first(_resolve(record, mapping.description.segments).values))


def _timestamp(record: Mapping[str, object], selector: _Selector | None) -> datetime | None:
    """A mapped ISO 8601 publication timestamp, which has to carry a UTC offset."""
    if selector is None:
        return None
    value = _first(_resolve(record, selector.segments).values)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _UnusableJob(f"{selector.text} is not an ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _UnusableJob(f"{selector.text} is not an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise _UnusableJob(f"{selector.text} has no UTC offset")
    return parsed.astimezone(UTC)


def _first(values: list[object]) -> object | None:
    return values[0] if values else None
