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


def test_the_agent_runs_the_monitor_from_this_checkout_with_laptop_delivery() -> None:
    agent = _module().render(ROOT, mode="live-suppressed")

    assert agent["Label"] == "com.fastjobalert.monitor"
    arguments = agent["ProgramArguments"]
    assert arguments[0] == str(ROOT / ".venv" / "bin" / "job-alert")
    assert arguments[1] == "run"
    assert arguments[arguments.index("--mode") + 1] == "live-suppressed"
    assert arguments[arguments.index("--provider") + 1] == "macos"
    assert arguments[arguments.index("--config") + 1] == str(ROOT / "config")
    assert arguments[arguments.index("--db") + 1] == str(ROOT / "data" / "job-alert.sqlite3")
    assert all(Path(part).is_absolute() for part in arguments if part.startswith("/"))
    assert agent["WorkingDirectory"] == str(ROOT)


def test_the_agent_restarts_after_failures_without_spinning() -> None:
    agent = _module().render(ROOT, mode="live")

    assert agent["RunAtLoad"] is True
    assert agent["KeepAlive"] is True
    # A refused LIVE gate exits 2 at once; launchd must not relaunch it every ten seconds.
    assert agent["ThrottleInterval"] >= 300
    assert agent["ProcessType"] == "Background"


def test_the_agent_can_find_homebrew_tools_and_logs_under_data() -> None:
    agent = _module().render(ROOT, mode="live")

    # launchd starts agents with /usr/bin:/bin:/usr/sbin:/sbin, which has no terminal-notifier.
    assert "/opt/homebrew/bin" in agent["EnvironmentVariables"]["PATH"].split(":")
    assert agent["StandardOutPath"].startswith(str(ROOT / "data" / "logs"))
    assert agent["StandardErrorPath"].startswith(str(ROOT / "data" / "logs"))


def test_only_the_two_running_modes_can_be_installed() -> None:
    module = _module()
    for mode in ("dry-run", "baseline", "anything"):
        with pytest.raises(ValueError, match="mode"):
            module.render(ROOT, mode=mode)


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
