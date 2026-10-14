"""Command-line interface for the fast job alert monitor.

Task 11 owns the ``audit`` command group: ``probe``, ``run``, ``set-source``, ``disable-source``,
and ``report``. The root Typer application is created here so that later tasks add their command
groups to the same application rather than to a second one.
"""

# The operator surface
# --------------------
# ``init-db`` applies the schema, ``collect`` and ``baseline`` run a known set of cycles and
# exit, and ``run`` is the scheduler that keeps going. ``report coverage`` and
# ``report health`` render what the monitor already knows, ``notify-test`` proves that one
# provider can deliver a message, and ``retry-notifications`` spends the retry budget on events
# that were already committed.
#
# Two rules hold across all of them: a JSON command writes parseable JSON to stdout and nothing
# else there, so every human line goes to stderr; and a refusal is exit code 2 -- a missing
# ``--confirm-seed``, an unknown source, an unreadable configuration, absent Pushover credentials,
# or a live run the coverage and filter gates do not allow -- because those are usage errors an
# operator has to fix, not delivery failures.

from __future__ import annotations

import asyncio
import json
import signal
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from job_alert.adapters.registry import AdapterRegistry
from job_alert.audit import (
    DEFAULT_AUDIT_CONCURRENCY,
    DEFAULT_RESULTS_PATH,
    DIRECT_STATUSES,
    MAX_AUDIT_CONCURRENCY,
    AuditError,
    ProbeResult,
    coverage_summary,
    disable_source_record,
    load_audit_store,
    now_utc,
    probe_source,
    probe_target,
    record_probe,
    render_report,
    replace_source_record,
    run_audit,
    source_by_id,
    to_https_url,
    validate_source_endpoint,
)
from job_alert.config import AppConfig, ConfigError, load_app_config
from job_alert.db import Repository
from job_alert.fetch import HttpFetcher
from job_alert.models import TIER_INTERVAL_MINUTES, SourceClass, SourceConfig, SupportStatus
from job_alert.notifications import MacOSNotifier, NtfyNotifier, PushoverNotifier
from job_alert.notifications.base import (
    NotificationConfigurationError,
    NotificationMessage,
    Notifier,
)
from job_alert.scheduler import (
    DEFAULT_ALERT_URL,
    Scheduler,
    SchedulerReport,
    build_digest,
    digest_payload,
)
from job_alert.service import (
    DELIVERABLE_EVENT_TYPES,
    CycleResult,
    JobAlertService,
    RunMode,
    WouldDeliver,
)

