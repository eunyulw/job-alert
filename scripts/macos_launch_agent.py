"""Install the monitor as a macOS LaunchAgent, so laptop alerts run whenever you are logged in.

    python3 scripts/macos_launch_agent.py install [--mode live-suppressed|live]
    python3 scripts/macos_launch_agent.py status
    python3 scripts/macos_launch_agent.py page      # open the openings page in the browser
    python3 scripts/macos_launch_agent.py recent    # open the last-24-hours page
    python3 scripts/macos_launch_agent.py print [--mode live-suppressed|live]
    python3 scripts/macos_launch_agent.py uninstall

macOS privacy protection (TCC) stops a background agent from reading ``~/Documents``, ``~/Desktop``,
and ``~/Downloads``, where a checkout often lives. ``install`` therefore stages a self-contained
runtime in ``~/Library/Application Support/FastJobAlert``: a virtual environment built from
``uv.lock`` with the package installed (not linked), a copy of ``config/``, and the state database,
reports, and logs. Run ``install`` again after changing code or ``config/``; the database is kept.
On the first install a ``data/job-alert.sqlite3`` already seeded in the checkout is moved into the
runtime, so its baseline is not lost.

The agent runs ``job-alert run --provider macos``, starts at login, restarts if it exits, and waits
five minutes between restarts so a refused LIVE gate does not spin. A sleeping laptop does not poll;
the loop resumes on wake. Only the standard library is used; ``install`` needs ``uv`` on ``PATH``.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

LABEL = "com.fastjobalert.monitor"
MODES = ("live-suppressed", "live")
LOG_ROLLOVER_BYTES = 10 * 1024 * 1024
RESTART_THROTTLE_SECONDS = 300
#: launchd starts agents with only the system directories on PATH.
AGENT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path.home() / "Library" / "Application Support" / "FastJobAlert"


def agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def openings_page(runtime: Path) -> Path:
    return runtime / "reports" / "openings.html"


def database(runtime: Path) -> Path:
    return runtime / "data" / "job-alert.sqlite3"


def _logs(runtime: Path) -> tuple[Path, Path]:
    directory = runtime / "logs"
    return directory / "job-alert.out.log", directory / "job-alert.err.log"


def render(runtime: Path, *, mode: str) -> dict[str, Any]:
    """The LaunchAgent property list for a runtime staged at ``runtime``."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}, not {mode!r}")
    stdout, stderr = _logs(runtime)
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(runtime / "venv" / "bin" / "job-alert"),
            "run",
            "--config",
            str(runtime / "config"),
            "--db",
            str(database(runtime)),
            "--mode",
            mode,
            "--provider",
            "macos",
            "--digest-report",
            str(runtime / "reports" / "daily-digest.md"),
            "--openings-page",
            str(openings_page(runtime)),
        ],
        "WorkingDirectory": str(runtime),
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


def sync_config(source: Path, runtime: Path) -> None:
    """Replace the runtime's registry with the checkout's, so the agent reads what was committed."""
    target = runtime / "config"
    staging = runtime / "config.new"
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(source, staging)
    shutil.rmtree(target, ignore_errors=True)
    staging.replace(target)


def adopt_database(checkout_db: Path, runtime: Path) -> bool:
    """Move a database seeded in the checkout into an empty runtime; never overwrite one."""
    target = database(runtime)
    if target.exists() or not checkout_db.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    reader = sqlite3.connect(f"file:{checkout_db}?mode=ro", uri=True)
    try:
        writer = sqlite3.connect(target)
        try:
            reader.backup(writer)
        finally:
            writer.close()
    finally:
        reader.close()
    for suffix in ("", "-wal", "-shm"):
        Path(f"{checkout_db}{suffix}").unlink(missing_ok=True)
    return True


