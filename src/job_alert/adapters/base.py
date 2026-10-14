from __future__ import annotations

from typing import Protocol

from job_alert.fetch import HttpFetcher
from job_alert.models import CollectionResult, SourceConfig

#: A source parameter naming the employment type its provider-side scope guarantees.
ASSERTED_EMPLOYMENT_TYPE = "asserted_employment_type"


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


def apply_source_assertions(result: CollectionResult, source: SourceConfig) -> CollectionResult:
    """Stamp what a source's provider-side scope guarantees onto every posting it collected.

    A source scoped to the provider's own internship classification (a Workday worker subtype,
    Google's ``employment_type=INTERN``, Apple's Students team) holds only internships, but titles
    there often omit "intern". ``asserted_employment_type`` records that guarantee, so the
    employment-type filter sees it. A text-search scope guarantees nothing and must not set it.
    """
    asserted = source.parameters.get(ASSERTED_EMPLOYMENT_TYPE)
    if asserted is None:
        return result
    if not isinstance(asserted, str) or not asserted.strip():
        raise AdapterError(
            f"source {source.id} has an unusable {ASSERTED_EMPLOYMENT_TYPE}: expected text"
        )
    jobs = [job.model_copy(update={"employment_type": asserted.strip()}) for job in result.jobs]
    return result.model_copy(update={"jobs": jobs})
