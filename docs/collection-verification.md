# All-company collection verification (Task 24)

Recorded 2026-10-14 (UTC) against the registry in `config/sources.yaml` as committed with this note,
on top of `9fef095`. This file is written by hand and is not regenerated: `docs/source-audit.md` is
rewritten in full by `audit report` and `report coverage`, so evidence kept there would be erased.

Runtime reports (`reports/*.json`) and the verification database (`data/verification.sqlite3`) hold
volatile job inventories and are not committed. The figures below are copied from them.

## Go/no-go summary

| Check | Result |
| --- | --- |
| Offline gate (`ruff`, `mypy src`, `pytest -m "not live"`, `git diff --check`) | pass, 629 tests |
| Coverage of `identity`, `diff`, `config`, `filters`, every enabled adapter | 93-100% each, 90% total |
| Every enabled source has a registered adapter and a direct audited status | pass (10 of 10) |
| Dry run against every enabled source | 10 of 10 `complete: true`, no warnings |
| Baseline | 10 of 10 complete, 6,584 postings, 0 events, 0 notifications |
| Immediate repeat (`live-suppressed`) | 10 of 10 complete, 0 new, 0 closed, 0 notifications |
| Independent board cross-check | 7 companies, all 4 adapter families, no discrepancy |
| Live contract probes (`pytest tests/live -m live`) | 4 of 4 pass; the Ashby probe previously skipped because it accepted only `SUPPORTED`, not `SUPPORTED_UNDOCUMENTED` |
| Broad-activation gate (90% direct coverage) | does not pass: 10 of 148 companies (6.76%) |

The collection workflow is verified for the ten enabled sources. Broad activation remains refused by
the CLI until direct coverage reaches 90%. Task 26 can proceed only as the five-company canary.

## Polling tiers

Before this task every enabled source polled every 30 minutes, the `audit set-source` default, which
contradicted the tiers in the plan's Global Constraints and the spec's 6- and 11-minute detection
objectives. The registry now polls each enabled source at its class tier, the loader refuses an
enabled source polled faster than its tier, and a test pins every enabled source to exactly its tier.

| Tier | Sources |
| --- | --- |
| `documented_api`, 5 min | `anthropic-greenhouse`, `intrinsic-greenhouse`, `neuralink-greenhouse`, `palantir-lever`, `bosch-smartrecruiters` |
| `public_ats`, 10 min | `cohere-ashby`, `mistral-ai-ashby`, `serve-robotics-ashby`, `1x-ashby`, `standard-bots-ashby` |

## Dry run, 2026-10-14T08:29Z

`uv run job-alert collect --all --mode dry-run --db data/verification.sqlite3 --report reports/all-sources-dry-run.json`

A dry run is stored with outcome `partial` and `error_code: dry_run` by design, so it can never become
the snapshot the absence logic reads; `complete` is the collection's own verdict.

| Source | Complete | Seconds | Pages | Declared | Parsed | Stable id | Direct URL |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `palantir-lever` | yes | 3.17 | 5 | - | 312 | 100% | 100% |
| `anthropic-greenhouse` | yes | 0.88 | 1 | 647 | 647 | 100% | 100% |
| `mistral-ai-ashby` | yes | 0.16 | 1 | - | 214 | 100% | 100% |
| `cohere-ashby` | yes | 0.08 | 1 | - | 117 | 100% | 100% |
| `intrinsic-greenhouse` | yes | 0.07 | 1 | 22 | 22 | 100% | 100% |
| `serve-robotics-ashby` | yes | 0.16 | 1 | - | 33 | 100% | 100% |
| `bosch-smartrecruiters` | yes | 25.46 | 51 | 5019 | 5019 | 100% | 100% |
| `1x-ashby` | yes | 0.12 | 1 | - | 100 | 100% | 100% |
| `standard-bots-ashby` | yes | 0.07 | 1 | - | 39 | 100% | 100% |
| `neuralink-greenhouse` | yes | 0.19 | 1 | 81 | 81 | 100% | 100% |

