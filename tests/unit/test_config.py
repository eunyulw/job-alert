from pathlib import Path

import pytest

from job_alert.config import ConfigError, load_app_config


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
