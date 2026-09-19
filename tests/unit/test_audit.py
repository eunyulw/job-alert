"""Coverage invariants and audit helpers for the company-by-company source audit."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
import yaml

from job_alert.audit import (
    PROBE_EVIDENCE_PARAMETERS,
    PROVIDER_ADAPTERS,
    AuditError,
    ProbeResult,
    coverage_summary,
    disable_source_record,
    fingerprint_provider,
    load_audit_store,
    load_source_records,
    probe_source,
    record_probe,
    render_report,
    run_audit,
    write_sources_yaml,
)
from job_alert.config import AppConfig, load_app_config
from job_alert.fetch import HttpFetcher
from job_alert.models import (
    CompanyTarget,
    SourceClass,
    SourceConfig,
    SupportStatus,
)

GREENHOUSE_FIXTURE = Path("tests/fixtures/greenhouse/jobs.json")
PROBE_TIME = datetime(2026, 9, 19, tzinfo=UTC)


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


def _write_config_dir(directory: Path) -> None:
    (directory / "company_targets.yaml").write_text(
        "companies:\n  - id: acme\n    name: Acme\n    categories: [big-tech-ai]\n",
        encoding="utf-8",
    )
    (directory / "sources.yaml").write_text(
        "sources:\n"
        "  - id: acme-discovery\n"
        "    company_id: acme\n"
        "    provider: discovery\n"
        "    source_class: fallback\n"
        "    support_status: NEEDS_MANUAL_REVIEW\n"
        "    endpoint: https://careers.acme.example/\n"
        "    interval_minutes: 30\n"
        "    enabled: false\n",
        encoding="utf-8",
    )
    (directory / "filters.yaml").write_text("filters: []\n", encoding="utf-8")


@respx.mock
def test_set_source_derives_enabled_from_the_status(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from job_alert.cli import app

    _write_config_dir(tmp_path)
    payload = json.loads(GREENHOUSE_FIXTURE.read_text(encoding="utf-8"))
    respx.get(
        url__regex=r"https://boards-api\.greenhouse\.io/v1/boards/acme/jobs.*"
    ).mock(return_value=httpx.Response(200, json=payload))
    runner = CliRunner()
    arguments = [
        "audit",
        "set-source",
        "--company",
        "acme",
        "--provider",
        "greenhouse",
        "--endpoint",
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
        "--class",
        "documented_api",
        "--config-dir",
        str(tmp_path),
    ]

    downgrade = runner.invoke(app, [*arguments, "--status", "NEEDS_MANUAL_REVIEW"])
    assert downgrade.exit_code == 0, downgrade.output
    assert "written disabled" in downgrade.output
    record = load_source_records(tmp_path / "sources.yaml")[0]
    assert record.support_status is SupportStatus.NEEDS_MANUAL_REVIEW
    assert record.enabled is False

    supported = runner.invoke(app, [*arguments, "--status", "SUPPORTED"])
    assert supported.exit_code == 0, supported.output
    assert "written disabled" not in supported.output
    promoted = load_source_records(tmp_path / "sources.yaml")[0]
    assert promoted.support_status is SupportStatus.SUPPORTED
    assert promoted.enabled is True


@respx.mock
@pytest.mark.asyncio
async def test_run_audit_retry_failed_reprobes_a_recorded_failure(tmp_path: Path) -> None:
    _write_config_dir(tmp_path)
    results = tmp_path / "reports" / "source-audit.json"
    results.parent.mkdir(parents=True)
    results.write_text(
        json.dumps(
            {
                "generated_at": "2026-09-18T00:00:00Z",
                "companies": {
                    "acme": {
                        "company_id": "acme",
                        "support_status": "BLOCKED",
                        "probe": {"notes": ["access challenge detected"]},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    respx.get("https://careers.acme.example/").mock(
        return_value=httpx.Response(
            200, headers={"Content-Type": "text/html"}, text="<html>Careers</html>"
        )
    )

    probed = await run_audit(tmp_path, results, retry_failed=True)

    assert probed == ["acme"]
    store = json.loads(results.read_text(encoding="utf-8"))["companies"]
    assert store["acme"]["support_status"] == "FALLBACK_ONLY"


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


def test_fingerprint_rejects_successfactors_and_oracle_assets() -> None:
    jquery = fingerprint_provider(
        final_url=(
            "https://performancemanager5.successfactors.eu/verp/vmod_v1/ui/extlib/"
            "jquery_3.5.1/jquery.js"
        ),
        media_type="text/javascript",
        body=b"",
    )
    assert jquery.provider is None

    image = fingerprint_provider(
        final_url=(
            "https://ibqbjb.fa.ocs.oraclecloud.com:443/hcmUI/CandidateExperience/images"
            "?imageId=F6FFEC9C-0349-4FF4-BC8E-34EA5672CC7A"
        ),
        media_type="text/html",
        body=b"",
    )
    assert image.provider is None

    errors_host = fingerprint_provider(
        final_url="https://77e708048c4747e1a801fa2ef5557ca6@vs-errors.eightfold.ai/10",
        media_type="text/html",
        body=b"",
    )
    assert errors_host.provider is None


def test_fingerprint_still_accepts_the_listed_enterprise_markers() -> None:
    successfactors = fingerprint_provider(
        final_url="https://career4.successfactors.com/career?company=example",
        media_type="text/html",
        body=b"",
    )
    assert successfactors.provider == "successfactors"

    oracle = fingerprint_provider(
        final_url=(
            "https://hdjq.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/jobs"
        ),
        media_type="text/html",
        body=b"",
    )
    assert oracle.provider == "oracle"

    eightfold = fingerprint_provider(
        final_url="https://example.eightfold.ai/careers",
        media_type="text/html",
        body=b"",
    )
    assert eightfold.provider == "eightfold"


@respx.mock
@pytest.mark.asyncio
async def test_workday_without_application_links_is_not_supported() -> None:
    """A Workday walk that yields ids but no application URL must not earn support."""
    respx.get("https://careers.example.com/").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text='<a href="https://acme.wd1.myworkdayjobs.com/en-US/External">Jobs</a>',
        )
    )
    route = respx.post(
        url__regex=r"https://acme\.wd1\.myworkdayjobs\.com/wday/cxs/acme/External/jobs"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "jobPostings": [{"title": "Engineer", "externalPath": "/job/Engineer_R1"}],
            },
        )
    )
    source = _source("acme-discovery", "acme", SupportStatus.NEEDS_MANUAL_REVIEW)
    source = source.model_copy(update={"endpoint": "https://careers.example.com/"})

    async with HttpFetcher() as fetcher:
        result = await probe_source(source, fetcher)

    assert result.provider_fingerprint == "workday"
    assert result.support_status is SupportStatus.NEEDS_MANUAL_REVIEW
    assert result.pagination_complete is True
    assert result.stable_id_percent == 100.0
    assert result.direct_url_percent == 0.0
    assert any("direct application URL" in note for note in result.notes)
    assert route.call_count == 2


def test_report_renders_the_careers_landing_page_for_a_verified_source() -> None:
    feed = "https://api.lever.co/v0/postings/acme?mode=json"
    landing = "https://www.acme.example/careers"
    config = AppConfig(
        companies=[CompanyTarget(id="acme", name="Acme", categories=["big-tech-ai"])],
        sources=[
            SourceConfig(
                id="acme-lever",
                company_id="acme",
                provider="lever",
                source_class=SourceClass.DOCUMENTED_API,
                support_status=SupportStatus.SUPPORTED,
                endpoint=feed,
                interval_minutes=30,
                tenant="acme",
                enabled=True,
                audit_date="2026-09-18",
            )
        ],
        filters=[],
    )
    store = {
        "acme": {
            "company_id": "acme",
            "source_id": "acme-lever",
            "provider": "lever",
            "support_status": "SUPPORTED",
            "careers_url": landing,
            "audited_at": "2026-09-18",
            "probe": {"final_url": landing, "parsed_job_count": 12, "notes": []},
        }
    }

    text, payload = render_report(config, store, generated_at=datetime(2026, 9, 18, tzinfo=UTC))

    row = next(line for line in text.splitlines() if line.startswith("| Acme |"))
    assert landing in row
    assert feed not in row
    assert payload["rows"][0]["careers_url"] == landing


def _probe_result(source_id: str, status: SupportStatus, **overrides: object) -> ProbeResult:
    fields: dict[str, object] = {
        "source_id": source_id,
        "support_status": status,
        "evidence_at": PROBE_TIME,
        "http_status": 200,
        "final_url": "https://careers.acme.example/",
        "media_type": "text/html",
    }
    fields.update(overrides)
    return ProbeResult.model_validate(fields)


def _seed_store(path: Path, company_id: str, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-09-18T00:00:00Z",
                "companies": {company_id: {"company_id": company_id, **record}},
            }
        ),
        encoding="utf-8",
    )


def test_reprobe_without_provider_evidence_clears_the_previous_provider(
    tmp_path: Path,
) -> None:
    _write_config_dir(tmp_path)
    write_sources_yaml(
        tmp_path / "sources.yaml",
        [
            SourceConfig(
                id="acme-successfactors",
                company_id="acme",
                provider="successfactors",
                source_class=SourceClass.PUBLIC_ATS,
                support_status=SupportStatus.NEEDS_MANUAL_REVIEW,
                endpoint="https://careers.acme.example/",
                interval_minutes=30,
                parameters={
                    "audit_provider": "successfactors",
                    "audit_endpoint": "https://career4.successfactors.com/career",
                    "audit_evidence": "careers page links to career4.successfactors.com",
                    "audit_notes": ["stale note"],
                    "audit_note": "human resolution carried into the audit",
                },
                enabled=False,
                audit_date="2026-09-18",
            )
        ],
    )
    results = tmp_path / "reports" / "source-audit.json"
    _seed_store(
        results,
        "acme",
        {
            "support_status": "NEEDS_MANUAL_REVIEW",
            "probe": {"provider_fingerprint": "successfactors"},
        },
    )
    source = load_source_records(tmp_path / "sources.yaml")[0]

    record_probe(
        tmp_path,
        results,
        source,
        _probe_result(
            "acme-successfactors",
            SupportStatus.FALLBACK_ONLY,
            notes=["no positive provider evidence on the official careers landing page"],
        ),
    )

    updated = load_source_records(tmp_path / "sources.yaml")[0]
    assert updated.provider == "discovery"
    assert updated.id == "acme-discovery"
    assert updated.source_class is SourceClass.FALLBACK
    assert updated.support_status is SupportStatus.FALLBACK_ONLY
    assert updated.enabled is False
    assert "audit_provider" not in updated.parameters
    assert "audit_endpoint" not in updated.parameters
    assert "audit_evidence" not in updated.parameters
    assert updated.parameters["audit_notes"] == [
        "no positive provider evidence on the official careers landing page"
    ]
    assert "stale note" not in updated.parameters["audit_notes"]
    assert updated.parameters["audit_note"] == "human resolution carried into the audit"

    stored = load_audit_store(results)["acme"]
    assert stored["provider"] == "discovery"
    text, _ = render_report(
        load_app_config(tmp_path), load_audit_store(results), generated_at=PROBE_TIME
    )
    row = next(line for line in text.splitlines() if line.startswith("| Acme |"))
    assert "| discovery |" in row


def test_reprobe_with_a_different_provider_overwrites_the_previous_evidence(
    tmp_path: Path,
) -> None:
    _write_config_dir(tmp_path)
    write_sources_yaml(
        tmp_path / "sources.yaml",
        [
            SourceConfig(
                id="acme-greenhouse",
                company_id="acme",
                provider="greenhouse",
                source_class=SourceClass.DOCUMENTED_API,
                support_status=SupportStatus.SUPPORTED,
                endpoint="https://boards-api.greenhouse.io/v1/boards/acme/jobs",
                interval_minutes=30,
                tenant="acme",
                parameters={
                    "audit_provider": "greenhouse",
                    "audit_endpoint": "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
                    "audit_evidence": "careers page links to boards.greenhouse.io/acme",
                    "apply_url_template": "https://boards.greenhouse.io/acme/jobs/{id}",
                    "page_size": 50,
                },
                enabled=True,
                audit_date="2026-09-18",
            )
        ],
    )
    results = tmp_path / "reports" / "source-audit.json"
    _seed_store(
        results,
        "acme",
        {"support_status": "SUPPORTED", "probe": {"provider_fingerprint": "greenhouse"}},
    )
    source = load_source_records(tmp_path / "sources.yaml")[0]

    record_probe(
        tmp_path,
        results,
        source,
        _probe_result(
            "acme-greenhouse",
            SupportStatus.SUPPORTED_UNDOCUMENTED,
            final_url="https://jobs.ashbyhq.com/globex",
            provider_fingerprint="ashby",
            provider_evidence="final URL host jobs.ashbyhq.com",
            provider_feed_url="https://api.ashbyhq.com/posting-api/job-board/globex",
            parsed_job_count=3,
            pagination_complete=True,
            stable_id_percent=100.0,
            direct_url_percent=100.0,
            notes=["ashby adapter collected 3 postings twice"],
        ),
    )

    updated = load_source_records(tmp_path / "sources.yaml")[0]
    assert updated.provider == "ashby"
    assert updated.id == "acme-ashby"
    assert updated.tenant == "globex"
    assert str(updated.endpoint) == "https://api.ashbyhq.com/posting-api/job-board/globex"
    assert updated.source_class is SourceClass.PUBLIC_ATS
    assert updated.support_status is SupportStatus.SUPPORTED_UNDOCUMENTED
    assert updated.enabled is True
    assert "audit_provider" not in updated.parameters
    assert updated.parameters["audit_evidence"] == "final URL host jobs.ashbyhq.com"
    # No tenant, board token, or adapter parameter of the superseded provider may survive.
    assert "apply_url_template" not in updated.parameters
    assert "page_size" not in updated.parameters
    assert "acme" not in str(updated.parameters)
    assert "greenhouse" not in str(updated.parameters)


def test_provider_change_reprobe_persists_the_renamed_source_id(tmp_path: Path) -> None:
    _write_config_dir(tmp_path)
    write_sources_yaml(
        tmp_path / "sources.yaml",
        [
            SourceConfig(
                id="acme-greenhouse",
                company_id="acme",
                provider="greenhouse",
                source_class=SourceClass.DOCUMENTED_API,
                support_status=SupportStatus.SUPPORTED,
                endpoint="https://boards-api.greenhouse.io/v1/boards/acme/jobs",
                interval_minutes=30,
                tenant="acme",
                parameters={
                    "audit_evidence": "careers page links to boards.greenhouse.io/acme",
                },
                enabled=True,
                audit_date="2026-09-18",
            )
        ],
    )
    results = tmp_path / "reports" / "source-audit.json"
    _seed_store(
        results,
        "acme",
        {"support_status": "SUPPORTED", "probe": {"provider_fingerprint": "greenhouse"}},
    )
    source = load_source_records(tmp_path / "sources.yaml")[0]

    record_probe(
        tmp_path,
        results,
        source,
        _probe_result(
            "acme-greenhouse",
            SupportStatus.SUPPORTED_UNDOCUMENTED,
            final_url="https://jobs.ashbyhq.com/globex",
            provider_fingerprint="ashby",
            provider_evidence="final URL host jobs.ashbyhq.com",
            provider_feed_url="https://api.ashbyhq.com/posting-api/job-board/globex",
            parsed_job_count=3,
            pagination_complete=True,
            stable_id_percent=100.0,
            direct_url_percent=100.0,
        ),
    )

    updated = load_source_records(tmp_path / "sources.yaml")[0]
    stored = load_audit_store(results)["acme"]
    assert stored["source_id"] == updated.id == "acme-ashby"
    assert stored["provider"] == updated.provider == "ashby"
    assert stored["support_status"] == str(updated.support_status)
    assert stored["probe"]["provider_feed_url"] == (
        "https://api.ashbyhq.com/posting-api/job-board/globex"
    )
    assert "globex" in stored["probe"]["provider_feed_url"]
    assert "acme" not in stored["probe"]["provider_feed_url"]


def test_reprobe_with_the_same_provider_keeps_the_source_identity(tmp_path: Path) -> None:
    _write_config_dir(tmp_path)
    write_sources_yaml(
        tmp_path / "sources.yaml",
        [
            SourceConfig(
                id="acme-greenhouse",
                company_id="acme",
                provider="greenhouse",
                source_class=SourceClass.DOCUMENTED_API,
                support_status=SupportStatus.SUPPORTED,
                endpoint="https://boards-api.greenhouse.io/v1/boards/acme/jobs",
                interval_minutes=30,
                tenant="acme",
                parameters={
                    "audit_evidence": "careers page links to boards.greenhouse.io/acme",
                    "apply_url_template": "https://boards.greenhouse.io/acme/jobs/{id}",
                },
                enabled=True,
                audit_date="2026-09-18",
            )
        ],
    )
    results = tmp_path / "reports" / "source-audit.json"
    _seed_store(
        results,
        "acme",
        {"support_status": "SUPPORTED", "probe": {"provider_fingerprint": "greenhouse"}},
    )
    source = load_source_records(tmp_path / "sources.yaml")[0]

    record_probe(
        tmp_path,
        results,
        source,
        _probe_result(
            "acme-greenhouse",
            SupportStatus.SUPPORTED,
            final_url="https://www.acme.example/careers",
            provider_fingerprint="greenhouse",
            provider_evidence="careers page links to boards.greenhouse.io/acme",
            provider_feed_url="https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true",
            parsed_job_count=12,
            pagination_complete=True,
            stable_id_percent=100.0,
            direct_url_percent=100.0,
        ),
    )

    updated = load_source_records(tmp_path / "sources.yaml")[0]
    assert updated.id == "acme-greenhouse"
    assert updated.tenant == "acme"
    assert updated.provider == "greenhouse"
    assert updated.enabled is True
    assert updated.parameters["apply_url_template"] == (
        "https://boards.greenhouse.io/acme/jobs/{id}"
    )


def test_probe_evidence_parameters_cover_every_probe_written_key() -> None:
    assert {"page_size", "locale", "detail_base_url"} <= set(PROBE_EVIDENCE_PARAMETERS)


def test_reprobe_without_evidence_clears_verified_workday_parameters(tmp_path: Path) -> None:
    _write_config_dir(tmp_path)
    write_sources_yaml(
        tmp_path / "sources.yaml",
        [
            SourceConfig(
                id="acme-workday",
                company_id="acme",
                provider="workday",
                source_class=SourceClass.PUBLIC_ATS,
                support_status=SupportStatus.SUPPORTED_UNDOCUMENTED,
                endpoint=(
                    "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
                ),
                interval_minutes=30,
                tenant="acme",
                parameters={
                    "page_size": 20,
                    "locale": "en-US",
                    "detail_base_url": "https://acme.wd1.myworkdayjobs.com/en-US/External",
                },
                enabled=True,
                audit_date="2026-09-18",
            )
        ],
    )
    results = tmp_path / "reports" / "source-audit.json"
    _seed_store(
        results,
        "acme",
        {
            "support_status": "SUPPORTED_UNDOCUMENTED",
            "probe": {"provider_fingerprint": "workday"},
        },
    )
    source = load_source_records(tmp_path / "sources.yaml")[0]

    record_probe(
        tmp_path,
        results,
        source,
        _probe_result(
            "acme-workday",
            SupportStatus.BLOCKED,
            http_status=403,
            challenge_detected=True,
            notes=["access challenge detected on the official careers landing page"],
        ),
    )

    updated = load_source_records(tmp_path / "sources.yaml")[0]
    assert updated.provider == "discovery"
    assert updated.id == "acme-discovery"
    assert updated.enabled is False
    for key in ("page_size", "locale", "detail_base_url"):
        assert key not in updated.parameters
    assert "audit_provider" not in updated.parameters
    assert "audit_endpoint" not in updated.parameters
    assert "workday" not in str(updated.parameters)