Lever and Ashby publish no board total, so their completeness rests on the independent cross-check
below. `1x-ashby` returning exactly 100 postings was checked as a possible cap: its board lists the
same 100 ids, and two other Ashby boards from the same API return 117 and 214.

## Discrepancies resolved before baselining

### `bosch-smartrecruiters`: postings published mid-walk

The first baseline attempt failed for Bosch on two consecutive cycles (`partial_collection`, 5006
unique of 5007 declared), after two complete collections minutes earlier. The SmartRecruiters list is
ordered newest first and the 51-page walk takes about 25 seconds, while the board grew from 5007 to
5020 postings in about twenty minutes. A posting published mid-walk lands ahead of the offset: every
later record shifts one place, a record repeats at the next page boundary (observed at indices 1300
and 3600), and the new posting sits on the head page the walk already left.

Fixed test-first in `src/job_alert/adapters/smartrecruiters.py`, not waived:

1. A walk that ends short of `totalFound` re-reads the head page once. If that recovers exactly the
   missing postings, the repeats it explains are dropped and the walk is complete.
2. If the only remaining defects are a repeated id or a count mismatch, the board is walked once more
   and that walk's verdict stands. A loop, the page cap, or an unusable record is never re-walked.

After the fix, Bosch completed the baseline, the repeat, and six further consecutive cycles: four
needed nothing, one was recovered by the head re-read (53 requests), and one by the re-walk (about 52
seconds). A failed walk still fails closed and never closes jobs.

This investigation also made two pipeline changes:

- A failed cycle's report kept only "adapter returned an incomplete collection". It now carries the
  adapter's own warnings, which is how the page-boundary repeats above were found.
- **A source that has never applied a complete snapshot is now seeded by its first one.** Before this
  change, Bosch's failed baseline let a later `live-suppressed` cycle record 4,907 pending `new` events
  for postings that were already public, which a switch to `live` would have announced. This applied
  to any source enabled after the initial seeding too. A first complete snapshot outside `baseline`
  mode now stores jobs, stages no event, and reports `seeded: true`. The spec names baseline seeding
  as the alert-flood mitigation, so a missing baseline must not be able to bypass it. The affected
  verification database was discarded and every step re-run from an empty one.

## Baseline and immediate repeat, 2026-10-14T08:29-08:30Z

```sh
uv run job-alert baseline --all --confirm-seed --db data/verification.sqlite3 --report reports/baseline.json
uv run job-alert collect --all --mode live-suppressed --db data/verification.sqlite3 --report reports/post-baseline.json
```

The baseline seeded all ten sources (6,584 postings) with no event staged and nothing sent. The repeat
31 seconds later was complete for all ten with zero new, reopened, or closed postings, so there was no
real new event to verify manually. This database is local evidence only; the deployed container
seeds its own baseline through the Task 25 runbook.

## Independent board cross-check, 2026-10-14T08:15Z

Each sampled source was collected through its adapter and compared with an ordinary HTTPS read of
the board a person uses, never the adapter's own endpoint. Three sampled postings per company were
opened at their collected `posting_url`; every one answered HTTP 200 and showed the collected title.
This is an automated read, not a human visual check: the Task 26 operator repeats a visual check of
the same companies on a phone before the canary.

| Company | Family | Human-facing board | Board count | Collected | Ids compared |
| --- | --- | --- | --- | --- | --- |
| Anthropic | greenhouse | `job-boards.greenhouse.io/anthropic` | 647 ("647 jobs") | 647 | page one: 50 of 50 collected |
| Intrinsic | greenhouse | `job-boards.greenhouse.io/embed/job_board?for=intrinsicrobotics` | 22 | 22 | 22 of 22, both directions |
| Neuralink | greenhouse | `job-boards.greenhouse.io/embed/job_board?for=neuralink` (2 pages) | 81 | 81 | 81 of 81, both directions |
| Palantir | lever | `jobs.lever.co/palantir` | 312 | 312 (5 pages) | 312 of 312, titles equal |
| Cohere | ashby | `jobs.ashbyhq.com/cohere` | 117 | 117 | 117 of 117, titles equal |
| 1X | ashby | `jobs.ashbyhq.com/1x` | 100 | 100 | 100 of 100, titles equal |
| Bosch | smartrecruiters | `careers.smartrecruiters.com/BoschGroup` | not published | 5005 of 5005 declared (51 pages) | sampled posting pages only |

