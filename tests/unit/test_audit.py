"""Coverage invariants and audit helpers for the company-by-company source audit."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
import yaml

from job_alert.audit import (
    PROVIDER_ADAPTERS,
    AuditError,
    coverage_summary,
    disable_source_record,
    fingerprint_provider,
    load_source_records,
    probe_source,
    write_sources_yaml,
)
from job_alert.config import AppConfig
from job_alert.fetch import HttpFetcher
from job_alert.models import (
    CompanyTarget,
    SourceClass,
    SourceConfig,
    SupportStatus,
)

GREENHOUSE_FIXTURE = Path("tests/fixtures/greenhouse/jobs.json")


def _source(
    source_id: str,
    company_id: str,
    status: SupportStatus,
    provider: str = "discovery",
) -> SourceConfig:
    return SourceConfig(
        id=source_id,
        company_id=company_id,
        provider=provider,
        source_class=SourceClass.FALLBACK,
        support_status=status,
        endpoint="https://example.com/careers",
        interval_minutes=30,
        enabled=False,
    )


def test_coverage_counts_each_company_once_by_best_status(app_config) -> None:
    summary = coverage_summary(app_config)
    assert summary.total_companies == len(app_config.companies)
    assert sum(summary.by_status.values()) == summary.total_companies


def test_broad_activation_requires_ninety_percent_direct_coverage(app_config) -> None:
    summary = coverage_summary(app_config)
    assert summary.broad_activation_allowed is (
        summary.direct_supported / summary.total_companies >= 0.90
    )


def test_coverage_prefers_the_best_status_of_every_company_source() -> None:
    config = AppConfig(
        companies=[
            CompanyTarget(id="acme", name="Acme", categories=["big-tech-ai"]),
            CompanyTarget(id="globex", name="Globex", categories=["big-tech-ai"]),
        ],
        sources=[
            _source("acme-blocked", "acme", SupportStatus.BLOCKED),
            _source("acme-board", "acme", SupportStatus.SUPPORTED, provider="greenhouse"),
            _source("globex-review", "globex", SupportStatus.NEEDS_MANUAL_REVIEW),
        ],
        filters=[],
    )

    summary = coverage_summary(config)

    assert summary.total_companies == 2
    assert summary.by_status[SupportStatus.SUPPORTED] == 1
    assert summary.by_status[SupportStatus.NEEDS_MANUAL_REVIEW] == 1
    assert summary.by_status[SupportStatus.BLOCKED] == 0
    assert summary.direct_supported == 1
    assert summary.broad_activation_allowed is False


def test_provider_adapters_cover_the_four_documented_providers() -> None:
    assert sorted(PROVIDER_ADAPTERS) == ["ashby", "greenhouse", "lever", "smartrecruiters"]
    for provider, adapter in PROVIDER_ADAPTERS.items():
        assert adapter.provider == provider


def test_fingerprint_detects_only_positive_provider_evidence() -> None:
    greenhouse = fingerprint_provider(
        final_url="https://boards.greenhouse.io/acme",
        media_type="text/html",
        body=b"<html><body>Careers</body></html>",
    )
    assert greenhouse.provider == "greenhouse"
    assert greenhouse.tenant == "acme"
    assert greenhouse.feed_url == (
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    )

    embedded = fingerprint_provider(
        final_url="https://www.example.com/careers",
        media_type="text/html",
        body=b'<a href="https://jobs.ashbyhq.com/Example%20Corp">Open roles</a>',
    )
    assert embedded.provider == "ashby"
    assert embedded.tenant == "Example Corp"

    ambiguous = fingerprint_provider(
        final_url="https://www.example.com/careers",
        media_type="text/html",
        body=b"<html><body>We are hiring! Send a resume to jobs@example.com.</body></html>",
    )
    assert ambiguous.provider is None
    assert ambiguous.feed_url is None


def test_fingerprint_recognises_jsonld_job_postings() -> None:
    result = fingerprint_provider(
        final_url="https://www.example.com/careers",
        media_type="text/html",
        body=(
            b'<script type="application/ld+json">'
            b'{"@type": "JobPosting", "title": "Engineer"}</script>'
        ),
    )
    assert result.provider == "json-ld"


def test_write_sources_yaml_is_stable_ordered_and_atomic(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    sources = [
        SourceConfig(
            id="acme-greenhouse",
            company_id="acme",
            provider="greenhouse",
            source_class=SourceClass.DOCUMENTED_API,
            support_status=SupportStatus.SUPPORTED,
            endpoint="https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true",
            interval_minutes=30,
            tenant="acme",
            parameters={"audit_evidence": "careers page redirects to the board"},
            enabled=True,
            audit_date="2026-09-18",
        )
    ]

    write_sources_yaml(path, sources)

    assert list(yaml.safe_load(path.read_text(encoding="utf-8"))["sources"][0]) == [
        "id",
        "company_id",
        "provider",
        "source_class",
        "support_status",
        "endpoint",
        "interval_minutes",
        "tenant",
        "parameters",
        "enabled",
        "audit_date",
    ]
    assert load_source_records(path) == sources
    assert list(tmp_path.iterdir()) == [path]


def test_write_sources_yaml_leaves_the_previous_file_when_validation_fails(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sources.yaml"
    write_sources_yaml(path, [_source("acme-discovery", "acme", SupportStatus.NEEDS_MANUAL_REVIEW)])
    before = path.read_text(encoding="utf-8")

    broken = _source("acme-broken", "acme", SupportStatus.SUPPORTED).model_copy(
        update={"interval_minutes": 3}
    )
    with pytest.raises(AuditError):
        write_sources_yaml(path, [broken])

    assert path.read_text(encoding="utf-8") == before


def test_disable_source_record_preserves_evidence_and_reason(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    write_sources_yaml(
        path,
        [
            SourceConfig(
                id="acme-greenhouse",
                company_id="acme",
                provider="greenhouse",
                source_class=SourceClass.DOCUMENTED_API,
                support_status=SupportStatus.SUPPORTED,
                endpoint="https://boards-api.greenhouse.io/v1/boards/acme/jobs",
                interval_minutes=30,
                parameters={"audit_evidence": "board returned 42 jobs"},
                enabled=True,
            )
        ],
    )

    disabled = disable_source_record(
        path,
        source_id="acme-greenhouse",
        status=SupportStatus.BLOCKED,
        reason="challenge page detected",
        evidence={"http_status": 403, "final_url": "https://boards.greenhouse.io/acme"},
    )

    assert disabled.enabled is False
    assert disabled.support_status is SupportStatus.BLOCKED
    assert disabled.parameters["audit_evidence"] == "board returned 42 jobs"
    assert disabled.parameters["audit_reason"] == "challenge page detected"
    assert disabled.parameters["audit_http_status"] == 403
    assert disabled.audit_date is not None
    assert load_source_records(path) == [disabled]


def test_disable_source_record_rejects_an_unknown_source(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    write_sources_yaml(path, [])

    with pytest.raises(AuditError, match="unknown source id ghost"):
        disable_source_record(
            path,
            source_id="ghost",
            status=SupportStatus.BLOCKED,
            reason="gone",
        )


@respx.mock
@pytest.mark.asyncio
async def test_probe_verifies_a_board_that_the_careers_page_links_to() -> None:
    payload = json.loads(GREENHOUSE_FIXTURE.read_text(encoding="utf-8"))
    route = respx.get(
        url__regex=r"https://boards-api\.greenhouse\.io/v1/boards/acme/jobs.*"
    ).mock(return_value=httpx.Response(200, json=payload))
    respx.get("https://careers.example.com/").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text='<html><a href="https://boards.greenhouse.io/acme">Open roles</a></html>',
        )
    )
    source = _source("acme-discovery", "acme", SupportStatus.NEEDS_MANUAL_REVIEW)
    source = source.model_copy(update={"endpoint": "https://careers.example.com/"})

    async with HttpFetcher() as fetcher:
        result = await probe_source(source, fetcher)

    assert result.support_status is SupportStatus.SUPPORTED
    assert result.provider_fingerprint == "greenhouse"
    assert result.provider_feed_url == (
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    )
    assert result.parsed_job_count == len(payload["jobs"])
    assert result.pagination_complete is True
    assert result.stable_id_percent == 100.0
    assert result.direct_url_percent == 100.0
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_probe_records_a_refused_careers_page_as_blocked() -> None:
    respx.get("https://careers.example.com/").mock(return_value=httpx.Response(403))
    source = _source("acme-discovery", "acme", SupportStatus.NEEDS_MANUAL_REVIEW)
    source = source.model_copy(update={"endpoint": "https://careers.example.com/"})

    async with HttpFetcher() as fetcher:
        result = await probe_source(source, fetcher)

    assert result.support_status is SupportStatus.BLOCKED
    assert result.http_status == 403
    assert result.challenge_detected is True
    assert result.provider_fingerprint is None


@respx.mock
@pytest.mark.asyncio
async def test_probe_records_a_plain_careers_page_as_fallback_only() -> None:
    respx.get("https://careers.example.com/").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="<html><body>We are always hiring. Contact hr@example.com.</body></html>",
        )
    )
    source = _source("acme-discovery", "acme", SupportStatus.NEEDS_MANUAL_REVIEW)
    source = source.model_copy(update={"endpoint": "https://careers.example.com/"})

    async with HttpFetcher() as fetcher:
        result = await probe_source(source, fetcher)

    assert result.support_status is SupportStatus.FALLBACK_ONLY
    assert result.http_status == 200
    assert result.media_type == "text/html"
    assert result.provider_fingerprint is None
