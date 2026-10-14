"""Explainable post-ingestion notification filters.

Every posting is ingested and stored whatever these rules say; a filter only decides whether a
matching posting produces a phone alert. Each filter therefore returns one :class:`FilterDecision`
that carries the filter id, whether it matched, whether a notification is authorized, the
priority, one human-readable reason, and any quiet-hours deferral.

Normalization
-------------

Text is Unicode NFKC-normalized, casefolded, and stripped of runs of whitespace before any
comparison, so ``"  Ｒｏｂｏｔｉｃｓ\u3000Soft "`` and ``"robotics  soft"`` compare equal. Reason
strings quote the normalized needle, so a configured ``"Sales"`` is reported as
``excluded title keyword: sales``.

Evaluation order and reasons
----------------------------

Steps run in the order below and the first decisive step ends the evaluation:

1. ``enabled`` — a disabled filter returns ``filter disabled: <id>`` and never matches.
2. ``include_companies`` — normalized equality; a miss returns ``company not included: <id>``,
   a hit records ``company included: <id>``.
3. ``exclude_title_keywords`` — any normalized substring match returns
   ``excluded title keyword: <term>``. An exclusion match is never overturned by any inclusion,
   because it is decided before every inclusion other than the company list.
4. ``include_title_keywords`` — a miss returns ``no included title keyword matched: <terms>``,
   a hit records ``included title keyword: <term>``. ``require_title_keywords`` is a second title
   list checked right after it and ANDed with it: a miss returns
   ``no required title keyword matched: <terms>``, a hit ``required title keyword: <term>``.
5. ``include_locations`` — a configured needle that appears in any of the posting's locations;
   a miss returns ``no included location matched: <terms>``, a hit ``included location: <term>``.
   ``countries`` follows: at least one location must be placed in a listed country by
   ``job_alert.geo``. No location returns ``location unknown, expected: <codes>``, only unplaceable
   ones ``location country unknown, expected: <codes>``, and other countries
   ``location not in included countries: <codes>``.
6. ``workplace_types`` — compared against the vocabulary below; a posting with no workplace type
   returns ``workplace type unknown, expected one of: <terms>``, an unlisted one
   ``workplace type not included: <type>``, and a hit ``included workplace type: <type>``.
7. ``departments`` — ``department not included: <value>`` or ``included department: <value>``.
8. ``categories`` — the company's configured categories, supplied by the caller; with none
   supplied the filter refuses to match and returns ``company categories unavailable``, a miss
   returns ``no included category matched: <terms>``, a hit ``included category: <term>``.
9. ``employment_types`` — ``employment type not included: <value>`` or
   ``included employment type: <value>``.
10. ``description_keywords`` — ``no included description keyword matched: <terms>`` or
    ``included description keyword: <term>``.

Every inclusion list that is empty imposes no restriction, and a filter that matched without any
positive evidence reports ``no inclusion restrictions configured`` — or
``no excluded title keyword matched`` when it configured exclusions only.

Workplace-type vocabulary
-------------------------

Adapters disagree about casing and spelling, so both sides of the comparison are canonicalized
into this vocabulary instead of trusting a provider's convention:

``remote``
    Ashby publishes ``Remote``, Lever and SmartRecruiters publish ``remote``, and the schema.org
    JSON-LD adapter publishes ``TELECOMMUTE`` (also accepted as ``telecommute``/``telecommuting``).
``hybrid``
    Ashby publishes ``Hybrid``; Lever and SmartRecruiters publish ``hybrid``.
``on-site``
    Ashby publishes ``OnSite``, also accepted as ``onsite`` or ``on site``. Nothing else emits it:
    no provider in the current registry reports an explicit on-site posting.

``None`` or blank means the board said nothing about the workplace, which is not the same as
on-site and never satisfies a workplace rule. A label outside the vocabulary keeps its normalized
form, so a new provider spelling still compares literally against a configured value; adapters
that emit workplace types should reuse one of the three canonical terms above.

Quiet hours
-----------

Quiet hours apply to matches, not to storage. The filter's own IANA ``timezone`` wins for reading
the window; without one the window is read in UTC rather than in the host's local time. A start
after the end wraps past midnight, the window is half-open (``start <= now < end``), and a match
inside the window receives ``defer_until``: the next instant at which the local clock reads
``quiet_hours_end``, keeping the scheduler from delivering before the window closes. Crossings and
gaps are resolved against the zone's real offsets, so a window spanning a DST transition ends at
the wall-clock end time in the offset in force then, and an end time that a spring-forward jump
skips defers to the moment the clock jumps past it. A filter without quiet hours never defers.

Decisions are deterministic: the same job, filters, and evaluation instant always produce the
same decisions in the same order.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

from job_alert.config import FilterConfig, FilterPriority
from job_alert.geo import location_country
from job_alert.models import NormalizedJob, StrictModel

#: The canonical workplace types a filter may name; see the module docstring for provider spellings.
WORKPLACE_TYPE_VOCABULARY: tuple[str, ...] = ("remote", "hybrid", "on-site")

#: Provider spellings (already normalized) mapped onto the vocabulary terms above.
_WORKPLACE_TYPE_ALIASES: dict[str, str] = {
    "remote": "remote",
    "fully remote": "remote",
    "telecommute": "remote",
    "telecommuting": "remote",
    "hybrid": "hybrid",
    "onsite": "on-site",
    "on-site": "on-site",
    "on site": "on-site",
}

_WHITESPACE = re.compile(r"\s+")

# A quiet window ends within a day of any evaluation instant; three days leaves room for the
# wall-clock time that a DST transition skips on the first day.
_QUIET_WINDOW_SEARCH_DAYS = 3


def normalize_text(value: str) -> str:
    """NFKC-normalize, casefold, and collapse whitespace so comparisons ignore cosmetics."""
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip().casefold()


def canonical_workplace_type(value: str | None) -> str | None:
    """A provider's workplace label as a documented vocabulary term, or ``None`` when absent."""
    if value is None:
        return None
    normalized = normalize_text(value)
    if not normalized:
        return None
    return _WORKPLACE_TYPE_ALIASES.get(normalized, normalized)


