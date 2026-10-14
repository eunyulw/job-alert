from pathlib import Path

import pytest
from pydantic import ValidationError

from job_alert.config import AppConfig, ConfigError, FilterConfig, FilterPriority, load_app_config
from job_alert.models import TIER_INTERVAL_MINUTES, SourceClass


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


def _single_source_config(
    tmp_path: Path, *, source_class: str, interval: int, enabled: str
) -> None:
    (tmp_path / "company_targets.yaml").write_text(
        "companies:\n  - id: acme\n    name: Acme\n    categories: [big-tech-ai]\n"
    )
    (tmp_path / "sources.yaml").write_text(
        "sources:\n"
        "  - id: acme-board\n"
        "    company_id: acme\n"
        "    provider: greenhouse\n"
        f"    source_class: {source_class}\n"
        "    support_status: SUPPORTED\n"
        "    endpoint: https://example.com/jobs\n"
        f"    interval_minutes: {interval}\n"
        f"    enabled: {enabled}\n"
    )
    (tmp_path / "filters.yaml").write_text("filters: []\n")


def test_tier_intervals_follow_the_polling_policy() -> None:
    assert TIER_INTERVAL_MINUTES == {
        SourceClass.DOCUMENTED_API: 5,
        SourceClass.PUBLIC_ATS: 10,
        SourceClass.COMPANY_JSON: 15,
        SourceClass.HTML: 30,
        SourceClass.FALLBACK: 30,
    }


def test_load_config_rejects_an_enabled_source_polled_faster_than_its_tier(
    tmp_path: Path,
) -> None:
    _single_source_config(tmp_path, source_class="public_ats", interval=5, enabled="true")
    with pytest.raises(ConfigError, match="acme-board polls every 5 minutes.*public_ats.*10"):
        load_app_config(tmp_path)


def test_load_config_allows_a_slower_or_disabled_source(tmp_path: Path) -> None:
    # Slower than the tier is how an operator backs off a 429, so it stays legal.
    _single_source_config(tmp_path, source_class="documented_api", interval=30, enabled="true")
    assert load_app_config(tmp_path).sources[0].interval_minutes == 30
    # A disabled source is never polled, so its interval cannot overload anything.
    _single_source_config(tmp_path, source_class="public_ats", interval=5, enabled="false")
    assert load_app_config(tmp_path).sources[0].interval_minutes == 5


def test_every_enabled_committed_source_polls_at_its_tier_or_says_why_not(
    app_config: AppConfig,
) -> None:
    """The freshness objectives in the spec (6/11/16 minutes) hold only at the tier interval.

    A source may poll more slowly (a large whole-board walk, a 429 back-off), but only with a
    recorded ``slow_poll_reason``; nothing may poll faster than its tier.
    """
    unexplained = sorted(
        (source.id, source.interval_minutes, TIER_INTERVAL_MINUTES[source.source_class])
        for source in app_config.sources
        if source.enabled
        and source.interval_minutes != TIER_INTERVAL_MINUTES[source.source_class]
        and not (
            source.interval_minutes > TIER_INTERVAL_MINUTES[source.source_class]
            and str(source.parameters.get("slow_poll_reason", "")).strip()
        )
    )
    assert unexplained == []


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


def test_repository_filters_are_the_approved_internship_canary(app_config: AppConfig) -> None:
    records = {record.id: record for record in app_config.filters}
    assert set(records) == {
        "all-jobs-dry-run",
        "production-matches",
        "production-matches-intern-type",
        "expansion-matches",
        "expansion-matches-intern-type",
    }
    dry_run = records["all-jobs-dry-run"]
    assert dry_run.enabled is True and dry_run.notify is False
    assert dry_run.include_companies == [] and dry_run.include_title_keywords == []

    polled = {source.company_id for source in app_config.sources if source.enabled}
    canary = {"1x", "standard-bots", "intrinsic", "serve-robotics", "neuralink"}
    for pair in (
        ("production-matches", "production-matches-intern-type"),
        ("expansion-matches", "expansion-matches-intern-type"),
    ):
        # Each pair alerts on the same companies through a title rule and a type rule.
        assert set(records[pair[0]].include_companies) == set(records[pair[1]].include_companies)
    for record in records.values():
        if not record.notify:
            continue
        assert record.enabled is True
        assert record.quiet_hours_start is None and record.timezone is None
        # An allowlist keeps every alerting filter out of broad activation, and each company it
        # names must have an enabled source or LIVE refuses it.
        assert record.include_companies and set(record.include_companies) <= polled
        # Alerts are strictly US-only.
        assert record.countries == ["us"]
    assert canary <= set(records["production-matches"].include_companies)
    assert records["production-matches"].include_title_keywords == ["intern", "co-op", "student"]
    assert records["production-matches-intern-type"].employment_types == ["intern"]
    # Expansion companies alert only on internships that name robotics or mechanical work.
    expansion = records["expansion-matches"]
    assert expansion.require_title_keywords == ["intern", "co-op", "student"]
    assert {"robot", "mechanical"} <= set(expansion.include_title_keywords)
    assert records["expansion-matches-intern-type"].employment_types == ["intern"]
    assert not set(records["production-matches"].include_companies) & set(
        expansion.include_companies
    )
