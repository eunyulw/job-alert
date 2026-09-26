"""The operator CLI: every command, its safety gate, and the exit code it answers with.

Each test builds its own configuration directory and database under ``tmp_path`` and owns the
provider endpoint through ``respx``, so nothing here reaches the network and no command can
write outside the temporary directory the test made. JSON commands are asserted to put parseable
JSON on stdout and their human log lines on stderr.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner, Result

import job_alert.cli as cli
from job_alert.db import Repository
from job_alert.models import JobEvent, JobEventType, NormalizedJob, RunOutcome
from job_alert.notifications.base import DeliveryResult, NotificationMessage

GREENHOUSE_FIXTURE = Path("tests/fixtures/greenhouse/jobs.json")
ENDPOINT = "https://boards-api.greenhouse.io/v1/boards/example/jobs?content=true"
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
runner = CliRunner()


def invoke(*args: str) -> Result:
    """Run the real Typer application the way an operator would."""
    return runner.invoke(cli.app, list(args))


# ------------------------------------------------------------------ the command surface


def test_the_operator_surface_lists_every_command() -> None:
    result = invoke("--help")
    assert result.exit_code == 0
    for command in (
        "init-db",
        "audit",
        "collect",
        "baseline",
        "run",
        "report",
        "notify-test",
        "retry-notifications",
    ):
        assert command in result.stdout


def test_init_db_creates_the_schema(tmp_path: Path) -> None:
    database = tmp_path / "data" / "job-alert.sqlite3"

    result = invoke("init-db", "--db", str(database))

    assert result.exit_code == 0
    assert database.exists()
    with sqlite3.connect(database) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {"poll_runs", "jobs", "job_events", "source_state"} <= tables


# ------------------------------------------------------------------ collect


def test_collect_dry_run_writes_json_to_stdout_and_logs_to_stderr(
    tmp_path: Path, greenhouse_endpoint: respx.Router
) -> None:
    config = make_config(tmp_path)
    database = tmp_path / "state.sqlite3"

    result = invoke(
        "collect",
        "--source",
        "example-greenhouse",
        "--mode",
        "dry-run",
        "--json",
        "--config",
        str(config),
        "--db",
        str(database),
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["mode"] == "dry-run"
    assert [cycle["source_id"] for cycle in payload["sources"]] == ["example-greenhouse"]
    cycle = payload["sources"][0]
    assert cycle["outcome"] == "partial"  # a dry run is a diagnostic, never the applied snapshot
    assert cycle["complete"] is True
    assert cycle["new"] == 2
    assert cycle["notifications_sent"] == 0
    # The human line is a log: it belongs on stderr so stdout stays machine-readable.
    assert "example-greenhouse" in result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_collect_live_suppressed_writes_a_cycle_report(
    tmp_path: Path, greenhouse_endpoint: respx.Router
) -> None:
    config = make_config(tmp_path)
    database = tmp_path / "state.sqlite3"
    report = tmp_path / "reports" / "cycle.json"

    result = invoke(
        "collect",
        "--all",
        "--mode",
        "live-suppressed",
        "--report",
        str(report),
        "--config",
        str(config),
        "--db",
        str(database),
    )

    assert result.exit_code == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["mode"] == "live-suppressed"
    assert payload["totals"]["sources"] == 1
    assert payload["sources"][0]["would_deliver"] == []  # the only filter never notifies
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_collect_live_refuses_while_the_only_enabled_filter_never_notifies(
    tmp_path: Path, greenhouse_endpoint: respx.Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Important 1: a live cycle is gated wherever it is asked for, not only through run."""
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "test-token")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "test-user")
    config = make_config(tmp_path)

    result = invoke(
        "collect",
        "--all",
        "--mode",
        "live",
        "--config",
        str(config),
        "--db",
        str(tmp_path / "state.sqlite3"),
    )

    assert result.exit_code == 2
    assert "all-jobs-dry-run" in result.stderr
    assert not (tmp_path / "state.sqlite3").exists()


