"""schema.org `JobPosting` JSON-LD adapter.

A careers page that publishes its openings as `application/ld+json` is the last fallback before a
company cannot be monitored directly, so this adapter is deliberately narrow: it reads the
structured documents a page publishes and refuses to guess from the rendered markup around them.

Block discovery
---------------
Only `<script type="application/ld+json">` blocks are read. The media type is compared
case-insensitively and ignores a `;charset=` parameter; `text/javascript`, `application/json`, a
bare mention of `JobPosting` in body text, and an untyped script are all ignored. A block whose body
is not valid JSON produces a warning and the next block is read, and because an unreadable block may
have held a posting the collection is reported incomplete rather than complete.

Traversal
---------
Within a block the adapter visits the top-level object, a top-level array and its items, and every
`@graph` those nodes publish (a graph may itself be a list or a single object), to a bounded depth.
An object is selected when its `@type` is `JobPosting`, however it is spelled or namespaced, or when
its `@type` is a list that contains it. Selecting a document whose `@type` is a list keeps that list
as published.

Field mapping
-------------
- `identifier` (a string, a number, a `PropertyValue` object, or a list) is the posting id, recorded
  as `requisition_id` and used for the shared per-source key `json-ld:{tenant}:{id}`. Without one,
  identity falls back to the shared deterministic fingerprint, so the same posting keeps the same
  key on every collection.
- `title` must be nonblank, and `url` or `applicationUrl` must be present; `url` is the posting link
  and `applicationUrl` the application link, which defaults to the posting link.
- `jobLocation` entries become locations: a structured address reads as locality, region, country,
  and a named place reads as its name. `applicantLocationRequirements` entries add their name.
- `employmentType` is copied verbatim, joining a list with `, `. `jobLocationType: TELECOMMUTE`
  becomes the workplace type `remote`, the only workplace statement the schema.org vocabulary makes.
- `datePosted` becomes `source_published_at` and `validThrough` is kept in `raw_metadata` after both
  are validated as ISO 8601 values with a UTC offset. A malformed mapped timestamp makes the posting
  unusable, which makes the collection incomplete.
- `description` is flattened by the shared `html_to_text()`, and `hiringOrganization` is kept in
  `raw_metadata` for diagnosis.
- URLs are canonicalized (tracking parameters and fragments removed) after resolution. A relative
  reference resolves against the audited source origin and nothing else: a protocol-relative
  reference such as `//mirror.example/jobs` is refused rather than followed, since that would move
  the link or a request onto a host the page itself names.

Duplicates and completeness
---------------------------
Two documents that publish one identity and identical normalized content collapse to one job. Two
documents that publish one identity and different content make the collection fail outright: the
page disagrees with itself about what that posting is, so neither record can be trusted. A page
that publishes no `JobPosting` document at all, a challenge page (which the shared ``HttpFetcher``
raises ``ChallengeDetected`` for), an HTTP failure, and a 304 are all errors rather than an empty
successful board.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

from pydantic import HttpUrl, ValidationError

from job_alert.adapters.base import AdapterError
from job_alert.adapters.common import html_to_text, https_origin, optional_text, resolve_https_url
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.identity import build_dedupe_key, build_job_key, canonicalize_url, content_hash
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "json-ld"

JSONLD_MEDIA_TYPE = "application/ld+json"
JOB_POSTING_TYPE = "jobposting"

# A page may nest `@graph` inside `@graph`; the walk is bounded so a pathological document cannot
# turn block parsing into an unbounded traversal.
MAX_GRAPH_DEPTH = 8


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _UnusableJob(ValueError):
    """One posting cannot be normalized, so the collection is incomplete."""


@dataclass(frozen=True, slots=True)
class JsonLdExtraction:
    """Every `JobPosting` document a page publishes, plus the blocks that could not be read."""

    postings: list[Mapping[str, Any]]
    warnings: list[str]


class _JsonLdScripts(HTMLParser):
    """Collect the raw text of every `application/ld+json` script block, in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocks: list[str] = []
        self._capturing = False
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attributes = {name.lower(): value for name, value in attrs}
        if _is_jsonld_type(attributes.get("type")):
            self._capturing = True
            self._chunks = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._capturing:
            self.blocks.append("".join(self._chunks))
            self._capturing = False
            self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._chunks.append(data)

    def close(self) -> None:
        super().close()
        # A block whose closing tag never arrived still contributes the text that was read, and a
        # body that cannot be parsed is reported as a malformed block rather than silently dropped.
        if self._capturing:
            self.blocks.append("".join(self._chunks))
            self._capturing = False
            self._chunks = []


