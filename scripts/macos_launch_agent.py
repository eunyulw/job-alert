"""Install the monitor as a macOS LaunchAgent, so laptop alerts run whenever you are logged in.

    python scripts/macos_launch_agent.py print [--mode live-suppressed|live]
    python scripts/macos_launch_agent.py install [--mode live-suppressed|live]
    python scripts/macos_launch_agent.py uninstall

The agent runs ``job-alert run --provider macos`` from this checkout's ``.venv`` (create it with
``uv sync``), keeps state in ``data/job-alert.sqlite3``, and logs to ``data/logs/``. It starts at
login, restarts if it exits, and waits five minutes between restarts so a refused LIVE gate does not
spin. A sleeping laptop does not poll; the loop resumes on wake.

``install`` replaces an existing agent with the same label. Only the standard library is used.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any

LABEL = "com.fastjobalert.monitor"
MODES = ("live-suppressed", "live")
LOG_ROLLOVER_BYTES = 10 * 1024 * 1024
RESTART_THROTTLE_SECONDS = 300
#: launchd starts agents with only the system directories on PATH.
AGENT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
ROOT = Path(__file__).resolve().parents[1]


def agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _logs(root: Path) -> tuple[Path, Path]:
    directory = root / "data" / "logs"
    return directory / "job-alert.out.log", directory / "job-alert.err.log"


def render(root: Path, *, mode: str) -> dict[str, Any]:
    """The LaunchAgent property list for a checkout at ``root``."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}, not {mode!r}")
    stdout, stderr = _logs(root)
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(root / ".venv" / "bin" / "job-alert"),
            "run",
            "--config",
            str(root / "config"),
            "--db",
            str(root / "data" / "job-alert.sqlite3"),
            "--mode",
            mode,
            "--provider",
            "macos",
            "--digest-report",
            str(root / "data" / "reports" / "daily-digest.md"),
        ],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {"PATH": AGENT_PATH},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": RESTART_THROTTLE_SECONDS,
        "ProcessType": "Background",
        "StandardOutPath": str(stdout),
        "StandardErrorPath": str(stderr),
    }


def roll_over_logs(paths: list[Path]) -> None:
    """Move a log past ``LOG_ROLLOVER_BYTES`` to ``<name>.1``; launchd never rotates them."""
    for path in paths:
        if path.is_file() and path.stat().st_size > LOG_ROLLOVER_BYTES:
            path.replace(path.with_name(path.name + ".1"))


def _launchctl(*arguments: str, check: bool) -> None:
    subprocess.run(["launchctl", *arguments], check=check)


def install(mode: str) -> int:
    executable = ROOT / ".venv" / "bin" / "job-alert"
    if not executable.is_file():
        print(f"missing {executable}: run 'uv sync' in {ROOT} first", file=sys.stderr)
        return 2
    agent = render(ROOT, mode=mode)
    stdout, stderr = _logs(ROOT)
    stdout.parent.mkdir(parents=True, exist_ok=True)
    roll_over_logs([stdout, stderr])
    target = agent_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{LABEL}", check=False)
    target.write_bytes(plistlib.dumps(agent))
    _launchctl("bootstrap", domain, str(target), check=True)
    print(f"installed {target} in {mode} mode; logs in {stdout.parent}")
    return 0


def uninstall() -> int:
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}", check=False)
    agent_path().unlink(missing_ok=True)
    print(f"removed {LABEL}")
    return 0


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("print", "install", "uninstall"))
    parser.add_argument("--mode", choices=MODES, default="live-suppressed")
    options = parser.parse_args(arguments)
    if options.command == "print":
        sys.stdout.buffer.write(plistlib.dumps(render(ROOT, mode=options.mode)))
        return 0
    if options.command == "install":
        return install(options.mode)
    return uninstall()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
