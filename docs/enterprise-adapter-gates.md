# Enterprise adapter gates

> **Update 2026-10-14 (Task 29).** The Workday gate now passes: `src/job_alert/adapters/workday.py`
> exists and `nvidia-workday` is an enabled, scoped source. The record below describes the state
> on 2026-09-19 and is kept as history; the other four providers are unchanged.

Recorded 2026-09-19 against the registry in `config/sources.yaml` as committed at revision
`400dae2` - the base this note builds on, not the revision that added the note - and the persisted
audit in `reports/source-audit.json`. The note itself was first committed as `87c07a8` and corrected
in the commit that carries this sentence.

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

Workday is the one provider here that already has provider-specific code in the repository, and it is
worth naming so a grep for Workday is not confusing. `src/job_alert/audit.py` contains a Workday CXS
probe, `_walk_workday`, which walks up to `WORKDAY_PAGE_CAP` pages twice to gather audit evidence for
an existing feed and compares the posting identities. That is the audit's own read-only verification,
not an adapter: it is registered nowhere, it produces no collection, and there is no enabled Workday
source for it to run against. There is no `WorkdayAdapter` and no `src/job_alert/adapters/workday.py`.

## Audit-evidenced candidates

`parameters.audit_provider` records that a recognised provider marker appeared at or near an official
careers URL during the probe. It is not a statement that the provider's own job feed was reached, and
it is not a supported source: every one of these records is disabled, keeps support status
`NEEDS_MANUAL_REVIEW`, and has no proven pagination, stable identity, or direct application link.
They are candidates for a future adapter task, not an input to one, and their evidence is not equally
strong:

- Workday carries the most. The audit walked each CXS feed twice and recorded how many postings it
  returned against the board's declared total, which is why those records show a posting count. It is
  still not a complete listing: the walk stops after `WORKDAY_PAGE_CAP` pages, so `intel`,
  `crowdstrike`, `rockwell-automation`, `stryker`, `zebra-technologies`, and `boeing` each
  returned 100 postings (99 for `samsung`) against a far larger declared total.
  `boston-dynamics` returned all 77 of its declared postings twice, but the CXS list payload
  publishes no direct application URL, so pagination is the only thing proven for it and the record
  still stays unverified.
- The SuccessFactors candidates rest on a careers-page link alone, with no postings verified at audit
  time. `sap` and `bmw-group` are the weakest of that group: their recorded links carry
  `loginFlowRequired=true`.
- The iCIMS candidates and the Eightfold candidates also rest on a careers-page link alone. `arm` is
  the weakest candidate in this document: its recorded link is a login page,
  `https://earlycareers-arm.icims.com/jobs/login?loginOnly=1&redirect=dashboard`, so no public listing
  was observed for it at all.
- The Oracle candidates rest on a careers-page link that resolves to an Oracle Candidate Experience
  site, again with no postings verified.

The table below is scoped to tasks 13-17: it lists every audit-evidenced candidate for these five
providers, and nothing else. `parameters.audit_provider` records also exist for providers outside this
document - there is a `lever` record (`mobileye`) - so this is not the complete set of audit-evidenced
records in the registry.

| Provider | Candidates for this task |
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