def _is_jsonld_type(value: str | None) -> bool:
    if value is None:
        return False
    return value.split(";", maxsplit=1)[0].strip().casefold() == JSONLD_MEDIA_TYPE


def extract_jsonld(document: str) -> JsonLdExtraction:
    """Every `JobPosting` document a page publishes, with the malformed blocks it skipped."""
    parser = _JsonLdScripts()
    parser.feed(document)
    parser.close()

    postings: list[Mapping[str, Any]] = []
    warnings: list[str] = []
    for index, block in enumerate(parser.blocks, start=1):
        text = block.strip()
        if not text:
            warnings.append(f"skipped empty JSON-LD script block {index}")
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            warnings.append(f"skipped malformed JSON-LD script block {index}: {exc}")
            continue
        postings.extend(_job_postings(payload))
    return JsonLdExtraction(postings=postings, warnings=warnings)


def extract_job_postings(document: str) -> list[Mapping[str, Any]]:
    """The `JobPosting` documents a page publishes; use ``extract_jsonld()`` for its warnings."""
    return extract_jsonld(document).postings


def _job_postings(payload: object) -> list[Mapping[str, Any]]:
    return [node for node in _candidate_nodes(payload) if _is_job_posting(node)]


def _candidate_nodes(value: object, *, depth: int = 0) -> Iterator[Mapping[str, Any]]:
    """Top-level objects, top-level arrays, and every `@graph` they publish, depth-bounded."""
    if isinstance(value, list):
        for item in value:
            yield from _candidate_nodes(item, depth=depth)
        return
    if not isinstance(value, Mapping):
        return
    yield value
    if depth >= MAX_GRAPH_DEPTH:
        return
    graph = value.get("@graph")
    if graph is not None:
        yield from _candidate_nodes(graph, depth=depth + 1)


def _is_job_posting(node: Mapping[str, Any]) -> bool:
    declared = node.get("@type")
    if isinstance(declared, str):
        return _normalized_type(declared) == JOB_POSTING_TYPE
    if isinstance(declared, list):
        return any(
            isinstance(item, str) and _normalized_type(item) == JOB_POSTING_TYPE
            for item in declared
        )
    return False


def _normalized_type(value: str) -> str:
    """A schema.org type name, accepting the `JobPosting` spelling and its full IRI."""
    return value.strip().rstrip("/").rsplit("/", maxsplit=1)[-1].casefold()


class JsonLdAdapter:
    """Normalize one careers page's `JobPosting` documents into jobs."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        origin = https_origin(str(source.endpoint))
        tenant = _tenant(source)
        payload = await fetcher.get(
            RequestSpec(
                url=str(source.endpoint),
                headers=dict(source.headers),
                expected="html",
            )
        )
        if payload.not_modified:
            raise AdapterError(f"json-ld returned 304 Not Modified for {payload.final_url}")

        extraction = extract_jsonld(payload.body.decode("utf-8", "replace"))
        if not extraction.postings:
            detail = (
                f" ({'; '.join(extraction.warnings)})" if extraction.warnings else ""
            )
            raise AdapterError(
                f"json-ld payload from {payload.final_url} publishes no JobPosting documents"
                f"{detail}"
            )

        first_seen_at = self._now()
        warnings = list(extraction.warnings)
        jobs: dict[str, NormalizedJob] = {}
        for index, posting in enumerate(extraction.postings):
            try:
                job = _normalize_job(
                    posting,
                    source=source,
                    tenant=tenant,
                    origin=origin,
                    first_seen_at=first_seen_at,
                )
            except _UnusableJob as exc:
                warnings.append(f"skipped malformed job posting at index {index}: {exc}")
                continue
            existing = jobs.get(job.canonical_job_key)
            if existing is None:
                jobs[job.canonical_job_key] = job
                continue
            if _stable_payload(existing) != _stable_payload(job):
                raise AdapterError(
                    f"json-ld payload from {payload.final_url} publishes conflicting records for "
                    f"identity {job.canonical_job_key}"
                )

        return CollectionResult(
            source_id=source.id,
            complete=not warnings,
            jobs=list(jobs.values()),
            warnings=warnings,
            page_count=1,
            # A JSON-LD page publishes no total, so completeness rests on the graph walk, the
            # duplicate rules above, and the block warnings, and not on a declared size.
            declared_total=None,
        )


def _stable_payload(job: NormalizedJob) -> dict[str, Any]:
    """Every normalized field except the volatile ones, so two records can be compared."""
    return job.model_dump(exclude={"first_seen_at", "raw_metadata"})


def _tenant(source: SourceConfig) -> str:
    """The board token that makes a source job id unique, or the audited endpoint's host."""
    if source.tenant is not None and source.tenant.strip():
        return source.tenant.strip()
    return https_origin(str(source.endpoint)).split("://", maxsplit=1)[-1]