class FilterDecision(StrictModel):
    """What one filter concluded about one job, and why.

    ``notify`` is the authorization to alert: it is only true when the filter matched and the
    record has ``notify`` enabled. ``defer_until`` is set when a match fell inside the filter's
    quiet hours, so the scheduler can hold the event until the window closes without dropping it.
    """

    filter_id: str
    matched: bool
    notify: bool
    reason: str
    priority: FilterPriority = FilterPriority.NORMAL
    defer_until: datetime | None = None


def evaluate_filters(
    job: NormalizedJob,
    filters: Sequence[FilterConfig],
    *,
    company_categories: Sequence[str] = (),
    now: datetime | None = None,
) -> list[FilterDecision]:
    """Evaluate every filter against one job, in the order the filters are configured.

    ``company_categories`` are the categories of ``job.company_id`` from the company registry,
    which the normalized job does not carry itself. ``now`` is the instant the event is created —
    the runner passes the event's detection time — and defaults to the current UTC instant; a naive
    value is read as UTC, matching the storage layer.
    """
    evaluated_at = _as_utc(now) if now is not None else datetime.now(UTC)
    categories = tuple(normalize_text(category) for category in company_categories)
    return [
        _evaluate_filter(job, record, categories=categories, evaluated_at=evaluated_at)
        for record in filters
    ]


