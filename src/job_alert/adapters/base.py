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