Intrinsic's and Neuralink's plain Greenhouse boards redirect to their company careers pages, so their
embeddable Greenhouse board was read instead. Bosch's careers page is a country index that publishes
no total over ordinary HTTP, so Bosch rests on the API's declared total and the sampled posting pages.

Sample ids and titles:

| Company | Posting id | Title |
| --- | --- | --- |
| Anthropic | 5418323008 | Performance Engineer, Inference Engine |
| Anthropic | 5183044008 | Anthropic Fellows Program, AI Safety & Security |
| Intrinsic | 5832787004 | Senior Full Stack Software Engineer, Platform |
| Intrinsic | 5989863004 | Executive Business Partner |
| Neuralink | 6353417003 | Software Engineer, Implant Manufacturing |
| Neuralink | 6576326003 | Robot Optics Intern |
| Palantir | 1124402c-2088-40c6-b6aa-b9ef7777519b | Information Security Engineer |
| Palantir | b81be534-e1d6-4946-8121-1e1360d4d030 | Deployment Strategist |
| Cohere | f154a1cb-b7c1-4f74-ac50-04ef9dcfc9e0 | Solutions Architect - UAE |
| Cohere | cb981ecd-a161-482c-8d8e-5f19bb6e7fdd | Senior Security Engineer |
| 1X | 19004256-4253-41d3-baa7-1f25bbd125be | Staff Firmware Engineer - Charger |
| 1X | 0651ab56-3763-4e51-9bd8-35653ffad62c | Coatings and Materials Manufacturing Engineer |
| Bosch | 744000134444186 | Praktikum in der Fertigungsplanung - Montage, Hydraulische Prüfung, Lackieren |
| Bosch | 744000152900001 | Estudiante en prácticas – Departamento de Gestión de Calidad (QMM-P) |

## Category coverage

The plan asks for two sampled companies per category. Only ten sources are enabled, so a category is
sampled as far as it has enabled companies, and the shortfall is recorded instead of waived.

| Category | Companies | Enabled | Sampled | Gap |
| --- | --- | --- | --- | --- |
| `big-tech-ai` | 45 | Anthropic, Palantir, Mistral AI, Cohere | Anthropic, Palantir, Cohere | none |
| `robotics-autonomy-physical-ai` | 74 | Intrinsic, Serve Robotics, Bosch, 1X, Standard Bots, Neuralink | Intrinsic, 1X, Bosch, Neuralink | none |
| `aerospace-defense` | 10 | Palantir | Palantir | one enabled company |
| `medical-robotics` | 6 | Neuralink | Neuralink | one enabled company |
| `industrial-automation-advanced-manufacturing` | 22 | Bosch | Bosch | one enabled company |
| `automotive-ev-autonomous-mobility` | 37 | none | none | **no enabled source** |

Every enabled adapter family was sampled: greenhouse (3), lever (1), ashby (2), smartrecruiters (1).
An automotive alert cannot be delivered until a source in that category is verified, and Task 26's
category-by-category expansion has nothing to enable for it.

## Bosch request volume

At the 5-minute `documented_api` tier, Bosch costs 51 requests per cycle, about 612 an hour, and up to
twice that in a cycle that needs the re-walk. No `429` was observed during this task. If one appears,
the runbook's back-off is to raise Bosch's `interval_minutes` (slower than the tier is allowed).

## Expansion log (Task 29)

Each company passes the per-company gate in Task 29 before it joins the alert allowlist. "Live
check" is two consecutive adapter collections on the date shown.