DEFAULT_CONFIG_DIR = Path("config")
DEFAULT_MARKDOWN_PATH = Path("docs/source-audit.md")
DEFAULT_DB_PATH = Path("data/job-alert.sqlite3")
DEFAULT_COVERAGE_PATH = Path("reports/coverage.md")
DEFAULT_DIGEST_PATH = Path("reports/daily-digest.md")
DEFAULT_RETRY_LIMIT = 100
TEST_MESSAGE_TITLE = "TEST \u2014 Fast Job Alert"
#: Laptop notifications ship first; Pushover (Android) follows in Task 28 of the plan.
DEFAULT_PROVIDER = "macos"

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
ConfigPathOption = Annotated[
    Path,
    typer.Option(
        "--config",
        "--config-dir",
        help="Directory holding company_targets/sources/filters",
    ),
]
DbPathOption = Annotated[Path, typer.Option("--db", help="SQLite state database")]
JsonOption = Annotated[
    bool, typer.Option("--json", help="Write parseable JSON to stdout and logs to stderr")
]
ReportPathOption = Annotated[
    Path | None, typer.Option("--report", help="Write the cycle report to this file")
]
ProviderOption = Annotated[
    str, typer.Option("--provider", help="Delivery provider: macos, pushover, or ntfy")
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
    store = load_audit_store(results)
    target = probe_target(source_config, store.get(source_config.company_id))

    async def run() -> ProbeResult:
        async with HttpFetcher() as fetcher:
            return await probe_source(target, fetcher)

    result = asyncio.run(run())
    try:
        record_probe(config_dir, results, target, result)
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
    force: Annotated[
        bool, typer.Option("--force", help="Re-probe companies that already have a record")
    ] = False,
    retry_failed: Annotated[
        bool,
        typer.Option(
            "--retry-failed", help="Re-probe recorded blocked and manual-review companies"
        ),
    ] = False,
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
                force=force,
                retry_failed=retry_failed,
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
        int | None,
        typer.Option("--interval", help="Poll interval in minutes (default: the class tier)"),
    ] = None,
    tenant: Annotated[
        str | None, typer.Option("--tenant", help="Provider tenant or board token")
    ] = None,
    parameters: Annotated[
        str | None,
        typer.Option(
            "--parameters",
            help='Adapter parameters as a JSON object, e.g. a scope: {"applied_facets": {...}}',
        ),
    ] = None,
    config_dir: ConfigDirOption = DEFAULT_CONFIG_DIR,
) -> None:
    """Validate an endpoint with its provider adapter, then replace the discovery record."""
    config = _config(config_dir)
    try:
        adapter_parameters = json.loads(parameters) if parameters is not None else {}
    except json.JSONDecodeError as exc:
        _fail(f"--parameters is not JSON: {exc}")
    if not isinstance(adapter_parameters, dict):
        _fail("--parameters must be a JSON object")
    if company not in {item.id for item in config.companies}:
        _fail(f"unknown company id {company}")
    candidate = SourceConfig(
        id=f"{company}-{provider}",
        company_id=company,
        provider=provider,
        source_class=source_class,
        support_status=status,
        endpoint=to_https_url(endpoint),
        interval_minutes=TIER_INTERVAL_MINUTES[source_class] if interval is None else interval,
        tenant=tenant,
        parameters=adapter_parameters,
        # `enabled` follows the status: only a direct source (SUPPORTED or SUPPORTED_UNDOCUMENTED)
        # is polled, and an operator may still record a downgrade without enabling it.
        enabled=status in DIRECT_STATUSES,
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
    if not candidate.enabled:
        typer.echo(f"{candidate.id} written disabled because {status} is not a direct source")


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


# ------------------------------------------------------------------ operator surface


def _refuse(message: str) -> NoReturn:
    """Report an operator error and exit with the usage code the brief pins for one."""
    typer.secho(message, err=True, fg=typer.colors.RED)
    raise typer.Exit(code=2)


def _config_or_refuse(config_dir: Path) -> AppConfig:
    try:
        return load_app_config(config_dir)
    except ConfigError as exc:
        _refuse(f"invalid configuration: {exc}")


def _open_repository(database: Path) -> Repository:
    """Open (and upgrade) the state database, refusing with the usage code when it cannot."""
    try:
        repository = Repository.open(database)
        repository.initialize()
    except (sqlite3.Error, RuntimeError) as exc:
        _refuse(f"cannot open {database}: {exc}")
    return repository


def _open_repository_for_read(database: Path) -> Repository:
    """Open an existing database for reading, without creating or migrating it.

    A report must not invent the state it reports on: an operator who mistypes a path gets a clear
    refusal instead of an empty digest and a new database file.
    """
    try:
        return Repository.open_read_only(database)
    except (sqlite3.Error, OSError) as exc:
        _refuse(f"cannot read {database}: {exc}")


def _build_service(
    repository: Repository,
    config: AppConfig,
    fetcher: HttpFetcher,
    notifier: Notifier | None,
) -> JobAlertService:
    """The collection service every operator command drives."""
    return JobAlertService(
        repository,
        registry=AdapterRegistry.default(),
        fetcher=fetcher,
        filters=config.filters,
        companies=config.companies,
        notifier=notifier,
    )


def build_notifier(provider: str, *, fetcher: HttpFetcher | None = None) -> Notifier:
    """Build the delivery provider the operator named, from credentials held in the environment."""
    name = provider.strip().lower()
    try:
        if name == MacOSNotifier.provider:
            return MacOSNotifier()
        if name == PushoverNotifier.provider:
            return PushoverNotifier(fetcher=fetcher)
        if name == NtfyNotifier.provider:
            return NtfyNotifier(fetcher=fetcher)
    except NotificationConfigurationError as exc:
        _refuse(str(exc))
    _refuse(f"unknown provider {provider!r}: choose macos, pushover, or ntfy")


def test_message(alert_url: str = DEFAULT_ALERT_URL) -> NotificationMessage:
    """The one message notify-test sends: labelled, and built from no stored job or event."""
    return NotificationMessage(
        title=TEST_MESSAGE_TITLE,
        body=(
            "Delivery test from the fast job alert monitor. No job event is involved and no stored"
            " posting, event, or alert was read or written."
        ),
        url=alert_url,
        url_title="Fast Job Alert",
    )


def _selected_sources(
    config: AppConfig, *, source_ids: Sequence[str], all_sources: bool
) -> list[SourceConfig]:
    """The sources one command should act on, or a refusal that says how to ask for them."""
    if source_ids and all_sources:
        _refuse("choose either --source or --all, not both")
    if source_ids:
        by_id = {source.id: source for source in config.sources}
        selected: list[SourceConfig] = []
        for source_id in dict.fromkeys(source_ids):
            source = by_id.get(source_id)
            if source is None:
                _refuse(f"unknown source {source_id!r}: it is not in the configured registry")
            selected.append(source)
        return selected
    if all_sources:
        return [source for source in config.sources if source.enabled]
    _refuse("select a source with --source SOURCE_ID, or every enabled source with --all")


def _require_live_activation(config: AppConfig) -> None:
    """Refuse LIVE while nothing could be delivered, or while broad activation is not yet earned.

    The 90% coverage gate is for *broad* activation: an enabled notifying filter with no
    ``include_companies`` allowlist. A filter that names its companies is a canary and may run
    below the threshold, but only if every company it names has an enabled source to alert from.
    """
    enabled = [record for record in config.filters if record.enabled]
    notifying = [record for record in enabled if record.notify]
    if not notifying:
        names = ", ".join(record.id for record in enabled) or "none"
        _refuse(
            "LIVE mode refused: no enabled filter notifies, so nothing could ever be delivered"
            f" (enabled filters: {names})"
        )
    polled = {source.company_id.casefold() for source in config.sources if source.enabled}
    for record in notifying:
        unreachable = sorted(
            company for company in record.include_companies
            if company.strip().casefold() not in polled
        )
        if unreachable:
            _refuse(
                f"LIVE mode refused: filter {record.id} names {', '.join(unreachable)}, which has"
                " no enabled source, so it could never alert there"
            )
    broad = [record.id for record in notifying if not record.include_companies]
    summary = coverage_summary(config)
    if broad and not summary.broad_activation_allowed:
        _refuse(
            f"LIVE mode refused: direct coverage is {summary.direct_coverage_percent:.2f}%"
            f" ({summary.direct_supported}/{summary.total_companies}), below the 90%"
            f" broad-activation threshold, and {', '.join(broad)} has no include_companies"
            " allowlist"
        )


def _would_deliver_payload(item: WouldDeliver) -> dict[str, Any]:
    return {
        "event_id": item.event_id,
        "event_type": str(item.event_type),
        "canonical_job_key": item.canonical_job_key,
        "company_id": item.company_id,
        "title": item.title,
        "posting_url": item.posting_url,
        "priority": str(item.priority),
        "reason": item.reason,
        "deferred": item.deferred,
        "defer_until": None if item.defer_until is None else item.defer_until.isoformat(),
    }


def _cycle_payload(result: CycleResult) -> dict[str, Any]:
    """One collection cycle as JSON, including what it would have delivered and why."""
    metrics = result.metrics
    return {
        "source_id": result.source_id,
        "mode": str(result.mode),
        "run_id": result.run_id,
        "outcome": str(result.outcome),
        "complete": result.complete,
        "new": result.new_count,
        "updated": result.updated_count,
        "reopened": result.reopened_count,
        "unchanged": result.unchanged_count,
        "missing": result.missing_count,
        "closed": result.closed_count,
        "notifications_sent": result.notifications_sent,
        "would_deliver": [_would_deliver_payload(item) for item in result.would_deliver],
        "suppressed_duplicates": list(result.suppressed_duplicates),
        "quarantined": result.quarantined,
        "not_modified": result.not_modified,
        "warnings": list(result.warnings),
        "error_code": result.error_code,
        "error_message": result.error_message,
        "seeded": result.seeded,
        "duration_seconds": result.duration_seconds,
        "page_count": None if metrics is None else metrics.page_count,
        "declared_total": None if metrics is None else metrics.declared_total,
        "parsed_total": None if metrics is None else metrics.parsed_total,
        "stable_id_percent": None if metrics is None else metrics.stable_id_percent,
        "direct_url_percent": None if metrics is None else metrics.direct_url_percent,
        "health_events": [
            {
                "kind": str(event.kind),
                "source_id": event.source_id,
                "company_id": event.company_id,
                "reason": str(event.reason),
                "consecutive_failures": event.consecutive_failures,
                "occurred_at": event.occurred_at.isoformat(),
                "last_error": event.last_error,
            }
            for event in result.health_events
        ],
    }


def _cycle_report_payload(
    mode: RunMode, results: Sequence[CycleResult], *, generated_at: datetime
) -> dict[str, Any]:
    """One command's whole set of cycles, which is what --json prints and --report writes."""
    return {
        "mode": str(mode),
        "generated_at": generated_at.isoformat(),
        "totals": {
            "sources": len(results),
            "new": sum(result.new_count for result in results),
            "reopened": sum(result.reopened_count for result in results),
            "closed": sum(result.closed_count for result in results),
            "notifications_sent": sum(result.notifications_sent for result in results),
            "incomplete": len([result for result in results if not result.complete]),
            "seeded": len([result for result in results if result.seeded]),
        },
        "sources": [_cycle_payload(result) for result in results],
    }


def _cycle_line(result: CycleResult) -> str:
    """One cycle as an operator log line."""
    return (
        f"{result.source_id}  {result.outcome}  new={result.new_count}"
        f"  updated={result.updated_count}  missing={result.missing_count}"
        f"  notifications={result.notifications_sent}"
        + (f"  error={result.error_code}" if result.error_code else "")
        + ("  seeded (first complete snapshot, no events)" if result.seeded else "")
    )


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _emit(payload: dict[str, Any], lines: Sequence[str], *, json_output: bool) -> None:
    """JSON commands put the document on stdout and their log lines on stderr; others just talk."""
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        for line in lines:
            typer.secho(line, err=True)
        return
    for line in lines:
        typer.echo(line)


async def _run_cycles(
    service: JobAlertService,
    sources: Sequence[SourceConfig],
    *,
    mode: RunMode,
) -> list[CycleResult]:
    """One cycle per source, in order. A live cycle delivers its own health events."""
    return [await service.collect_source(source, mode=mode) for source in sources]


def _collect_and_close(
    config: AppConfig,
    sources: Sequence[SourceConfig],
    *,
    mode: RunMode,
    notifier: Notifier | None,
    database: Path,
) -> list[CycleResult]:
    """Run the selected cycles on one HTTP client, then close it and the database."""
    repository = _open_repository(database)
    fetcher = HttpFetcher()

    async def run() -> list[CycleResult]:
        try:
            service = _build_service(repository, config, fetcher, notifier)
            return await _run_cycles(service, sources, mode=mode)
        finally:
            await fetcher.close()

    try:
        return asyncio.run(run())
    finally:
        repository.close()


async def _run_scheduler(scheduler: Scheduler, *, max_wakes: int | None) -> SchedulerReport:
    """Run the loop until it stops, turning SIGINT and SIGTERM into a graceful shutdown."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            loop.add_signal_handler(number, stop.set)
        except (NotImplementedError, RuntimeError):  # not the main thread, or unsupported
            continue
    return await scheduler.run(stop=stop, max_wakes=max_wakes)


@app.command("init-db")
def init_db(db: DbPathOption = DEFAULT_DB_PATH) -> None:
    """Create or upgrade the SQLite state database."""
    repository = _open_repository(db)
    repository.close()
    typer.echo(f"initialized {db}")


@app.command("collect")
def collect(
    source: Annotated[
        list[str] | None, typer.Option("--source", help="Source id to collect, repeatable")
    ] = None,
    all_sources: Annotated[
        bool, typer.Option("--all", help="Collect every enabled source")
    ] = False,
    mode: Annotated[
        RunMode, typer.Option("--mode", help="What one cycle is allowed to change")
    ] = RunMode.DRY_RUN,
    json_output: JsonOption = False,
    report: ReportPathOption = None,
    config: ConfigPathOption = DEFAULT_CONFIG_DIR,
    db: DbPathOption = DEFAULT_DB_PATH,
    provider: ProviderOption = DEFAULT_PROVIDER,
) -> None:
    """Run one collection cycle for the selected sources and exit."""
    app_config = _config_or_refuse(config)
    if mode is RunMode.LIVE:
        # One gate for every live path in this CLI, whichever verb asked for it: a live cycle acts
        # on the registry the gates describe, whether it is one wake of the loop or one set of
        # cycles, so no command can reach a provider below the coverage threshold.
        _require_live_activation(app_config)
    sources = _selected_sources(app_config, source_ids=source or (), all_sources=all_sources)
    notifier = build_notifier(provider) if mode is RunMode.LIVE else None
    results = _collect_and_close(
        app_config, sources, mode=mode, notifier=notifier, database=db
    )
    payload = _cycle_report_payload(mode, results, generated_at=datetime.now(UTC))
    if report is not None:
        _write_json(report, payload)
    lines = [*(_cycle_line(result) for result in results)]
    lines.append(f"collected {len(results)} source(s) in {mode} mode")
    if report is not None:
        lines.append(f"wrote {report}")
    _emit(payload, lines, json_output=json_output)


@app.command("baseline")
def baseline(
    source: Annotated[
        list[str] | None, typer.Option("--source", help="Source id to seed, repeatable")
    ] = None,
    all_sources: Annotated[
        bool, typer.Option("--all", help="Seed every enabled source")
    ] = False,
    confirm_seed: Annotated[
        bool,
        typer.Option(
            "--confirm-seed",
            help="Confirm that the board as it stands now is the state to seed from",
        ),
    ] = False,
    json_output: JsonOption = False,
    report: ReportPathOption = None,
    config: ConfigPathOption = DEFAULT_CONFIG_DIR,
    db: DbPathOption = DEFAULT_DB_PATH,
) -> None:
    """Seed job state for the selected sources, announcing nothing at all."""
    if not confirm_seed:
        _refuse(
            "baseline records every posting a board holds right now as already seen, so it cannot"
            " announce them later; pass --confirm-seed to confirm that is the intent"
        )
    app_config = _config_or_refuse(config)
    sources = _selected_sources(app_config, source_ids=source or (), all_sources=all_sources)
    results = _collect_and_close(
        app_config, sources, mode=RunMode.BASELINE, notifier=None, database=db
    )
    payload = _cycle_report_payload(RunMode.BASELINE, results, generated_at=datetime.now(UTC))
    if report is not None:
        _write_json(report, payload)
    lines = [*(_cycle_line(result) for result in results)]
    lines.append(f"seeded {len(results)} source(s); no event was staged and nothing was sent")
    _emit(payload, lines, json_output=json_output)


@app.command("run")
def run_monitor(
    config: ConfigPathOption = DEFAULT_CONFIG_DIR,
    db: DbPathOption = DEFAULT_DB_PATH,
    mode: Annotated[
        RunMode, typer.Option("--mode", help="What a cycle is allowed to change")
    ] = RunMode.LIVE_SUPPRESSED,
    provider: ProviderOption = DEFAULT_PROVIDER,
    once: Annotated[
        bool, typer.Option("--once", help="Run one wake cycle and exit")
    ] = False,
    max_wakes: Annotated[
        int | None, typer.Option("--max-wakes", help="Stop after this many wake cycles")
    ] = None,
    alert_url: Annotated[
        str, typer.Option("--alert-url", help="Link a health or digest push opens")
    ] = DEFAULT_ALERT_URL,
    digest_report: Annotated[
        Path, typer.Option("--digest-report", help="Daily digest markdown path")
    ] = DEFAULT_DIGEST_PATH,
) -> None:
    """Run the scheduler until it is stopped.

    LIVE mode is the only mode that contacts a delivery provider, and it is refused while the
    configured filters cannot approve anything, or while direct coverage is below the 90%
    broad-activation threshold.
    """
    app_config = _config_or_refuse(config)
    if mode is RunMode.LIVE:
        _require_live_activation(app_config)
    if max_wakes is not None and max_wakes < 1:
        _refuse("--max-wakes must be at least 1")
    notifier = build_notifier(provider) if mode is RunMode.LIVE else None
    repository = _open_repository(db)
    fetcher = HttpFetcher()
    service = _build_service(repository, app_config, fetcher, notifier)
    scheduler = Scheduler(
        repository,
        sources=app_config.sources,
        companies=app_config.companies,
        service=service,
        fetcher=fetcher,
        mode=mode,
        notifier=notifier,
        alert_url=alert_url,
        digest_report_path=digest_report,
        logger=lambda line: typer.secho(line, err=True),
    )
    report = asyncio.run(_run_scheduler(scheduler, max_wakes=1 if once else max_wakes))
    typer.secho(f"stopped after {len(report.wakes)} wake cycle(s) in {mode} mode", err=True)


report_app = typer.Typer(
    help="Reports the monitor can render from stored state.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(report_app, name="report")


@report_app.command("coverage")
def report_coverage(
    markdown: Annotated[Path, typer.Option("--markdown", help="Markdown report path")] = (
        DEFAULT_COVERAGE_PATH
    ),
    results: Annotated[
        Path, typer.Option("--results", help="Persisted audit results read and written")
    ] = DEFAULT_RESULTS_PATH,
    config: ConfigPathOption = DEFAULT_CONFIG_DIR,
) -> None:
    """Render the company coverage report from the audit store and the registry."""
    app_config = _config_or_refuse(config)
    try:
        store = load_audit_store(results)
        text, payload = render_report(app_config, store, generated_at=now_utc())
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markdown.write_text(text, encoding="utf-8")
        results.parent.mkdir(parents=True, exist_ok=True)
        results.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except AuditError as exc:
        _refuse(str(exc))
    summary = coverage_summary(app_config)
    typer.echo(f"wrote {markdown} and {results}")
    typer.echo(
        f"direct coverage {summary.direct_supported}/{summary.total_companies} ="
        f" {summary.direct_coverage_percent:.2f}% (90% gate"
        f" {'passes' if summary.broad_activation_allowed else 'does not pass'})"
    )


@report_app.command("health")
def report_health(
    json_output: JsonOption = False,
    db: DbPathOption = DEFAULT_DB_PATH,
    config: ConfigPathOption = DEFAULT_CONFIG_DIR,
    provider: ProviderOption = DEFAULT_PROVIDER,
) -> None:
    """Report source freshness, open incidents, delivery activity, and the retry policy."""
    app_config = _config_or_refuse(config)
    repository = _open_repository_for_read(db)
    try:
        moment = datetime.now(UTC)
        try:
            digest = build_digest(
                repository,
                app_config.sources,
                companies={company.id: company.name for company in app_config.companies},
                now=moment,
                window_start=moment - timedelta(days=1),
                provider=provider,
            )
        except (sqlite3.Error, KeyError, IndexError) as exc:
            # An older file is refused rather than half-read: the columns this report needs arrive
            # with the migration an applying command runs.
            _refuse(
                f"cannot read state from {db}: {exc}; run 'job-alert init-db --db {db}' to"
                " upgrade it"
            )
    finally:
        repository.close()
    if json_output:
        typer.echo(json.dumps(digest_payload(digest), indent=2))
        return
    typer.echo(digest.text)


@app.command("notify-test")
def notify_test(
    provider: ProviderOption = DEFAULT_PROVIDER,
    alert_url: Annotated[
        str, typer.Option("--alert-url", help="Link the test message opens")
    ] = DEFAULT_ALERT_URL,
) -> None:
    """Send one labelled test message. No job event is involved or read."""
    notifier = build_notifier(provider)
    result = asyncio.run(notifier.send(test_message(alert_url)))
    if not result.delivered:
        _fail(
            f"{result.provider} did not accept the test message:"
            f" {result.error or f'HTTP {result.http_status}'}"
        )
        return
    status = "" if result.http_status is None else f" (HTTP {result.http_status})"
    typer.echo(f"{result.provider} accepted the test message{status}")


async def _retry_committed(
    service: JobAlertService,
    repository: Repository,
    sources: Sequence[SourceConfig],
    *,
    limit: int,
) -> dict[str, Any]:
    """Deliver what is already committed, bounded by the operator's limit.

    An event inside the delay its last attempt recorded is counted and left alone: that is the
    retry pacing doing its job, and the count is what makes it visible to an operator.
    """
    moment = datetime.now(UTC)
    remaining = limit
    delivered = 0
    due_total = 0
    waiting = 0
    touched: list[str] = []
    for source in sources:
        outstanding = repository.pending_events(source.id, event_types=DELIVERABLE_EVENT_TYPES)
        if not outstanding:
            continue
        due = repository.pending_events(
            source.id, event_types=DELIVERABLE_EVENT_TYPES, now=moment
        )
        waiting += len(outstanding) - len(due)
        allowance = min(remaining, len(due))
        if allowance <= 0:
            continue
        sent, _ = await service.deliver_pending(source, limit=allowance)
        remaining -= allowance
        due_total += allowance
        delivered += sent
        touched.append(source.id)
    return {
        "delivered": delivered,
        "due": due_total,
        "waiting_for_backoff": waiting,
        "sources": touched,
        "limit": limit,
    }


@app.command("retry-notifications")
def retry_notifications(
    limit: Annotated[
        int, typer.Option("--limit", help="At most this many events across all sources")
    ] = DEFAULT_RETRY_LIMIT,
    provider: ProviderOption = DEFAULT_PROVIDER,
    json_output: JsonOption = False,
    config: ConfigPathOption = DEFAULT_CONFIG_DIR,
    db: DbPathOption = DEFAULT_DB_PATH,
) -> None:
    """Spend the retry budget on events that were committed but never delivered."""
    if limit < 0:
        _refuse("--limit must not be negative")
    app_config = _config_or_refuse(config)
    notifier = build_notifier(provider)
    repository = _open_repository(db)
    fetcher = HttpFetcher()
    sources = [source for source in app_config.sources if source.enabled]

    async def run() -> dict[str, Any]:
        try:
            service = _build_service(repository, app_config, fetcher, notifier)
            return await _retry_committed(service, repository, sources, limit=limit)
        finally:
            await fetcher.close()

    try:
        payload = asyncio.run(run())
    finally:
        repository.close()
    lines = [
        f"delivered {payload['delivered']} of {payload['due']} due event(s) across"
        f" {len(payload['sources'])} source(s)",
        f"{payload['waiting_for_backoff']} event(s) are inside their recorded retry delay",
    ]
    _emit(payload, lines, json_output=json_output)
