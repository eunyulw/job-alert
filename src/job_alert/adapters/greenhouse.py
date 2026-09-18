"""Greenhouse Job Board adapter.

``https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true`` returns every
published posting of one board in a single JSON document, so a collection costs exactly one
request. Greenhouse publishes no pagination for this endpoint and the adapter invents none;
completeness rests on the provider's ``meta.total``, which the caller compares with the number of
parsed postings through ``validate_collection()``.

Mapping decisions the payload does not spell out:

- ``source_job_id`` is the posting ``id``, and identity is ``greenhouse:{tenant}:{id}``.
- ``locations`` is ``location.name`` followed by every ``offices[*].name``, deduplicated
  case-insensitively in provider order, because a Greenhouse board describes one posting both as a
  display string and as a list of offices.
- ``department`` is the first listed department name. ``team``, ``employment_type`` and
  ``workplace_type`` stay empty: the list endpoint exposes them only through board-specific
  ``metadata`` labels, which are preserved in ``raw_metadata`` instead of being guessed.
- ``content`` is an HTML document that becomes deterministic plain text, and the shared
  ``content_hash()`` digests title, locations and that text.
- ``apply_url`` is ``absolute_url`` unless the registry supplies an ``apply_url_template``
  parameter. Templates come from the reviewed registry, and one that does not render an absolute
  HTTPS URL is a configuration error rather than a silent fallback.

Challenge pages and HTTP failures are classified by ``HttpFetcher`` before this adapter sees the
body, so a challenge is never mistaken for an empty board.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import HttpUrl, ValidationError

from job_alert.adapters.base import AdapterError
from job_alert.fetch import HttpFetcher, HttpPayload, RequestSpec
from job_alert.identity import build_dedupe_key, build_job_key, content_hash
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "greenhouse"

APPLY_URL_TEMPLATE_PARAMETER = "apply_url_template"

_BOARD_PATH = re.compile(r"/boards/(?P<token>[^/]+)/jobs/?$")

_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)

# Element content that is markup or styling, not description text.
_DISCARDED_TAGS = frozenset({"script", "style", "noscript", "template"})

_WHITESPACE = re.compile(r"\s+")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _UnusableJob(ValueError):
    """One provider record cannot be normalized, so the collection is incomplete."""


class _PlainTextExtractor(HTMLParser):
    """Flatten HTML to text, treating block elements as line breaks and list items as bullets."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._discarded_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DISCARDED_TAGS:
            self._discarded_depth += 1
        elif tag == "li":
            self._chunks.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DISCARDED_TAGS:
            self._discarded_depth = max(0, self._discarded_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._discarded_depth == 0:
            # Source line wrapping is not a paragraph break; block tags are the only line breaks.
            self._chunks.append(_WHITESPACE.sub(" ", data))

    def text(self) -> str | None:
        lines = [" ".join(line.split()) for line in "".join(self._chunks).splitlines()]
        return "\n".join(line for line in lines if line) or None


class GreenhouseAdapter:
    """Normalize one Greenhouse board into jobs with a single request per collection."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        tenant = _resolve_tenant(source)
        payload = await fetcher.get(
            RequestSpec(
                url=_list_url(str(source.endpoint)),
                headers=dict(source.headers),
                expected="json",
            )
        )
        records, declared_total = _parse_board(payload)
        first_seen_at = self._now()

        jobs: list[NormalizedJob] = []
        warnings: list[str] = []
        seen: set[str] = set()
        for index, record in enumerate(records):
            try:
                job = _normalize_job(
                    record, source=source, tenant=tenant, first_seen_at=first_seen_at
                )
            except _UnusableJob as exc:
                warnings.append(f"skipped malformed job record at index {index}: {exc}")
                continue
            if job.canonical_job_key in seen:
                warnings.append(
                    f"skipped duplicate job record at index {index}: "
                    "the source job id already appeared"
                )
                continue
            seen.add(job.canonical_job_key)
            jobs.append(job)

        return CollectionResult(
            source_id=source.id,
            complete=not warnings,
            jobs=jobs,
            warnings=warnings,
            page_count=1,
            declared_total=declared_total,
        )


def _list_url(endpoint: str) -> str:
    """The configured endpoint, forced to ask for posting descriptions.

    A board recorded without ``content=true`` would silently lose every description, so the
    parameter is set explicitly. An endpoint that already asks for content keeps its exact URL.
    """
    parts = urlsplit(endpoint)
    query = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if name != "content"
    ]
    query.append(("content", "true"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _resolve_tenant(source: SourceConfig) -> str:
    """The board token that makes a source job ID unique."""
    if source.tenant is not None and source.tenant.strip():
        return source.tenant.strip()
    match = _BOARD_PATH.search(urlsplit(str(source.endpoint)).path)
    if match is not None:
        return match.group("token")
    raise AdapterError(
        f"greenhouse source {source.id} has no tenant: set tenant or point the endpoint at "
        "/v1/boards/{board_token}/jobs"
    )


def _parse_board(payload: HttpPayload) -> tuple[list[object], int]:
    """Validate the response envelope and return its postings and declared total."""
    if payload.not_modified:
        raise AdapterError(f"greenhouse returned 304 Not Modified for {payload.final_url}")
    media_type = payload.headers.get("Content-Type")
    try:
        document = json.loads(payload.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError(
            f"greenhouse payload from {payload.final_url} is not JSON ({media_type}): {exc}"
        ) from exc
    if not isinstance(document, Mapping):
        raise AdapterError("greenhouse payload is not a JSON object")
    records = document.get("jobs")
    if not isinstance(records, list):
        raise AdapterError("greenhouse payload has no 'jobs' array")
    meta = document.get("meta")
    total = meta.get("total") if isinstance(meta, Mapping) else None
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AdapterError("greenhouse payload has no non-negative integer meta.total")
    return records, total


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
    posting_url = _https_url(record, "absolute_url")
    locations = _locations(record)
    department = _first_name(record.get("departments"))
    requisition_id = _optional_text(record.get("requisition_id"))
    description_text = _plain_text(record.get("content"))
    apply_url = _apply_url(
        source, tenant=tenant, source_job_id=source_job_id, posting_url=posting_url
    )
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
            description_text=description_text,
            content_hash=content_hash(title, locations, description_text),
            # ``NormalizedJob`` types both URLs as ``HttpUrl``, so validate them here and let the
            # surrounding handler turn an unusable URL into an incomplete collection.
            posting_url=HttpUrl(posting_url),
            apply_url=HttpUrl(apply_url),
            source_published_at=_timestamp(record.get("first_published"), "first_published"),
            source_updated_at=_timestamp(record.get("updated_at"), "updated_at"),
            first_seen_at=first_seen_at,
            raw_metadata=_raw_metadata(record),
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
    value = record.get("title")
    if not isinstance(value, str) or not value.strip():
        raise _UnusableJob("missing title")
    return value


def _https_url(record: Mapping[str, object], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _UnusableJob(f"missing {field}")
    url = value.strip()
    if not _is_absolute_https(url):
        raise _UnusableJob(f"{field} is not an absolute https URL")
    return url


def _is_absolute_https(value: str) -> bool:
    parts = urlsplit(value)
    return parts.scheme == "https" and bool(parts.netloc)


def _names(value: object) -> list[str]:
    """Nonblank ``name`` fields of a provider collection such as ``offices`` or ``departments``."""
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            name = item.get("name")
            if isinstance(name, str) and name.strip():
                names.append(name)
    return names


def _first_name(value: object) -> str | None:
    names = _names(value)
    return names[0] if names else None


def _locations(record: Mapping[str, object]) -> list[str]:
    location = record.get("location")
    display = location.get("name") if isinstance(location, Mapping) else None
    candidates = [display] if isinstance(display, str) and display.strip() else []
    candidates.extend(_names(record.get("offices")))

    locations: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        text = " ".join(candidate.split())
        key = text.casefold()
        if key and key not in seen:
            seen.add(key)
            locations.append(text)
    return locations


def _optional_text(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value).strip()
    return text or None


def _timestamp(value: object, field: str) -> datetime | None:
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
    return parsed


def _plain_text(content: object) -> str | None:
    if not isinstance(content, str) or not content.strip():
        return None
    document = content
    if "<" not in document and "&lt;" in document:
        # Some boards store the description as an escaped HTML document, so every tag arrives as
        # ``&lt;div&gt;``. One pass rehydrates the markup; entities inside the text are decoded
        # again by the parser, which is what those boards mean.
        document = html.unescape(document)
    extractor = _PlainTextExtractor()
    extractor.feed(document)
    extractor.close()
    return extractor.text()


def _apply_url(
    source: SourceConfig,
    *,
    tenant: str,
    source_job_id: str,
    posting_url: str,
) -> str:
    template = source.parameters.get(APPLY_URL_TEMPLATE_PARAMETER)
    if template is None:
        return posting_url
    if not isinstance(template, str) or not template.strip():
        raise AdapterError(
            f"greenhouse source {source.id} has an empty {APPLY_URL_TEMPLATE_PARAMETER}"
        )
    try:
        rendered = template.format(tenant=tenant, job_id=source_job_id).strip()
    except (IndexError, KeyError, ValueError) as exc:
        raise AdapterError(
            f"greenhouse source {source.id} has an unusable {APPLY_URL_TEMPLATE_PARAMETER}: {exc}"
        ) from exc
    if not _is_absolute_https(rendered):
        raise AdapterError(
            f"greenhouse source {source.id} {APPLY_URL_TEMPLATE_PARAMETER} "
            "did not render an absolute https URL"
        )
    return rendered


def _raw_metadata(record: Mapping[str, object]) -> dict[str, object]:
    """Provider bookkeeping kept for diagnosis; not part of job identity."""
    board_metadata = record.get("metadata")
    return {
        "internal_job_id": _optional_text(record.get("internal_job_id")),
        "department_names": _names(record.get("departments")),
        "office_names": _names(record.get("offices")),
        "board_metadata": [
            dict(item) for item in board_metadata if isinstance(item, Mapping)
        ]
        if isinstance(board_metadata, list)
        else [],
    }
