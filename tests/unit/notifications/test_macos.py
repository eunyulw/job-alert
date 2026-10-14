"""The macOS notifier: argument construction, injection safety, and result classification.

Every test injects the subprocess runner and the tool lookup, so nothing here posts a notification.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from job_alert.config import FilterPriority
from job_alert.notifications.base import NotificationConfigurationError, NotificationMessage
from job_alert.notifications.macos import MacOSNotifier

TERMINAL_NOTIFIER = "/opt/homebrew/bin/terminal-notifier"
OSASCRIPT = "/usr/bin/osascript"


class FakeRunner:
    """Records each argv and answers with a queued exit code and stderr, or raises."""

    def __init__(self, *answers: tuple[int, str] | BaseException) -> None:
        self.answers = list(answers) or [(0, "")]
        self.calls: list[tuple[list[str], float]] = []

    async def __call__(self, argv: Sequence[str], timeout: float) -> tuple[int, str]:
        self.calls.append((list(argv), timeout))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def lookup(*available: str):  # type: ignore[no-untyped-def]
    paths = {"terminal-notifier": TERMINAL_NOTIFIER, "osascript": OSASCRIPT}

    def which(name: str) -> str | None:
        return paths[name] if name in available else None

    return which


def message(**overrides: object) -> NotificationMessage:
    values: dict[str, object] = {
        "title": "Acme Robotics: Robotics Engineer",
        "body": "Palo Alto, CA",
        "url": "https://jobs.acme.example/postings/1001",
    }
    values.update(overrides)
    return NotificationMessage.model_validate(values)


def notifier(runner: FakeRunner, *available: str) -> MacOSNotifier:
    return MacOSNotifier(runner=runner, which=lookup(*available), platform="darwin")


def test_a_host_that_is_not_macos_is_a_configuration_error() -> None:
    with pytest.raises(NotificationConfigurationError, match="macOS"):
        MacOSNotifier(runner=FakeRunner(), which=lookup("osascript"), platform="linux")


@pytest.mark.asyncio
async def test_terminal_notifier_is_preferred_so_a_click_opens_the_posting() -> None:
    runner = FakeRunner()

    result = await notifier(runner, "terminal-notifier", "osascript").send(message())

    assert result.delivered is True
    assert result.provider == "macos"
    argv, _ = runner.calls[0]
    assert argv == [
        TERMINAL_NOTIFIER,
        "-title",
        "Acme Robotics: Robotics Engineer",
        "-message",
        "Palo Alto, CA",
        "-open",
        "https://jobs.acme.example/postings/1001",
    ]


@pytest.mark.asyncio
async def test_a_leading_dash_cannot_be_read_as_a_terminal_notifier_option() -> None:
    runner = FakeRunner()

    await notifier(runner, "terminal-notifier").send(message(title="-remove ALL", body="-open x"))

    argv, _ = runner.calls[0]
    assert argv[argv.index("-title") + 1] == " -remove ALL"
    assert argv[argv.index("-message") + 1] == " -open x"


@pytest.mark.asyncio
async def test_high_priority_plays_the_default_sound() -> None:
    runner = FakeRunner()

    await notifier(runner, "terminal-notifier").send(message(priority=FilterPriority.HIGH))
    await notifier(runner, "osascript").send(message(priority=FilterPriority.HIGH))

    tn_argv, _ = runner.calls[0]
    assert tn_argv[-2:] == ["-sound", "default"]
    osa_argv, _ = runner.calls[1]
    assert 'sound name "default"' in " ".join(osa_argv)


@pytest.mark.asyncio
async def test_osascript_receives_untrusted_text_as_arguments_never_as_script() -> None:
    runner = FakeRunner()
    hostile = 'x" & (do shell script "touch /tmp/pwned") & "'

    result = await notifier(runner, "osascript").send(message(title=hostile, body=hostile))

    assert result.delivered is True
    argv, _ = runner.calls[0]
    assert argv[0] == OSASCRIPT
    script = [argv[i + 1] for i, part in enumerate(argv) if part == "-e"]
    assert script and all("touch" not in line for line in script)
    # After the script lines come exactly the three argv items the script reads.
    assert argv[-3:] == [hostile, hostile, "https://jobs.acme.example/postings/1001"]
    assert "on run argv" in script[0]


@pytest.mark.asyncio
async def test_osascript_is_found_at_its_system_path_without_a_lookup() -> None:
    runner = FakeRunner()

    await notifier(runner).send(message())

    assert runner.calls[0][0][0] == OSASCRIPT


@pytest.mark.asyncio
async def test_a_failed_command_is_retryable_and_reports_its_stderr() -> None:
    runner = FakeRunner((1, "execution error: Notifications are not allowed (-1743)\n"))

    result = await notifier(runner, "osascript").send(message())

    assert result.delivered is False
    assert result.retryable is True
    assert result.uncertain is False
    assert result.error is not None
    assert "exit 1" in result.error and "-1743" in result.error


@pytest.mark.asyncio
async def test_a_timeout_may_already_have_shown_the_alert() -> None:
    runner = FakeRunner(TimeoutError())

    result = await notifier(runner, "osascript").send(message())

    assert result.delivered is False
    assert result.retryable is True
    assert result.uncertain is True


@pytest.mark.asyncio
async def test_a_command_that_cannot_start_never_showed_anything() -> None:
    runner = FakeRunner(FileNotFoundError(2, "No such file or directory"))

    result = await notifier(runner, "osascript").send(message())

    assert result.delivered is False
    assert result.retryable is True
    assert result.uncertain is False
    assert result.error is not None and "FileNotFoundError" in result.error


@pytest.mark.asyncio
async def test_the_runner_is_given_a_bounded_timeout() -> None:
    runner = FakeRunner()

    await MacOSNotifier(
        runner=runner, which=lookup("osascript"), platform="darwin", timeout_seconds=7.5
    ).send(message())

    assert runner.calls[0][1] == 7.5


@pytest.mark.asyncio
async def test_the_default_runner_executes_without_a_shell(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The real runner, against harmless stand-ins for both notification tools."""
    marker = tmp_path / "args.txt"
    tool = tmp_path / "terminal-notifier"
    tool.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{marker}"\necho oops >&2\nexit 3\n')
    tool.chmod(0o755)
    # The fallback must hit a stub too, or the test would post a real notification.
    fallback = tmp_path / "osascript"
    fallback.write_text("#!/bin/sh\necho refused too >&2\nexit 4\n")
    fallback.chmod(0o755)
    tools = {"terminal-notifier": str(tool), "osascript": str(fallback)}

    result = await MacOSNotifier(which=tools.get, platform="darwin").send(
        message(title="a; rm -rf /", body="$(whoami)")
    )

    assert result.delivered is False
    assert result.error is not None and "exit 4" in result.error and "refused too" in result.error
    assert marker.read_text().splitlines()[:4] == ["-title", "a; rm -rf /", "-message", "$(whoami)"]


@pytest.mark.asyncio
async def test_a_refused_terminal_notifier_falls_back_to_osascript() -> None:
    """Until macOS allows terminal-notifier, the alert must still be shown, through osascript."""
    runner = FakeRunner(
        (3, "Could not request notification permission: Notifications are not allowed\n"),
        (0, ""),
    )

    result = await notifier(runner, "terminal-notifier", "osascript").send(message())

    assert result.delivered is True
    assert [argv[0] for argv, _ in runner.calls] == [TERMINAL_NOTIFIER, OSASCRIPT]


@pytest.mark.asyncio
async def test_when_both_tools_fail_the_alert_is_retried_later() -> None:
    runner = FakeRunner((3, "not allowed\n"), (1, "also refused\n"))

    result = await notifier(runner, "terminal-notifier", "osascript").send(message())

    assert result.delivered is False
    assert result.retryable is True
    assert result.error is not None and "also refused" in result.error
