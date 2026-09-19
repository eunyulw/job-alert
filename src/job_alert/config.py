from __future__ import annotations

import re
from datetime import time
from enum import StrEnum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from job_alert.models import CompanyTarget, SourceConfig


class ConfigError(ValueError):
    pass


# Quiet hours are whole minutes: the window computation reads ``hour`` and ``minute`` only, so a
# value it cannot honour (seconds, fractions, an offset) must never validate.
_CLOCK_TIME = re.compile(r"^\d{2}:\d{2}$")


class FilterPriority(StrEnum):
    """Notification urgency a filter asks for; delivery providers map these to their own."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class FilterConfig(BaseModel):
    """One notification filter: the criteria, the urgency, and the quiet window.

    Filters never remove a posting from storage; they only decide whether a match produces a
    phone alert. ``quiet_hours_start`` and ``quiet_hours_end`` are 24-hour ``HH:MM`` clock times
    in ``timezone`` (an IANA name). A match evaluated inside the window stays durable and carries
    a computed ``defer_until``; when quiet hours are set without a timezone the window is read in
    UTC, so a decision never depends on the clock of the machine running the collector.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    enabled: bool = True
    notify: bool = False
    include_companies: list[str] = Field(default_factory=list)
    include_title_keywords: list[str] = Field(default_factory=list)
    exclude_title_keywords: list[str] = Field(default_factory=list)
    include_locations: list[str] = Field(default_factory=list)
    workplace_types: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)
    description_keywords: list[str] = Field(default_factory=list)
    employment_types: list[str] = Field(default_factory=list)
    priority: FilterPriority = FilterPriority.NORMAL
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    timezone: str | None = None

    @field_validator(
        "include_companies",
        "include_title_keywords",
        "exclude_title_keywords",
        "include_locations",
        "workplace_types",
        "categories",
        "departments",
        "description_keywords",
        "employment_types",
    )
    @classmethod
    def _reject_blank_terms(cls, value: list[str]) -> list[str]:
        """A blank term would match every posting, so refuse it instead of over-notifying."""
        if any(not term.strip() for term in value):
            raise ValueError("filter terms must not be blank")
        return value

    @field_validator("quiet_hours_start", "quiet_hours_end")
    @classmethod
    def _validate_clock_time(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _CLOCK_TIME.match(value):
            raise ValueError("quiet hours must be HH:MM 24-hour clock times")
        try:
            time.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("quiet hours must be HH:MM 24-hour clock times") from exc
        return value

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError(f"unknown IANA timezone: {value}") from exc
        return value

    @model_validator(mode="after")
    def _validate_quiet_hours(self) -> FilterConfig:
        start = self.quiet_hours_start
        end = self.quiet_hours_end
        if (start is None) != (end is None):
            raise ValueError("quiet_hours_start and quiet_hours_end must be set together")
        if start is None or end is None:
            return self
        if time.fromisoformat(start) == time.fromisoformat(end):
            raise ValueError("quiet_hours_start and quiet_hours_end must differ")
        return self


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    companies: list[CompanyTarget]
    sources: list[SourceConfig]
    filters: list[FilterConfig]


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return value


def load_app_config(config_dir: Path) -> AppConfig:
    try:
        config = AppConfig(
            companies=_read_yaml(config_dir / "company_targets.yaml").get("companies", []),
            sources=_read_yaml(config_dir / "sources.yaml").get("sources", []),
            filters=_read_yaml(config_dir / "filters.yaml").get("filters", []),
        )
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc
    company_ids = {company.id for company in config.companies}
    if len(company_ids) != len(config.companies):
        raise ConfigError("duplicate company id")
    for source in config.sources:
        if source.company_id not in company_ids:
            raise ConfigError(f"unknown company_id {source.company_id}")
    source_company_ids = {source.company_id for source in config.sources}
    for company_id in sorted(company_ids - source_company_ids):
        raise ConfigError(f"missing source entry: {company_id}")
    return config
