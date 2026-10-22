"""The walk shared by adapters that read a *scoped*, paged search with a declared total.

Large company boards (amazon.jobs, Workday, Apple, Google) cannot be read whole: their search caps
the total it will page through, so each source is scoped to a provider-side query and its
completeness is judged against the total that query declares. Every such adapter walks the same
way, so the rules live here once:

- pages are requested until one comes back empty or the records read reach the declared total;
- the first page's declared total is the reference (Workday reports it on page one only);
- a declared total at or above the provider's cap cannot be read whole: the walk stops after one
  request and reports an incomplete collection;
- a malformed record, a duplicate id, or a count that misses the declared total makes the
  collection incomplete;
- these boards list newest first, so a posting published mid-walk shifts later pages, repeating one
  record and hiding another, and a board whose order is unstable between requests (Workday's) does
  the same with nothing published at all. A walk whose only defects are those is walked once
  more. A complete second walk stands; otherwise the two walks are pooled, and the pool is complete
  when both walks declared the same total and their unique ids reach it, since each walk hides
  different postings. ``DEFAULT_CLOSE_AFTER`` absences are needed to close a job, so a pool that
  holds a posting closed between the walks costs one cycle at most. The cap, the page cap, or a
  missing total never earns a re-walk; a malformed record earns one only when the adapter passes
  ``rewalk_malformed`` because its provider serves such records transiently;
- a board can also count postings it never serves. When both walks read exactly the same postings
  and fall short of the shared total by at most ``OVERCOUNT_SHARE`` of it, the shortfall is the
  board's count, not hidden postings, and the pool is complete. Measured 2026-10-22: Albertsons'
  Oracle search counted 401 and served the same 398 in date, relevance and title order. A board
  under 100 postings gets no such allowance, and neither do walks that disagree or that repeated a
  record at the start of a page that followed a full one, which is how a shifting board hides a
  posting it does have. (After a page that came back short, as Oracle's 199- and 198-row pages of
  200 do, the repeat is the provider's collapsed double listing; an adapter passes ``page_size`` so
  the walk can tell a short page from a full one.)
- a record the provider counts in its total but publishes nothing for (Workday serves some as a
  bare requisition id) is a placeholder: it makes no job and no defect, and counts toward the total
  once per requisition id.

An adapter supplies ``fetch_page(page_index, offset)``, which returns the page's records and its
declared total (or ``None`` when a later page omits it), and ``normalize(record)``, which raises
``UnusableRecord`` for a record that cannot become a job and ``Placeholder`` for one the total
counts but that holds no posting. A complete result's ``declared_total`` leaves placeholders out,
so it equals the jobs it carries.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from job_alert.models import CollectionResult, NormalizedJob

#: The share of a declared total that two identical walks may fall short of and still be complete.
OVERCOUNT_SHARE = 0.01

FetchPage = Callable[[int, int], Awaitable[tuple[Sequence[object], int | None]]]
Normalize = Callable[[object], NormalizedJob]


class UnusableRecord(ValueError):
    """One provider record cannot be normalized, so the collection is incomplete."""


class Placeholder(Exception):
    """A record the declared total counts that publishes no posting, keyed by its requisition."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass(frozen=True)
class _Walk:
    result: CollectionResult
    #: The walk's only defects are what a shifting board causes, so another walk can repair them.
    shifted: bool
    placeholders: frozenset[str]
    #: A record at the start of a page after a full one repeated one already read, the mark of a
    #: shifting board.
    boundary_repeat: bool = False


async def walk_scoped(
    *,
    source_id: str,
    fetch_page: FetchPage,
    normalize: Normalize,
    cap: int,
    max_pages: int,
    cap_advice: str,
    rewalk_malformed: bool = False,
    page_size: int | None = None,
) -> CollectionResult:
    """Walk a scoped search, and walk it once more if the board shifted underneath."""

    def walk() -> Awaitable[_Walk]:
        return _walk_once(
            source_id=source_id,
            fetch_page=fetch_page,
            normalize=normalize,
            cap=cap,
            max_pages=max_pages,
            cap_advice=cap_advice,
            rewalk_malformed=rewalk_malformed,
            page_size=page_size,
        )

    first = await walk()
    if first.result.complete or not first.shifted:
        return first.result
    second = await walk()
    if second.result.complete or not second.shifted:
        return second.result
    return _pool(first, second)