def _stage_environment(runtime: Path) -> None:
    """A virtual environment with the locked dependencies and an installed copy of the package."""
    venv = runtime / "venv"
    python = venv / "bin" / "python"
    requirements = runtime / "requirements.txt"
    subprocess.run(["uv", "venv", "--allow-existing", "--python", "3.12", str(venv)], check=True)
    subprocess.run(
        ["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes",
         "--output-file", str(requirements)],
        cwd=ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), "-r", str(requirements)], check=True
    )
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), "--no-deps", "--reinstall",
         "--no-cache", str(ROOT)],
        check=True,
    )


def _launchctl(*arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["launchctl", *arguments], capture_output=True, check=False)


def _wait_until_unloaded(domain: str, attempts: int = 20) -> None:
    """``bootout`` returns before the old instance is gone; wait for it, briefly."""
    for _ in range(attempts):
        if _launchctl("print", f"{domain}/{LABEL}").returncode != 0:
            return
        time.sleep(0.5)


def install(mode: str) -> int:
    if shutil.which("uv") is None:
        print("install needs uv on PATH: https://docs.astral.sh/uv/", file=sys.stderr)
        return 2
    RUNTIME.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{LABEL}")  # nothing to stop on a first install
    _stage_environment(RUNTIME)
    sync_config(ROOT / "config", RUNTIME)
    if adopt_database(ROOT / "data" / "job-alert.sqlite3", RUNTIME):
        print(f"moved the checkout's seeded database to {database(RUNTIME)}")
    stdout, stderr = _logs(RUNTIME)
    stdout.parent.mkdir(parents=True, exist_ok=True)
    roll_over_logs([stdout, stderr])
    target = agent_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plistlib.dumps(render(RUNTIME, mode=mode)))
    _wait_until_unloaded(domain)
    loaded = _launchctl("bootstrap", domain, str(target))
    if loaded.returncode != 0:
        # launchd can still be tearing the previous instance down; one late retry settles it.
        time.sleep(2)
        loaded = _launchctl("bootstrap", domain, str(target))
    if loaded.returncode != 0:
        print(loaded.stderr.decode(errors="replace").strip(), file=sys.stderr)
        return 1
    print(f"installed {LABEL} in {mode} mode; runtime and logs in {RUNTIME}")
    return 0


def status() -> int:
    state = _launchctl("print", f"gui/{os.getuid()}/{LABEL}")
    lines = state.stdout.decode(errors="replace").splitlines()
    loaded = state.returncode == 0
    print(f"agent: {'loaded' if loaded else 'not loaded'}")
    for line in lines:
        if line.strip().startswith(("state =", "pid =", "last exit code =")):
            print(f"  {line.strip()}")
    print(f"database: {database(RUNTIME)}")
    _, stderr = _logs(RUNTIME)
    if stderr.is_file():
        print(f"log: {stderr}")
        for line in stderr.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]:
            print(f"  {line}")
    return 0 if loaded else 1


def page(name: str = "openings.html") -> int:
    """Open a page the agent keeps current: the openings page, or the last-24-hours page."""
    target = openings_page(RUNTIME).with_name(name)
    if not target.is_file():
        print(f"no page yet at {target}: is the agent installed and running?", file=sys.stderr)
        return 1
    subprocess.run(["open", str(target)], check=False)
    print(target)
    return 0


def uninstall() -> int:
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    agent_path().unlink(missing_ok=True)
    print(f"removed {LABEL}; the runtime and its database stay in {RUNTIME}")
    return 0


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command", choices=("print", "install", "status", "page", "recent", "uninstall")
    )
    parser.add_argument("--mode", choices=MODES, default="live-suppressed")
    options = parser.parse_args(arguments)
    if options.command == "print":
        sys.stdout.buffer.write(plistlib.dumps(render(RUNTIME, mode=options.mode)))
        return 0
    if options.command == "install":
        return install(options.mode)
    if options.command == "status":
        return status()
    if options.command == "page":
        return page()
    if options.command == "recent":
        return page("recent.html")
    return uninstall()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