def test_collect_live_refuses_below_the_ninety_percent_coverage_threshold(
    tmp_path: Path, greenhouse_endpoint: respx.Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Important 1: collect --all is broad activation, so it obeys the same 90% rule as run."""
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "test-token")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "test-user")
    config = make_config(tmp_path, fallback_company=True, notify_filter=True)

    result = invoke(
        "collect",
        "--all",
        "--mode",
        "live",
        "--config",
        str(config),
        "--db",
        str(tmp_path / "state.sqlite3"),
    )

    assert result.exit_code == 2
    assert "below the 90%" in result.stderr
    assert "50.00%" in result.stderr


def test_collect_live_delivers_when_the_gates_pass(
    tmp_path: Path, greenhouse_endpoint: respx.Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate refuses live without coverage; above the threshold the live path still works."""
    config = make_config(tmp_path, notify_filter=True)
    database = tmp_path / "state.sqlite3"
    recorder = RecordingNotifier()
    monkeypatch.setattr(cli, "build_notifier", lambda provider, **_: recorder)

    result = invoke(
        "collect",
        "--all",
        "--mode",
        "live",
        "--json",
        "--config",
        str(config),
        "--db",
        str(database),
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    cycle = payload["sources"][0]
    assert cycle["outcome"] == "complete"
    assert cycle["notifications_sent"] == 1
    assert len(cycle["would_deliver"]) == 2
    assert len(recorder.messages) == 1
    assert "Example Robotics" in recorder.messages[0].title


def test_collect_refuses_a_source_it_cannot_find(
    tmp_path: Path, greenhouse_endpoint: respx.Router
) -> None:
    config = make_config(tmp_path)

    result = invoke(
        "collect",
        "--source",
        "ghost-board",
        "--mode",
        "dry-run",
        "--config",
        str(config),
        "--db",
        str(tmp_path / "state.sqlite3"),
    )

    assert result.exit_code == 2
    assert "ghost-board" in result.stderr


def test_collect_refuses_without_a_selection(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    result = invoke("collect", "--mode", "dry-run", "--config", str(config))

    assert result.exit_code == 2
    assert "--source" in result.stderr and "--all" in result.stderr


def test_an_invalid_configuration_is_refused_with_exit_code_two(tmp_path: Path) -> None:
    config = tmp_path / "broken"
    config.mkdir()
    (config / "sources.yaml").write_text("sources: [\n", encoding="utf-8")

    result = invoke(
        "collect",
        "--all",
        "--mode",
        "dry-run",
        "--config",
        str(config),
        "--db",
        str(tmp_path / "state.sqlite3"),
    )

    assert result.exit_code == 2
    assert "invalid configuration" in result.stderr


# ------------------------------------------------------------------ baseline


def test_baseline_refuses_to_seed_without_the_confirmation_flag(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    database = tmp_path / "state.sqlite3"

    result = invoke("baseline", "--all", "--config", str(config), "--db", str(database))

    assert result.exit_code == 2
    assert "--confirm-seed" in result.stderr
    assert not database.exists()


def test_baseline_seeds_without_announcing_anything(
    tmp_path: Path, greenhouse_endpoint: respx.Router
) -> None:
    config = make_config(tmp_path)
    database = tmp_path / "state.sqlite3"

    result = invoke(
        "baseline",
        "--all",
        "--confirm-seed",
        "--json",
        "--config",
        str(config),
        "--db",
        str(database),
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["mode"] == "baseline"
    assert payload["sources"][0]["outcome"] == "complete"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM job_events").fetchone()[0] == 0


# ------------------------------------------------------------------ run and its safety gates


def test_run_refuses_live_while_the_only_enabled_filter_never_notifies(
    tmp_path: Path, greenhouse_endpoint: respx.Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "test-token")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "test-user")
    config = make_config(tmp_path)

    result = invoke(
        "run",
        "--mode",
        "live",
        "--once",
        "--config",
        str(config),
        "--db",
        str(tmp_path / "state.sqlite3"),
    )

    assert result.exit_code == 2
    assert "all-jobs-dry-run" in result.stderr


def test_run_refuses_live_below_the_ninety_percent_coverage_threshold(
    tmp_path: Path, greenhouse_endpoint: respx.Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "test-token")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "test-user")
    config = make_config(tmp_path, fallback_company=True, notify_filter=True)

    result = invoke(
        "run",
        "--mode",
        "live",
        "--once",
        "--config",
        str(config),
        "--db",
        str(tmp_path / "state.sqlite3"),
    )

    assert result.exit_code == 2
    assert "below the 90%" in result.stderr
    assert "50.00%" in result.stderr


def test_run_completes_one_suppressed_wake(
    tmp_path: Path, greenhouse_endpoint: respx.Router
) -> None:
    config = make_config(tmp_path)
    database = tmp_path / "state.sqlite3"

    result = invoke(
        "run",
        "--mode",
        "live-suppressed",
        "--once",
        "--config",
        str(config),
        "--db",
        str(database),
    )

    assert result.exit_code == 0
    assert "example-greenhouse" in result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM poll_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


# ------------------------------------------------------------------ reports


def test_report_coverage_writes_the_markdown_report(
    tmp_path: Path, greenhouse_endpoint: respx.Router
) -> None:
    config = make_config(tmp_path)
    markdown = tmp_path / "reports" / "coverage.md"
    results = tmp_path / "reports" / "source-audit.json"

    result = invoke(
        "report",
        "coverage",
        "--markdown",
        str(markdown),
        "--results",
        str(results),
        "--config",
        str(config),
    )

    assert result.exit_code == 0
    text = markdown.read_text(encoding="utf-8")
    assert "Example Robotics" in text
    assert "direct coverage" in result.stdout.lower()
    assert results.exists()


def test_report_health_emits_json_on_stdout(
    tmp_path: Path, greenhouse_endpoint: respx.Router
) -> None:
    config = make_config(tmp_path)
    database = tmp_path / "state.sqlite3"
    invoke(
        "run",
        "--mode",
        "live-suppressed",
        "--once",
        "--config",
        str(config),
        "--db",
        str(database),
    )

    result = invoke(
        "report", "health", "--json", "--config", str(config), "--db", str(database)
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["total_enabled_sources"] == 1
    assert payload["sources_complete"] == 1
    assert payload["retry_policy"]["enforced"] is True
    assert payload["open_incidents"] == []


def test_report_health_refuses_a_missing_database_without_creating_one(
    tmp_path: Path, greenhouse_endpoint: respx.Router
) -> None:
    """Minor 4: a report reads state, so it must not invent the database it reports on."""
    config = make_config(tmp_path)
    database = tmp_path / "data" / "job-alert.sqlite3"

    result = invoke(
        "report", "health", "--json", "--config", str(config), "--db", str(database)
    )

    assert result.exit_code == 2
    assert "cannot read" in result.stderr
    assert not database.exists()


# ------------------------------------------------------------------ notify-test


def test_notify_test_refuses_without_pushover_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PUSHOVER_APP_TOKEN", raising=False)
    monkeypatch.delenv("PUSHOVER_USER_KEY", raising=False)

    result = invoke("notify-test", "--provider", "pushover")

    assert result.exit_code == 2
    assert "PUSHOVER_APP_TOKEN" in result.stderr


def test_notify_test_refuses_an_unknown_provider() -> None:
    result = invoke("notify-test", "--provider", "carrier-pigeon")

    assert result.exit_code == 2
    assert "carrier-pigeon" in result.stderr


def test_notify_test_labels_its_message_and_never_touches_a_job_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = RecordingNotifier()
    monkeypatch.setattr(cli, "build_notifier", lambda provider, **_: recorder)

    result = invoke("notify-test", "--provider", "pushover")

    assert result.exit_code == 0
    message = recorder.messages[0]
    assert message.title == "TEST \u2014 Fast Job Alert"
    assert "no job event" in message.body.lower()
    assert not message.url.startswith("https://boards.")


# ------------------------------------------------------------------ retry-notifications


def test_retry_notifications_delivers_the_events_already_committed(
    tmp_path: Path, greenhouse_endpoint: respx.Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path, notify_filter=True)
    database = tmp_path / "state.sqlite3"
    seed_pending_event(database)
    recorder = RecordingNotifier()
    monkeypatch.setattr(cli, "build_notifier", lambda provider, **_: recorder)

    result = invoke(
        "retry-notifications",
        "--limit",
        "100",
        "--json",
        "--config",
        str(config),
        "--db",
        str(database),
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["delivered"] == 1
    assert payload["waiting_for_backoff"] == 0
    assert payload["sources"] == ["example-greenhouse"]
    assert len(recorder.messages) == 1
    with Repository.open(database) as repository:
        assert repository.pending_events("example-greenhouse") == []


def test_retry_notifications_refuses_without_pushover_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PUSHOVER_APP_TOKEN", raising=False)
    monkeypatch.delenv("PUSHOVER_USER_KEY", raising=False)
    config = make_config(tmp_path)

    result = invoke(
        "retry-notifications", "--limit", "10", "--config", str(config),
        "--db", str(tmp_path / "state.sqlite3"),
    )

    assert result.exit_code == 2
    assert "PUSHOVER_APP_TOKEN" in result.stderr


# ------------------------------------------------------------------ fixtures and helpers


@pytest.fixture()
def greenhouse_endpoint() -> Any:
    """The example Greenhouse board, served offline with an ETag."""
    payload = json.loads(GREENHOUSE_FIXTURE.read_text(encoding="utf-8"))
    with respx.mock(assert_all_called=False) as router:
        router.get(ENDPOINT).mock(
            return_value=httpx.Response(200, json=payload, headers={"ETag": '"cli-one"'})
        )
        yield router


COMPANY = """companies:
  - id: example
    name: Example Robotics
    categories: [Robotics/Autonomy]
"""

FALLBACK_COMPANY = """  - id: fallback
    name: Fallback Robotics
    categories: [Robotics/Autonomy]
"""

DIRECT_SOURCE = f"""sources:
  - id: example-greenhouse
    company_id: example
    provider: greenhouse
    source_class: documented_api
    support_status: SUPPORTED
    endpoint: {ENDPOINT}
    interval_minutes: 5
    tenant: example
    enabled: true
"""

FALLBACK_SOURCE = """  - id: fallback-discovery
    company_id: fallback
    provider: discovery
    source_class: fallback
    support_status: NEEDS_MANUAL_REVIEW
    endpoint: https://careers.fallback-robotics.com/
    interval_minutes: 30
    enabled: false
"""

DRY_RUN_FILTER = """filters:
  - id: all-jobs-dry-run
    enabled: true
    notify: false
"""

NOTIFY_FILTER = """  - id: production-matches
    enabled: true
    notify: true
"""


def make_config(
    tmp_path: Path,
    *,
    fallback_company: bool = False,
    notify_filter: bool = False,
) -> Path:
    """A minimal but complete configuration directory under the test's temporary path."""
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    companies = COMPANY + (FALLBACK_COMPANY if fallback_company else "")
    sources = DIRECT_SOURCE + (FALLBACK_SOURCE if fallback_company else "")
    filters = DRY_RUN_FILTER + (NOTIFY_FILTER if notify_filter else "")
    (config / "company_targets.yaml").write_text(companies, encoding="utf-8")
    (config / "sources.yaml").write_text(sources, encoding="utf-8")
    (config / "filters.yaml").write_text(filters, encoding="utf-8")
    return config


def seed_pending_event(database: Path) -> int:
    """One committed new-job event that no delivery attempt has resolved yet."""
    posting_url = "https://boards-api.greenhouse.io/v1/boards/example/jobs/6200000001"
    job = NormalizedJob(
        company_id="example",
        source_id="example-greenhouse",
        source_job_id="6200000001",
        canonical_job_key="greenhouse:example:6200000001",
        dedupe_key=f"url:example:{posting_url}",
        title="Robotics Software Engineer",
        locations=["Chicago, IL"],
        posting_url=posting_url,
        apply_url=posting_url,
        first_seen_at=NOW,
    )
    with Repository.open(database) as repository:
        repository.initialize()
        run_id = repository.start_run("example-greenhouse", NOW)
        repository.upsert_observations(run_id, [job])
        repository.finish_run(run_id, RunOutcome.COMPLETE, NOW, jobs_seen=1)
        return repository.record_events(
            run_id, [JobEvent(event_type=JobEventType.NEW, job=job, detected_at=NOW)]
        )[0]


class RecordingNotifier:
    """A notifier that records every message and always reports a delivery."""

    provider = "pushover"

    def __init__(self) -> None:
        self.messages: list[NotificationMessage] = []

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        self.messages.append(message)
        return DeliveryResult(
            provider=self.provider,
            delivered=True,
            retryable=False,
            http_status=200,
            provider_request_id=f"req-{len(self.messages)}",
        )