| Phase | Company | Source | Scope | Live check | Allowlisted |
| --- | --- | --- | --- | --- | --- |
| 1 | Amazon | `amazon-amazon` (`company_json`, 15 min) | `base_query=intern` (hits capped at 10,000) | 2026-10-14: 428 twice, identical keys, 5 pages, 100% ids and links | 2026-10-14, expansion filter |
| 1 | NVIDIA | `nvidia-workday` (`public_ats`, 10 min) | facet `workerSubType = Intern (Fixed Term)` (total capped at 2,000) | 2026-10-14: 163 twice, identical keys, 9 pages, 100% ids and links | 2026-10-14, expansion filter |
| 1 | Apple | `apple-apple` (`html`, 30 min) | `team=internships-STDNT-INTRN` | 2026-10-14: 101 twice, identical keys, 6 pages, 100% ids and links | 2026-10-14, expansion filter |
| 1 | Alphabet/Google | `alphabet-google-google` (`html`, 30 min) | `employment_type=INTERN` | 2026-10-14: 77 twice, identical keys, 4 pages, 100% ids and links | 2026-10-14, expansion filter |
| 1 | Netflix | `netflix-eightfold` (`company_json`, 15 min) | `query=intern` (loose) | 2026-10-14: 4 twice, identical keys, 1 page, 100% ids and links | 2026-10-14, expansion filter |
| 1 | Microsoft | not collected | — | public search answered, then HTTP 429 after about four requests; v2 API refuses | deferred: needs a paced source |
| 1 | Meta | not collected | — | client-rendered; GraphQL needs page-issued session tokens; no public endpoint | `FALLBACK_ONLY` |
| 2 | Boston Dynamics | `boston-dynamics-workday` | whole board (77; no intern facet) | 2026-10-14: 77 twice, identical keys, 4 pages | 2026-10-14, robotics filter (every internship) |
| 2 | Rockwell Automation | `rockwell-automation-workday` | whole board (404; internships titled Co-op) | 2026-10-14: 404 twice, identical keys, 21 pages | 2026-10-14, expansion filter |
| 2 | Zebra Technologies | `zebra-technologies-workday` | facet Intern (Fixed Term) (Trainee) | 2026-10-14: 10 twice, identical keys, 1 page | 2026-10-14, expansion filter |
| 2 | Boeing | `boeing-workday` | facets Intern - Paid (Seasonal), Intern Fixed Term (Non-US) | 2026-10-14: 46 twice, identical keys, 3 pages | 2026-10-14, expansion filter |
| 2 | Intel | `intel-workday` | facet Student / Intern (Fixed Term) | 2026-10-14: 84 twice, identical keys, 5 pages | 2026-10-14, expansion filter |
| 2 | Samsung | `samsung-workday` | facet Intern(Empl.) | 2026-10-14: 13 twice, identical keys, 1 page | 2026-10-14, expansion filter |
| 2 | CrowdStrike | `crowdstrike-workday` | facet Intern (Fixed Term) | 2026-10-14: 4 twice, identical keys, 1 page | 2026-10-14, expansion filter |
| 2 | Stryker | not collected | facet Intern (Fixed Term) | one stub posting with no title or link; not waived | re-check next pass |

Sources scoped to the provider's own internship classification (the Workday intern facets, Apple's
internships team, Google's `employment_type=INTERN`) carry `asserted_employment_type: Intern`, so
the `production-matches-intern-type` filter alerts on postings whose titles omit "intern" (11 of
Samsung's 13, Google's Student Researcher roles). Text-search and whole-board scopes do not.

### Phase 3 — Fortune 100 (2026 list)

The 80 Fortune 100 companies not already in the registry were added under `fortune-100` (the 20
already present gained the category) and audited on 2026-10-14: 1 `SUPPORTED`, 32 `BLOCKED`,
23 `FALLBACK_ONLY`, 24 `NEEDS_MANUAL_REVIEW`. Provider evidence among the latter: Workday 7 (all
promoted below), Eightfold 5 (American Express, Capital One, Chevron, Citigroup, Morgan Stanley; no
list endpoint derived yet), SuccessFactors 3 (American Airlines, Exxon Mobil, Phillips 66), Oracle 3
(JPMorgan Chase, UnitedHealth Group, Enterprise Products), iCIMS 2 (PepsiCo, State Farm). Source of
the ranking: the 2026 Fortune 500 as listed by DemandSage, whose top 10 matches Fortune's 2026-06-03
announcement.