def _evaluate_filter(
    job: NormalizedJob,
    record: FilterConfig,
    *,
    categories: tuple[str, ...],
    evaluated_at: datetime,
) -> FilterDecision:
    if not record.enabled:
        return _rejected(record, f"filter disabled: {record.id}")

    evidence: str | None = None

    companies = _terms(record.include_companies)
    if companies:
        if normalize_text(job.company_id) not in companies:
            return _rejected(record, f"company not included: {job.company_id}")
        evidence = f"company included: {job.company_id}"

    exclusions = _terms(record.exclude_title_keywords)
    title = normalize_text(job.title)
    for exclusion in exclusions:
        if exclusion in title:
            return _rejected(record, f"excluded title keyword: {exclusion}")

    title_keywords = _terms(record.include_title_keywords)
    if title_keywords:
        hit = _first_match(title_keywords, (title,))
        if hit is None:
            return _rejected(record, f"no included title keyword matched: {_join(title_keywords)}")
        evidence = evidence or f"included title keyword: {hit}"

    required_title_keywords = _terms(record.require_title_keywords)
    if required_title_keywords:
        hit = _first_match(required_title_keywords, (title,))
        if hit is None:
            return _rejected(
                record, f"no required title keyword matched: {_join(required_title_keywords)}"
            )
        evidence = evidence or f"required title keyword: {hit}"

    locations = _terms(record.include_locations)
    if locations:
        haystack = tuple(normalize_text(location) for location in job.locations)
        hit = _first_match(locations, haystack)
        if hit is None:
            return _rejected(record, f"no included location matched: {_join(locations)}")
        evidence = evidence or f"included location: {hit}"

    countries = _terms(record.countries)
    if countries:
        wanted = _join(countries)
        if not job.locations:
            return _rejected(record, f"location unknown, expected: {wanted}")
        placed = {location_country(location) for location in job.locations}
        hit = next((country for country in countries if country in placed), None)
        if hit is None:
            if placed == {None}:
                return _rejected(record, f"location country unknown, expected: {wanted}")
            return _rejected(record, f"location not in included countries: {wanted}")
        evidence = evidence or f"included country: {hit}"

    workplace_types = _canonical_terms(record.workplace_types)
    if workplace_types:
        published = canonical_workplace_type(job.workplace_type)
        if published is None:
            return _rejected(
                record, f"workplace type unknown, expected one of: {_join(workplace_types)}"
            )
        if published not in workplace_types:
            return _rejected(record, f"workplace type not included: {published}")
        evidence = evidence or f"included workplace type: {published}"

    departments = _terms(record.departments)
    if departments:
        department = normalize_text(job.department) if job.department else ""
        hit = _first_match(departments, (department,))
        if hit is None:
            return _rejected(record, f"department not included: {department or 'unknown'}")
        evidence = evidence or f"included department: {department}"

    wanted_categories = _terms(record.categories)
    if wanted_categories:
        if not categories:
            return _rejected(record, "company categories unavailable")
        hit = _first_match(wanted_categories, categories)
        if hit is None:
            return _rejected(record, f"no included category matched: {_join(wanted_categories)}")
        evidence = evidence or f"included category: {hit}"

    employment_types = _terms(record.employment_types)
    if employment_types:
        employment_type = normalize_text(job.employment_type) if job.employment_type else ""
        hit = _first_match(employment_types, (employment_type,))
        if hit is None:
            return _rejected(
                record, f"employment type not included: {employment_type or 'unknown'}"
            )
        evidence = evidence or f"included employment type: {employment_type}"

    description_keywords = _terms(record.description_keywords)
    if description_keywords:
        description = normalize_text(job.description_text) if job.description_text else ""
        hit = _first_match(description_keywords, (description,))
        if hit is None:
            return _rejected(
                record, f"no included description keyword matched: {_join(description_keywords)}"
            )
        evidence = evidence or f"included description keyword: {hit}"

    if evidence is None:
        reason = (
            "no excluded title keyword matched"
            if exclusions
            else "no inclusion restrictions configured"
        )
    else:
        reason = evidence
    return FilterDecision(
        filter_id=record.id,
        matched=True,
        notify=record.notify,
        reason=reason,
        priority=record.priority,
        defer_until=_quiet_hours_defer_until(record, evaluated_at),
    )


def _rejected(record: FilterConfig, reason: str) -> FilterDecision:
    """A decision that did not match, which therefore never authorizes a notification."""
    return FilterDecision(filter_id=record.id, matched=False, notify=False, reason=reason)


