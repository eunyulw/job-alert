# Fast Company Job Alert System Design

Date: 2026-09-16

## Purpose

Build a continuously running service that monitors the approved company list for newly public job postings and sends actionable alerts to the user's Android phone as quickly as practical. The system targets detection within 5–15 minutes of a posting becoming visible through the earliest accessible official public source. It does not claim to detect private requisitions before an employer publishes them.

## Goals

- Monitor every company in the cleaned company registry across these categories:
  - Big Tech and AI
  - Robotics, Autonomy, and Physical AI
  - Industrial Automation and Advanced Manufacturing
  - Automotive, EVs, and Autonomous Mobility
  - Aerospace and Defense
  - Medical Robotics
- Prefer official public job-board APIs and structured first-party sources.
- Avoid browser navigation and browser automation during normal operation.
- Detect new public postings within 5–15 minutes where the source supports that cadence.
- Send a concise Android notification containing a direct job or application link.
- Avoid duplicate alerts, including after process restarts.
- Preserve all observed jobs so notification filters can evolve independently of ingestion.
- Report stale, broken, or blocked sources separately from new-job notifications.
- Operate continuously when the user's Mac is asleep or offline.

## Non-goals

- Detecting jobs before they are publicly published by the employer.
- Guaranteeing a fixed delay between an employer's internal requisition approval and an alert.
- Automatically applying to jobs.
- Circumventing CAPTCHAs, authentication, access controls, or anti-bot measures.
- Using LinkedIn, Indeed, Google Jobs, or another aggregator as the primary source of truth.
- Using browser automation as a routine collection method.
- Building a user-facing web dashboard in the initial release.
- Inferring confidential or internal positions.

## Definition of Freshness

The service measures freshness from the earliest accessible public source, not from internal job creation.

For each posting, it stores:

- `source_published_at`: the publication time supplied by the source, when present and semantically trustworthy.
- `source_updated_at`: the update time supplied by the source, when present.
- `first_seen_at`: the first successful polling run in which the service observed the posting.

The primary operational service objective is:

- Official public API sources polled every 5 minutes: detect within 6 minutes of the posting appearing in that API in normal operation.
- Stable public ATS endpoints polled every 10 minutes: detect within 11 minutes of the posting appearing in that endpoint in normal operation.
- Undocumented but stable company JSON endpoints polled every 15 minutes: detect within 16 minutes of the posting appearing in that endpoint in normal operation.

These are polling objectives rather than end-to-end employer publication guarantees. Employer scheduling, ATS propagation, CDN caching, mobile connectivity, and provider outages remain outside the service's control.

## Design Principles

1. Monitor the earliest legitimate public source.
2. Treat every provider response as untrusted input.
3. Separate ingestion from notification filtering.
4. Use stable provider job IDs when available and deterministic fallback identities otherwise.
5. Never interpret a failed or partial fetch as all jobs being closed.
6. Seed a baseline before sending alerts.
7. Prefer a few reusable provider adapters over company-specific scraping.
8. Make source health visible and actionable.
9. Apply conservative request rates, backoff, and jitter.
10. Preserve enough raw evidence to diagnose parsing changes without retaining applicant or personal data.

## Source Strategy

### Source priority

For each company, select the highest available source in this order:

1. Official public ATS API or documented job-board API.
2. Public structured ATS endpoint used by the company's official career site.
3. Official company JSON search endpoint.
4. Server-rendered official career pages.
5. Official sitemap, RSS/Atom feed, or `JobPosting` JSON-LD.
6. Official company job alert email.
7. External aggregator used only as a coverage cross-check.

The initial reusable adapters are expected to cover:

- Greenhouse
- Lever
- Ashby
- SmartRecruiters
- Workday career-site endpoints
- SAP SuccessFactors career-site endpoints
- iCIMS career portals
- Eightfold career sites
- Oracle Recruiting/Taleo career sites
- Generic JSON/REST sources
- Generic HTML plus `JobPosting` JSON-LD

An adapter is added only after a live source audit verifies its request shape, pagination, job identity, and failure behavior.

### Company source registry

Every monitored company has a declarative registry entry with:

- canonical company identifier and display name;
- aliases and subsidiaries included in the same feed;
- categories;
- official careers URL;
- provider and adapter;
- tenant, board, or company identifier;
- one or more source endpoints;
- regional or subsidiary scope;
- source classification: documented API, public ATS endpoint, company JSON, HTML, or fallback;
- expected job-ID field;
- timestamp semantics;
- pagination parameters;
- recommended poll interval;
- request headers required for ordinary access;
- compliance and access notes;
- enabled/disabled state;
- last audit date.

