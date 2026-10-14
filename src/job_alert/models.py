from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, UrlConstraints, model_validator

HttpsUrl = Annotated[HttpUrl, UrlConstraints(allowed_schemes=["https"])]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceClass(StrEnum):
    DOCUMENTED_API = "documented_api"
    PUBLIC_ATS = "public_ats"
    COMPANY_JSON = "company_json"
    HTML = "html"
    FALLBACK = "fallback"


#: The polling tier for each source class, in minutes. The spec's detection objectives (6, 11, and
#: 16 minutes) hold only at these intervals, and polling faster than the tier is never allowed.
#: Slower is allowed, because that is how an operator backs off a source that answers 429.
TIER_INTERVAL_MINUTES: dict[SourceClass, int] = {
    SourceClass.DOCUMENTED_API: 5,
    SourceClass.PUBLIC_ATS: 10,
    SourceClass.COMPANY_JSON: 15,
    SourceClass.HTML: 30,
    SourceClass.FALLBACK: 30,
}


class SupportStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    SUPPORTED_UNDOCUMENTED = "SUPPORTED_UNDOCUMENTED"
    HTML_ONLY = "HTML_ONLY"
    FALLBACK_ONLY = "FALLBACK_ONLY"
    BLOCKED = "BLOCKED"
    NEEDS_MANUAL_REVIEW = "NEEDS_MANUAL_REVIEW"


class RunOutcome(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    CHALLENGED = "challenged"
    QUARANTINED = "quarantined"


class JobEventType(StrEnum):
    NEW = "new"
    UPDATED = "updated"
    REOPENED = "reopened"
    CLOSED = "closed"


class CompanyTarget(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1)
    categories: list[str] = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)


class SourceConfig(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    company_id: str
    provider: str
    source_class: SourceClass
    support_status: SupportStatus
    endpoint: HttpsUrl
    interval_minutes: int = Field(ge=5, le=240)
    tenant: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    audit_date: str | None = None


class NormalizedJob(StrictModel):
    company_id: str
    source_id: str
    source_job_id: str
    canonical_job_key: str
    dedupe_key: str
    requisition_id: str | None = None
    title: str
    locations: list[str] = Field(default_factory=list)
    department: str | None = None
    team: str | None = None
    employment_type: str | None = None
    workplace_type: str | None = None
    description_text: str | None = None
    content_hash: str | None = None
    posting_url: HttpUrl
    apply_url: HttpUrl
    source_published_at: datetime | None = None
    source_updated_at: datetime | None = None
    first_seen_at: datetime
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


class CollectionResult(StrictModel):
    source_id: str
    complete: bool
    jobs: list[NormalizedJob]
    warnings: list[str] = Field(default_factory=list)
    page_count: int = Field(default=1, ge=0)
    declared_total: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def unique_keys(self) -> CollectionResult:
        keys = [job.canonical_job_key for job in self.jobs]
        if len(keys) != len(set(keys)):
            raise ValueError("collection contains duplicate canonical job keys")
        return self


class PollRun(StrictModel):
    id: int | None = None
    source_id: str
    started_at: datetime
    finished_at: datetime | None = None
    outcome: RunOutcome
    http_status: int | None = None
    jobs_seen: int = 0
    error_code: str | None = None
    error_message: str | None = None


class JobEvent(StrictModel):
    event_type: JobEventType
    job: NormalizedJob
    detected_at: datetime
