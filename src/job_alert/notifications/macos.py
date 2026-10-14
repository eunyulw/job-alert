"""macOS Notification Center delivery for a monitor running on the laptop it alerts.

Two local tools can post a notification. ``terminal-notifier`` (from Homebrew) is preferred when
it is on ``PATH``, because ``-open`` makes a click open the posting. ``osascript`` ships with macOS
and always works, but a click on its banner opens Script Editor, so the posting link is shown as
the subtitle instead.

Titles and bodies are untrusted provider text. Both tools are run without a shell, and the
AppleScript never contains message text: the script reads its title, body, and link from ``argv``,
so a title such as ``" & (do shell script "...") & "`` is only ever displayed. ``terminal-notifier``
parses ``-key value`` pairs, so a value that starts with ``-`` is prefixed with a space to keep it a
value.

Classification follows the HTTP providers. Exit status 0 is delivered: the notification was
handed to Notification Center, which may still hide it if notifications are disallowed for the
tool (``notify-test`` is how an operator checks). A non-zero exit, or a command that could not
start, never showed anything and is retryable. A timeout is retryable but ``uncertain``, because
the banner may already be on screen. When terminal-notifier fails (macOS refuses it until the user
allows it in System Settings › Notifications), the alert is shown through ``osascript`` instead;
a terminal-notifier timeout is not retried that way, since it may already have been shown.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable, Sequence

from job_alert.config import FilterPriority
from job_alert.notifications.base import (
    DeliveryResult,
    NotificationConfigurationError,
    NotificationMessage,
)

Runner = Callable[[Sequence[str], float], Awaitable[tuple[int, str]]]

DEFAULT_TIMEOUT_SECONDS = 10.0
SYSTEM_OSASCRIPT = "/usr/bin/osascript"
_ERROR_DETAIL_LIMIT = 300

_SCRIPT = (
    "on run argv",
    "display notification (item 2 of argv) with title (item 1 of argv) subtitle (item 3 of argv)",
    "end run",
)
_SCRIPT_WITH_SOUND = (
    "on run argv",
    "display notification (item 2 of argv) with title (item 1 of argv) subtitle (item 3 of argv)"
    ' sound name "default"',
    "end run",
)


async def run_command(argv: Sequence[str], timeout: float) -> tuple[int, str]:
    """Run one command without a shell and return its exit status and stderr."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    return process.returncode if process.returncode is not None else -1, stderr.decode(
        "utf-8", errors="replace"
    )


class MacOSNotifier:
    """Post one alert per call to the local Notification Center."""

    provider = "macos"

    def __init__(
        self,
        *,
        runner: Runner | None = None,
        which: Callable[[str], str | None] = shutil.which,
        platform: str = sys.platform,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if platform != "darwin":
            raise NotificationConfigurationError(
                f"the macos provider posts to Notification Center and needs macOS, not {platform}"
            )
        self._runner = run_command if runner is None else runner
        self._terminal_notifier = which("terminal-notifier")
        self._osascript = which("osascript") or SYSTEM_OSASCRIPT
        self._timeout = timeout_seconds

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        if self._terminal_notifier is None:
            return await self._attempt(self._osascript_argv(message))
        result = await self._attempt(self._terminal_notifier_argv(message))
        if result.delivered or result.uncertain:
            # A timeout may already have shown the banner; a second tool would duplicate it.
            return result
        # terminal-notifier is refused until macOS allows it to notify, so show the alert anyway.
        return await self._attempt(self._osascript_argv(message))

    async def _attempt(self, argv: list[str]) -> DeliveryResult:
        try:
            status, stderr = await self._runner(argv, self._timeout)
        except TimeoutError:
            return DeliveryResult(
                provider=self.provider,
                delivered=False,
                retryable=True,
                uncertain=True,
                error=f"{argv[0]} timed out after {self._timeout:g}s",
            )
        except OSError as exc:
            return DeliveryResult(
                provider=self.provider,
                delivered=False,
                retryable=True,
                error=f"{type(exc).__name__}: {exc}",
            )
        if status == 0:
            return DeliveryResult(provider=self.provider, delivered=True, retryable=False)
        detail = stderr.strip()[:_ERROR_DETAIL_LIMIT]
        return DeliveryResult(
            provider=self.provider,
            delivered=False,
            retryable=True,
            error=f"{argv[0]} failed with exit {status}" + (f": {detail}" if detail else ""),
        )

    def _terminal_notifier_argv(self, message: NotificationMessage) -> list[str]:
        assert self._terminal_notifier is not None
        argv = [
            self._terminal_notifier,
            "-title",
            _as_value(message.title),
            "-message",
            _as_value(message.body),
            "-open",
            message.url,
        ]
        return [*argv, "-sound", "default"] if message.priority is FilterPriority.HIGH else argv

    def _osascript_argv(self, message: NotificationMessage) -> list[str]:
        script = _SCRIPT_WITH_SOUND if message.priority is FilterPriority.HIGH else _SCRIPT
        lines = [part for line in script for part in ("-e", line)]
        return [self._osascript, *lines, message.title, message.body, message.url]


def _as_value(text: str) -> str:
    """Keep ``terminal-notifier`` from reading a value that starts with ``-`` as an option."""
    return f" {text}" if text.startswith("-") else text
