from pathlib import Path

import pytest
from pydantic import ValidationError

from job_alert.config import AppConfig, ConfigError, FilterConfig, FilterPriority, load_app_config


def test_load_config_rejects_unknown_company_reference(tmp_path: Path) -> None:
    (tmp_path / "company_targets.yaml").write_text(
        "companies:\n  - id: openai\n    name: OpenAI\n    categories: [big-tech-ai]\n"
    )
    (tmp_path / "sources.yaml").write_text(
        "sources:\n"
        "  - id: ghost-board\n"
        "    company_id: ghost\n"
        "    provider: greenhouse\n"
        "    source_class: documented_api\n"
        "    support_status: SUPPORTED\n"
        "    endpoint: https://example.com/jobs\n"
        "    interval_minutes: 5\n"
    )
    (tmp_path / "filters.yaml").write_text("filters: []\n")
    with pytest.raises(ConfigError, match="unknown company_id ghost"):
        load_app_config(tmp_path)


def test_load_config_requires_every_target_to_have_source_or_review_status(tmp_path: Path) -> None:
    (tmp_path / "company_targets.yaml").write_text(
        "companies:\n  - id: openai\n    name: OpenAI\n    categories: [big-tech-ai]\n"
    )
    (tmp_path / "sources.yaml").write_text("sources: []\n")
    (tmp_path / "filters.yaml").write_text("filters: []\n")
    with pytest.raises(ConfigError, match="missing source entry: openai"):
        load_app_config(tmp_path)


def test_filter_config_defaults_keep_the_new_production_fields_inert() -> None:
    record = FilterConfig(id="unused")
    assert record.priority is FilterPriority.NORMAL
    assert record.categories == []
    assert record.departments == []
    assert record.description_keywords == []
    assert record.employment_types == []
    assert record.quiet_hours_start is None
    assert record.quiet_hours_end is None
    assert record.timezone is None


def test_filter_config_rejects_a_priority_outside_the_vocabulary() -> None:
    with pytest.raises(ValidationError, match="priority"):
        FilterConfig(id="urgent-alerts", priority="emergency")


def test_filter_config_rejects_blank_terms() -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        FilterConfig(id="blank", include_title_keywords=["engineer", "   "])


def test_filter_config_requires_quiet_hours_to_be_paired() -> None:
    with pytest.raises(ValidationError, match="must be set together"):
        FilterConfig(id="half-window", quiet_hours_start="22:00")
    with pytest.raises(ValidationError, match="must be set together"):
        FilterConfig(id="half-window", quiet_hours_end="07:00")


def test_filter_config_rejects_equal_quiet_hours_bounds() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        FilterConfig(id="empty-window", quiet_hours_start="07:00", quiet_hours_end="07:00")


def test_filter_config_accepts_only_two_digit_hh_mm_quiet_hours() -> None:
    record = FilterConfig(id="valid-clock", quiet_hours_start="07:00", quiet_hours_end="22:30")
    assert record.quiet_hours_start == "07:00"
    assert record.quiet_hours_end == "22:30"

    # Seconds, fractions, offsets, and other shapes parse as clock times but cannot be honoured
    # by the window computation, which reads whole minutes; accepting them would silently widen
    # quiet hours and suppress real alerts.
    for value in (
        "07:00:30",
        "07:00:45",
        "07:00:00",
        "07:00:00.500",
        "07:00+01:00",
        "7:00",
        "0700",
        "7am",
    ):
        with pytest.raises(ValidationError, match="HH:MM"):
            FilterConfig(id="bad-clock", quiet_hours_start="22:00", quiet_hours_end=value)


def test_filter_config_rejects_an_unknown_timezone() -> None:
    with pytest.raises(ValidationError, match="unknown IANA timezone"):
        FilterConfig(id="bad-zone", timezone="Mars/Phobos")
    with pytest.raises(ValidationError, match="unknown IANA timezone"):
        FilterConfig(id="bad-zone", quiet_hours_start="22:00", quiet_hours_end="07:00", timezone="")


def test_repository_filters_stay_disabled_and_restriction_free(app_config: AppConfig) -> None:
    records = {record.id: record for record in app_config.filters}
    assert set(records) == {"all-jobs-dry-run", "production-matches"}
    for record in records.values():
        assert record.notify is False
        assert record.include_companies == []
        assert record.include_title_keywords == []
        assert record.exclude_title_keywords == []
        assert record.include_locations == []
        assert record.workplace_types == []
        assert record.categories == []
        assert record.departments == []
        assert record.description_keywords == []
        assert record.employment_types == []
        assert record.priority is FilterPriority.NORMAL
        assert record.quiet_hours_start is None
        assert record.quiet_hours_end is None
        assert record.timezone is None
    assert records["all-jobs-dry-run"].enabled is True
    assert records["production-matches"].enabled is False