| Phase | Company | Source | Scope | Live check | Allowlisted |
| --- | --- | --- | --- | --- | --- |
| 3 | Galaxy Digital | `galaxy-digital-greenhouse` | whole board (Greenhouse) | audit 2026-10-14: 39 twice | 2026-10-14, expansion filter |
| 3 | Target | `target-workday` | facet Intern (unscoped board hits the cap) | 2026-10-14: 128 twice, 7 pages | 2026-10-14, expansion filter |
| 3 | Nike | `nike-workday` (30 min) | whole board (478; no intern facet) | 2026-10-14: 478 twice, 24 pages | 2026-10-15, expansion filter |
| 3 | Coca-Cola | `coca-cola-workday` | facet Interim & Interns (no asserted type) | 2026-10-14: 8 twice | 2026-10-14, expansion filter |
| 3 | Elevance Health | `elevance-health-workday` | facet Interns | 2026-10-14: 7 twice | 2026-10-14, expansion filter |
| 3 | Bank of America | `bank-of-america-workday` | facet EE - Intern (Fixed Term) | 2026-10-14: 2 twice | 2026-10-14, expansion filter |
| 3 | Comcast | `comcast-workday` | facet Intern (Trainee) | 2026-10-14: 2 twice | 2026-10-14, expansion filter |
| 3 | Allstate | `allstate-workday` | facet Intern/Co-Op (Fixed Term) | 2026-10-14: 1 twice | 2026-10-14, expansion filter |

| 1 | Tesla (requested 2026-10-14) | not collected | — | `cua-api/apps/careers/state` and `/careers/search` answer HTTP 403 Access Denied (Akamai) | `BLOCKED`; not circumvented |

### Phase 3b — mechanical and robotics companies (2026-10-14)

The 2026-09-18 audit read only company landing pages, so it marked most robotics companies
`BLOCKED` (a challenge on the marketing site) or `FALLBACK_ONLY` (no feed linked). Their job boards
were probed directly on 2026-10-14: candidate board tokens on the public Greenhouse, Lever and
Ashby posting APIs, accepted only when the board name or posting URLs identify the company
(`figure` is Figure Lending and `archer` a veterinary clinic, so neither was used), plus Workday
tenants with their intern facet. 26 registry companies moved to a direct source and 43 new
companies were added (categories robotics, aerospace-defense, automotive, industrial). Every source
below passed `audit set-source`: two collections with identical canonical keys.

| Provider | Companies (postings at audit) |
| --- | --- |
| Greenhouse | SpaceX (2742)\*, Anduril (2522)\*, Lucid (453), Waymo (366), Relativity Space (361), Zipline (351), Rocket Lab (579), Scout Motors (210), Vast (187), Astranis (180), CHAOS Industries (148), Divergent (128), Varda (111), Ursa Major (111), Nuro (108), Figure (97), Harbinger (92), Motional (81), Apptronik (78), Agility Robotics (73), Kodiak (67), Torc (50), Skild AI (47), Path Robotics (44), Nimble (41), Formic (40), Epirus (40), May Mobility (39), Electra (35), Dexory (29), Skyryse (20), Stack AV (13), Carbon Robotics (11), Outrider (8), Locus Robotics (4) |
| Lever | Shield AI (612, 15 min), Zoox (240), Field AI (106), Waabi (96), Mujin (90), Hermeus (81), Tutor Intelligence (47), Merlin Labs (25), ANYbotics (23), Dexterity (22), Bright Machines (16), Toyota Research Institute (11), Cobalt Robotics (10), Pickle Robot (8), Agtonomy (4) |
| Ashby | Skydio (161), Helion (69), Physical Intelligence (39), Matic (24), Collaborative Robotics (13), Aescape (12), The Bot Company (7), Sanctuary AI (3) |
| Workday (intern facet) | GE Vernova (195, Co-op/Intern), GE Aerospace (154, Co-op/Intern), GM (79, Intern + Co-Op), Caterpillar (66, Intern + Student Worker), Medtronic (56, Intern), BorgWarner (52, Student), Aptiv (38, Intern - Developmental/Working), Northrop Grumman (33, Intern (Global)), Blue Origin (28, Intern (Fixed Term)) |
| Workday (whole board) | 3M (746; no intern facet) |

