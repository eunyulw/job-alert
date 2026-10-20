from __future__ import annotations

from typing import Protocol

from job_alert.fetch import HttpFetcher
from job_alert.geo import explicit_location_country
from job_alert.models import CollectionResult, SourceConfig

#: A source parameter naming the employment type its provider-side scope guarantees.
ASSERTED_EMPLOYMENT_TYPE = "asserted_employment_type"
ASSERTED_COUNTRY = "asserted_country"


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

    ``asserted_country`` does the same for a board that only hires in one country but writes
    bare city names, so the US-only filter can place them.
    """
    jobs = result.jobs
    asserted = source.parameters.get(ASSERTED_EMPLOYMENT_TYPE)
    if asserted is not None:
        if not isinstance(asserted, str) or not asserted.strip():
            raise AdapterError(
                f"source {source.id} has an unusable {ASSERTED_EMPLOYMENT_TYPE}: expected text"
            )
        jobs = [job.model_copy(update={"employment_type": asserted.strip()}) for job in jobs]
    country = source.parameters.get(ASSERTED_COUNTRY)
    if country is not None:
        if not isinstance(country, str) or explicit_location_country(country) is None:
            raise AdapterError(
                f"source {source.id} has an unusable {ASSERTED_COUNTRY}: expected a country name"
            )
        jobs = [
            job.model_copy(update={"locations": _with_country(job.locations, country)})
            for job in jobs
        ]
    if jobs is result.jobs:
        return result
    return result.model_copy(update={"jobs": jobs})


def _with_country(locations: list[str], country: str) -> list[str]:
    """Name the country on every label that does not already place itself.

    For a board whose company hires in one country but labels places as bare cities ("San
    Francisco", "Greater Seattle Area", "Remote"). A label that names any country is kept as is.
    """
    if not locations:
        return [country]
    return [
        label if explicit_location_country(label) is not None else f"{label}, {country}"
        for label in locations
    ]