Every company ends the source-audit phase in exactly one state:

- `SUPPORTED`
- `SUPPORTED_UNDOCUMENTED`
- `HTML_ONLY`
- `FALLBACK_ONLY`
- `BLOCKED`
- `NEEDS_MANUAL_REVIEW`

No source is labeled supported merely because it returned HTTP 200. A supported source must return a complete, parseable, paginated public listing with stable identity and application links.

## System Architecture

### Registry loader

Loads and validates the company registry before a run begins. Invalid entries fail closed for that company and produce a configuration-health event.

### Scheduler

Maintains tiered polling schedules:

- Tier A, documented public APIs: every 5 minutes.
- Tier B, stable public ATS endpoints: every 10 minutes.
- Tier C, undocumented company JSON endpoints: every 15 minutes.
- Tier D, HTML, sitemap, or JSON-LD fallbacks: every 30 minutes.
- Aggregator coverage checks: every 1–4 hours.

Each scheduled time receives 0–60 seconds of deterministic jitter. Per-host concurrency is limited. A source under backoff is skipped until its next eligible time.

### Fetch layer

Performs HTTPS requests with:

- explicit connection and response timeouts;
- bounded response sizes;
- TLS verification;
- redirect limits;
- compression support;
- conditional requests using ETag or Last-Modified when supported;
- provider-specific pagination;
- per-host concurrency limits;
- retry only for retryable failures;
- exponential backoff with jitter for `429`, `5xx`, timeouts, and transient network errors.

The fetch layer does not attempt to defeat access controls or challenges. CAPTCHA or bot-challenge responses move the source to manual review.

### Provider adapters

Each adapter accepts a registry entry and fetched response and returns a provider-neutral list of normalized jobs plus diagnostics. Adapter responsibilities are limited to:

- constructing source requests;
- traversing pagination;
- validating response type and shape;
- detecting challenge or error pages;
- extracting provider fields;
- mapping source records into normalized jobs.

Adapters do not decide whether a job is new and do not send notifications.

### Normalizer

Produces a consistent record containing:

- company ID and display name;
- source provider;
- source job ID;
- canonical job key;
- requisition ID when exposed;
- title;
- location list and normalized location text;
- department and team;
- employment and workplace types;
- description text and content hash;
- posting URL and application URL;
- source publication and update times;
- raw source metadata required for diagnosis.

URLs are canonicalized by removing known tracking parameters while preserving identifiers required to open or apply.

### State and comparison engine

SQLite is the initial persistent store. It records companies, sources, polling runs, jobs, job observations, notification decisions, and delivery attempts.

The comparison engine classifies each normalized posting as:

- new;
- unchanged;
- materially updated;
- reopened;
- absent from one successful snapshot;
- closed after the configured absence threshold.

A job is new only if its canonical identity has never appeared for that company and source scope. Failed or partial runs never close jobs. Jobs become closed only after consecutive complete successful snapshots confirm absence, with the threshold configurable by source.

### Job identity and deduplication

Identity priority is:

1. provider plus tenant plus stable source job ID;
2. provider plus stable requisition ID plus public location variant;
3. canonicalized application URL;
4. deterministic fingerprint of company, normalized title, normalized locations, and source scope.

Cross-board duplicates are linked when stable requisition IDs or equivalent canonical URLs match. The service retains every source observation but emits at most one new-job alert for the linked posting.

Reposts with a genuinely new source job ID are treated as reopened or new according to configurable company policy. The initial policy records them as reopened and sends one notification labeled `Reposted`.

### Filters and notification policy

All jobs are ingested. Notification filters are evaluated afterward. The filter model supports:

- include/exclude title keywords;
- include/exclude departments or job families;
- locations and remote/hybrid/on-site preferences;
- internships, university, new-graduate, entry-level, or experienced roles;
- company and category priority;
- description keywords;
- security-clearance exclusions;
- notification quiet hours and urgency.

The first end-to-end test uses an explicit test filter or dry-run channel. Production defaults are not enabled until the user approves the initial filter set.

### Android notification delivery

Pushover is the primary delivery provider. Each alert contains:

- event type: New or Reposted;
- company;
- job title;
- location and workplace type when available;
- source-published time or first-seen time;
- a direct posting or application URL.

Individual high-priority matches receive one notification per job. Bursts are grouped by company within a short configurable window to avoid notification floods. Notifications use normal priority by default; urgency is reserved for user-defined highest-priority matches.