\* Fetched with `descriptions: false` every 15 minutes: SpaceX's board is 31 MB with descriptions
and 2.6 MB without.

Not collected: Saronic (Ashby board answers, but one record has no title, so every collection is
incomplete; not waived), and, with no public board found under any guessed token: Intuitive
Surgical, Symbotic, Joby, Rivian, Aurora, Mobileye, Teradyne/Universal Robots, Gecko Robotics,
Covariant, John Deere, Honeywell, L3Harris, Applied Intuition, Firefly, Hadrian, Machina Labs.

Allowlisted 2026-10-14 after two clean scheduler cycles (Anduril's second run followed an
interrupted one, caused by an agent reinstall). Robotics startups joined `production-matches`
(every internship): Physical Intelligence, Figure, Apptronik, Agility, Skydio, Field AI, Skild AI,
Dexterity, Nimble, Locus, Matic, Collaborative Robotics, The Bot Company, Pickle, Formic, Path,
Carbon Robotics, Mujin, Bright Machines, Cobalt, Agtonomy, Tutor Intelligence, Aescape, Dexory,
ANYbotics, Sanctuary AI, Toyota Research Institute. Everything else joined `expansion-matches`
(engineering keywords in the title). A preview against the stored postings matched 296 current
US internships, none of which alert, because they were seeded before monitoring.

US placement: boards that hire only in the US but write bare city names carry
`asserted_country: United States` (Astranis, SpaceX, Blue Origin, Nuro, Merlin Labs, The Bot
Company, Tutor Intelligence, Stack AV, Boston Dynamics, Path Robotics, Apptronik, Kodiak, May
Mobility, Outrider, Ursa Major, Standard Bots). Serve Robotics and Nimble do not: their boards
also hold Canadian or unidentified sites. GE Aerospace and GE Vernova write bare city names
across many countries and expose no country facet, so their postings alert only when the label
says "USA".

Bosch and Nike joined `expansion-matches` on 2026-10-15 after many clean cycles (Bosch had been
collected since the canary without ever being allowlisted). Bosch writes SmartRecruiters
locations; when a posting has no region, its label was "City, CC", and a country code that is also a
US state code read as that state (183 "bangalore, IN" postings looked like Indiana). The adapter
now spells such codes out ("bangalore, India") when the region is blank. The preview then matched
17 current US Bosch internships and none at Nike.

### Phase 3c — robotics, mechanical and Fortune 500 industrial companies (2026-10-15)

Robotics and mechanical companies came first, then Fortune 500 industrial, aerospace, medical-device
and automotive companies. Workday tenants were found from the site names their `robots.txt` lists,
which avoids guessing site names. Every source passed `audit set-source`. 51 companies are new to
the registry; RTX, Stryker, Micron, HP, Magna, ABB, Johnson & Johnson, Continental and Mobileye
were already registered without a working source.

