from __future__ import annotations

from typing import Protocol

from job_alert.fetch import HttpFetcher
from job_alert.models import CollectionResult, SourceConfig


class AdapterError(RuntimeError):
    pass


class PartialCollectionError(AdapterError):
    pass


class SourceAdapter(Protocol):
    provider: str

    async def collect(
        self, source: SourceConfig, fetcher: HttpFetcher
    ) -> CollectionResult: ...


def validate_collection(result: CollectionResult) -> CollectionResult:
    if (
        result.complete
        and result.declared_total is not None
        and len(result.jobs) != result.declared_total
    ):
        raise AdapterError(
            f"declared {result.declared_total} jobs but parsed {len(result.jobs)}"
        )
    if not result.complete:
        raise PartialCollectionError("adapter returned an incomplete collection")
    return result


def job_metrics(result: CollectionResult) -> tuple[float | None, float | None]:
    """The stable-id and direct-link percentages of one collection, or ``None`` for an empty one.

    A posting has a stable id when its provider id is non-blank, and a direct link when both its
    posting URL and its application URL are HTTPS. The audit grades a source on these numbers and
    every collection cycle reports them, so both read this one definition.
    """
    if not result.jobs:
        return None, None
    total = len(result.jobs)
    stable = sum(1 for job in result.jobs if job.source_job_id.strip())
    direct = sum(
        1
        for job in result.jobs
        if str(job.posting_url).startswith("https://")
        and str(job.apply_url).startswith("https://")
    )
    return round(100.0 * stable / total, 2), round(100.0 * direct / total, 2)
