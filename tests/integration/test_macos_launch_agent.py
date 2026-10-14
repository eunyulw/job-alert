"""The LaunchAgent the laptop release runs under: what it renders, never what launchctl does."""

from __future__ import annotations

import importlib.util
import plistlib
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "macos_launch_agent.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("macos_launch_agent", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNTIME = Path("/Users/someone/Library/Application Support/FastJobAlert")


def test_the_agent_runs_a_staged_runtime_outside_privacy_protected_folders() -> None:
    """launchd agents cannot read ~/Documents, so the agent touches nothing in the checkout."""
    agent = _module().render(RUNTIME, mode="live-suppressed")

    assert agent["Label"] == "com.fastjobalert.monitor"
    arguments = agent["ProgramArguments"]
    assert arguments[0] == str(RUNTIME / "venv" / "bin" / "job-alert")
    assert arguments[1] == "run"
    assert arguments[arguments.index("--mode") + 1] == "live-suppressed"
    assert arguments[arguments.index("--provider") + 1] == "macos"
    assert arguments[arguments.index("--config") + 1] == str(RUNTIME / "config")
    assert arguments[arguments.index("--db") + 1] == str(RUNTIME / "data" / "job-alert.sqlite3")
    assert arguments[arguments.index("--openings-page") + 1] == str(
        RUNTIME / "reports" / "openings.html"
    )
    paths = [part for part in arguments if part.startswith("/")]
    paths += [agent["WorkingDirectory"], agent["StandardOutPath"], agent["StandardErrorPath"]]
    assert all(Path(path).is_relative_to(RUNTIME) for path in paths)


def test_the_default_runtime_is_in_application_support() -> None:
    runtime = _module().RUNTIME
    assert runtime == Path.home() / "Library" / "Application Support" / "FastJobAlert"


def test_the_agent_restarts_after_failures_without_spinning() -> None:
    agent = _module().render(RUNTIME, mode="live")

    assert agent["RunAtLoad"] is True
    assert agent["KeepAlive"] is True
    # A refused LIVE gate exits 2 at once; launchd must not relaunch it every ten seconds.
    assert agent["ThrottleInterval"] >= 300
    assert agent["ProcessType"] == "Background"


def test_the_agent_can_find_homebrew_tools_and_logs_in_the_runtime() -> None:
    agent = _module().render(RUNTIME, mode="live")

    # launchd starts agents with /usr/bin:/bin:/usr/sbin:/sbin, which has no terminal-notifier.
    assert "/opt/homebrew/bin" in agent["EnvironmentVariables"]["PATH"].split(":")
    assert agent["StandardErrorPath"] == str(RUNTIME / "logs" / "job-alert.err.log")


def test_config_is_copied_into_the_runtime_replacing_the_old_copy(tmp_path: Path) -> None:
    module = _module()
    source = tmp_path / "checkout" / "config"
    source.mkdir(parents=True)
    (source / "filters.yaml").write_text("filters: []\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    (runtime / "config").mkdir(parents=True)
    (runtime / "config" / "stale.yaml").write_text("old", encoding="utf-8")

    module.sync_config(source, runtime)

    assert sorted(path.name for path in (runtime / "config").iterdir()) == ["filters.yaml"]


def test_a_seeded_checkout_database_moves_once_and_never_overwrites(tmp_path: Path) -> None:
    import sqlite3

    module = _module()
    checkout_db = tmp_path / "checkout" / "data" / "job-alert.sqlite3"
    checkout_db.parent.mkdir(parents=True)
    with sqlite3.connect(checkout_db) as connection:
        connection.execute("CREATE TABLE marker (value TEXT)")
        connection.execute("INSERT INTO marker VALUES ('seeded')")
    connection.close()
    runtime = tmp_path / "runtime"

    assert module.adopt_database(checkout_db, runtime) is True
    assert not checkout_db.exists()
    adopted = module.database(runtime)
    with sqlite3.connect(adopted) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone() == ("seeded",)
    connection.close()

    checkout_db.write_bytes(b"a later, different file")
    assert module.adopt_database(checkout_db, runtime) is False
    assert checkout_db.exists()


def test_only_the_two_running_modes_can_be_installed() -> None:
    module = _module()
    for mode in ("dry-run", "baseline", "anything"):
        with pytest.raises(ValueError, match="mode"):
            module.render(RUNTIME, mode=mode)


def test_print_emits_a_valid_property_list() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "print", "--mode", "live"],
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    agent = plistlib.loads(result.stdout)
    assert agent["Label"] == "com.fastjobalert.monitor"


def test_a_large_log_is_rolled_over_before_install(tmp_path: Path) -> None:
    module = _module()
    log = tmp_path / "job-alert.err.log"
    log.write_bytes(b"x" * (module.LOG_ROLLOVER_BYTES + 1))
    small = tmp_path / "job-alert.out.log"
    small.write_text("kept", encoding="utf-8")

    module.roll_over_logs([log, small])

    assert not log.exists()
    assert (tmp_path / "job-alert.err.log.1").stat().st_size == module.LOG_ROLLOVER_BYTES + 1
    assert small.read_text(encoding="utf-8") == "kept"