| Provider | Companies (postings at audit) |
| --- | --- |
| Greenhouse | Formlabs (241), Mach Industries (144), Redwood Materials (143), Muon Space (141), Re:Build Manufacturing (129), Planet (118), Avride (77), Oklo (73), Wing (50), Kairos Power (40), JetZero (22), Carbon (12), Diligent Robotics (10), Solid Power (7), Fervo (5), Noah Medical (5), Markforged (4) |
| Lever | Mobileye (174, EU instance), Commonwealth Fusion (123), Loft Orbital (65), Velo3D (42), Aeva (40), Osaro (10), Ambi Robotics (5), RAI Institute (4) |
| Ashby | Form Energy (208), Reframe Systems (34), HavocAI (28), General Robotics (14), Glacier (5) |
| SmartRecruiters | Continental (558) |
| Workday (intern facet) | RTX (322), Stryker (210), Micron (193), J&J (183), ABB (137), Applied Materials (87), HP (69), Abbott (66), Illinois Tool Works (55), Analog Devices (43), Sierra Nevada (35), KLA (34), JCI (24), Generac (21), Moog (18), Stanley Black & Decker (13), Sensata (11), Polaris (10), Trimble (8), Woodward (6), Dover (3) |
| Workday (whole board) | Trane (1682)\*, Magna (1431)\*, Carrier (1088)\*, Xylem (476), Curtiss-Wright (374), Graco (185), Cognex (89), Regal Rexnord (34, early-career site) |

\* Polled every 30 minutes: a whole board over 1,000 postings with no internship facet.

Stanley Black & Decker's whole board came back incomplete, so it uses its intern and trainee
facets. The companies that could not be reached, with the reason for each, are listed in
[unreachable-companies.md](unreachable-companies.md).

### Unreachable-list pass (2026-10-15)

Careers pages were fingerprinted for the job system they link to, which found boards for companies
the token probes missed. Seven adapters were added: SuccessFactors career-site search, Oracle
Recruiting Cloud, Rippling, Workable, BambooHR, Pinpoint and UKG. Every source passed
`audit set-source`.

| Provider | Companies (postings at audit) |
| --- | --- |
| SuccessFactors (US search) | Teradyne/Universal Robots (294), Boston Scientific (290), PACCAR (182), ZF (151), Doosan (133), ExxonMobil (120), Lear (98), Phillips 66 (83), Epson (27), Keyence (10), JLR (9), Timken (1) |
| Oracle | Honeywell (1235)\*, Emerson (1130)\*, Howmet (804), Subaru (55) |
| Rippling | Overland AI (43), Forterra (40), Mytra (39), Boom Supersonic (30), Zap Energy (16), Blue Water Autonomy (6) |
| Workable / BambooHR / Pinpoint / UKG | Distalmotion (6), Agile Robots (4) / Plus One Robotics (2) / Impulse Space (216) / Yaskawa (29), Our Next Energy (18) |
| Existing providers | Axon (491), Applied Intuition (329), True Anomaly (206), Apex (139), Neros (105), AeroVironment (402), Axiom Space (98), Saildrone (43), Brain Corp (11), Robust AI (5), Wisk (0) |

\* Polled every 30 minutes. Oracle pages by offset over a sort with ties, and some sites list one
requisition twice yet count both; the adapter combines up to three walks and counts only an
explained double listing toward the declared total. See `adapters/oracle.py`.

All of these were allowlisted at once instead of after two clean cycles, because a new source's
first collection only seeds and cannot alert. The remaining companies are in
[unreachable-companies.md](unreachable-companies.md).

### freehire board-catalog pass (2026-10-18)

freehire (github.com/strelov1/freehire, MIT) is a public job aggregator. Its keyless search API
returns each posting's source and URL, which name the company's board, so its catalog located
boards for companies the earlier passes missed. Its adapters for nine more job systems were
reimplemented, reading listing pages only: iCIMS (search pages checked against the site sitemap),
Phenom, Jibe, Taleo, ADP MyJobs, HRM Direct, Radancy, Breezy and JazzHR. freehire's tiers that
reach refusing sites (TLS-fingerprint spoofing, residential proxies, a headless browser, a hosted
scraping service) were not ported. Every source passed `audit set-source`.