Pushover credentials are supplied through environment secrets and are never stored in the repository. Delivery IDs, status codes, and retry eligibility are recorded. Permanent `4xx` failures do not retry indefinitely. Transient failures retry with backoff.

ntfy remains an optional secondary provider and development fallback. If the public ntfy service is used, the topic must be unguessable and authenticated service or self-hosting is preferred.

### Health and observability

The service records:

- run start and finish times;
- source response status, duration, and payload size;
- jobs fetched, parsed, added, updated, and closed;
- pagination completeness;
- adapter warnings;
- consecutive failures;
- last complete successful check;
- notification delivery status;
- current backoff state.

Health notifications are distinct from job alerts. A source sends one health alert after a threshold of consecutive failures and one recovery alert after returning to normal. Repeated failures are summarized rather than pushed every cycle.

A daily digest reports:

- enabled source count;
- sources checked successfully;
- stale, blocked, or failing sources;
- new jobs observed;
- matching alerts delivered;
- adapters needing review.

## Deployment

### Development and audit environment

The first source audit and adapter tests run locally from the workspace. Local state and secrets are excluded from version control.

### Production environment

The initial production target is a small always-on Linux host or container capable of running Python and persistent SQLite storage. This avoids dependence on the user's Mac and avoids free serverless subrequest and CPU constraints.

Production requirements:

- automatic service restart;
- persistent volume for SQLite;
- encrypted secret injection;
- outbound HTTPS access;
- daily database backup;
- log rotation;
- clock synchronization;
- deployment health check.

The service is designed so the storage and scheduler can later move to a managed database and serverless runner without changing provider-adapter interfaces.

## Error Handling

### Retryable errors

- connection timeouts;
- DNS or temporary network failures;
- HTTP `408`, `425`, `429`, and `5xx` responses;
- clearly truncated responses.

Retries are bounded and use exponential backoff with jitter. `Retry-After` is honored.

### Non-retryable or manual-review errors

- invalid registry configuration;
- `401` or `403` indicating required authorization;
- CAPTCHA or challenge pages;
- repeated schema mismatch;
- invalid pagination creating loops;
- payload exceeding the configured safety limit;
- explicit provider prohibition.

These errors disable closure inference for the affected run and create a health event.

### Partial data safeguards

A run is complete only when all expected pages succeed and validation passes. Partial runs may update the `last_seen_at` value of returned jobs but cannot mark missing jobs closed. Unexpected large drops in job count quarantine the snapshot pending another successful run.

## Security, Privacy, and Compliance

- Collect only public job-posting information.
- Do not collect applicant data.
- Do not bypass authentication, CAPTCHAs, or bot controls.
- Review provider documentation, site terms, and relevant robots guidance during source audit.
- Use reasonable per-host request rates and cache validators.
- Store secrets only in environment or deployment secret storage.
- Redact secret-bearing headers and query parameters from logs.
- Bound response sizes and parsing time.
- Escape notification text and treat source HTML as untrusted.
- Retain raw payload samples only where needed for fixtures or diagnosis, with size and retention limits.

## Test Strategy

### Adapter contract tests

Every adapter must pass shared contract tests using recorded or synthetic fixtures:

- parse an empty valid board;
- parse one and many jobs;
- traverse multiple pages without duplication;
- retain stable identity across repeated runs;
- extract direct posting and application URLs;
- reject malformed or unexpected content;
- identify HTML challenge pages returned with HTTP 200;
- handle missing optional fields;
- detect pagination loops and partial results.

### State-transition tests

- Baseline synchronization sends no job notifications.
- Repeating an identical snapshot sends no notifications.
- Adding one fixture job emits exactly one new-job event.
- Updating description text does not create a duplicate new-job alert.
- Removing a job from one partial or failed snapshot does not close it.
- Consecutive complete snapshots can close a missing job.
- Reappearing stable IDs reopen without duplicating history.
- Process restart preserves deduplication.

### Representative live probes

Test at least one verified company for every source type before enabling all companies. A successful probe confirms:

- authoritative official career URL;
- source endpoint and provider identity;
- complete pagination;
- stable job identity;
- required normalized fields;
- repeated no-change behavior;
- response time and ordinary access requirements.

One Greenhouse success cannot validate Lever, Ashby, Workday, or custom adapters.

### Company-wide dry run

After representative adapters pass:

1. Run every registry entry without job notifications.
2. Store the baseline.
3. Produce a company-by-company coverage report.
4. Run a second complete cycle.
5. Confirm zero false new-job alerts.
6. Manually compare a selected sample with official career pages.
7. Resolve or classify failures.
8. Enable production notification filters only after review.

