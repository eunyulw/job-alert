# Enterprise adapter gates

Recorded 2026-09-19 against the committed registry in `config/sources.yaml` (HEAD `400dae2`) and
the persisted audit in `reports/source-audit.json`.

Tasks 13-17 of the fast company job alert implementation are the enterprise adapter tasks: Workday,
SAP SuccessFactors, iCIMS, Eightfold, and Oracle Recruiting/Taleo. Each brief opens with a gate - count
the enabled audited sources for that provider, and treat a count of zero as "task not applicable"
without inventing an endpoint. All five gates return zero, so none of the five adapters was written.

## Why the record lives here

`docs/source-audit.md` is regenerated in full by
`job-alert audit report --markdown docs/source-audit.md`: the renderer assembles the whole document
and replaces the file, so a section appended there is erased by the next report run (verified by
appending a marker section, running the command, and observing the marker disappear). This file is
committed separately so the gate outcome stays visible to a future session.

## Gate command

Run once per provider, substituting the provider name:

```sh
env UV_CACHE_DIR=/tmp/job-alert-uv-cache uv run python -c 'from pathlib import Path; from job_alert.config import load_app_config; c=load_app_config(Path("config")); print(sum(s.enabled and s.provider=="workday" for s in c.sources))'
```

## Gate results

| Task | Provider | Enabled audited sources | Audit-evidenced candidates (disabled, unverified) | Status |
| --- | --- | --- | --- | --- |
| 13 | `workday` | 0 | 8 | Task not applicable |
| 14 | `successfactors` | 0 | 7 | Task not applicable |
| 15 | `icims` | 0 | 2 | Task not applicable |
| 16 | `eightfold` | 0 | 2 | Task not applicable |
| 17 | `oracle` | 0 | 2 | Task not applicable |

Every row carries the same consequence: no adapter module, test, fixture, or registry entry exists for
the provider, no endpoint, credential, or sample response was invented, and each company keeps the
audited classification already recorded in `config/sources.yaml`.

## Audit-evidenced candidates

`parameters.audit_provider` is evidence that a recognised enterprise provider appeared on an official
careers page. It is not a supported source: every one of these records is disabled, keeps support
status `NEEDS_MANUAL_REVIEW`, and has no proven pagination, stable identity, or direct application
link. They are candidates for a future adapter task, not an input to one.

| Provider | Companies |
| --- | --- |
| `workday` | `samsung`, `intel`, `crowdstrike`, `boston-dynamics`, `rockwell-automation`, `stryker`, `zebra-technologies`, `boeing` |
| `successfactors` | `sap`, `keyence`, `epson-robots`, `doosan-robotics`, `bmw-group`, `tata-motors-jlr`, `zf` |
| `icims` | `arm`, `daifuku` |
| `eightfold` | `qualcomm`, `lockheed-martin` |
| `oracle` | `honeywell`, `emerson` |

These 21 records are part of the 45 `NEEDS_MANUAL_REVIEW` companies in the audit coverage summary
(148 companies audited: 5 `SUPPORTED`, 5 `SUPPORTED_UNDOCUMENTED`, 0 `HTML_ONLY`, 55
`FALLBACK_ONLY`, 38 `BLOCKED`, 45 `NEEDS_MANUAL_REVIEW`). The only enabled sources in the registry
belong to providers with a working adapter: `greenhouse` (3), `ashby` (5), `lever` (1), and
`smartrecruiters` (1).

## Picking the work up later

An adapter task for one of these providers becomes real work only when its gate returns a positive
count, which requires a verified source: a probed public endpoint that yields a complete, repeatable
listing with stable posting ids and direct HTTPS application links. Until then the provider stays
without an adapter, and these companies keep their audited status.