| Provider | Companies (postings at audit) |
| --- | --- |
| iCIMS | General Dynamics Electric Boat (231), Snap-on (205), Joby (169), Daifuku (162) |
| Phenom | BAE Systems (2015)\*, Zimmer Biomet (423), Honda (303) |
| Jibe | Rivian (947)\*, Schneider Electric (US, 822)\*, AMD (US, 662)\* |
| Taleo | Textron (656 served of 717 counted)\* |
| ADP | Stellantis (873)\*, HEICO (326)\*, FANUC (78), Aisin (165) |
| HRM Direct / Radancy | Firefly (77), Mitsubishi Electric US (42) / L3Harris (2173)\*, SharkNinja (149), iRobot (4) |
| Breezy / JazzHR | Bear Robotics (45), RightHand Robotics (3) / Ouster (24), Miso Robotics (6) |
| Oracle | Cummins (1215)\*, Ford (869)\*, Texas Instruments (848)\*, Dell (446), DENSO (107) |
| Workday (whole board) | Otis (1377)\*, Oshkosh (537)\*, Microchip (528), Nissan (327), Nordson (230), Sierra Space (203), Symbotic (127), Toyota (115), X-energy (115), PROCEPT (34) |
| SuccessFactors | Hubbell (669)\*, Corning (US, 559)\*, AMETEK (US, 499)\*, John Deere (US, 228)\*, ULA (143)\*, Harley-Davidson (US, 82)\*, QuantumScape (29)\*, Volkswagen Group (US, 20)\* |
| SmartRecruiters / UKG | Intuitive Surgical (752)\*, Western Digital (334) / TAE Technologies (23) |
| Greenhouse / Lever / Ashby | K2 Space (250), Castelion (221), BETA (125), Archer Aviation (141), Stoke (71), Gatik (67) / Machina Labs (29), Simbe (14), Burro (10) / NEURA (378), Saronic (231), Hadrian (131), Reliable Robotics (59), Persona AI (27), Gecko (21), Built Robotics (7) |
| Rippling / Workable / BambooHR | Engineered Arts (7), Dusty Robotics (4) / Exotec (64) / Berkshire Grey (21) |

\* Polled every 30 minutes, with the reason recorded in the source's `slow_poll_reason`.

Taleo sections can count requisitions they never serve (Textron's pages come back short with no
repeat), so a short section is accepted only when two walks read the same postings. Radancy sites
can count content pages in their total, so the job-only total is used, and a "Multiple Locations"
or "Remote" label gains the place its posting link names. Robotics companies joined the
every-internship filters; the rest joined the engineering-title filters. The remaining companies
are in [unreachable-companies.md](unreachable-companies.md).

### Freehire company-feed fallback (2026-10-19)

Fifty-five companies that still had no direct source now use Freehire's keyless public
`GET /api/v1/jobs/search` endpoint as an enabled fallback. Each source names exactly one
`company_slug`; the adapter rejects any result carrying a different slug and pages until Freehire's
declared total is read. Postings keep the original ATS source and URL, so the normal cross-source
dedupe and geography filters still apply.

This covers Tesla, Lockheed Martin, Eaton, Whirlpool, Lam Research, Qualcomm, OMRON, CMR Surgical,
Mercedes-Benz Group, Ferrari and SAP in the priority set; Adobe, Broadcom, Cisco, Cloudflare,
Microsoft, Snowflake, Cadence, OpenAI, ServiceNow, Synopsys, TSMC, Tencent and xAI among big tech;
and 31 of the provider-refused Fortune 100 entries. The fallback is polled every 60 minutes and is
recorded as `FALLBACK_ONLY`, not counted as direct coverage.

The adapter leaves Freehire's transport tiers untouched. It does not spoof a TLS fingerprint, use a
residential proxy, launch a headless browser, or call a hosted scraping service; it reads the same
ordinary public API available to any HTTP client. Unit tests cover mapping, pagination, company
isolation, malformed envelopes and original-link retention. An opt-in live test collects the CMR
Surgical feed twice and requires complete, stable output.

AT&T and HCA Healthcare remain unconfigured because Freehire has no exact canonical company record
for either. DENSO Robotics remains part of `denso-oracle`, because Freehire exposes the parent
company rather than a separately identifiable robotics feed. The exact unresolved entries are in
[unreachable-companies.md](unreachable-companies.md).
