# Production acceptance: laptop release (Task 26)

Evidence for each acceptance criterion that applies to laptop delivery. Android-specific criteria
(Pushover delivery, delay to the phone, quota headroom) are carried to Task 28.

## Delivery test

| Field | Value |
| --- | --- |
| Command | `uv run job-alert notify-test` (provider `macos`) |
| Sent | 2026-10-14T08:56:07Z |
| Host | macOS 15.4.1 |
| Tool | `osascript` (terminal-notifier not installed) |
| Result | accepted by Notification Center (exit 0) |
| Seen on screen | **pending: operator confirmation** |
| Click opens link | not applicable with `osascript`; re-test after installing terminal-notifier |

## Criteria

| Criterion | Status | Evidence |
| --- | --- | --- |
| Every company has an explicit source status | met | `docs/source-audit.md`, 148 of 148 |
| Direct coverage at least 90% or broad activation disabled | met (broad disabled) | 10 of 148; the CLI refuses LIVE for a filter without an allowlist |
| Two consecutive complete runs have no false new events | met | `docs/collection-verification.md`, baseline and immediate repeat |
| A synthetic added job emits exactly one event | met | `tests/integration/test_service_cycle.py`, `test_restart_and_delivery.py` |
| Baseline sent zero alerts | met | baseline report, 0 events, 0 notifications |
| Restart retained state | met | `tests/integration/test_restart_and_delivery.py` |
| Duplicate fixtures sent one alert | met | `tests/integration/test_service_cycle.py` (dedupe across boards) |
| Source failure did not close jobs | met | partial, challenge, and quarantine tests; Bosch partial cycles closed nothing |
| Laptop delivery and direct link succeed | pending | needs on-screen confirmation and the canary |
| Secrets absent from Git | met | laptop delivery uses no secret; `deploy/*.env` and `.env` are ignored |
| Health report identifies degraded sources | met | `job-alert report health`, scheduler digest tests |
| Canary: 24 h, no duplicates, links open, no open incident | pending | needs the user's filter choices |
| Detection delay by tier (median, p95, max) | pending | measured during the canary |
