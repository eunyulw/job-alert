"""Command-line interface for the fast job alert monitor.

Task 11 owns the ``audit`` command group: ``probe``, ``run``, ``set-source``, ``disable-source``,
and ``report``. The root Typer application is created here so that later tasks add their command
groups to the same application rather than to a second one.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from job_alert.audit import (
    DEFAULT_AUDIT_CONCURRENCY,
    DEFAULT_RESULTS_PATH,
    MAX_AUDIT_CONCURRENCY,
    AuditError,
    ProbeResult,
    coverage_summary,
    disable_source_record,
    load_audit_store,
    now_utc,
    probe_source,
    record_probe,
    render_report,
    replace_source_record,
    run_audit,
    source_by_id,
    to_https_url,
    validate_source_endpoint,
)
from job_alert.config import AppConfig, ConfigError, load_app_config
from job_alert.fetch import HttpFetcher
from job_alert.models import SourceClass, SourceConfig, SupportStatus

DEFAULT_CONFIG_DIR = Path("config")
DEFAULT_MARKDOWN_PATH = Path("docs/source-audit.md")
DEFAULT_INTERVAL_MINUTES = 30

app = typer.Typer(
    help="Fast company job alert monitor.", no_args_is_help=True, add_completion=False
)
audit_app = typer.Typer(
    help="Company-by-company source audit and coverage report.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(audit_app, name="audit")

ConfigDirOption = Annotated[
    Path, typer.Option("--config-dir", help="Directory holding company_targets/sources/filters")
]
ResultsOption = Annotated[
    Path, typer.Option("--json", help="Persisted audit results read and written by the audit")
]


def _fail(message: str) -> NoReturn:
    typer.secho(message, err=True, fg=typer.colors.RED)
    raise typer.Exit(code=1)


def _config(config_dir: Path) -> AppConfig:
    try:
        return load_app_config(config_dir)
    except ConfigError as exc:
        _fail(f"invalid configuration: {exc}")


def _summary_line(result: ProbeResult) -> str:
    parts = [
        f"{result.source_id}",
        str(result.support_status),
        f"http={result.http_status if result.http_status is not None else '-'}",
        f"jobs={result.parsed_job_count}",
        f"provider={result.provider_fingerprint or '-'}",
    ]
    if result.notes:
        parts.append(result.notes[-1])
    return "  ".join(parts)


def _load_source(config_dir: Path, source_id: str) -> SourceConfig:
    return source_by_id(_config(config_dir), source_id)


@audit_app.command("probe")
def audit_probe(
    source: Annotated[str, typer.Option("--source", help="Source id to probe")],
    config_dir: ConfigDirOption = DEFAULT_CONFIG_DIR,
    results: ResultsOption = DEFAULT_RESULTS_PATH,
) -> None:
    """Probe one source with ordinary HTTPS and record the evidence."""
    config = _config(config_dir)
    try:
        source_config = source_by_id(config, source)
    except AuditError as exc:
        _fail(str(exc))
        return

    async def run() -> ProbeResult:
        async with HttpFetcher() as fetcher:
            return await probe_source(source_config, fetcher)

    result = asyncio.run(run())
    try:
        record_probe(config_dir, results, source_config, result)
    except AuditError as exc:
        _fail(str(exc))
        return
    typer.echo(_summary_line(result))
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))


@audit_app.command("run")
def audit_run(
    limit: Annotated[
        int | None, typer.Option("--limit", help="Probe at most this many unaudited companies")
    ] = None,
    company: Annotated[
        list[str] | None, typer.Option("--company", help="Restrict the chunk to a company id")
    ] = None,
    category: Annotated[
        str | None, typer.Option("--category", help="Restrict the chunk to one category")
    ] = None,
    concurrency: Annotated[
        int, typer.Option("--concurrency", help="Companies probed at once (maximum 4)")
    ] = DEFAULT_AUDIT_CONCURRENCY,
    config_dir: ConfigDirOption = DEFAULT_CONFIG_DIR,
    results: ResultsOption = DEFAULT_RESULTS_PATH,
) -> None:
    """Probe unaudited companies in a bounded, resumable chunk."""
    if concurrency > MAX_AUDIT_CONCURRENCY:
        _fail(f"concurrency must not exceed {MAX_AUDIT_CONCURRENCY}")
    try:
        probed = asyncio.run(
            run_audit(
                config_dir,
                results,
                limit=limit,
                company_ids=tuple(company or ()),
                category=category,
                concurrency=concurrency,
                on_result=lambda result: typer.echo(_summary_line(result)),
            )
        )
    except AuditError as exc:
        _fail(str(exc))
        return
    typer.echo(f"probed {len(probed)} compan{'y' if len(probed) == 1 else 'ies'}")


@audit_app.command("set-source")
def audit_set_source(
    company: Annotated[str, typer.Option("--company", help="Canonical company id")],
    provider: Annotated[str, typer.Option("--provider", help="Provider name, e.g. greenhouse")],
    endpoint: Annotated[str, typer.Option("--endpoint", help="Public HTTPS list endpoint")],
    source_class: Annotated[SourceClass, typer.Option("--class", help="Source class")],
    status: Annotated[SupportStatus, typer.Option("--status", help="Support status")],
    interval: Annotated[
        int, typer.Option("--interval", help="Poll interval in minutes")
    ] = DEFAULT_INTERVAL_MINUTES,
    tenant: Annotated[
        str | None, typer.Option("--tenant", help="Provider tenant or board token")
    ] = None,
    config_dir: ConfigDirOption = DEFAULT_CONFIG_DIR,
) -> None:
    """Validate an endpoint with its provider adapter, then replace the discovery record."""
    config = _config(config_dir)
    if company not in {item.id for item in config.companies}:
        _fail(f"unknown company id {company}")
    candidate = SourceConfig(
        id=f"{company}-{provider}",
        company_id=company,
        provider=provider,
        source_class=source_class,
        support_status=status,
        endpoint=to_https_url(endpoint),
        interval_minutes=interval,
        tenant=tenant,
        parameters={},
        enabled=True,
        audit_date=now_utc().date().isoformat(),
    )

    async def validate() -> Any:
        async with HttpFetcher() as fetcher:
            return await validate_source_endpoint(candidate, fetcher)

    try:
        collection = asyncio.run(validate())
    except AuditError as exc:
        _fail(str(exc))
        return

    try:
        replace_source_record(config_dir / "sources.yaml", candidate)
    except AuditError as exc:
        _fail(str(exc))
        return
    typer.echo(
        f"validated {candidate.id}: {len(collection.jobs)} jobs from {candidate.endpoint}"
    )


@audit_app.command("disable-source")
def audit_disable_source(
    source: Annotated[str, typer.Option("--source", help="Source id to disable")],
    status: Annotated[
        SupportStatus, typer.Option("--status", help="Support status to record")
    ] = SupportStatus.NEEDS_MANUAL_REVIEW,
    reason: Annotated[str, typer.Option("--reason", help="Evidence-backed reason")] = (
        "disabled by the source audit"
    ),
    config_dir: ConfigDirOption = DEFAULT_CONFIG_DIR,
    results: ResultsOption = DEFAULT_RESULTS_PATH,
) -> None:
    """Disable a source, keeping the reason and the probe evidence in its parameters."""
    evidence: dict[str, Any] = {}
    store = load_audit_store(results)
    config = _config(config_dir)
    try:
        source_config = source_by_id(config, source)
    except AuditError as exc:
        _fail(str(exc))
        return
    record = store.get(source_config.company_id)
    probe = record.get("probe") if isinstance(record, dict) else None
    if isinstance(probe, dict):
        if probe.get("http_status") is not None:
            evidence["http_status"] = probe["http_status"]
        if probe.get("final_url"):
            evidence["final_url"] = probe["final_url"]
        if probe.get("provider_evidence"):
            evidence["provider"] = probe["provider_evidence"]

    try:
        disabled = disable_source_record(
            config_dir / "sources.yaml",
            source_id=source,
            status=status,
            reason=reason,
            evidence=evidence,
        )
    except AuditError as exc:
        _fail(str(exc))
        return
    typer.echo(f"disabled {disabled.id} as {disabled.support_status}")


@audit_app.command("report")
def audit_report(
    markdown: Annotated[Path, typer.Option("--markdown", help="Markdown report path")] = (
        DEFAULT_MARKDOWN_PATH
    ),
    results: ResultsOption = DEFAULT_RESULTS_PATH,
    config_dir: ConfigDirOption = DEFAULT_CONFIG_DIR,
) -> None:
    """Render the audit report from the persisted results."""
    config = _config(config_dir)
    try:
        store = load_audit_store(results)
        generated_at = now_utc()
        text, payload = render_report(config, store, generated_at=generated_at)
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markdown.write_text(text, encoding="utf-8")
        results.parent.mkdir(parents=True, exist_ok=True)
        results.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except AuditError as exc:
        _fail(str(exc))
        return

    summary = coverage_summary(config)
    typer.echo(f"audited {summary.total_companies - len(summary.unaudited_companies)}/"
               f"{summary.total_companies} companies")
    typer.echo(
        f"direct coverage {summary.direct_supported}/{summary.total_companies} = "
        f"{summary.direct_coverage_percent:.2f}% "
        f"(90% gate {'passes' if summary.broad_activation_allowed else 'does not pass'})"
    )
    for status in summary.by_status:
        typer.echo(f"  {status}: {summary.by_status[status]}")
    typer.echo(f"wrote {markdown} and {results}")