### Notification tests

- Send a clearly labeled test notification to the Android device.
- Verify the direct link opens correctly.
- Verify grouping behavior.
- Verify delivery retry and permanent-failure behavior.
- Verify Android notification permission, sound, and battery-optimization settings.

## Rollout Phases

### Phase 1: Registry and source audit

- Finalize and deduplicate the company list.
- Discover the official careers source for every company.
- Classify provider, access method, stability, and limitations.
- Produce the coverage matrix before promising full support.

### Phase 2: Core engine and documented API adapters

- Implement normalized models, registry validation, SQLite state, comparison logic, and dry-run reporting.
- Implement Greenhouse, Lever, Ashby, and SmartRecruiters adapters.
- Validate with contract fixtures and representative live probes.

### Phase 3: Enterprise ATS and structured fallback adapters

- Implement verified Workday and other enterprise-provider adapters discovered in Phase 1.
- Add generic JSON, HTML, sitemap, and JSON-LD fallbacks.
- Classify blocked or unstable sources instead of bypassing controls.

### Phase 4: All-company dry run

- Establish baseline state.
- Run repeated collection cycles.
- Resolve duplicates, pagination gaps, regional boards, and source failures.
- Present the final source-coverage report.

### Phase 5: Android notification activation

- Configure Pushover credentials.
- Send and verify a test alert.
- Approve production filters.
- Enable live notifications gradually, beginning with a small canary group.

### Phase 6: Production hosting and hardening

- Deploy to the always-on host.
- Configure service supervision, backups, log rotation, and health alerts.
- Observe at least 24 hours of runs before declaring the service stable.

### Phase 7: Coverage cross-checking

- Add slow aggregator or company-alert comparisons only to identify coverage gaps.
- Do not generate duplicate phone alerts from secondary sources.

## Acceptance Criteria

- Every approved company has a source-registry entry and explicit support status.
- At least 90% of the registry has a working direct official or official-career-site source before broad notification activation.
- All enabled sources complete two consecutive production-shaped runs without false new-job events.
- A synthetic added job produces one and only one event.
- Official API sources meet the 6-minute normal-operation detection objective in controlled polling tests.
- Stable non-official endpoints meet their configured 11- or 16-minute objective in controlled polling tests.
- Initial baseline synchronization sends no production job alerts.
- Duplicate source records produce one phone alert.
- Every alert includes company, title, location when available, and a working direct URL.
- Source failures never cause mass closure or reopening events.
- The process can restart without losing job history or notification state.
- Pushover delivery succeeds on the user's Android device.
- Secrets and local runtime state are absent from version control.
- A source-health report clearly lists stale, failing, blocked, and unsupported companies.

## Decisions Deferred Until Source Audit or Implementation Planning

The following choices do not change the approved architecture but must be resolved before their implementation tasks:

- final canonical company registry after alias and subsidiary review;
- exact production host and provider;
- user-approved job-title, location, experience-level, and remote-work filters;
- company priority tiers;
- burst-grouping window and quiet hours;
- job-history and raw-fixture retention periods;
- whether reopened jobs should always notify or only notify when they match priority filters.

## Primary Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Provider changes response schema | Contract validation, fixtures, schema-mismatch health alerts |
| Company changes ATS | Registry audit metadata, stale-source alerts, periodic re-discovery |
| Partial pagination appears complete | Page-count validation, total-count checks, large-drop quarantine |
| Rate limiting or blocking | Tiered schedules, per-host limits, jitter, conditional requests, backoff |
| Posting appears on multiple boards | Provider IDs, requisition linkage, canonical URL deduplication |
| Untrustworthy publish timestamps | Preserve source time but use `first_seen_at` operationally |
| Alert flood | Baseline seeding, filters, burst grouping, daily quota monitoring |
| Android delays notifications | Pushover test, notification permission, battery-optimization exemption |
| Host outage | Service supervision, health heartbeat, persistent state, backup |
| Unsupported source | Explicit degraded status and official alert or aggregator fallback |

## Final Architectural Decision

The system will use provider-specific HTTP adapters, a normalized job model, persistent first-seen state, tiered polling, and Pushover Android notifications. It will prioritize direct official public ATS sources, test one representative company per provider, complete an all-company baseline dry run, and enable alerts only after source coverage and duplicate behavior are verified. Browser automation and aggregator-only monitoring are excluded from the initial production design.
