"""Deployment policy: what the image and Compose file must and must not contain, and the two
operator scripts that run beside the service (the container health check and the SQLite backup).

The file checks read the committed deployment files as text and YAML; the script checks run the
real scripts in a subprocess against databases under ``tmp_path``. Nothing here needs Docker.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from job_alert.db import Repository

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "deploy" / "Dockerfile"
COMPOSE = ROOT / "deploy" / "compose.yaml"
ENV_EXAMPLE = ROOT / "deploy" / "job-alert.env.example"
HEALTHCHECK = ROOT / "deploy" / "healthcheck.py"
BACKUP = ROOT / "scripts" / "backup_sqlite.py"
SECRETS = ("PUSHOVER_APP_TOKEN", "PUSHOVER_USER_KEY")


def _service() -> dict[str, object]:
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    service = document["services"]["job-alert"]
    assert isinstance(service, dict)
    return service


# ------------------------------------------------------------------ compose


def test_compose_mounts_persistent_data_and_does_not_embed_secrets() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    assert "job-alert-data:/app/data" in compose
    for name in SECRETS:
        # The value is always interpolated from the operator's environment, never written here.
        assert f"{name}: ${{{name}" in compose
    assert "secret-token" not in compose
    assert "job-alert-data" in yaml.safe_load(compose)["volumes"]


def test_compose_runs_read_only_without_ports_and_restarts() -> None:
    service = _service()
    assert service["read_only"] is True
    assert service["restart"] == "unless-stopped"
    assert "ports" not in service
    assert any(str(mount).startswith("/tmp") for mount in service["tmpfs"])  # type: ignore[union-attr]
    logging = service["logging"]
    assert isinstance(logging, dict)
    assert logging["options"] == {"max-size": "10m", "max-file": "3"}


def test_compose_mounts_the_registry_read_only() -> None:
    volumes = [str(volume) for volume in _service()["volumes"]]  # type: ignore[union-attr]
    assert "../config:/app/config:ro" in volumes


def test_compose_defaults_to_suppressed_mode_and_writes_reports_to_the_volume() -> None:
    command = [str(part) for part in _service()["command"]]  # type: ignore[union-attr]
    assert command[:2] == ["job-alert", "run"]
    assert command[command.index("--mode") + 1] == "${JOB_ALERT_MODE:-live-suppressed}"
    # The root filesystem is read-only, so every file the loop writes must live on the volume.
    assert command[command.index("--digest-report") + 1].startswith("/app/data/")
    assert command[command.index("--db") + 1] == "/app/data/job-alert.sqlite3"


def test_example_env_contains_names_but_no_values() -> None:
    lines = [
        line.strip()
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for name in SECRETS:
        assert f"{name}=" in lines
    assert all(line.endswith("=") or line.startswith("JOB_ALERT_MODE=") for line in lines)


def test_real_env_files_are_ignored() -> None:
    ignored = subprocess.run(
        ["git", "check-ignore", "deploy/job-alert.env", ".env"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ignored.stdout.split() == ["deploy/job-alert.env", ".env"]


# ------------------------------------------------------------------ image


def test_image_runs_as_an_unprivileged_user_and_publishes_no_port() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "USER jobalert" in dockerfile
    assert "EXPOSE" not in dockerfile
    assert "FROM python:3.12-slim" in dockerfile
    assert "HEALTHCHECK" in dockerfile


def test_image_installs_dependencies_before_the_project_from_the_lockfile() -> None:
    lines = DOCKERFILE.read_text(encoding="utf-8").splitlines()
    dependencies = next(i for i, line in enumerate(lines) if "--no-install-project" in line)
    source = next(i for i, line in enumerate(lines) if line.startswith("COPY src"))
    project = next(i for i, line in enumerate(lines) if "--no-editable" in line)
    assert dependencies < source < project
    assert all("--frozen" in lines[i] for i in (dependencies, project))


def test_image_pins_the_uv_release() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "ghcr.io/astral-sh/uv:0.11.3" in dockerfile


# ------------------------------------------------------------------ health check


def _seed_state(database: Path, last_complete_at: datetime | None) -> None:
    with Repository.open(database) as repository:
        repository.initialize()
    if last_complete_at is None:
        return
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO source_state (source_id, last_complete_at) VALUES (?, ?)",
            ("acme-greenhouse", last_complete_at.isoformat()),
        )


def _healthcheck(database: Path) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ, JOB_ALERT_DB=str(database))
    environment["JOB_ALERT_HEALTH_MAX_AGE_MINUTES"] = "65"
    return subprocess.run(
        [sys.executable, str(HEALTHCHECK)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_healthcheck_fails_without_a_database(tmp_path: Path) -> None:
    database = tmp_path / "missing.sqlite3"
    result = _healthcheck(database)
    assert result.returncode != 0
    assert "database missing" in result.stderr
    assert not database.exists()


def test_healthcheck_explains_a_database_without_the_schema(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    sqlite3.connect(database).close()
    result = _healthcheck(database)
    assert result.returncode != 0
    assert "cannot read" in result.stderr
    assert "Traceback" not in result.stderr


def test_healthcheck_fails_before_any_complete_run(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    _seed_state(database, None)
    result = _healthcheck(database)
    assert result.returncode != 0
    assert "no complete source run" in result.stderr


def test_healthcheck_fails_when_the_newest_complete_run_is_stale(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    _seed_state(database, datetime.now(UTC) - timedelta(minutes=90))
    result = _healthcheck(database)
    assert result.returncode != 0
    assert "stale" in result.stderr


def test_healthcheck_passes_after_a_recent_complete_run(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    _seed_state(database, datetime.now(UTC) - timedelta(minutes=4))
    result = _healthcheck(database)
    assert result.returncode == 0, result.stderr


# ------------------------------------------------------------------ backup and restore


def _backup(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BACKUP), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def _seeded_database(path: Path, *, marker: str) -> Path:
    _seed_state(path, datetime(2026, 10, 14, 12, 0, tzinfo=UTC))
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE source_state SET last_error = ?", (marker,))
    return path


def test_backup_writes_a_verified_copy_and_prints_its_path(tmp_path: Path) -> None:
    database = _seeded_database(tmp_path / "state.sqlite3", marker="original")
    backups = tmp_path / "backups"

    result = _backup("backup", str(database), str(backups))

    assert result.returncode == 0, result.stderr
    target = Path(result.stdout.strip())
    assert target.parent == backups
    assert target.name.startswith("job-alert-") and target.suffix == ".sqlite3"
    with sqlite3.connect(target) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT last_error FROM source_state").fetchone() == (
            "original",
        )


def test_backup_keeps_only_the_newest_fourteen(tmp_path: Path) -> None:
    database = _seeded_database(tmp_path / "state.sqlite3", marker="original")
    backups = tmp_path / "backups"
    backups.mkdir()
    for day in range(1, 17):
        stale = backups / f"job-alert-202609{day:02d}T000000Z.sqlite3"
        stale.write_bytes(b"")
        os.utime(stale, (day * 86400, day * 86400))
    unrelated = backups / "notes.txt"
    unrelated.write_text("kept", encoding="utf-8")

    result = _backup("backup", str(database), str(backups))

    assert result.returncode == 0, result.stderr
    kept = sorted(path.name for path in backups.glob("job-alert-*.sqlite3"))
    assert len(kept) == 14
    assert Path(result.stdout.strip()).name in kept
    # The oldest files go first, and nothing that is not a backup is ever touched.
    assert "job-alert-20260901T000000Z.sqlite3" not in kept
    assert unrelated.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["backup"],
        ["backup", "state.sqlite3"],
        ["backup", "state.sqlite3", "/"],
        ["backup", "", "backups"],
        ["restore", "only-one"],
        ["unknown", "a", "b"],
    ],
)
def test_backup_refuses_unusable_arguments(tmp_path: Path, arguments: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, str(BACKUP), *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "usage" in result.stderr


def test_backup_refuses_a_database_that_does_not_exist(tmp_path: Path) -> None:
    result = _backup("backup", str(tmp_path / "missing.sqlite3"), str(tmp_path / "backups"))
    assert result.returncode == 2
    assert "does not exist" in result.stderr
    assert not (tmp_path / "backups").exists()


def test_restore_replaces_the_database_with_a_verified_backup(tmp_path: Path) -> None:
    database = _seeded_database(tmp_path / "state.sqlite3", marker="original")
    backed_up = _backup("backup", str(database), str(tmp_path / "backups"))
    assert backed_up.returncode == 0, backed_up.stderr
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE source_state SET last_error = 'changed after backup'")

    result = _backup("restore", backed_up.stdout.strip(), str(database))

    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT last_error FROM source_state").fetchone() == (
            "original",
        )


def test_restore_refuses_a_file_that_is_not_a_database(tmp_path: Path) -> None:
    database = _seeded_database(tmp_path / "state.sqlite3", marker="original")
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a database at all, just bytes" * 200)

    result = _backup("restore", str(corrupt), str(database))

    assert result.returncode == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT last_error FROM source_state").fetchone() == (
            "original",
        )