def _pool(first: _Walk, second: _Walk) -> CollectionResult:
    """Two shifted walks of one search, complete if their unique ids reach the shared total."""
    declared = second.result.declared_total
    if declared is None or first.result.declared_total != declared:
        return second.result
    jobs = {job.source_job_id: job for job in second.result.jobs}
    for job in first.result.jobs:
        jobs.setdefault(job.source_job_id, job)
    placeholders = (first.placeholders | second.placeholders) - jobs.keys()
    found = len(jobs) + len(placeholders)
    page_count = first.result.page_count + second.result.page_count
    first_ids = {job.source_job_id for job in first.result.jobs} | first.placeholders
    second_ids = {job.source_job_id for job in second.result.jobs} | second.placeholders
    overcounted = (
        first_ids == second_ids
        and not (first.boundary_repeat or second.boundary_repeat)
        and found < declared <= found + declared * OVERCOUNT_SHARE
    )
    if found == declared or overcounted:
        return CollectionResult(
            source_id=second.result.source_id,
            complete=True,
            jobs=list(jobs.values()),
            warnings=[],
            page_count=page_count,
            declared_total=len(jobs),
        )
    return second.result.model_copy(
        update={
            "warnings": [
                *second.result.warnings,
                f"collected {found} unique postings across two walks but the search declared"
                f" {declared}, so the collection is incomplete",
            ],
            "page_count": page_count,
        }
    )


async def _walk_once(
    *,
    source_id: str,
    fetch_page: FetchPage,
    normalize: Normalize,
    cap: int,
    max_pages: int,
    cap_advice: str,
    rewalk_malformed: bool,
    page_size: int | None,
) -> _Walk:
    jobs: list[NormalizedJob] = []
    warnings: list[str] = []
    structural = False
    seen: set[str] = set()
    placeholders: set[str] = set()
    boundary_repeat = False
    previous_full = False
    offset = 0
    page_count = 0
    declared: int | None = None

    while True:
        records, total = await fetch_page(page_count, offset)
        page_count += 1
        if declared is None:
            declared = total
        if declared is not None and declared >= cap:
            structural = True
            warnings.append(
                f"the search declares {declared} postings, the provider's cap, so it cannot be"
                f" read whole: {cap_advice}"
            )
            break
        for index_in_page, record in enumerate(records):
            index = offset + index_in_page
            try:
                job = normalize(record)
            except Placeholder as placeholder:
                if placeholder.key in placeholders:
                    warnings.append(
                        f"skipped duplicate job record at index {index}: the id already appeared"
                    )
                placeholders.add(placeholder.key)
                continue
            except UnusableRecord as exc:
                structural = structural or not rewalk_malformed
                warnings.append(f"skipped malformed job record at index {index}: {exc}")
                continue
            if job.source_job_id in seen:
                boundary_repeat = boundary_repeat or (index_in_page == 0 and previous_full)
                warnings.append(
                    f"skipped duplicate job record at index {index}: the id already appeared"
                )
                continue
            seen.add(job.source_job_id)
            jobs.append(job)
        previous_full = page_size is None or len(records) >= page_size
        if not records or (declared is not None and offset + len(records) >= declared):
            break
        if page_count >= max_pages:
            structural = True
            warnings.append(f"stopped after {page_count} pages: the page cap is reached")
            break
        offset += len(records)

    found = len(seen) + len(placeholders - seen)
    if declared is None:
        structural = True
        warnings.append("the search declared no total, so completeness cannot be judged")
    elif declared < cap and found != declared:
        warnings.append(
            f"collected {found} unique postings but the search declared {declared},"
            " so the collection is incomplete"
        )
    complete = not warnings
    result = CollectionResult(
        source_id=source_id,
        complete=complete,
        jobs=jobs,
        warnings=warnings,
        page_count=page_count,
        declared_total=len(jobs) if complete else declared,
    )
    return _Walk(
        result=result,
        shifted=not structural,
        placeholders=frozenset(placeholders),
        boundary_repeat=boundary_repeat,
    )