def _normalize_job(
    posting: Mapping[str, Any],
    *,
    source: SourceConfig,
    tenant: str,
    origin: str,
    first_seen_at: datetime,
) -> NormalizedJob:
    title = _title(posting)
    posting_reference = _url_reference(posting.get("url"))
    apply_reference = _url_reference(posting.get("applicationUrl"))
    identifier = _identifier(posting.get("identifier"))
    if posting_reference is not None:
        posting_url = _resolve(posting_reference, origin=origin, field="url")
    elif apply_reference is not None:
        posting_url = _resolve(apply_reference, origin=origin, field="url")
    else:
        raise _UnusableJob("missing url")
    apply_url = (
        _resolve(apply_reference, origin=origin, field="applicationUrl")
        if apply_reference is not None
        else posting_url
    )
    locations = _dedupe(
        [*_place_locations(posting.get("jobLocation")), *_requirement_locations(
            posting.get("applicantLocationRequirements")
        )]
    )
    description_text = html_to_text(posting.get("description"))
    source_published_at = _timestamp(posting.get("datePosted"), "datePosted")
    valid_through = _timestamp(posting.get("validThrough"), "validThrough")
    source_job_id = identifier or _fallback_source_job_id(
        source=source,
        tenant=tenant,
        title=title,
        locations=locations,
        apply_url=apply_url,
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
                requisition_id=identifier,
                apply_url=apply_url,
                title=title,
                locations=locations,
                department=None,
            ),
            requisition_id=identifier,
            title=title,
            locations=locations,
            employment_type=_employment_type(posting.get("employmentType")),
            workplace_type=_workplace_type(posting.get("jobLocationType")),
            description_text=description_text,
            content_hash=content_hash(title, locations, description_text),
            # ``NormalizedJob`` types both URLs as ``HttpUrl``, so validate them here and let the
            # surrounding handler turn an unusable URL into an incomplete collection.
            posting_url=HttpUrl(posting_url),
            apply_url=HttpUrl(apply_url),
            source_published_at=source_published_at,
            first_seen_at=first_seen_at,
            raw_metadata=_raw_metadata(posting, valid_through=valid_through),
        )
    except ValidationError as exc:
        raise _UnusableJob("posting does not normalize into a job") from exc


def _fallback_source_job_id(
    *,
    source: SourceConfig,
    tenant: str,
    title: str,
    locations: list[str],
    apply_url: str,
) -> str:
    """The shared deterministic fingerprint, which is what an id-less posting is identified by.

    ``build_job_key()`` already defines that fingerprint; reusing it as the posting id keeps the
    fallback identity inside the shared rules instead of inventing a second digest, and keeps the
    per-source key in the ``json-ld:{tenant}:{id}`` shape.
    """
    return build_job_key(
        provider=PROVIDER,
        tenant=tenant,
        source_job_id=None,
        company_id=source.company_id,
        title=title,
        locations=locations,
        apply_url=apply_url,
    )


def _title(posting: Mapping[str, Any]) -> str:
    value = posting.get("title")
    if not isinstance(value, str) or not value.strip():
        raise _UnusableJob("missing title")
    return value


def _identifier(value: object) -> str | None:
    """A posting id published as a string, a number, a `PropertyValue` node, or a list."""
    if isinstance(value, list):
        for item in value:
            found = _identifier(item)
            if found is not None:
                return found
        return None
    if isinstance(value, Mapping):
        for key in ("value", "@id", "name"):
            found = _identifier(value.get(key))
            if found is not None:
                return found
        return None
    return optional_text(value)


