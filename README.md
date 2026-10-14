# Fast company job alert

A browser-free monitor that polls official company job boards, detects newly public postings, and
sends deduplicated Android alerts through Pushover. The design is in
`docs/superpowers/specs/2026-09-16-fast-company-job-alert-design.md` and the build plan in
`docs/superpowers/plans/2026-09-18-fast-company-job-alert-implementation.md`.

## Status

- 148 companies audited (`docs/source-audit.md`), 10 with a verified direct source
  (`docs/collection-verification.md`). The 90% broad-activation gate does not pass, so LIVE alerts are
  refused and the deployment runs `live-suppressed`.
- Supported providers: Greenhouse, Lever, Ashby, SmartRecruiters, plus generic JSON and JSON-LD
  fallbacks. Enterprise ATS adapters are gated on audit evidence (`docs/enterprise-adapter-gates.md`).

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
uv run job-alert notify-test --provider pushover
```

State lives in `data/job-alert.sqlite3` and runtime reports in `reports/`; both are ignored by git.
Secrets come only from the environment: `PUSHOVER_APP_TOKEN` and `PUSHOVER_USER_KEY`.

## Deployment

`deploy/` holds a non-root, read-only container with persistent state on a named volume. See
`docs/operator-runbook.md` for deployment, baseline, live enablement, backup and restore, and
incident handling.
