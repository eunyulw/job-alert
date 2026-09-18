from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from job_alert.models import (
    CollectionResult,
    CompanyTarget,
    NormalizedJob,
    SourceClass,
    SourceConfig,
    SupportStatus,
)


def test_source_config_rejects_non_https_endpoint() -> None:
    with pytest.raises(ValidationError):
        SourceConfig(
            id="openai-greenhouse",
            company_id="openai",
            provider="greenhouse",
            source_class=SourceClass.DOCUMENTED_API,
            support_status=SupportStatus.SUPPORTED,
            endpoint="http://boards-api.greenhouse.io/v1/boards/openai/jobs",
            interval_minutes=5,
        )


def test_collection_result_requires_unique_job_keys() -> None:
    job = NormalizedJob(
        company_id="openai",
        source_id="openai-greenhouse",
        source_job_id="123",
        canonical_job_key="greenhouse:openai:123",
        dedupe_key="requisition:openai:research-123",
        title="Research Engineer",
        locations=["San Francisco, CA"],
        posting_url="https://example.com/jobs/123",
        apply_url="https://example.com/jobs/123/apply",
        first_seen_at=datetime(2026, 9, 18, tzinfo=UTC),
    )
    with pytest.raises(ValidationError):
        CollectionResult(source_id="openai-greenhouse", complete=True, jobs=[job, job])


def test_company_target_requires_at_least_one_category() -> None:
    with pytest.raises(ValidationError):
        CompanyTarget(id="openai", name="OpenAI", categories=[])