def _url_reference(value: object) -> str | None:
    """A URL published as text, as a list, or as a node carrying `url` or `@id`."""
    if isinstance(value, list):
        for item in value:
            found = _url_reference(item)
            if found is not None:
                return found
        return None
    if isinstance(value, Mapping):
        for key in ("url", "@id"):
            found = _url_reference(value.get(key))
            if found is not None:
                return found
        return None
    return optional_text(value)


def _resolve(reference: str, *, origin: str, field: str) -> str:
    """Resolve and canonicalize one URL the page published, or describe why it is unusable."""
    try:
        return canonicalize_url(resolve_https_url(reference, origin=origin, field=field))
    except ValueError as exc:
        raise _UnusableJob(str(exc)) from exc


def _place_locations(value: object) -> list[str]:
    """One location string per `jobLocation` entry: a structured address, or a place name."""
    if isinstance(value, list):
        locations: list[str] = []
        for item in value:
            locations.extend(_place_locations(item))
        return locations
    if isinstance(value, Mapping):
        composed = _address_text(value.get("address"))
        if composed is not None:
            return [composed]
        name = optional_text(value.get("name"))
        return [name] if name is not None else []
    text = optional_text(value)
    return [text] if text is not None else []


def _requirement_locations(value: object) -> list[str]:
    """One location string per `applicantLocationRequirements` entry."""
    if isinstance(value, list):
        locations: list[str] = []
        for item in value:
            locations.extend(_requirement_locations(item))
        return locations
    if isinstance(value, Mapping):
        name = optional_text(value.get("name"))
        if name is not None:
            return [name]
        return _place_locations(value.get("address"))
    text = optional_text(value)
    return [text] if text is not None else []


def _address_text(value: object) -> str | None:
    """A `PostalAddress` as display text, skipping the parts the page left blank."""
    if isinstance(value, list):
        for item in value:
            found = _address_text(item)
            if found is not None:
                return found
        return None
    if not isinstance(value, Mapping):
        return None
    parts = [
        optional_text(value.get("addressLocality")),
        optional_text(value.get("addressRegion")),
        _country_text(value.get("addressCountry")),
    ]
    text = ", ".join(part for part in parts if part is not None)
    return text or None


def _country_text(value: object) -> str | None:
    """A country published as a code or as a `Country` node."""
    if isinstance(value, Mapping):
        return optional_text(value.get("name")) or optional_text(value.get("addressCountry"))
    return optional_text(value)


def _employment_type(value: object) -> str | None:
    if isinstance(value, list):
        parts = [text for text in (optional_text(item) for item in value) if text is not None]
        return ", ".join(parts) or None
    return optional_text(value)


def _workplace_type(value: object) -> str | None:
    """`TELECOMMUTE` is the only workplace statement the schema.org vocabulary makes."""
    candidates = value if isinstance(value, list) else [value]
    if any(
        text is not None and text.casefold() == "telecommute"
        for text in (optional_text(candidate) for candidate in candidates)
    ):
        return "remote"
    return None


def _organization_name(value: object) -> str | None:
    if isinstance(value, list):
        for item in value:
            name = _organization_name(item)
            if name is not None:
                return name
        return None
    if isinstance(value, Mapping):
        return optional_text(value.get("name"))
    return optional_text(value)


def _timestamp(value: object, field: str) -> datetime | None:
    """A schema.org date, which has to be ISO 8601 with a UTC offset when it is published."""
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


def _raw_metadata(
    posting: Mapping[str, Any], *, valid_through: datetime | None
) -> dict[str, object]:
    """Provider bookkeeping kept for diagnosis; not part of job identity."""
    return {
        "valid_through": valid_through.isoformat() if valid_through is not None else None,
        "hiring_organization": _organization_name(posting.get("hiringOrganization")),
        "job_location_type": optional_text(posting.get("jobLocationType")),
    }


def _dedupe(values: list[str]) -> list[str]:
    """Deduplicate case-insensitively in document order, keeping the published spelling."""
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = " ".join(value.split())
        key = normalized.casefold()
        if key and key not in seen:
            seen.add(key)
            deduped.append(normalized)
    return deduped
