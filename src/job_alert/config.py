from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from job_alert.models import CompanyTarget, SourceConfig


class ConfigError(ValueError):
    pass


class FilterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    enabled: bool = True
    notify: bool = False
    include_companies: list[str] = Field(default_factory=list)
    include_title_keywords: list[str] = Field(default_factory=list)
    exclude_title_keywords: list[str] = Field(default_factory=list)
    include_locations: list[str] = Field(default_factory=list)
    workplace_types: list[str] = Field(default_factory=list)


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
