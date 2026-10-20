# Fast company job alert

A browser-free monitor that polls official company job boards, detects newly public postings, and
sends deduplicated alerts to your Mac's Notification Center. Android alerts through Pushover come
next (the notifier exists; rollout is Task 28 of the plan). The design is in
`docs/superpowers/specs/2026-09-16-fast-company-job-alert-design.md` and the build plan in
`docs/superpowers/plans/2026-09-18-fast-company-job-alert-implementation.md`.

## Status

- 399 companies are registered, 268 have a verified direct source (67.17%), and 323 sources are
  enabled (`docs/collection-verification.md`). The 90% broad-activation gate does not pass, so only
  a canary filter with an `include_companies` allowlist may alert.
- Supported providers include Greenhouse, Lever, Ashby, SmartRecruiters, Workday, Eightfold,
  SuccessFactors, Oracle, Phenom, Jibe, Taleo, ADP, iCIMS and the other adapters listed in
  `src/job_alert/audit.py`. Generic JSON and JSON-LD remain available as fallbacks.
- Fifty-five provider-refused companies use an enabled, 60-minute Freehire company-feed fallback.
  It reads Freehire's keyless public API, keeps original source URLs, and is recorded as
  `FALLBACK_ONLY` rather than direct coverage.

## Local development

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --extra dev
uv run ruff check .
uv run mypy src
uv run pytest                      # offline; live probes are opt-in:
uv run pytest tests/live -m live   # reads the public boards of enabled sources
```

## Common commands

```sh
uv run job-alert --help
uv run job-alert collect --all --mode dry-run --report reports/dry-run.json
uv run job-alert baseline --all --confirm-seed
uv run job-alert run                       # scheduler, live-suppressed by default
uv run job-alert report coverage --markdown reports/coverage.md
uv run job-alert report health --json
uv run job-alert report openings --html reports/openings.html   # matching openings, hours open
uv run job-alert notify-test               # laptop notification (macos)
```

State lives in `data/job-alert.sqlite3` and runtime reports in `reports/`; both are ignored by git.
Secrets come only from the environment: `PUSHOVER_APP_TOKEN` and `PUSHOVER_USER_KEY`.

## Running on the laptop

`scripts/macos_launch_agent.py install` runs the monitor in the background at login, and
`scripts/macos_launch_agent.py page` opens the openings page it keeps current. See
`docs/operator-runbook.md` for the laptop setup, and for the container deployment (`deploy/`) that
the later always-on and mobile phase uses.