def _terms(values: Iterable[str]) -> tuple[str, ...]:
    """Configured terms, normalized, with blanks dropped so none of them can match everything."""
    return tuple(term for term in (normalize_text(value) for value in values) if term)


def _canonical_terms(values: Iterable[str]) -> tuple[str, ...]:
    """Configured terms mapped onto the documented vocabulary, blanks and duplicates dropped."""
    canonical: dict[str, None] = {}
    for value in values:
        term = canonical_workplace_type(value)
        if term is not None:
            canonical.setdefault(term, None)
    return tuple(canonical)


def _first_match(needles: tuple[str, ...], haystack: tuple[str, ...]) -> str | None:
    """The first configured needle found in any haystack string, normalizing nothing further."""
    return next((needle for needle in needles if any(needle in value for value in haystack)), None)


def _join(terms: tuple[str, ...]) -> str:
    return ", ".join(terms)


def _as_utc(moment: datetime) -> datetime:
    """Read a naive timestamp as UTC and convert an aware one, matching the storage layer."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _quiet_hours_defer_until(record: FilterConfig, evaluated_at: datetime) -> datetime | None:
    """When a match falls inside the filter's quiet hours, the instant delivery may resume."""
    start = _clock_time(record.quiet_hours_start)
    end = _clock_time(record.quiet_hours_end)
    if start is None or end is None:
        return None
    zone: tzinfo = ZoneInfo(record.timezone) if record.timezone else UTC
    local = evaluated_at.astimezone(zone)
    minutes = local.hour * 60 + local.minute
    start_minutes = start.hour * 60 + start.minute
    end_minutes = end.hour * 60 + end.minute
    if start_minutes < end_minutes:
        quiet = start_minutes <= minutes < end_minutes
    else:
        # A window whose start is later than its end wraps past midnight, e.g. 22:00-07:00.
        quiet = minutes >= start_minutes or minutes < end_minutes
    if not quiet:
        return None
    return _next_window_end(evaluated_at, end, zone)


def _clock_time(value: str | None) -> time | None:
    """The clock time a validated ``FilterConfig`` field names."""
    return None if value is None else time.fromisoformat(value)


def _next_window_end(evaluated_at: datetime, end: time, zone: tzinfo) -> datetime:
    """The next instant at which the local clock in ``zone`` reads ``end``."""
    local_date = evaluated_at.astimezone(zone).date()
    for day_offset in range(_QUIET_WINDOW_SEARCH_DAYS):
        day = local_date + timedelta(days=day_offset)
        naive = datetime.combine(day, end)
        realized: list[datetime] = []
        skipped: list[datetime] = []
        for fold in (0, 1):
            instant = naive.replace(tzinfo=zone, fold=fold).astimezone(UTC)
            # A wall-clock reading that survives a round trip really happens; the other fold of a
            # spring-forward gap is the reading the clock never shows.
            if instant.astimezone(zone).replace(tzinfo=None) == naive:
                realized.append(instant)
            else:
                skipped.append(instant)
        later = sorted(instant for instant in realized if instant > evaluated_at)
        if later:
            return later[0]
        if skipped:
            resolved = _clock_reaches(min(skipped), max(skipped), end, zone)
            if resolved > evaluated_at:
                return resolved
    raise AssertionError(
        f"no occurrence of {end} within {_QUIET_WINDOW_SEARCH_DAYS} days in {zone}"
    )


def _clock_reaches(low: datetime, high: datetime, end: time, zone: tzinfo) -> datetime:
    """The earliest instant in ``(low, high]`` whose local clock is at or past ``end``.

    ``low`` and ``high`` bracket a spring-forward gap, so the wall clock never runs backwards
    between them and bisection lands on the transition, which is where the window really ends.
    """
    while high - low > timedelta(seconds=1):
        middle = low + (high - low) / 2
        if middle.astimezone(zone).time() >= end:
            high = middle
        else:
            low = middle
    return high
