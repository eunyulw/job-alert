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
  record and hiding another. A walk whose only defects are those is walked once more, and that
  second walk's verdict stands. A malformed record or the cap never earns a re-walk.

An adapter supplies ``fetch_page(page_index, offset)``, which returns the page's records and its
declared total (or ``None`` when a later page omits it), and ``normalize(record)``, which raises
``UnusableRecord`` for a record that cannot become a job.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

from job_alert.models import CollectionResult, NormalizedJob

FetchPage = Callable[[int, int], Awaitable[tuple[Sequence[object], int | None]]]
Normalize = Callable[[object], NormalizedJob]


class UnusableRecord(ValueError):
    """One provider record cannot be normalized, so the collection is incomplete."""


async def walk_scoped(
    *,
    source_id: str,
    fetch_page: FetchPage,
    normalize: Normalize,
    cap: int,
    max_pages: int,
    cap_advice: str,
) -> CollectionResult:
    """Walk a scoped search, and walk it once more if the board shifted underneath."""
    first, shifted = await _walk_once(
        source_id=source_id,
        fetch_page=fetch_page,
        normalize=normalize,
        cap=cap,
        max_pages=max_pages,
        cap_advice=cap_advice,
    )
    if first.complete or not shifted:
        return first
    second, _ = await _walk_once(
        source_id=source_id,
        fetch_page=fetch_page,
        normalize=normalize,
        cap=cap,
        max_pages=max_pages,
        cap_advice=cap_advice,
    )
    return second


async def _walk_once(
    *,
    source_id: str,
    fetch_page: FetchPage,
    normalize: Normalize,
    cap: int,
    max_pages: int,
    cap_advice: str,
) -> tuple[CollectionResult, bool]:
    jobs: list[NormalizedJob] = []
    warnings: list[str] = []
    structural = False
    seen: set[str] = set()
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
            except UnusableRecord as exc:
                structural = True
                warnings.append(f"skipped malformed job record at index {index}: {exc}")
                continue
            if job.source_job_id in seen:
                warnings.append(
                    f"skipped duplicate job record at index {index}: the id already appeared"
                )
                continue
            seen.add(job.source_job_id)
            jobs.append(job)
        if not records or (declared is not None and offset + len(records) >= declared):
            break
        if page_count >= max_pages:
            structural = True
            warnings.append(f"stopped after {page_count} pages: the page cap is reached")
            break
        offset += len(records)

    if declared is None:
        structural = True
        warnings.append("the search declared no total, so completeness cannot be judged")
    elif declared < cap and len(seen) != declared:
        warnings.append(
            f"collected {len(seen)} unique postings but the search declared {declared},"
            " so the collection is incomplete"
        )
    result = CollectionResult(
        source_id=source_id,
        complete=not warnings,
        jobs=jobs,
        warnings=warnings,
        page_count=page_count,
        declared_total=declared,
    )
    return result, not structural
