# Production acceptance: laptop release (Task 26)

Evidence for each acceptance criterion that applies to laptop delivery. Android-specific criteria
(Pushover delivery, delay to the phone, quota headroom) are carried to Task 28.

## Delivery test

| Field | Value |
| --- | --- |
| Command | `uv run job-alert notify-test` (provider `macos`) |
| Sent | 2026-10-14T08:56:07Z, resent 2026-10-14T09:41:20Z |
| Host | macOS 15.4.1 |
| Tool | `osascript` (terminal-notifier install blocked by the untrusted Homebrew tap `osrf/simulation`) |
| Result | accepted by Notification Center (exit 0) |
| Seen on screen | yes: confirmed by the user for the 09:41 send |
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
| Laptop delivery and direct link succeed | delivery met; link pending | test notification seen 2026-10-14; link check needs terminal-notifier and the canary |
| Secrets absent from Git | met | laptop delivery uses no secret; `deploy/*.env` and `.env` are ignored |
| Health report identifies degraded sources | met | `job-alert report health`, scheduler digest tests |
| Canary: 24 h, no duplicates, links open, no open incident | running | started 2026-10-14 ~10:10 UTC; see below |
| Detection delay by tier (median, p95, max) | pending | measured during the canary |

## Canary

| Field | Value |
| --- | --- |
| Filters | `production-matches` (title contains `intern` or `co-op`) and `production-matches-intern-type` (Ashby employment type `Intern`) |
| Companies | 1X, Standard Bots, Intrinsic, Serve Robotics, Neuralink (Ashby and Greenhouse) |
| Approved by | the user, 2026-10-14: internships in robotics and mechanical engineering, every internship at the robotics canary |
| Baseline | 2026-10-14, 10 sources complete, 6,596 postings, 0 events; repeat had 0 would-notify |
| Runtime | LaunchAgent `com.fastjobalert.monitor`, `--mode live --provider macos`, runtime in `~/Library/Application Support/FastJobAlert` |
| First live wake | all cycles complete; `bosch-smartrecruiters` found 2 new postings and correctly sent nothing (not on the allowlist) |
