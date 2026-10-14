# Fast Company Job Alert Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy a continuously running, browser-free job monitor that audits every approved company, detects newly public jobs from the earliest accessible official source, and sends deduplicated Android alerts through Pushover.

**Architecture:** A Python service loads a declarative company/source registry, polls provider-specific HTTP adapters on tiered schedules, normalizes every posting, and compares complete snapshots against persistent SQLite state. Notifications are evaluated after ingestion, source-health events are isolated from job events, and a dry-run baseline must pass before production alerts are enabled.

**Tech Stack:** Python 3.12, uv, Pydantic 2, PyYAML, HTTPX, Tenacity, Typer, SQLite, pytest, pytest-asyncio, respx, Ruff, mypy, Docker Compose, Pushover HTTPS API.

**Spec:** `docs/superpowers/specs/2026-09-16-fast-company-job-alert-design.md`

## Global Constraints

- Measure freshness from the earliest accessible official public source, never from private requisition creation.
- Do not use browser automation during normal operation.
- Do not bypass authentication, CAPTCHAs, access controls, bot challenges, or explicit provider prohibitions.
- Poll documented public APIs every 5 minutes, stable public ATS endpoints every 10 minutes, undocumented company JSON endpoints every 15 minutes, and HTML/JSON-LD fallbacks every 30 minutes.
- Add deterministic jitter of 0–60 seconds and limit per-host concurrency.
- A failed, challenged, truncated, quarantined, or partially paginated fetch must never close jobs.
- Seed a complete baseline without job notifications before enabling live alerts.
- Ingest all jobs; apply notification filters afterward.
- Treat provider payloads and HTML as untrusted input.
- Store secrets only in environment or deployment secret storage; never commit them.
- Use `first_seen_at` as operational truth when source publication time is absent or semantically unreliable.
- **Revised 2026-10-14:** macOS laptop notifications are the primary delivery channel for the first release; the monitor runs natively on the laptop under launchd. Pushover/Android delivery is deferred to Task 28 (its notifier already exists); ntfy stays optional.
- Every approved company must end the source audit with one explicit support status.
- At least 90% of companies must have a working direct official or official-career-site source before broad notification activation.

## File and Responsibility Map

```text
pyproject.toml                         dependency, tooling, and CLI configuration
.python-version                       Python 3.12 pin
.gitignore                            local state, secrets, caches, and audit artifacts
README.md                             operator setup and common commands
config/company_targets.yaml           canonical company inventory and category membership
config/sources.yaml                   audited source endpoints and polling policy
config/filters.yaml                   notification rules
src/job_alert/migrations/001_initial.sql complete initial SQLite schema (shipped in the wheel)
src/job_alert/models.py               provider-neutral Pydantic models and enums
src/job_alert/config.py               YAML loading and cross-file validation
src/job_alert/db.py                   SQLite repository and transactional state changes
src/job_alert/identity.py             job identity and canonical URL functions
src/job_alert/diff.py                 new/update/reopen/close classification
src/job_alert/fetch.py                bounded HTTP client, retry, backoff, and validators
src/job_alert/adapters/base.py        adapter protocol and response-validation helpers
src/job_alert/adapters/greenhouse.py  Greenhouse collection and normalization
src/job_alert/adapters/lever.py       Lever collection and normalization
src/job_alert/adapters/ashby.py       Ashby collection and normalization
src/job_alert/adapters/smartrecruiters.py SmartRecruiters pagination and normalization
src/job_alert/adapters/workday.py     verified Workday CXS source variants
src/job_alert/adapters/successfactors.py verified SAP career-site variants
src/job_alert/adapters/icims.py       verified iCIMS portal variants
src/job_alert/adapters/eightfold.py   verified Eightfold career-site variants
src/job_alert/adapters/oracle.py      verified Oracle Recruiting/Taleo variants
src/job_alert/adapters/generic_json.py registry-driven bounded JSON extraction
src/job_alert/adapters/jsonld.py      server HTML and JobPosting JSON-LD extraction
src/job_alert/adapters/registry.py    provider-to-adapter construction
src/job_alert/audit.py                source discovery, probe, and coverage reporting
src/job_alert/filters.py              deterministic post-ingestion match rules
src/job_alert/notifications/base.py   notification protocol and message models
src/job_alert/notifications/pushover.py Pushover delivery and response classification
src/job_alert/notifications/ntfy.py   optional ntfy delivery
src/job_alert/health.py               stale/failure/recovery/digest event policy
src/job_alert/service.py              one-source and one-cycle orchestration
src/job_alert/scheduler.py            due-source selection, jitter, and concurrency
src/job_alert/cli.py                  audit, collect, baseline, run, and report commands
tests/fixtures/                       sanitized provider responses and failure payloads
tests/unit/                           deterministic component tests
tests/integration/                    HTTP, SQLite, and complete-cycle tests
tests/live/                           opt-in public-endpoint contract probes
deploy/Dockerfile                     production image
deploy/compose.yaml                   persistent service deployment
deploy/job-alert.env.example          secret and runtime variable names
deploy/healthcheck.py                 container health verification
scripts/backup_sqlite.py              consistent database backup and restore
docs/source-audit.md                  company-by-company evidence and limitations
docs/operator-runbook.md              deployment, recovery, and adapter incident steps
```

---

### Task 1: Project Foundation and Domain Models

**Files:**
- Create: `.python-version`
- Create: `.gitignore`
- Create: `pyproject.toml`
- Create: `src/job_alert/__init__.py`
- Create: `src/job_alert/models.py`
- Create: `tests/unit/test_models.py`

**Interfaces:**
- Consumes: None.
- Produces: `CompanyTarget`, `SourceConfig`, `NormalizedJob`, `CollectionResult`, `PollRun`, `JobEvent`, `SourceClass`, `SupportStatus`, `RunOutcome`, and `JobEventType`.

- [ ] **Step 1: Write the failing domain-model tests**

```python
# tests/unit/test_models.py
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from job_alert.models import (
    CollectionResult,
    CompanyTarget,
    NormalizedJob,
    SourceClass,
    SourceConfig,
    SupportStatus,
)


def test_source_config_rejects_non_https_endpoint() -> None:
    with pytest.raises(ValidationError):
        SourceConfig(
            id="openai-greenhouse",
            company_id="openai",
            provider="greenhouse",
            source_class=SourceClass.DOCUMENTED_API,
            support_status=SupportStatus.SUPPORTED,
            endpoint="http://boards-api.greenhouse.io/v1/boards/openai/jobs",
            interval_minutes=5,
        )


def test_collection_result_requires_unique_job_keys() -> None:
    job = NormalizedJob(
        company_id="openai",
        source_id="openai-greenhouse",
        source_job_id="123",
        canonical_job_key="greenhouse:openai:123",
        dedupe_key="requisition:openai:research-123",
        title="Research Engineer",
        locations=["San Francisco, CA"],
        posting_url="https://example.com/jobs/123",
        apply_url="https://example.com/jobs/123/apply",
        first_seen_at=datetime(2026, 9, 18, tzinfo=UTC),
    )
    with pytest.raises(ValidationError):
        CollectionResult(source_id="openai-greenhouse", complete=True, jobs=[job, job])


def test_company_target_requires_at_least_one_category() -> None:
    with pytest.raises(ValidationError):
        CompanyTarget(id="openai", name="OpenAI", categories=[])
```

- [ ] **Step 2: Create the package metadata and run the tests to confirm import failure**

```toml
# pyproject.toml
[project]
name = "fast-company-job-alert"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
  "httpx>=0.28,<0.29",
  "pydantic>=2.11,<3",
  "pyyaml>=6.0.2,<7",
  "tenacity>=9.1,<10",
  "typer>=0.16,<1",
]

[project.optional-dependencies]
dev = [
  "mypy>=1.17,<2",
  "pytest>=8.4,<9",
  "pytest-asyncio>=1.1,<2",
  "respx>=0.22,<1",
  "ruff>=0.12,<1",
]

[project.scripts]
job-alert = "job_alert.cli:app"

[build-system]
requires = ["hatchling>=1.27"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/job_alert"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = ["live: opt-in test against public company endpoints"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]

[tool.mypy]
python_version = "3.12"
strict = true
packages = ["job_alert"]
```

Create `.python-version` with `3.12` and `.gitignore` with:

```gitignore
.venv/
.pytest_cache/
.mypy_cache/
.ruff_cache/
__pycache__/
*.pyc
.env
*.sqlite3
*.sqlite3-shm
*.sqlite3-wal
data/
backups/
reports/
```

Run: `uv sync --extra dev && uv run pytest tests/unit/test_models.py -v`

Expected: FAIL because `job_alert.models` does not exist.

- [ ] **Step 3: Implement the domain models and validators**

```python
# src/job_alert/models.py
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceClass(StrEnum):
    DOCUMENTED_API = "documented_api"
    PUBLIC_ATS = "public_ats"
    COMPANY_JSON = "company_json"
    HTML = "html"
    FALLBACK = "fallback"


class SupportStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    SUPPORTED_UNDOCUMENTED = "SUPPORTED_UNDOCUMENTED"
    HTML_ONLY = "HTML_ONLY"
    FALLBACK_ONLY = "FALLBACK_ONLY"
    BLOCKED = "BLOCKED"
    NEEDS_MANUAL_REVIEW = "NEEDS_MANUAL_REVIEW"


class RunOutcome(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    CHALLENGED = "challenged"
    QUARANTINED = "quarantined"


class JobEventType(StrEnum):
    NEW = "new"
    UPDATED = "updated"
    REOPENED = "reopened"
    CLOSED = "closed"


class CompanyTarget(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1)
    categories: list[str] = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)


class SourceConfig(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    company_id: str
    provider: str
    source_class: SourceClass
    support_status: SupportStatus
    endpoint: HttpUrl
    interval_minutes: int = Field(ge=5, le=240)
    tenant: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    audit_date: str | None = None


class NormalizedJob(StrictModel):
    company_id: str
    source_id: str
    source_job_id: str
    canonical_job_key: str
    dedupe_key: str
    requisition_id: str | None = None
    title: str
    locations: list[str] = Field(default_factory=list)
    department: str | None = None
    team: str | None = None
    employment_type: str | None = None
    workplace_type: str | None = None
    description_text: str | None = None
    content_hash: str | None = None
    posting_url: HttpUrl
    apply_url: HttpUrl
    source_published_at: datetime | None = None
    source_updated_at: datetime | None = None
    first_seen_at: datetime
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


class CollectionResult(StrictModel):
    source_id: str
    complete: bool
    jobs: list[NormalizedJob]
    warnings: list[str] = Field(default_factory=list)
    page_count: int = Field(default=1, ge=0)
    declared_total: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def unique_keys(self) -> CollectionResult:
        keys = [job.canonical_job_key for job in self.jobs]
        if len(keys) != len(set(keys)):
            raise ValueError("collection contains duplicate canonical job keys")
        return self


class PollRun(StrictModel):
    id: int | None = None
    source_id: str
    started_at: datetime
    finished_at: datetime | None = None
    outcome: RunOutcome
    http_status: int | None = None
    jobs_seen: int = 0
    error_code: str | None = None
    error_message: str | None = None


class JobEvent(StrictModel):
    event_type: JobEventType
    job: NormalizedJob
    detected_at: datetime
```

- [ ] **Step 4: Run model tests and static checks**

Run: `uv run pytest tests/unit/test_models.py -v && uv run ruff check . && uv run mypy src`

Expected: all commands exit 0.

- [ ] **Step 5: Commit the project foundation**

```bash
git add .python-version .gitignore pyproject.toml uv.lock src/job_alert tests/unit/test_models.py
git commit -m "feat: add job alert domain foundation"
```

### Task 2: Canonical Company Inventory and Configuration Loading

**Files:**
- Create: `config/company_targets.yaml`
- Create: `config/sources.yaml`
- Create: `config/filters.yaml`
- Create: `src/job_alert/config.py`
- Create: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: `CompanyTarget`, `SourceConfig` from Task 1.
- Produces: `AppConfig`, `load_app_config(config_dir: Path) -> AppConfig`, and a canonical target inventory independent of source discovery.

- [ ] **Step 1: Write failing configuration tests**

```python
# tests/unit/test_config.py
from pathlib import Path

import pytest

from job_alert.config import ConfigError, load_app_config


def test_load_config_rejects_unknown_company_reference(tmp_path: Path) -> None:
    (tmp_path / "company_targets.yaml").write_text(
        "companies:\n  - id: openai\n    name: OpenAI\n    categories: [big-tech-ai]\n"
    )
    (tmp_path / "sources.yaml").write_text(
        "sources:\n"
        "  - id: ghost-board\n"
        "    company_id: ghost\n"
        "    provider: greenhouse\n"
        "    source_class: documented_api\n"
        "    support_status: SUPPORTED\n"
        "    endpoint: https://example.com/jobs\n"
        "    interval_minutes: 5\n"
    )
    (tmp_path / "filters.yaml").write_text("filters: []\n")
    with pytest.raises(ConfigError, match="unknown company_id ghost"):
        load_app_config(tmp_path)


def test_load_config_requires_every_target_to_have_source_or_review_status(tmp_path: Path) -> None:
    (tmp_path / "company_targets.yaml").write_text(
        "companies:\n  - id: openai\n    name: OpenAI\n    categories: [big-tech-ai]\n"
    )
    (tmp_path / "sources.yaml").write_text("sources: []\n")
    (tmp_path / "filters.yaml").write_text("filters: []\n")
    with pytest.raises(ConfigError, match="missing source entry: openai"):
        load_app_config(tmp_path)
```

- [ ] **Step 2: Run the tests to verify failure**

Run: `uv run pytest tests/unit/test_config.py -v`

Expected: FAIL because `job_alert.config` does not exist.

- [ ] **Step 3: Create the canonical target inventory**

Create one YAML record per canonical company, allowing multiple categories on one company. Use these exact category IDs:

```yaml
categories:
  - big-tech-ai
  - robotics-autonomy-physical-ai
  - industrial-automation-advanced-manufacturing
  - automotive-ev-autonomous-mobility
  - aerospace-defense
  - medical-robotics
```

Seed the following exact canonical names, merging aliases instead of duplicating records:

- Big Tech and AI: NVIDIA, Apple, Alphabet/Google, Amazon, Meta, Microsoft, Tesla, Samsung, ASML, Qualcomm, Netflix, Oracle, Salesforce, Adobe, Cisco, Palantir, Uber, Airbnb, Stripe, Databricks, Snowflake, OpenAI, Anthropic, AMD, Intel, Broadcom, TSMC, ServiceNow, SAP, IBM, ByteDance, Tencent, Alibaba, Baidu, Xiaomi, Arm, Micron Technology, Synopsys, Cadence, CrowdStrike, Cloudflare, CoreWeave, Mistral AI, xAI, and Cohere.
- Robotics, Autonomy, and Physical AI: NVIDIA, Apple, Alphabet/Google, Amazon, Meta, Microsoft, Tesla, SpaceX, Blue Origin, Waymo, Zoox, Intuitive Surgical, Skydio, Aurora Innovation, Nuro, Zipline, Symbotic, Apptronik, Dexterity, Locus Robotics, Intrinsic, Physical Intelligence, Serve Robotics, Sanctuary AI, Boston Dynamics, Gecko Robotics, KUKA, Bosch, Lucid Motors, Joby Aviation, Hyundai Motor Group, Toyota Research Institute, 1X, Engineered Arts, Agility Robotics, Unitree, Pudu Robotics, Agile Robots, NEURA Robotics, AGIBOT, Nimble, Standard Bots, Matic, Geek+, GreyOrange, Honeywell, ABB, Neuralink, FANUC, Yaskawa, Universal Robots, Kawasaki Robotics, Mitsubishi Electric, OMRON, Rockwell Automation, Teradyne, Keyence, Siemens, Schneider Electric, Stryker, Medtronic, CMR Surgical, Figure AI, Skild AI, Covariant, RightHand Robotics, Zebra Technologies, Daifuku, DENSO Robotics, Epson Robots, ANYbotics, Doosan Robotics, Johnson & Johnson, and Anduril.
- Industrial Automation and Advanced Manufacturing: ABB, Rockwell Automation, Teradyne, John Deere, Caterpillar, Siemens, Bosch, Emerson, Schneider Electric, Mitsubishi Electric, Honeywell, FANUC, KUKA, Yaskawa, OMRON, Keyence, Universal Robots, Kawasaki Robotics, Daifuku, DENSO Robotics, Epson Robots, and Zebra Technologies.
- Automotive, EVs, and Autonomous Mobility: General Motors, Rivian, Ford Motor Company, Tesla, Toyota Motor, Volkswagen Group, BYD, Hyundai Motor Group, Kia, Honda Motor, Mercedes-Benz Group, BMW Group, Stellantis, Renault Group, Nissan Motor, Geely Holding, SAIC Motor, Chery Automobile, Volvo Cars, Mazda, Subaru, Tata Motors/JLR, Ferrari, Porsche, Lucid Motors, Waymo, Zoox, Mobileye, Aptiv, Magna International, Continental, ZF, DENSO, Aisin, BorgWarner, Aurora Innovation, and Nuro.
- Aerospace and Defense: SpaceX, Blue Origin, Boeing, Lockheed Martin, RTX, Anduril, Joby Aviation, Skydio, Honeywell, and Palantir.
- Medical Robotics: Intuitive Surgical, Johnson & Johnson, Stryker, Medtronic, CMR Surgical, and Neuralink.

Use aliases to preserve recognizable names: `Google` under Alphabet/Google, `Raytheon` and `Raytheon Technologies` under RTX, `John Deere USA` under John Deere, `Hyundai USA` under Hyundai Motor Group, `FANUC America Corporation` under FANUC, `Yaskawa America` under Yaskawa, `OMRON Group` under OMRON, `Agile Robots SE` under Agile Robots, `1X Technology` under 1X, `UniTree IT` under Unitree, and `Neuralinks`/`Neuralink Network` under Neuralink.

Initialize `config/sources.yaml` with one `NEEDS_MANUAL_REVIEW` disabled source record per company. Each record uses the company's official HTTPS careers landing page as `endpoint`, `provider: discovery`, `source_class: fallback`, and `interval_minutes: 30`. The source audit replaces each discovery record with verified source entries rather than guessing provider URLs.

Initialize `config/filters.yaml` as:

```yaml
filters:
  - id: all-jobs-dry-run
    enabled: true
    notify: false
    include_companies: []
    include_title_keywords: []
    exclude_title_keywords: []
    include_locations: []
    workplace_types: []
```

- [ ] **Step 4: Implement strict cross-file configuration validation**

```python
# src/job_alert/config.py
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from job_alert.models import CompanyTarget, SourceConfig


class ConfigError(ValueError):
    pass


class FilterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    enabled: bool = True
    notify: bool = False
    include_companies: list[str] = Field(default_factory=list)
    include_title_keywords: list[str] = Field(default_factory=list)
    exclude_title_keywords: list[str] = Field(default_factory=list)
    include_locations: list[str] = Field(default_factory=list)
    workplace_types: list[str] = Field(default_factory=list)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    companies: list[CompanyTarget]
    sources: list[SourceConfig]
    filters: list[FilterConfig]


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return value


def load_app_config(config_dir: Path) -> AppConfig:
    try:
        config = AppConfig(
            companies=_read_yaml(config_dir / "company_targets.yaml").get("companies", []),
            sources=_read_yaml(config_dir / "sources.yaml").get("sources", []),
            filters=_read_yaml(config_dir / "filters.yaml").get("filters", []),
        )
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc
    company_ids = {company.id for company in config.companies}
    if len(company_ids) != len(config.companies):
        raise ConfigError("duplicate company id")
    for source in config.sources:
        if source.company_id not in company_ids:
            raise ConfigError(f"unknown company_id {source.company_id}")
    source_company_ids = {source.company_id for source in config.sources}
    for company_id in sorted(company_ids - source_company_ids):
        raise ConfigError(f"missing source entry: {company_id}")
    return config
```

- [ ] **Step 5: Validate the real configuration and run unit tests**

Run: `uv run pytest tests/unit/test_config.py -v && uv run python -c 'from pathlib import Path; from job_alert.config import load_app_config; c=load_app_config(Path("config")); print(len(c.companies), len(c.sources))'`

Expected: tests pass; both printed counts are equal and greater than 100.

- [ ] **Step 6: Commit the canonical inventory and loader**

```bash
git add config src/job_alert/config.py tests/unit/test_config.py
git commit -m "feat: add canonical company registry"
```

### Task 3: SQLite Schema and Transactional Repository

**Files:**
- Create: `migrations/001_initial.sql`
- Create: `src/job_alert/db.py`
- Create: `tests/unit/test_db.py`

**Interfaces:**
- Consumes: `NormalizedJob`, `PollRun`, `RunOutcome`, and source/company IDs.
- Produces: `Repository.open(path)`, `Repository.initialize()`, `Repository.start_run()`, `Repository.finish_run()`, `Repository.upsert_observations()`, `Repository.load_active_jobs()`, `Repository.record_notification()`, and `Repository.source_health()`.

- [ ] **Step 1: Write failing repository tests**

```python
# tests/unit/test_db.py
from datetime import UTC, datetime
from pathlib import Path

from job_alert.db import Repository
from job_alert.models import NormalizedJob, RunOutcome


def make_job() -> NormalizedJob:
    return NormalizedJob(
        company_id="openai",
        source_id="openai-greenhouse",
        source_job_id="123",
        canonical_job_key="greenhouse:openai:123",
        dedupe_key="requisition:openai:research-123",
        title="Research Engineer",
        locations=["San Francisco, CA"],
        posting_url="https://example.com/jobs/123",
        apply_url="https://example.com/jobs/123/apply",
        first_seen_at=datetime(2026, 9, 18, tzinfo=UTC),
    )


def test_repository_persists_job_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    with Repository.open(path) as repo:
        repo.initialize()
        run_id = repo.start_run("openai-greenhouse", datetime.now(UTC))
        repo.upsert_observations(run_id, [make_job()])
        repo.finish_run(run_id, RunOutcome.COMPLETE, datetime.now(UTC), jobs_seen=1)
    with Repository.open(path) as repo:
        jobs = repo.load_active_jobs("openai-greenhouse")
        assert [job.canonical_job_key for job in jobs] == ["greenhouse:openai:123"]


def test_failed_run_does_not_change_last_complete_snapshot(tmp_path: Path) -> None:
    with Repository.open(tmp_path / "state.sqlite3") as repo:
        repo.initialize()
        first = repo.start_run("openai-greenhouse", datetime.now(UTC))
        repo.upsert_observations(first, [make_job()])
        repo.finish_run(first, RunOutcome.COMPLETE, datetime.now(UTC), jobs_seen=1)
        failed = repo.start_run("openai-greenhouse", datetime.now(UTC))
        repo.finish_run(failed, RunOutcome.FAILED, datetime.now(UTC), jobs_seen=0)
        assert repo.last_complete_job_keys("openai-greenhouse") == {
            "greenhouse:openai:123"
        }
```

- [ ] **Step 2: Run the tests to verify failure**

Run: `uv run pytest tests/unit/test_db.py -v`

Expected: FAIL because `job_alert.db` does not exist.

- [ ] **Step 3: Create the complete initial schema**

The migration must create these tables and constraints:

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS poll_runs (
  id INTEGER PRIMARY KEY,
  source_id TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  outcome TEXT NOT NULL,
  http_status INTEGER,
  jobs_seen INTEGER NOT NULL DEFAULT 0,
  page_count INTEGER NOT NULL DEFAULT 0,
  declared_total INTEGER,
  error_code TEXT,
  error_message TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
  canonical_job_key TEXT PRIMARY KEY,
  dedupe_key TEXT NOT NULL,
  company_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_job_id TEXT NOT NULL,
  requisition_id TEXT,
  title TEXT NOT NULL,
  locations_json TEXT NOT NULL,
  department TEXT,
  team TEXT,
  employment_type TEXT,
  workplace_type TEXT,
  description_text TEXT,
  content_hash TEXT,
  posting_url TEXT NOT NULL,
  apply_url TEXT NOT NULL,
  source_published_at TEXT,
  source_updated_at TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  closed_at TEXT,
  missing_complete_runs INTEGER NOT NULL DEFAULT 0,
  raw_metadata_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS jobs_source_active
ON jobs(source_id, closed_at);

CREATE INDEX IF NOT EXISTS jobs_dedupe_active
ON jobs(dedupe_key, closed_at);

CREATE TABLE IF NOT EXISTS job_observations (
  run_id INTEGER NOT NULL REFERENCES poll_runs(id),
  canonical_job_key TEXT NOT NULL REFERENCES jobs(canonical_job_key),
  observed_at TEXT NOT NULL,
  content_hash TEXT,
  PRIMARY KEY(run_id, canonical_job_key)
);

CREATE TABLE IF NOT EXISTS job_events (
  id INTEGER PRIMARY KEY,
  canonical_job_key TEXT NOT NULL REFERENCES jobs(canonical_job_key),
  event_type TEXT NOT NULL,
  detected_at TEXT NOT NULL,
  run_id INTEGER NOT NULL REFERENCES poll_runs(id),
  UNIQUE(canonical_job_key, event_type, run_id)
);

CREATE TABLE IF NOT EXISTS notification_attempts (
  id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL,
  attempted_at TEXT NOT NULL,
  outcome TEXT NOT NULL,
  provider_request_id TEXT,
  http_status INTEGER,
  error_message TEXT
);

CREATE TABLE IF NOT EXISTS notification_event_links (
  notification_attempt_id INTEGER NOT NULL REFERENCES notification_attempts(id),
  event_id INTEGER NOT NULL REFERENCES job_events(id),
  PRIMARY KEY(notification_attempt_id, event_id)
);

CREATE TABLE IF NOT EXISTS source_state (
  source_id TEXT PRIMARY KEY,
  etag TEXT,
  last_modified TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_complete_at TEXT,
  backoff_until TEXT,
  health_alert_open INTEGER NOT NULL DEFAULT 0
);
```

- [ ] **Step 4: Implement `Repository` with explicit transactions and UTC ISO serialization**

Use `sqlite3.Row`, `BEGIN IMMEDIATE` for state-changing snapshot operations, `json.dumps(..., sort_keys=True)`, and `datetime.isoformat()`. `finish_run()` updates `source_state.last_complete_at` and resets `consecutive_failures` only for `RunOutcome.COMPLETE`; failed outcomes increment the counter without modifying job absence state. `upsert_observations()` preserves the earliest `first_seen_at` and clears `closed_at` only when a later diff explicitly classifies a reopening.

- [ ] **Step 5: Run repository tests, migration idempotence, and static checks**

Run: `uv run pytest tests/unit/test_db.py -v && uv run python -c 'from pathlib import Path; from job_alert.db import Repository; p=Path("/tmp/job-alert-plan-check.sqlite3"); r=Repository.open(p); r.initialize(); r.initialize(); r.close()' && uv run ruff check src/job_alert/db.py tests/unit/test_db.py && uv run mypy src`

Expected: all commands exit 0; initializing twice succeeds.

- [ ] **Step 6: Commit the storage layer**

```bash
git add migrations/001_initial.sql src/job_alert/db.py tests/unit/test_db.py
git commit -m "feat: add persistent job state repository"
```

### Task 4: Canonical Identity, Snapshot Diffing, and Closure Safety

**Files:**
- Create: `src/job_alert/identity.py`
- Create: `src/job_alert/diff.py`
- Create: `tests/unit/test_identity.py`
- Create: `tests/unit/test_diff.py`

**Interfaces:**
- Consumes: normalized job fields and the prior active snapshot.
- Produces: `canonicalize_url(url: str) -> str`, `build_job_key(...) -> str`, `build_dedupe_key(...) -> str`, `SnapshotDiff`, and `compare_snapshot(previous, current, complete, close_after=2) -> SnapshotDiff`.

- [ ] **Step 1: Write failing identity and diff tests**

```python
# tests/unit/test_identity.py
from job_alert.identity import canonicalize_url


def test_canonicalize_url_removes_tracking_but_preserves_job_id() -> None:
    assert canonicalize_url(
        "https://jobs.example.com/opening?id=123&utm_source=linkedin&gh_src=abc#apply"
    ) == "https://jobs.example.com/opening?id=123"
```

```python
# tests/unit/test_diff.py
from job_alert.diff import compare_snapshot


def test_incomplete_snapshot_never_marks_missing_job() -> None:
    diff = compare_snapshot(
        previous={"a": {"content_hash": "one", "missing_complete_runs": 0}},
        current={},
        complete=False,
        close_after=2,
    )
    assert diff.closed == []
    assert diff.missing == []


def test_job_closes_only_after_two_complete_absences() -> None:
    first = compare_snapshot(
        previous={"a": {"content_hash": "one", "missing_complete_runs": 0}},
        current={},
        complete=True,
        close_after=2,
    )
    second = compare_snapshot(
        previous={"a": {"content_hash": "one", "missing_complete_runs": 1}},
        current={},
        complete=True,
        close_after=2,
    )
    assert first.missing == ["a"]
    assert first.closed == []
    assert second.closed == ["a"]
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/unit/test_identity.py tests/unit/test_diff.py -v`

Expected: FAIL because the modules do not exist.

- [ ] **Step 3: Implement deterministic URL and job identity rules**

`canonicalize_url()` must lowercase the hostname, remove fragments, sort query parameters, and remove only this explicit tracking allowlist: `utm_source`, `utm_medium`, `utm_campaign`, `utm_term`, `utm_content`, `gh_src`, `lever-source`, and `source`. It must preserve all other parameters. `build_job_key()` must use provider, tenant, and source job ID when present; otherwise use a SHA-256 hash of company ID, normalized title, sorted locations, and canonicalized application URL. `build_dedupe_key()` must prefer company plus stable requisition ID, then company plus canonicalized application URL, then a SHA-256 hash of company, normalized title, sorted locations, and normalized department. This second key links the same public job observed on multiple boards while preserving each source observation.

- [ ] **Step 4: Implement immutable `SnapshotDiff` and comparison rules**

```python
# src/job_alert/diff.py
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class SnapshotDiff:
    new: list[str]
    updated: list[str]
    unchanged: list[str]
    reopened: list[str]
    missing: list[str]
    closed: list[str]


def compare_snapshot(
    previous: Mapping[str, Mapping[str, object]],
    current: Mapping[str, Mapping[str, object]],
    *,
    complete: bool,
    close_after: int = 2,
) -> SnapshotDiff:
    new = sorted(current.keys() - previous.keys())
    updated: list[str] = []
    unchanged: list[str] = []
    reopened: list[str] = []
    for key in sorted(current.keys() & previous.keys()):
        old = previous[key]
        now = current[key]
        if old.get("closed_at") is not None:
            reopened.append(key)
        elif old.get("content_hash") != now.get("content_hash"):
            updated.append(key)
        else:
            unchanged.append(key)
    missing: list[str] = []
    closed: list[str] = []
    if complete:
        for key in sorted(previous.keys() - current.keys()):
            count = int(previous[key].get("missing_complete_runs", 0)) + 1
            (closed if count >= close_after else missing).append(key)
    return SnapshotDiff(new, updated, unchanged, reopened, missing, closed)
```

- [ ] **Step 5: Run focused and complete unit tests**

Run: `uv run pytest tests/unit/test_identity.py tests/unit/test_diff.py -v && uv run pytest tests/unit -q`

Expected: all tests pass.

- [ ] **Step 6: Commit identity and comparison logic**

```bash
git add src/job_alert/identity.py src/job_alert/diff.py tests/unit/test_identity.py tests/unit/test_diff.py
git commit -m "feat: add safe job snapshot comparison"
```

### Task 5: Bounded HTTP Fetcher, Retry Policy, and Challenge Detection

**Files:**
- Create: `src/job_alert/fetch.py`
- Create: `tests/unit/test_fetch.py`

**Interfaces:**
- Consumes: `SourceConfig`, conditional-request state, and a requested URL.
- Produces: `RequestSpec`, `HttpPayload`, `FetchFailure`, and `HttpFetcher.get(spec) -> HttpPayload`.

- [ ] **Step 1: Write failing async HTTP safety tests**

```python
# tests/unit/test_fetch.py
import httpx
import pytest
import respx

from job_alert.fetch import ChallengeDetected, HttpFetcher, PayloadTooLarge, RequestSpec


@respx.mock
@pytest.mark.asyncio
async def test_fetcher_rejects_html_challenge_returned_as_200() -> None:
    respx.get("https://example.com/jobs").mock(
        return_value=httpx.Response(200, text="<html><title>Just a moment...</title></html>")
    )
    async with HttpFetcher() as fetcher:
        with pytest.raises(ChallengeDetected):
            await fetcher.get(RequestSpec(url="https://example.com/jobs", expected="json"))


@respx.mock
@pytest.mark.asyncio
async def test_fetcher_rejects_oversized_payload() -> None:
    respx.get("https://example.com/jobs").mock(
        return_value=httpx.Response(200, content=b"x" * 101)
    )
    async with HttpFetcher(max_bytes=100) as fetcher:
        with pytest.raises(PayloadTooLarge):
            await fetcher.get(RequestSpec(url="https://example.com/jobs", expected="bytes"))
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/unit/test_fetch.py -v`

Expected: FAIL because `job_alert.fetch` does not exist.

- [ ] **Step 3: Implement request/response types and bounded streaming**

`RequestSpec` contains URL, headers, expected media class, timeout seconds, and optional ETag/Last-Modified. `HttpPayload` contains final URL, status, headers, body bytes, fetched time, ETag, and Last-Modified. Stream response bytes and abort once `max_bytes` is exceeded. Default limits are 20 seconds connect, 30 seconds total read, five redirects, and 15 MiB per response.

- [ ] **Step 4: Implement retry and response classification**

Retry at most three attempts for connection failures, timeouts, `408`, `425`, `429`, and `5xx`. Honor a numeric or HTTP-date `Retry-After`; otherwise use exponential waits of approximately 1, 2, and 4 seconds with jitter. Do not retry other `4xx` responses. Detect common HTML challenges using response media type plus bounded case-insensitive markers: `captcha`, `cf-chl-`, `just a moment`, `access denied`, and `verify you are human`. A `304` response returns an explicit `not_modified=True` payload and does not parse a body.

- [ ] **Step 5: Add tests for ETag, `304`, `429`, and non-retryable `403`**

Use `respx` side effects to assert the number of calls. Patch the retry wait function to avoid real sleeps. Assert `If-None-Match` and `If-Modified-Since` are sent when present.

- [ ] **Step 6: Run fetcher tests and static checks**

Run: `uv run pytest tests/unit/test_fetch.py -v && uv run ruff check src/job_alert/fetch.py tests/unit/test_fetch.py && uv run mypy src`

Expected: all commands exit 0.

- [ ] **Step 7: Commit the fetch layer**

```bash
git add src/job_alert/fetch.py tests/unit/test_fetch.py
git commit -m "feat: add bounded resilient HTTP fetching"
```

### Task 6: Adapter Contract and Shared Validation Harness

**Files:**
- Create: `src/job_alert/adapters/__init__.py`
- Create: `src/job_alert/adapters/base.py`
- Create: `tests/contract/test_adapter_contract.py`
- Create: `tests/helpers.py`

**Interfaces:**
- Consumes: `HttpFetcher`, `SourceConfig`, `CollectionResult`, and normalized models.
- Produces: `SourceAdapter` protocol, `AdapterError`, `PartialCollectionError`, `validate_collection()`, and reusable contract assertions.

- [ ] **Step 1: Write the failing adapter contract test with a fake adapter**

```python
# tests/contract/test_adapter_contract.py
from datetime import UTC, datetime

import pytest

from job_alert.adapters.base import AdapterError, validate_collection
from job_alert.models import CollectionResult, NormalizedJob


def make_job(key: str = "provider:tenant:1") -> NormalizedJob:
    return NormalizedJob(
        company_id="company",
        source_id="source",
        source_job_id="1",
        canonical_job_key=key,
        dedupe_key="requisition:company:1",
        title="Engineer",
        locations=["Chicago, IL"],
        posting_url="https://example.com/jobs/1",
        apply_url="https://example.com/jobs/1/apply",
        first_seen_at=datetime.now(UTC),
    )


def test_validate_collection_rejects_declared_total_mismatch() -> None:
    result = CollectionResult(
        source_id="source", complete=True, jobs=[make_job()], declared_total=2
    )
    with pytest.raises(AdapterError, match="declared 2 jobs but parsed 1"):
        validate_collection(result)
```

- [ ] **Step 2: Run the contract test to verify failure**

Run: `uv run pytest tests/contract/test_adapter_contract.py -v`

Expected: FAIL because `job_alert.adapters.base` does not exist.

- [ ] **Step 3: Implement the adapter boundary**

```python
# src/job_alert/adapters/base.py
from __future__ import annotations

from typing import Protocol

from job_alert.fetch import HttpFetcher
from job_alert.models import CollectionResult, SourceConfig


class AdapterError(RuntimeError):
    pass


class PartialCollectionError(AdapterError):
    pass


class SourceAdapter(Protocol):
    provider: str

    async def collect(
        self, source: SourceConfig, fetcher: HttpFetcher
    ) -> CollectionResult: ...


def validate_collection(result: CollectionResult) -> CollectionResult:
    if result.complete and result.declared_total is not None:
        if len(result.jobs) != result.declared_total:
            raise AdapterError(
                f"declared {result.declared_total} jobs but parsed {len(result.jobs)}"
            )
    if not result.complete:
        raise PartialCollectionError("adapter returned an incomplete collection")
    return result
```

- [ ] **Step 4: Add shared adapter assertions**

In `tests/helpers.py`, add `assert_normalized_job(job)` that verifies a nonblank stable ID, canonical key, title, HTTPS posting URL, HTTPS application URL, timezone-aware `first_seen_at`, and no URL containing obvious test placeholders. Add `assert_repeat_is_stable(first, second)` that compares canonical keys and normalized core fields from two collections.

- [ ] **Step 5: Run contract and full unit tests**

Run: `uv run pytest tests/contract tests/unit -q`

Expected: all tests pass.

- [ ] **Step 6: Commit the adapter contract**

```bash
git add src/job_alert/adapters tests/contract tests/helpers.py
git commit -m "feat: define provider adapter contract"
```

### Task 7: Greenhouse Adapter and Live Contract Probe

**Files:**
- Create: `src/job_alert/adapters/greenhouse.py`
- Create: `tests/fixtures/greenhouse/jobs.json`
- Create: `tests/unit/adapters/test_greenhouse.py`
- Create: `tests/live/test_greenhouse_live.py`

**Interfaces:**
- Consumes: `SourceAdapter`, `HttpFetcher`, and a `SourceConfig` whose endpoint is the Greenhouse list-jobs URL.
- Produces: `GreenhouseAdapter.collect(source, fetcher) -> CollectionResult` with provider `greenhouse`.

- [ ] **Step 1: Capture and sanitize a representative Greenhouse fixture**

Create a fixture with exactly two jobs. Include `id`, `internal_job_id`, `title`, `updated_at`, `first_published`, `requisition_id`, `location.name`, `absolute_url`, `departments`, `offices`, and `content`. Replace personal recruiter data if present; preserve field structure and realistic HTTPS URLs.

- [ ] **Step 2: Write failing Greenhouse normalization tests**

```python
# tests/unit/adapters/test_greenhouse.py
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from job_alert.adapters.greenhouse import GreenhouseAdapter
from job_alert.fetch import HttpFetcher
from job_alert.models import SourceClass, SourceConfig, SupportStatus


FIXTURE = Path("tests/fixtures/greenhouse/jobs.json")


@respx.mock
@pytest.mark.asyncio
async def test_greenhouse_normalizes_ids_locations_and_urls() -> None:
    endpoint = "https://boards-api.greenhouse.io/v1/boards/example/jobs?content=true"
    respx.get(endpoint).mock(return_value=httpx.Response(200, json=json.loads(FIXTURE.read_text())))
    source = SourceConfig(
        id="example-greenhouse",
        company_id="example",
        provider="greenhouse",
        source_class=SourceClass.DOCUMENTED_API,
        support_status=SupportStatus.SUPPORTED,
        endpoint=endpoint,
        interval_minutes=5,
        tenant="example",
    )
    async with HttpFetcher() as fetcher:
        result = await GreenhouseAdapter(now=lambda: datetime(2026, 9, 18, tzinfo=UTC)).collect(
            source, fetcher
        )
    assert result.complete is True
    assert result.declared_total == 2
    assert result.jobs[0].canonical_job_key.startswith("greenhouse:example:")
    assert result.jobs[0].locations
    assert str(result.jobs[0].apply_url).startswith("https://")
```

- [ ] **Step 3: Run the focused test to verify failure**

Run: `uv run pytest tests/unit/adapters/test_greenhouse.py -v`

Expected: FAIL because `GreenhouseAdapter` does not exist.

- [ ] **Step 4: Implement Greenhouse parsing**

Issue one GET to the configured endpoint with `content=true`. Use `id` as `source_job_id`, the configured board tenant plus `id` for identity, `first_published` as `source_published_at`, and `updated_at` as `source_updated_at`. Prefer `absolute_url` as both posting and application URL unless the registry explicitly supplies a safe application URL template. Convert HTML description content to plain text deterministically before hashing. Set `declared_total` from `meta.total` and reject mismatches.

- [ ] **Step 5: Add the opt-in live probe**

The live test loads `config/sources.yaml`, selects the first enabled `greenhouse` source with `SUPPORTED` status, collects it twice, invokes `validate_collection()`, and uses `assert_repeat_is_stable()`. Skip with a clear message if the audited registry has no enabled Greenhouse source. Mark it `@pytest.mark.live`.

- [ ] **Step 6: Run fixture tests and the live probe separately**

Run: `uv run pytest tests/unit/adapters/test_greenhouse.py tests/contract -q`

Expected: all offline tests pass.

Run after Task 11 has enabled a verified source: `uv run pytest tests/live/test_greenhouse_live.py -m live -v`

Expected: one complete stable collection; no challenge or pagination warnings.

- [ ] **Step 7: Commit the Greenhouse adapter**

```bash
git add src/job_alert/adapters/greenhouse.py tests/fixtures/greenhouse tests/unit/adapters/test_greenhouse.py tests/live/test_greenhouse_live.py
git commit -m "feat: add Greenhouse job adapter"
```

### Task 8: Lever Adapter and Live Contract Probe

**Files:**
- Create: `src/job_alert/adapters/lever.py`
- Create: `tests/fixtures/lever/postings.json`
- Create: `tests/unit/adapters/test_lever.py`
- Create: `tests/live/test_lever_live.py`

**Interfaces:**
- Consumes: Lever public Postings API endpoint and adapter contract.
- Produces: `LeverAdapter.collect(source, fetcher) -> CollectionResult` with complete pagination.

- [ ] **Step 1: Create a two-posting Lever JSON fixture**

Include `id`, `text`, `categories.location`, `categories.allLocations`, `categories.commitment`, `categories.team`, `categories.department`, `country`, `descriptionPlain`, `hostedUrl`, `applyUrl`, `workplaceType`, and `createdAt`. Include one posting without `createdAt` to prove `first_seen_at` remains usable.

- [ ] **Step 2: Write failing Lever pagination and normalization tests**

```python
# tests/unit/adapters/test_lever.py
@respx.mock
@pytest.mark.asyncio
async def test_lever_collects_until_short_page_and_deduplicates_locations() -> None:
    first_page = [fixture_job("1"), fixture_job("2")]
    respx.get(
        "https://api.lever.co/v0/postings/example",
        params={"mode": "json", "skip": "0", "limit": "2"},
    ).mock(return_value=httpx.Response(200, json=first_page))
    respx.get(
        "https://api.lever.co/v0/postings/example",
        params={"mode": "json", "skip": "2", "limit": "2"},
    ).mock(return_value=httpx.Response(200, json=[]))
    source = make_lever_source(parameters={"page_size": 2})
    async with HttpFetcher() as fetcher:
        result = await LeverAdapter(now=fixed_now).collect(source, fetcher)
    assert result.complete is True
    assert result.page_count == 2
    assert [job.source_job_id for job in result.jobs] == ["1", "2"]
```

Define `fixture_job`, `make_lever_source`, and `fixed_now` in the test file with concrete valid job dictionaries and UTC time.

- [ ] **Step 3: Run the test to verify failure**

Run: `uv run pytest tests/unit/adapters/test_lever.py -v`

Expected: FAIL because `LeverAdapter` does not exist.

- [ ] **Step 4: Implement Lever pagination and field mapping**

Use GET `/v0/postings/{site}` with `mode=json`, `skip`, and `limit`. Continue until a page is shorter than `limit`; reject repeated page IDs as a pagination loop. Map `id`, `text`, all unique locations, team, department, commitment, workplace type, plain description, `hostedUrl`, and `applyUrl`. Convert millisecond `createdAt` to UTC when present. The canonical key is `lever:{tenant}:{id}`.

- [ ] **Step 5: Add and run the Lever live contract probe**

Mirror the Greenhouse live-test policy: load the first enabled verified Lever source, collect twice, require completeness, and compare stable identities. Run: `uv run pytest tests/live/test_lever_live.py -m live -v`.

Expected: stable complete results or a deliberate skip when the source audit found no enabled Lever company.

- [ ] **Step 6: Run all offline adapter tests and commit**

Run: `uv run pytest tests/unit/adapters/test_lever.py tests/contract -q`

```bash
git add src/job_alert/adapters/lever.py tests/fixtures/lever tests/unit/adapters/test_lever.py tests/live/test_lever_live.py
git commit -m "feat: add Lever job adapter"
```

### Task 9: Ashby Adapter and Live Contract Probe

**Files:**
- Create: `src/job_alert/adapters/ashby.py`
- Create: `tests/fixtures/ashby/job_board.json`
- Create: `tests/unit/adapters/test_ashby.py`
- Create: `tests/live/test_ashby_live.py`

**Interfaces:**
- Consumes: Ashby Public Job Postings API endpoint.
- Produces: `AshbyAdapter.collect(source, fetcher) -> CollectionResult`.

- [ ] **Step 1: Capture a sanitized Ashby fixture and document field semantics in the test**

The fixture must contain `jobs` with two records and preserve `id`, `title`, `location`, `secondaryLocations`, `department`, `team`, `employmentType`, `workplaceType`, `descriptionPlain`, `publishedAt`, `jobUrl`, and `applyUrl` when present. The test comments must state which timestamp is publication time and which fields were absent in the real response.

- [ ] **Step 2: Write failing Ashby tests**

Test that primary and secondary locations are combined without duplicates, `publishedAt` is parsed as timezone-aware UTC, `jobUrl` and `applyUrl` remain distinct, and a missing optional department does not invalidate the job.

- [ ] **Step 3: Run the focused test to verify failure**

Run: `uv run pytest tests/unit/adapters/test_ashby.py -v`

Expected: FAIL because `AshbyAdapter` does not exist.

- [ ] **Step 4: Implement the Ashby adapter**

GET the configured `https://api.ashbyhq.com/posting-api/job-board/{board}` endpoint. Require a top-level `jobs` list. Use the published record ID as the stable ID, strip description HTML only when no plain version exists, and calculate a content hash from title, normalized locations, and description. Mark the collection complete only after response shape and unique ID validation pass.

- [ ] **Step 5: Add and run the Ashby live probe**

Select the first audited enabled Ashby source, collect twice, and assert stable keys. Run: `uv run pytest tests/live/test_ashby_live.py -m live -v`.

Expected: stable complete results or a deliberate skip if the audit finds no enabled Ashby source.

- [ ] **Step 6: Run offline tests and commit**

Run: `uv run pytest tests/unit/adapters/test_ashby.py tests/contract -q`

```bash
git add src/job_alert/adapters/ashby.py tests/fixtures/ashby tests/unit/adapters/test_ashby.py tests/live/test_ashby_live.py
git commit -m "feat: add Ashby job adapter"
```

### Task 10: SmartRecruiters Adapter and Live Contract Probe

**Files:**
- Create: `src/job_alert/adapters/smartrecruiters.py`
- Create: `tests/fixtures/smartrecruiters/postings-page-1.json`
- Create: `tests/fixtures/smartrecruiters/postings-page-2.json`
- Create: `tests/unit/adapters/test_smartrecruiters.py`
- Create: `tests/live/test_smartrecruiters_live.py`

**Interfaces:**
- Consumes: SmartRecruiters company postings endpoint and offset/limit pagination.
- Produces: `SmartRecruitersAdapter.collect(source, fetcher) -> CollectionResult`.

- [ ] **Step 1: Create two sanitized pagination fixtures**

Preserve `totalFound`, `offset`, `limit`, and `content`. Job entries must include `id`, `name`, `refNumber`, `releasedDate`, `location`, `department`, `typeOfEmployment`, and the posting URL information returned by the live source.

- [ ] **Step 2: Write failing total-count and pagination tests**

Test two pages with three total jobs. Assert exactly three stable keys, `page_count == 2`, `declared_total == 3`, and an error if page two repeats a page-one ID or the final unique count differs from `totalFound`.

- [ ] **Step 3: Run focused tests to verify failure**

Run: `uv run pytest tests/unit/adapters/test_smartrecruiters.py -v`

Expected: FAIL because `SmartRecruitersAdapter` does not exist.

- [ ] **Step 4: Implement offset pagination and normalization**

GET the configured company `/postings` endpoint with `offset` and `limit`. Advance offset by the number of returned entries; stop only when collected unique IDs equal `totalFound`. Use `id` as the source job ID, `refNumber` as requisition ID, `releasedDate` as publication time, and normalize city/region/country into one location string. If the list response lacks an application URL, use the provider's documented public posting URL derived from the company identifier and posting ID.

- [ ] **Step 5: Add and run the live probe**

Select the first audited enabled SmartRecruiters source, collect twice, and assert stable keys and declared-total agreement. Run: `uv run pytest tests/live/test_smartrecruiters_live.py -m live -v`.

- [ ] **Step 6: Run offline tests and commit**

Run: `uv run pytest tests/unit/adapters/test_smartrecruiters.py tests/contract -q`

```bash
git add src/job_alert/adapters/smartrecruiters.py tests/fixtures/smartrecruiters tests/unit/adapters/test_smartrecruiters.py tests/live/test_smartrecruiters_live.py
git commit -m "feat: add SmartRecruiters job adapter"
```

### Task 11: Company-by-Company Source Audit and Coverage Report

**Files:**
- Create: `src/job_alert/audit.py`
- Create: `src/job_alert/cli.py`
- Create: `tests/unit/test_audit.py`
- Create: `docs/source-audit.md`
- Modify: `config/sources.yaml`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: canonical company inventory and ordinary public HTTPS responses.
- Produces: `probe_source(source, fetcher) -> ProbeResult`, `coverage_summary(config) -> CoverageSummary`, audit CLI commands, verified source records, and human-readable evidence for every company.

- [ ] **Step 1: Write failing coverage-invariant tests**

```python
# tests/unit/test_audit.py
from job_alert.audit import coverage_summary


def test_coverage_counts_each_company_once_by_best_status(app_config) -> None:
    summary = coverage_summary(app_config)
    assert summary.total_companies == len(app_config.companies)
    assert sum(summary.by_status.values()) == summary.total_companies


def test_broad_activation_requires_ninety_percent_direct_coverage(app_config) -> None:
    summary = coverage_summary(app_config)
    assert summary.broad_activation_allowed is (
        summary.direct_supported / summary.total_companies >= 0.90
    )
```

- [ ] **Step 2: Run the audit tests to verify failure**

Run: `uv run pytest tests/unit/test_audit.py -v`

Expected: FAIL because `job_alert.audit` does not exist.

- [ ] **Step 3: Implement probe result and coverage calculations**

`ProbeResult` records source ID, HTTP status, final URL, media type, provider fingerprint, challenge detection, elapsed milliseconds, parsed job count, pagination completeness, stable-ID percentage, direct-URL percentage, and evidence timestamp. `coverage_summary()` chooses each company's best status in this order: `SUPPORTED`, `SUPPORTED_UNDOCUMENTED`, `HTML_ONLY`, `FALLBACK_ONLY`, `BLOCKED`, `NEEDS_MANUAL_REVIEW`.

- [ ] **Step 4: Implement non-browser provider fingerprinting**

Use ordinary GET/HEAD responses and URLs to detect only positive evidence:

- `boards.greenhouse.io`, `job-boards.greenhouse.io`, or `boards-api.greenhouse.io` → Greenhouse.
- `jobs.lever.co`, `api.lever.co`, or `jobs.eu.lever.co` → Lever.
- `jobs.ashbyhq.com` or `api.ashbyhq.com/posting-api` → Ashby.
- `jobs.smartrecruiters.com` or `api.smartrecruiters.com` → SmartRecruiters.
- `/wday/cxs/` or `myworkdayjobs.com` → Workday.
- `career*.successfactors.*` or `careerSiteCompanyId` → SAP SuccessFactors.
- `icims.com/jobs/` → iCIMS.
- `eightfold.ai/careers` → Eightfold.
- `oraclecloud.com/hcmUI/CandidateExperience` or Taleo markers → Oracle.
- `application/ld+json` containing `JobPosting` → JSON-LD fallback.

Ambiguous evidence remains `NEEDS_MANUAL_REVIEW`; do not infer a provider from company reputation or an unverified third-party directory.

- [ ] **Step 5: Add exact audit CLI operations**

Expose:

```text
job-alert audit probe --source SOURCE_ID
job-alert audit set-source --company COMPANY_ID --provider PROVIDER --endpoint HTTPS_URL --class SOURCE_CLASS --status SUPPORT_STATUS --interval MINUTES --tenant TENANT
job-alert audit disable-source --source SOURCE_ID --status SUPPORT_STATUS --reason TEXT
job-alert audit report --markdown docs/source-audit.md --json reports/source-audit.json
```

`set-source` must validate the endpoint with its provider adapter before replacing the discovery record. `disable-source` must preserve the evidence and reason in registry parameters. YAML writes must use a stable key order and an atomic temporary-file rename.

Create the Typer root application in `src/job_alert/cli.py` with the `audit` subcommands used in this task; Task 23 expands the same application with collection, scheduling, reporting, and notification operations.

- [ ] **Step 6: Audit every company in deterministic category/name order**

For each canonical company:

1. Open only its official careers landing URL using ordinary HTTP.
2. Follow bounded redirects.
3. Record positive provider evidence.
4. Identify every regional, subsidiary, university, or specialized board necessary for complete scope.
5. Run the provider adapter twice when one exists.
6. Verify stable IDs, direct application links, pagination completeness, and repeated no-change behavior.
7. Store the source record and audit date.
8. If access is challenged or disallowed, record `BLOCKED` or `NEEDS_MANUAL_REVIEW`; do not attempt evasion.
9. Add one row to `docs/source-audit.md` containing company, categories, official careers URL, provider, source class, status, job count, evidence date, expected poll interval, and limitations.

Run after each category: `uv run job-alert audit report --markdown docs/source-audit.md --json reports/source-audit.json`.

Expected: every company appears exactly once in the best-status summary and every configured source appears in its company detail.

- [ ] **Step 7: Review the source audit gate**

Run: `uv run pytest tests/unit/test_audit.py -v && uv run job-alert audit report --markdown docs/source-audit.md --json reports/source-audit.json`.

Expected: no company remains without an explicit support status; the report states the direct-coverage percentage and whether the 90% broad-activation gate passes. Continuing to adapter implementation is allowed below 90%; broad production notifications are not.

- [ ] **Step 8: Commit the auditable registry**

```bash
git add config/sources.yaml src/job_alert/audit.py src/job_alert/cli.py tests/unit/test_audit.py docs/source-audit.md pyproject.toml
git commit -m "feat: audit official company job sources"
```

### Task 12: Adapter Registry and Verified Enterprise ATS Fixtures

**Files:**
- Create: `src/job_alert/adapters/registry.py`
- Create: `tests/unit/adapters/test_registry.py`
- Create: `tests/fixtures/workday/`
- Create: `tests/fixtures/successfactors/`
- Create: `tests/fixtures/icims/`
- Create: `tests/fixtures/eightfold/`
- Create: `tests/fixtures/oracle/`

**Interfaces:**
- Consumes: audited `SourceConfig.provider` values and the four documented adapters.
- Produces: `AdapterRegistry.get(provider: str) -> SourceAdapter` and sanitized fixtures representing every enabled enterprise provider variant discovered in Task 11.

- [ ] **Step 1: Write failing adapter-registry tests**

```python
# tests/unit/adapters/test_registry.py
import pytest

from job_alert.adapters.base import AdapterError
from job_alert.adapters.registry import AdapterRegistry


def test_registry_returns_documented_adapter() -> None:
    assert AdapterRegistry.default().get("greenhouse").provider == "greenhouse"


def test_registry_rejects_unknown_provider() -> None:
    with pytest.raises(AdapterError, match="unsupported provider unknown"):
        AdapterRegistry.default().get("unknown")
```

- [ ] **Step 2: Run the registry tests to verify failure**

Run: `uv run pytest tests/unit/adapters/test_registry.py -v`

Expected: FAIL because the registry module does not exist.

- [ ] **Step 3: Implement explicit provider registration**

The registry constructor accepts adapter instances and rejects duplicate provider names. `default()` registers Greenhouse, Lever, Ashby, and SmartRecruiters at this stage. Unknown providers fail closed with the source ID included by the caller.

- [ ] **Step 4: Capture sanitized enterprise fixtures from audited enabled sources**

For each provider family found in Task 11, store one list response, every subsequent pagination response needed to prove completion, and one detail response if details require an additional request. Add a `README.md` inside each provider fixture directory recording source ID, capture date, removed sensitive fields, response media type, and whether the endpoint is documented or unsupported. Do not commit session cookies, authorization headers, anti-CSRF tokens, or personal information.

- [ ] **Step 5: Validate fixture provenance and commit**

Add a test that scans every enterprise fixture directory and requires `README.md`, at least one response file, valid JSON/HTML, and absence of case-insensitive strings `authorization`, `cookie`, `set-cookie`, `api_key`, and `access_token`.

Run: `uv run pytest tests/unit/adapters/test_registry.py tests/unit/adapters/test_fixture_provenance.py -v`.

```bash
git add src/job_alert/adapters/registry.py tests/unit/adapters tests/fixtures/workday tests/fixtures/successfactors tests/fixtures/icims tests/fixtures/eightfold tests/fixtures/oracle
git commit -m "test: capture verified enterprise ATS contracts"
```

### Task 13: Workday CXS Adapter

**Files:**
- Create: `src/job_alert/adapters/workday.py`
- Create: `tests/unit/adapters/test_workday.py`
- Create: `tests/live/test_workday_live.py`
- Modify: `src/job_alert/adapters/registry.py`

**Interfaces:**
- Consumes: an audited Workday source whose endpoint is the public `/wday/cxs/{tenant}/{site}/jobs` URL and whose parameters contain `page_size`, `locale`, and `detail_base_url`.
- Produces: `WorkdayAdapter.collect(source, fetcher) -> CollectionResult`.

- [ ] **Step 1: Enforce the source-audit gate**

Run: `uv run python -c 'from pathlib import Path; from job_alert.config import load_app_config; c=load_app_config(Path("config")); print(sum(s.enabled and s.provider=="workday" for s in c.sources))'`.

Expected: a positive integer before implementing the adapter. If zero, add `Workday: no enabled audited source` to the execution notes, leave Workday sources explicitly classified by the audit, and mark this task not applicable without inventing an endpoint.

- [ ] **Step 2: Write failing Workday pagination tests from the captured fixtures**

```python
# tests/unit/adapters/test_workday.py
@respx.mock
@pytest.mark.asyncio
async def test_workday_pages_by_offset_until_total() -> None:
    route = respx.post("https://example.wd5.myworkdayjobs.com/wday/cxs/example/site/jobs")
    route.side_effect = [
        httpx.Response(200, json=load_fixture("workday/jobs-offset-0.json")),
        httpx.Response(200, json=load_fixture("workday/jobs-offset-20.json")),
    ]
    source = make_workday_source(page_size=20)
    async with HttpFetcher() as fetcher:
        result = await WorkdayAdapter(now=fixed_now).collect(source, fetcher)
    assert result.complete is True
    assert len(result.jobs) == result.declared_total
    assert route.call_count == 2
    assert route.calls[1].request.content != route.calls[0].request.content
```

Use exact fixture totals and assert the decoded request bodies contain offsets `0` and `20`, `limit: 20`, and an empty `searchText`.

- [ ] **Step 3: Run the test to verify failure**

Run: `uv run pytest tests/unit/adapters/test_workday.py -v`

Expected: FAIL because `WorkdayAdapter` does not exist.

- [ ] **Step 4: Implement the audited Workday request shape**

POST JSON with `appliedFacets: {}`, `limit`, `offset`, and `searchText: ""`. Require top-level `total` and `jobPostings`. Continue by `offset += len(jobPostings)`; fail partial if a non-final page is empty or IDs repeat. Use `externalPath` or the audited stable field as `source_job_id`, preserve `bulletFields` in raw metadata, parse `postedOn` only when the fixture proves an absolute timestamp, and use `first_seen_at` otherwise. Construct detail/posting URLs only from the audited `detail_base_url`; do not guess hostnames.

- [ ] **Step 5: Add repeatability and partial-page tests**

Test exact repeated-key stability, a total mismatch, an empty middle page, a challenge response, and a job without an absolute publication timestamp. The latter must have `source_published_at is None` and a populated timezone-aware `first_seen_at`.

- [ ] **Step 6: Register and live-test Workday**

Register `WorkdayAdapter` only after offline tests pass. The live test selects one enabled audited Workday source, collects twice, verifies declared-total agreement and stable identities, and does not assert that human-readable relative dates are publication timestamps.

Run: `uv run pytest tests/unit/adapters/test_workday.py -v && uv run pytest tests/live/test_workday_live.py -m live -v`.

- [ ] **Step 7: Commit the Workday adapter**

```bash
git add src/job_alert/adapters/workday.py src/job_alert/adapters/registry.py tests/unit/adapters/test_workday.py tests/live/test_workday_live.py
git commit -m "feat: add audited Workday job adapter"
```

### Task 14: SAP SuccessFactors Adapter

**Files:**
- Create: `src/job_alert/adapters/successfactors.py`
- Create: `tests/unit/adapters/test_successfactors.py`
- Create: `tests/live/test_successfactors_live.py`
- Modify: `src/job_alert/adapters/registry.py`

**Interfaces:**
- Consumes: only the public SuccessFactors request variant and fixtures verified in Task 11/12.
- Produces: `SuccessFactorsAdapter.collect(source, fetcher) -> CollectionResult`.

- [ ] **Step 1: Enforce the audited-source gate**

Count enabled `successfactors` sources using the Task 13 command with the provider changed. A zero count makes the task not applicable; do not fabricate an OData credential or private API endpoint.

- [ ] **Step 2: Write a fixture-locked failing test**

The test must reproduce the exact audited method, query/body fields, pagination cursor or offset, and public response media type. Assert stable requisition identity, title, location, posting URL, and application URL. If the live source is HTML, parse only explicit result cards or embedded JSON proven by the fixture and set source class to `HTML` rather than pretending it is an API.

- [ ] **Step 3: Run the focused test to verify failure**

Run: `uv run pytest tests/unit/adapters/test_successfactors.py -v`

Expected: FAIL because `SuccessFactorsAdapter` does not exist.

- [ ] **Step 4: Implement exactly the verified public variant**

Reject any source whose `parameters.variant` differs from the audited variant names stored in fixtures. Validate every page/cursor, detect repeated job IDs, keep timestamp fields null unless their semantics are verified, and never send authentication material. A source requiring a login is `BLOCKED`, not an adapter variant.

- [ ] **Step 5: Run offline and live contract tests**

Run: `uv run pytest tests/unit/adapters/test_successfactors.py -v && uv run pytest tests/live/test_successfactors_live.py -m live -v`.

Expected: complete, stable, public data or deliberate task-not-applicable evidence from Step 1.

- [ ] **Step 6: Register and commit the adapter**

```bash
git add src/job_alert/adapters/successfactors.py src/job_alert/adapters/registry.py tests/unit/adapters/test_successfactors.py tests/live/test_successfactors_live.py
git commit -m "feat: add audited SuccessFactors job adapter"
```

### Task 15: iCIMS Adapter

**Files:**
- Create: `src/job_alert/adapters/icims.py`
- Create: `tests/unit/adapters/test_icims.py`
- Create: `tests/live/test_icims_live.py`
- Modify: `src/job_alert/adapters/registry.py`

**Interfaces:**
- Consumes: audited public iCIMS portal responses only.
- Produces: `ICIMSAdapter.collect(source, fetcher) -> CollectionResult`.

- [ ] **Step 1: Enforce the audited-source gate**

Count enabled `icims` sources. If zero, preserve the audit classifications and record this task as not applicable.

- [ ] **Step 2: Write failing tests against captured list and detail fixtures**

Test every audited page required to collect a complete list. Assert source job ID, requisition ID when public, canonical posting URL, direct application URL, title, location, and stable repeated results. Add a test proving a login redirect or challenge cannot be classified complete.

- [ ] **Step 3: Implement the public portal variant**

Use only the ordinary public requests demonstrated by fixtures. Follow pagination links within the same audited host. Restrict detail requests to same-host HTTPS URLs derived from parsed public links. Bound detail concurrency to two requests per host. Parse dates only when the portal provides an absolute documented label.

- [ ] **Step 4: Run unit/live tests, register, and commit**

Run: `uv run pytest tests/unit/adapters/test_icims.py -v && uv run pytest tests/live/test_icims_live.py -m live -v`.

```bash
git add src/job_alert/adapters/icims.py src/job_alert/adapters/registry.py tests/unit/adapters/test_icims.py tests/live/test_icims_live.py
git commit -m "feat: add audited iCIMS job adapter"
```

### Task 16: Eightfold Adapter

**Files:**
- Create: `src/job_alert/adapters/eightfold.py`
- Create: `tests/unit/adapters/test_eightfold.py`
- Create: `tests/live/test_eightfold_live.py`
- Modify: `src/job_alert/adapters/registry.py`

**Interfaces:**
- Consumes: audited public Eightfold career-site request and fixtures.
- Produces: `EightfoldAdapter.collect(source, fetcher) -> CollectionResult`.

- [ ] **Step 1: Enforce the audited-source gate**

Count enabled `eightfold` sources. A zero count makes the task not applicable and prohibits speculative endpoint construction.

- [ ] **Step 2: Write fixture-locked failing tests**

Assert the audited search payload, page token or offset, complete job count, stable job identity, locations, direct links, and handling of multi-location jobs. Include a failure test for a response that changes from expected JSON to challenge HTML.

- [ ] **Step 3: Implement only the audited public request variant**

Keep request keys in registry parameters, validate response types and pagination progress, and restrict URLs to the audited career host. Do not reuse browser session tokens or capture authenticated traffic.

- [ ] **Step 4: Run unit/live tests, register, and commit**

Run: `uv run pytest tests/unit/adapters/test_eightfold.py -v && uv run pytest tests/live/test_eightfold_live.py -m live -v`.

```bash
git add src/job_alert/adapters/eightfold.py src/job_alert/adapters/registry.py tests/unit/adapters/test_eightfold.py tests/live/test_eightfold_live.py
git commit -m "feat: add audited Eightfold job adapter"
```

### Task 17: Oracle Recruiting and Taleo Adapter

**Files:**
- Create: `src/job_alert/adapters/oracle.py`
- Create: `tests/unit/adapters/test_oracle.py`
- Create: `tests/live/test_oracle_live.py`
- Modify: `src/job_alert/adapters/registry.py`

**Interfaces:**
- Consumes: audited public Oracle Candidate Experience or Taleo variant.
- Produces: `OracleAdapter.collect(source, fetcher) -> CollectionResult`.

- [ ] **Step 1: Enforce the audited-source gate**

Count enabled `oracle` sources. If none exist, record task-not-applicable evidence and leave sources classified by the audit.

- [ ] **Step 2: Write separate failing tests for each captured variant**

Parameterize the test by exact `parameters.variant` values found in Task 11. Each variant must prove complete pagination, stable IDs, requisition numbers when available, locations, posting/apply URLs, and repeated identity. Unknown variants must raise `AdapterError("unsupported Oracle variant ...")`.

- [ ] **Step 3: Implement strict variant dispatch**

Create one private collector per audited variant. Do not share selectors merely because two payloads look similar. Reject a changed response shape before producing a complete collection. Absolute publication timestamps may populate `source_published_at`; relative labels must remain in raw metadata only.

- [ ] **Step 4: Run unit/live tests, register, and commit**

Run: `uv run pytest tests/unit/adapters/test_oracle.py -v && uv run pytest tests/live/test_oracle_live.py -m live -v`.

```bash
git add src/job_alert/adapters/oracle.py src/job_alert/adapters/registry.py tests/unit/adapters/test_oracle.py tests/live/test_oracle_live.py
git commit -m "feat: add audited Oracle job adapter"
```

### Task 18: Generic JSON and JobPosting JSON-LD Fallbacks

**Files:**
- Create: `src/job_alert/adapters/generic_json.py`
- Create: `src/job_alert/adapters/jsonld.py`
- Create: `tests/fixtures/generic_json/jobs.json`
- Create: `tests/fixtures/jsonld/jobs.html`
- Create: `tests/unit/adapters/test_generic_json.py`
- Create: `tests/unit/adapters/test_jsonld.py`
- Modify: `src/job_alert/adapters/registry.py`

**Interfaces:**
- Consumes: registry-controlled selectors limited to a safe dotted-key grammar, or public HTML containing schema.org `JobPosting` JSON-LD.
- Produces: `GenericJsonAdapter` and `JsonLdAdapter` for audited fallbacks.

- [ ] **Step 1: Write failing selector-safety and JSON-LD tests**

```python
def test_generic_json_rejects_executable_or_recursive_selector() -> None:
    with pytest.raises(AdapterError):
        compile_selector("jobs[*].__class__")


def test_jsonld_extracts_multiple_job_postings_from_graph() -> None:
    jobs = extract_job_postings(Path("tests/fixtures/jsonld/jobs.html").read_text())
    assert [job["@type"] for job in jobs] == ["JobPosting", "JobPosting"]
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/unit/adapters/test_generic_json.py tests/unit/adapters/test_jsonld.py -v`

Expected: FAIL because fallback modules do not exist.

- [ ] **Step 3: Implement bounded dotted selectors**

Allow identifiers separated by dots plus a single list expansion token `[]`, such as `results.jobs[]`. Reject dunder names, brackets, call syntax, recursive descent, filters, and arbitrary expressions. Registry parameters must explicitly map collection, ID, title, locations, posting URL, application URL, description, published time, and next-page fields. Missing required mapped fields fail the collection instead of silently dropping jobs.

- [ ] **Step 4: Implement JSON-LD extraction**

Parse only `<script type="application/ld+json">` blocks. Traverse top-level objects, arrays, and `@graph`, selecting objects whose `@type` equals `JobPosting` or contains it in a list. Require `title`, `url` or application URL, and an identifier or deterministic fallback identity. Support `jobLocation`, `applicantLocationRequirements`, `employmentType`, `datePosted`, `validThrough`, `hiringOrganization`, and `description`. Strip markup from descriptions and canonicalize URLs.

- [ ] **Step 5: Test malformed scripts, relative URLs, and duplicate graph records**

Malformed JSON-LD produces a warning and continues to other script blocks. Relative URLs resolve only against the audited source origin. Duplicate identifiers collapse within the collection; conflicting duplicates fail validation.

- [ ] **Step 6: Register fallbacks and commit**

Run: `uv run pytest tests/unit/adapters/test_generic_json.py tests/unit/adapters/test_jsonld.py -v`.

```bash
git add src/job_alert/adapters/generic_json.py src/job_alert/adapters/jsonld.py src/job_alert/adapters/registry.py tests/fixtures/generic_json tests/fixtures/jsonld tests/unit/adapters/test_generic_json.py tests/unit/adapters/test_jsonld.py
git commit -m "feat: add structured fallback adapters"
```

### Task 19: Post-Ingestion Job Filters

**Files:**
- Create: `src/job_alert/filters.py`
- Create: `tests/unit/test_filters.py`
- Modify: `src/job_alert/config.py`
- Modify: `config/filters.yaml`

**Interfaces:**
- Consumes: `NormalizedJob` and validated `FilterConfig` records.
- Produces: `FilterDecision`, `evaluate_filters(job, filters) -> list[FilterDecision]`, and deterministic case-insensitive matching.

- [ ] **Step 1: Write failing filter tests**

```python
# tests/unit/test_filters.py
from job_alert.filters import evaluate_filters


def test_exclusion_wins_over_inclusion(engineering_job, filter_config) -> None:
    rule = filter_config(
        include_title_keywords=["engineer"],
        exclude_title_keywords=["sales"],
    )
    job = engineering_job(title="Sales Engineer")
    assert evaluate_filters(job, [rule])[0].matched is False
    assert evaluate_filters(job, [rule])[0].reason == "excluded title keyword: sales"


def test_empty_include_lists_mean_no_restriction(engineering_job, filter_config) -> None:
    decision = evaluate_filters(engineering_job(), [filter_config()])[0]
    assert decision.matched is True
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/unit/test_filters.py -v`

Expected: FAIL because `job_alert.filters` does not exist.

- [ ] **Step 3: Implement normalized matching and explainable decisions**

Normalize Unicode with NFKC, casefold strings, and collapse whitespace. Apply company inclusion, excluded title keywords, included title keywords, location substring rules, and workplace types in that order. Every decision contains `filter_id`, `matched`, `notify`, and one exact human-readable reason. Empty inclusion arrays impose no restriction. Any exclusion match wins.

- [ ] **Step 4: Add first production-filter configuration fields without enabling notifications**

Extend `FilterConfig` with categories, departments, description keywords, employment types, and `priority` constrained to `low`, `normal`, or `high`. Add optional `quiet_hours_start`, `quiet_hours_end`, and an IANA `timezone`; matching events created during quiet hours remain durable and receive a computed `defer_until` rather than being discarded. Keep `all-jobs-dry-run` as `notify: false`. Add a second disabled record named `production-matches` whose arrays are empty and whose `notify` is false; user-specific criteria and live notification are activated only at Task 26.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/unit/test_filters.py tests/unit/test_config.py -v && uv run mypy src`.

```bash
git add src/job_alert/filters.py src/job_alert/config.py config/filters.yaml tests/unit/test_filters.py tests/unit/test_config.py
git commit -m "feat: add explainable job alert filters"
```

### Task 20: Pushover and Optional ntfy Delivery

**Files:**
- Create: `src/job_alert/notifications/__init__.py`
- Create: `src/job_alert/notifications/base.py`
- Create: `src/job_alert/notifications/pushover.py`
- Create: `src/job_alert/notifications/ntfy.py`
- Create: `tests/unit/notifications/test_pushover.py`
- Create: `tests/unit/notifications/test_ntfy.py`

**Interfaces:**
- Consumes: matched `JobEvent` objects and environment-provided credentials.
- Produces: `NotificationMessage`, `DeliveryResult`, `Notifier` protocol, `PushoverNotifier.send()`, and `NtfyNotifier.send()`.

- [ ] **Step 1: Write failing message and Pushover API tests**

```python
# tests/unit/notifications/test_pushover.py
@respx.mock
@pytest.mark.asyncio
async def test_pushover_sends_clickable_job_alert_without_secret_in_logs() -> None:
    route = respx.post("https://api.pushover.net/1/messages.json").mock(
        return_value=httpx.Response(200, json={"status": 1, "request": "req-123"})
    )
    notifier = PushoverNotifier(app_token="secret-token", user_key="secret-user")
    result = await notifier.send(
        NotificationMessage(
            title="New Robotics Job — NVIDIA",
            body="Robotics Software Engineer · Santa Clara, CA",
            url="https://example.com/jobs/123",
            url_title="Open and apply",
            priority="normal",
        )
    )
    assert result.delivered is True
    assert result.provider_request_id == "req-123"
    request_body = route.calls[0].request.content.decode()
    assert "Open+and+apply" in request_body
```

- [ ] **Step 2: Run notification tests to verify failure**

Run: `uv run pytest tests/unit/notifications -v`

Expected: FAIL because notification modules do not exist.

- [ ] **Step 3: Implement shared delivery types and message length policy**

`NotificationMessage` limits title to 250 characters and body to 1,024 UTF-8 characters for Pushover. Truncation must preserve the company, title, and direct URL field; descriptions are never included. Map priorities `low`, `normal`, and `high` to Pushover `-1`, `0`, and `1`. Reserve emergency priority `2` because it requires acknowledgment behavior not approved by the design.

- [ ] **Step 4: Implement Pushover response classification**

POST form data to `https://api.pushover.net/1/messages.json`. A `200` with JSON `status: 1` is delivered. `429` is retryable at the server-supplied reset or `Retry-After`; `5xx` is retryable no sooner than five seconds; other `4xx` responses are permanent. Capture `X-Limit-App-Limit`, `X-Limit-App-Remaining`, and `X-Limit-App-Reset` in `DeliveryResult`. Redact token and user key in exceptions and logs.

- [ ] **Step 5: Implement authenticated ntfy as an optional provider**

POST JSON or UTF-8 text to the configured HTTPS topic. Support access-token authorization, title, priority, tags, and click URL. Reject a public `ntfy.sh` topic without an access token unless `allow_public_topic: true` is explicitly set. Record `429` and server failures as retryable.

- [ ] **Step 6: Implement burst grouping before provider delivery**

Add `group_job_events(events, window_seconds=120)`. Normal/low-priority events for the same company detected within the window become one message titled `N new jobs — Company`, with up to five job titles in the body and a link to the highest-priority posting; the remaining count is stated explicitly. High-priority events bypass the delay and deliver individually. Store all underlying event IDs on the delivery record so grouped delivery remains exactly once.

- [ ] **Step 7: Test retryability, truncation, grouping, quota headers, and secret redaction**

Add `respx` cases for Pushover `400`, `429`, `500`, malformed success JSON, a long multibyte body, five same-company events, two companies in one window, a high-priority bypass, and ntfy unauthenticated public-topic rejection.

- [ ] **Step 8: Run tests and commit**

Run: `uv run pytest tests/unit/notifications -v && uv run ruff check src/job_alert/notifications tests/unit/notifications && uv run mypy src`.

```bash
git add src/job_alert/notifications tests/unit/notifications
git commit -m "feat: add Android push notification delivery"
```

### Task 21: One-Source Collection Service, Baseline Mode, and Event Persistence

**Files:**
- Create: `src/job_alert/service.py`
- Create: `tests/integration/test_service_cycle.py`
- Modify: `src/job_alert/db.py`

**Interfaces:**
- Consumes: source config, adapter registry, fetcher, repository, filters, and notifier.
- Produces: `JobAlertService.collect_source(source, mode) -> CycleResult` and modes `DRY_RUN`, `BASELINE`, `LIVE_SUPPRESSED`, and `LIVE`.

- [ ] **Step 1: Write failing baseline and live-cycle integration tests**

```python
# tests/integration/test_service_cycle.py
@pytest.mark.asyncio
async def test_baseline_stores_jobs_without_notifications(service_harness) -> None:
    result = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.BASELINE
    )
    assert result.new_count == 2
    assert result.notifications_sent == 0
    assert len(service_harness.repo.load_active_jobs(service_harness.source.id)) == 2


@pytest.mark.asyncio
async def test_live_cycle_sends_exactly_one_alert_for_one_added_job(service_harness) -> None:
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.adapter.set_fixture("three-jobs")
    first = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )
    second = await service_harness.service.collect_source(
        service_harness.source, mode=RunMode.LIVE
    )
    assert first.notifications_sent == 1
    assert second.notifications_sent == 0
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/integration/test_service_cycle.py -v`

Expected: FAIL because `JobAlertService` does not exist.

- [ ] **Step 3: Implement explicit run modes**

- `DRY_RUN`: fetch, parse, and compare without persistent job changes or delivery; persist the poll-run diagnostic only.
- `BASELINE`: persist jobs and observations but suppress all job notifications.
- `LIVE_SUPPRESSED`: persist normal state and durable job events, evaluate filters, and report would-deliver decisions without calling a notification provider.
- `LIVE`: persist the complete snapshot, create durable events, evaluate filters, and deliver only previously undelivered matching events.

Open a poll run before collection. Classify fetch/adapter errors as failed, challenged, partial, or quarantined. Only a validated complete collection may update absence counts or closures. Commit snapshot state and events in one transaction before attempting external delivery. Notification retries read unsent durable events, preventing loss after crashes. Before creating a deliverable new/reopened event, query active jobs and prior delivered events by `dedupe_key`; link every source observation, but send at most one alert for the linked public posting. Pass stored ETag/Last-Modified values into fetch requests, persist new validators only after a valid response, and treat `304 Not Modified` as a complete no-change run using the last complete snapshot. Respect filter `defer_until` and burst-grouping state when selecting delivery events.

- [ ] **Step 4: Implement large-drop quarantine**

If the previous complete snapshot has at least 20 jobs and the new complete candidate contains less than 50% of that count, mark the run `QUARANTINED`, persist diagnostics, and do not apply absence or new-job transitions until a second independently fetched collection confirms the same IDs. The second confirming run may proceed only if its collection is complete and identical by canonical-key set.

- [ ] **Step 5: Add crash, partial, and quarantine integration tests**

Test: notification failure after event commit; retry sends it once; partial page retains existing jobs; a 100-to-10 drop is quarantined; a second identical 10-job snapshot can apply; a 10-job challenge page cannot confirm quarantine.

- [ ] **Step 6: Run integration tests and commit**

Run: `uv run pytest tests/integration/test_service_cycle.py -v && uv run pytest tests/unit -q`.

```bash
git add src/job_alert/service.py src/job_alert/db.py tests/integration/test_service_cycle.py
git commit -m "feat: orchestrate safe job collection cycles"
```

### Task 22: Source Health, Failure Suppression, Recovery, and Daily Digest

**Files:**
- Create: `src/job_alert/health.py`
- Create: `tests/unit/test_health.py`
- Modify: `src/job_alert/db.py`

**Interfaces:**
- Consumes: poll-run history and source state.
- Produces: `HealthEvent`, `evaluate_source_health()`, and `build_daily_digest()`.

- [ ] **Step 1: Write failing health-event tests**

```python
# tests/unit/test_health.py
def test_health_alert_opens_once_after_three_failures_and_recovers_once() -> None:
    state = SourceHealthState(consecutive_failures=2, health_alert_open=False)
    failed = evaluate_source_health(state, RunOutcome.FAILED, now=fixed_now)
    assert [event.kind for event in failed.events] == ["source_failure"]
    repeated = evaluate_source_health(failed.state, RunOutcome.FAILED, now=fixed_now)
    assert repeated.events == []
    recovered = evaluate_source_health(repeated.state, RunOutcome.COMPLETE, now=fixed_now)
    assert [event.kind for event in recovered.events] == ["source_recovery"]
```

- [ ] **Step 2: Run the test to verify failure**

Run: `uv run pytest tests/unit/test_health.py -v`

Expected: FAIL because `job_alert.health` does not exist.

- [ ] **Step 3: Implement health policy**

Open one health incident after three consecutive failed/partial/challenged/quarantined runs or after twice the configured interval plus five minutes without a complete success, whichever occurs first. Suppress repeat push events while open. Emit one recovery after the next complete success. Store source ID, company, first failure, last error, consecutive count, and last complete time.

- [ ] **Step 4: Implement daily digest content**

The digest reports UTC and America/Chicago time, total enabled sources, complete sources, stale sources, open incidents, blocked/manual-review companies, new jobs, matched alerts, failed deliveries, and remaining Pushover quota when known. Generate deterministic Markdown and plain-text variants.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/unit/test_health.py -v`.

```bash
git add src/job_alert/health.py src/job_alert/db.py tests/unit/test_health.py
git commit -m "feat: add source health and digest policy"
```

### Task 23: Scheduler, CLI, and End-to-End Local Commands

**Files:**
- Create: `src/job_alert/scheduler.py`
- Modify: `src/job_alert/cli.py`
- Create: `tests/unit/test_scheduler.py`
- Create: `tests/integration/test_cli.py`
- Modify: `src/job_alert/service.py`

**Interfaces:**
- Consumes: app configuration, repository state, and `JobAlertService`.
- Produces: deterministic scheduling and operator commands `init-db`, `audit`, `collect`, `baseline`, `run`, `report`, `notify-test`, and `retry-notifications`.

- [ ] **Step 1: Write failing due-time and jitter tests**

```python
# tests/unit/test_scheduler.py
from datetime import UTC, datetime, timedelta

from job_alert.scheduler import deterministic_jitter, next_due_at


def test_jitter_is_stable_and_bounded() -> None:
    assert deterministic_jitter("openai-greenhouse") == deterministic_jitter(
        "openai-greenhouse"
    )
    assert 0 <= deterministic_jitter("openai-greenhouse").total_seconds() <= 60


def test_next_due_uses_last_complete_plus_interval_and_jitter() -> None:
    last = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    due = next_due_at("source", last, interval_minutes=5)
    assert last + timedelta(minutes=5) <= due <= last + timedelta(minutes=6)
```

- [ ] **Step 2: Run scheduler and CLI tests to verify failure**

Run: `uv run pytest tests/unit/test_scheduler.py tests/integration/test_cli.py -v`

Expected: FAIL because scheduler and CLI modules do not exist.

- [ ] **Step 3: Implement deterministic jitter and concurrency**

Derive jitter from the first eight hex characters of SHA-256(source ID), modulo 61 seconds. The scheduler wakes every 30 seconds, selects enabled due sources not under backoff, and dispatches them with a global concurrency of eight plus a per-host semaphore of two. It waits for an in-flight source before dispatching the same source again. Graceful shutdown stops new work, waits up to 30 seconds for current collections, and closes HTTP/database resources.

Schedule one daily digest at 18:00 America/Chicago and one lightweight health-heartbeat evaluation every five minutes. The heartbeat remains silent when every enabled source is within its freshness window and no delivery is pending; it opens or recovers health incidents through Task 22's suppression policy.

- [ ] **Step 4: Implement the complete CLI surface**

```text
job-alert init-db --db data/job-alert.sqlite3
job-alert collect --source SOURCE_ID --mode dry-run --json
job-alert collect --all --mode live-suppressed --report reports/cycle.json
job-alert baseline --all --confirm-seed
job-alert run --config config --db data/job-alert.sqlite3
job-alert report coverage --markdown reports/coverage.md
job-alert report health --json
job-alert notify-test --provider pushover
job-alert retry-notifications --limit 100
```

`baseline --all` refuses to run without `--confirm-seed`. `run` refuses LIVE mode while `all-jobs-dry-run` is the only enabled filter or while broad activation is requested below the 90% direct-coverage threshold. `notify-test` labels the message `TEST — Fast Job Alert` and never uses a real job event.

- [ ] **Step 5: Test exit codes and safety gates**

Use Typer's `CliRunner`. Assert exit code 2 for missing confirmation, invalid source, invalid config, missing Pushover secrets, or broad activation below threshold. Assert JSON commands emit parseable JSON to stdout while logs go to stderr.

- [ ] **Step 6: Run the CLI suite and commit**

Run: `uv run pytest tests/unit/test_scheduler.py tests/integration/test_cli.py -v && uv run job-alert --help`.

```bash
git add src/job_alert/scheduler.py src/job_alert/cli.py src/job_alert/service.py tests/unit/test_scheduler.py tests/integration/test_cli.py
git commit -m "feat: add scheduler and operator CLI"
```

### Task 24: Full Offline Verification and Company-Wide Dry Run

**Files:**
- Create: `tests/integration/test_all_configured_sources.py`
- Create: `tests/integration/test_restart_and_delivery.py`
- Create: `reports/.gitkeep`
- Create: `docs/collection-verification.md`
- Modify: `.gitignore`, `pyproject.toml`, `uv.lock`
- Modify: `config/sources.yaml`, `src/job_alert/config.py`, `src/job_alert/cli.py` (interval tiers)
- Modify: `src/job_alert/service.py`, `src/job_alert/adapters/base.py`, `src/job_alert/audit.py` (report metrics)

> **Revision 2026-10-14 (plan verification before execution).** Checked against the code at `9fef095`:
> 1. `docs/source-audit.md` is regenerated in full by `audit report`/`report coverage` (see
>    `docs/enterprise-adapter-gates.md`), so Step 7 evidence goes in `docs/collection-verification.md`.
> 2. Every enabled source polled every 30 minutes (the `audit set-source` default), contradicting the
>    5/10/15/30 tiers in Global Constraints and the spec's 6/11/16-minute objectives. Step 0 fixes this
>    before any baseline is recorded.
> 3. The cycle report had none of the Step 4 metrics; Step 4a adds them.
> 4. A dry run is stored with outcome `partial` (`error_code: dry_run`) by design so it can never become
>    the absence snapshot. Step 4 therefore checks `complete: true`, not `outcome: complete`.
> 5. `test_service_cycle.py` already covers a failed delivery retried after one restart. Step 2 covers
>    only what it lacks: a restart between baseline and the new posting, a retry through the
>    `retry-notifications` path (`deliver_pending`), and a final cycle proving nothing is re-sent.
> 6. Only 10 sources are enabled; `automotive-ev-autonomous-mobility` has none, and three categories have
>    one each, so Step 7 cannot sample two per category. It samples what exists and records the gap.
> 7. `reports/` was ignored wholesale, so `reports/.gitkeep` could not be committed; the ignore rule is
>    narrowed. Adding `pytest-cov` changes `uv.lock`, which the Step 8 commit now includes.
>
> Found while executing Step 6 (evidence in `docs/collection-verification.md`):
> 8. `bosch-smartrecruiters` intermittently failed as postings were published mid-walk on a newest-first
>    list. The adapter now re-reads the head page, then re-walks once, before failing closed.
> 9. A source with no applied complete snapshot (a failed baseline, or a source enabled later) recorded
>    its whole board as `new` events in LIVE/LIVE_SUPPRESSED. Its first complete snapshot now seeds
>    silently (`seeded: true`). Task 25's runbook and Task 26's canary rely on this: enabling a source
>    never announces the postings it already had.

- [ ] **Step 0: Enforce the polling tiers before any baseline**

Map source class to its tier interval (`documented_api` 5, `public_ats` 10, `company_json` 15,
`html`/`fallback` 30). The configuration loader rejects an enabled source polled *faster* than its tier
(slower stays legal so an operator can back off a `429`). `audit set-source` defaults `--interval` to the
tier of `--class`. A test pins every enabled committed source to exactly its tier, and
`config/sources.yaml` is corrected to match.

- [ ] **Step 1: Add an offline registry-to-adapter completeness test**

```python
def test_every_enabled_source_has_registered_adapter(app_config) -> None:
    registry = AdapterRegistry.default()
    missing = sorted(
        source.id
        for source in app_config.sources
        if source.enabled and not registry.supports(source.provider)
    )
    assert missing == []
```

- [ ] **Step 2: Add restart and exactly-once delivery tests**

Use a temporary on-disk SQLite database. Baseline two jobs, close the service, reopen it, present a third job, force one transient notification failure, restart again, retry through `deliver_pending` (the `retry-notifications` path, which fetches nothing), and assert exactly one successful delivery for the third job and none for the baseline jobs. Restart once more and run another live cycle to prove nothing is re-sent.

- [ ] **Step 3: Run the full offline quality gate**

Run:

```bash
uv run ruff check .
uv run mypy src
uv run pytest -m "not live" --cov=job_alert --cov-report=term-missing
git diff --check
```

Add `pytest-cov>=6,<7` to the dev dependencies before this command. Expected: all checks pass and coverage is at least 90% for `identity.py`, `diff.py`, `config.py`, `filters.py`, and every enabled adapter.

- [ ] **Step 4a: Add collection metrics to every cycle report (test first)**

`CycleResult` and the `collect`/`baseline` JSON gain `duration_seconds` (monotonic), `page_count`, `declared_total`, `parsed_total`, `stable_id_percent`, and `direct_url_percent`. The two rates use the audit's existing definition, moved next to `validate_collection` so the audit and the service share one function. A failed cycle reports whatever it knows and `null` for the rest.

- [ ] **Step 4: Run one dry collection against every enabled live source**

Run: `uv run job-alert collect --all --mode dry-run --db data/verification.sqlite3 --report reports/all-sources-dry-run.json`.

Expected: every enabled source reports `complete: true` (its stored outcome is `partial` with `error_code: dry_run`, by design); blocked and manual-review sources are excluded but remain visible in coverage. The report includes HTTP duration, pages, declared total, parsed total, stable-ID rate, direct-URL rate, and warnings.

The verification database is local evidence only. The deployed container seeds its own baseline through the runbook in Task 25.

- [ ] **Step 5: Resolve every dry-run discrepancy before baselining**

For any incomplete source, either fix its adapter with a failing fixture test first or change its audited status to `BLOCKED`, `FALLBACK_ONLY`, or `NEEDS_MANUAL_REVIEW` with evidence. Do not waive total mismatches, repeated IDs, missing application URLs, or challenge pages.

- [ ] **Step 6: Seed the all-company baseline and immediately repeat**

Run:

```bash
uv run job-alert baseline --all --confirm-seed --db data/verification.sqlite3 --report reports/baseline.json
uv run job-alert collect --all --mode live-suppressed --db data/verification.sqlite3 --report reports/post-baseline.json
```

Expected: the baseline records jobs and sends zero alerts; the immediate second run produces zero new-job events unless a posting genuinely appeared between runs. Any real new event remains suppressed but is listed for manual verification.

- [ ] **Step 7: Manually verify a stratified sample**

Compare at least two companies from every category (or every enabled company where a category has fewer than two, and record the category's gap explicitly) and at least one company from every enabled adapter family against its official career page. Record official visible job count, collected count, sample matching IDs/titles, pagination result, and timestamp in `docs/collection-verification.md`. A discrepancy blocks that source from live notification.

An agent performing this step uses an independent ordinary-HTTP read of the human-facing board, not the adapter's endpoint, and labels it as such. A human visual check of the same companies remains an operator item for Task 26.

- [ ] **Step 8: Commit the verification harness and nonvolatile evidence**

Do not commit runtime reports containing volatile job inventories. Commit test code, `.gitkeep`, and summarized audit evidence only.

```bash
git add .gitignore pyproject.toml uv.lock config/sources.yaml src tests reports/.gitkeep docs/collection-verification.md docs/superpowers/plans
git commit -m "test: verify all-company collection workflow"
```

### Task 25: Container Deployment, Persistent State, Backup, and Runbook

**Files:**
- Create: `deploy/Dockerfile`
- Create: `deploy/compose.yaml`
- Create: `deploy/job-alert.env.example`
- Create: `deploy/healthcheck.py`
- Create: `scripts/backup_sqlite.py` (replaces `scripts/backup_sqlite.sh`; see revision note)
- Create: `docs/operator-runbook.md`
- Create: `README.md`
- Create: `tests/integration/test_deployment_files.py`

**Interfaces:**
- Consumes: the `job-alert run` command and environment secrets.
- Produces: reproducible always-on deployment with persistent SQLite, health checks, backups, and recovery instructions.

> **Revision 2026-10-14 (plan verification before execution).** Checked against the code at `196c0ff`:
> 1. `run` writes the daily digest to `reports/daily-digest.md` relative to `/app`, which is read-only in
>    the container, and the write is not caught. The command passes
>    `--digest-report /app/data/reports/daily-digest.md`.
> 2. `uv sync --frozen` before `COPY src` cannot install the project. The image syncs dependencies with
>    `--no-install-project`, copies the source, then syncs again with `--no-editable`.
> 3. `python:3.12-slim` has no `sqlite3` CLI, and a named volume is not reachable from a macOS host, so
>    the backup is `scripts/backup_sqlite.py` (the sqlite3 backup API) with `backup` and `restore`
>    subcommands. It runs inside the container and on any host with Python. The shell script below is
>    replaced by it, with the same argument checks, integrity check, and 14-file retention.
> 4. `run` defaults to `live-suppressed`, and LIVE is refused without Pushover secrets anyway, so
>    Compose takes the mode from `JOB_ALERT_MODE` (default `live-suppressed`) and passes the secrets
>    through without `:?required`. Otherwise `docker compose config` fails before any credential exists.
> 5. `config/` is bind-mounted read-only so an operator can disable a source without rebuilding the image.
>
> Found by the Step 6 smoke test (image built, run read-only as `jobalert`):
> 6. The schema lived at the repository root and was resolved relative to the source tree, so the
>    installed wheel could not create a database. It now ships inside the package.
> 7. The audit's atomic writes left `config/sources.yaml` mode 0600 (`mkstemp`), unreadable by the
>    container user. Writes now get the mode an ordinary write would.
> 8. The health check raised a traceback on a database without the schema; it now exits with a reason.
>
> **Smoke test incomplete (2026-10-14):** `compose config`, `docker build`, `--help`, and the non-root
> user check passed. The host disk then filled (about 120 MB free) and the Docker daemon failed with
> I/O errors, so `init-db`, `run --once`, the health check, and the backup were not re-run inside the
> container after fixes 6-8. They are covered by host tests. Re-run Step 6 once the disk has room.

> **Open decision for Task 26 (not changed here).** `_require_live_activation` refuses LIVE whenever
> direct coverage is below 90%, but the spec gates only *broad* activation. With 10 of 148 companies
> covered, Task 26's five-company canary cannot run until "broad" is defined, for example as any
> notifying filter without an `include_companies` allowlist.

- [ ] **Step 1: Write failing deployment-policy tests**

```python
def test_compose_mounts_persistent_data_and_does_not_embed_secrets() -> None:
    compose = Path("deploy/compose.yaml").read_text()
    assert "job-alert-data:/app/data" in compose
    assert "PUSHOVER_APP_TOKEN:" in compose
    assert "PUSHOVER_USER_KEY:" in compose
    assert "secret-token" not in compose


def test_example_env_contains_names_but_no_values() -> None:
    env = Path("deploy/job-alert.env.example").read_text()
    assert "PUSHOVER_APP_TOKEN=" in env
    assert "PUSHOVER_USER_KEY=" in env
```

- [ ] **Step 2: Create a non-root production image**

Use `python:3.12-slim`, install the pinned uv release, sync from `uv.lock` with `--frozen --no-dev`, copy the package/config/migration files, create an unprivileged `jobalert` user, expose no network port, and set the command to `job-alert run --config /app/config --db /app/data/job-alert.sqlite3`. The image health check runs `deploy/healthcheck.py` against source-state freshness.

```dockerfile
# deploy/Dockerfile
FROM ghcr.io/astral-sh/uv:0.11.3 AS uv
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/app/.venv/bin:$PATH
RUN groupadd --system jobalert && useradd --system --gid jobalert --home /app jobalert
WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY src ./src
COPY config ./config
COPY migrations ./migrations
COPY deploy/healthcheck.py ./deploy/healthcheck.py
RUN mkdir -p /app/data && chown -R jobalert:jobalert /app
USER jobalert
HEALTHCHECK --interval=5m --timeout=10s --retries=2 CMD ["python", "/app/deploy/healthcheck.py"]
CMD ["job-alert", "run", "--config", "/app/config", "--db", "/app/data/job-alert.sqlite3"]
```

- [ ] **Step 3: Create Compose deployment with explicit secrets and volume**

The Compose service uses `restart: unless-stopped`, read-only application filesystem, writable named volume at `/app/data`, temporary filesystem at `/tmp`, environment expansion for `PUSHOVER_APP_TOKEN` and `PUSHOVER_USER_KEY`, UTC internal time, and `America/Chicago` as the reporting timezone. Do not publish ports. Set log rotation to 10 MiB and three files.

```yaml
# deploy/compose.yaml
services:
  job-alert:
    build:
      context: ..
      dockerfile: deploy/Dockerfile
    image: fast-company-job-alert:local
    restart: unless-stopped
    read_only: true
    tmpfs:
      - /tmp:size=64m,mode=1777
    volumes:
      - job-alert-data:/app/data
    environment:
      PUSHOVER_APP_TOKEN: ${PUSHOVER_APP_TOKEN:?required}
      PUSHOVER_USER_KEY: ${PUSHOVER_USER_KEY:?required}
      REPORT_TIMEZONE: America/Chicago
      TZ: UTC
      JOB_ALERT_HEALTH_MAX_AGE_MINUTES: "65"
    logging:
      driver: json-file
      options:
        max-size: 10m
        max-file: "3"

volumes:
  job-alert-data:
```

```dotenv
# deploy/job-alert.env.example
PUSHOVER_APP_TOKEN=
PUSHOVER_USER_KEY=
```

```python
# deploy/healthcheck.py
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

database = Path(os.getenv("JOB_ALERT_DB", "/app/data/job-alert.sqlite3"))
maximum_age = timedelta(
    minutes=int(os.getenv("JOB_ALERT_HEALTH_MAX_AGE_MINUTES", "65"))
)
if not database.is_file():
    raise SystemExit("database missing")
with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
    row = connection.execute(
        "SELECT MAX(last_complete_at) FROM source_state WHERE last_complete_at IS NOT NULL"
    ).fetchone()
if row is None or row[0] is None:
    raise SystemExit("no complete source run")
last_complete = datetime.fromisoformat(row[0]).astimezone(UTC)
if datetime.now(UTC) - last_complete > maximum_age:
    raise SystemExit("source state is stale")
sys.exit(0)
```

- [ ] **Step 4: Implement consistent SQLite backup**

`scripts/backup_sqlite.py backup` accepts the database and backup directory as positional arguments, refuses empty or root paths, creates the destination, copies through the sqlite3 backup API, verifies `PRAGMA integrity_check` returns `ok`, and retains the newest 14 daily backups. The runbook schedules it daily and documents restoration into a stopped container.

```sh
#!/bin/sh
set -eu
database=${1:-}
backup_directory=${2:-}
if [ -z "$database" ] || [ -z "$backup_directory" ] || [ "$backup_directory" = "/" ]; then
  echo "usage: backup_sqlite.sh DATABASE BACKUP_DIRECTORY" >&2
  exit 2
fi
if [ ! -f "$database" ]; then
  echo "database does not exist: $database" >&2
  exit 2
fi
mkdir -p "$backup_directory"
target="$backup_directory/job-alert-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
sqlite3 "$database" ".backup '$target'"
result=$(sqlite3 "$target" "PRAGMA integrity_check;")
if [ "$result" != "ok" ]; then
  echo "backup integrity check failed" >&2
  exit 1
fi
ls -1t "$backup_directory"/job-alert-*.sqlite3 | tail -n +15 | while IFS= read -r old; do
  rm -- "$old"
done
echo "$target"
```

- [ ] **Step 5: Write the operator runbook**

Document exact commands for initial deployment, migration, baseline, Pushover test, live enablement, logs, coverage/health reports, database backup, database restore, rotating Pushover credentials, disabling one broken source, handling `429`, handling a schema change, and rolling back the container image while retaining the database.

- [ ] **Step 6: Build and smoke-test deployment**

Run:

```bash
docker compose -f deploy/compose.yaml config
docker build -f deploy/Dockerfile -t fast-job-alert:test .
docker run --rm --read-only -v job-alert-smoke:/app/data fast-job-alert:test job-alert init-db --db /app/data/job-alert.sqlite3
docker run --rm fast-job-alert:test job-alert --help
uv run pytest tests/integration/test_deployment_files.py -v
```

Expected: Compose validates, image builds, CLI help exits 0, and deployment tests pass.

- [ ] **Step 7: Commit deployment assets**

```bash
git add deploy scripts/backup_sqlite.py docs/operator-runbook.md README.md tests/integration/test_deployment_files.py docs/superpowers/plans
git commit -m "ops: add persistent job alert deployment"
```

### Task 26: Laptop (macOS) Delivery, Canary Rollout, and Production Acceptance

> **Revision 2026-10-14.** The user asked to ship laptop notifications first and implement mobile
> alerts afterwards. The original Android/Pushover version of this task moved, unchanged in intent,
> to Task 28. A container cannot post macOS notifications, so the laptop release runs natively as a
> launchd LaunchAgent; the Task 25 container remains the path for the later always-on/mobile phase.
> The user delegated the open activation decision: the 90% coverage gate applies to *broad*
> activation only, meaning any enabled notifying filter without an `include_companies` allowlist.

**Files:**
- Create: `src/job_alert/notifications/macos.py`, `tests/unit/notifications/test_macos.py`
- Modify: `src/job_alert/notifications/__init__.py`, `src/job_alert/cli.py`, `tests/integration/test_cli.py`
- Create: `scripts/macos_launch_agent.py`, `tests/integration/test_macos_launch_agent.py`
- Modify: `config/filters.yaml`, `docs/operator-runbook.md`, `README.md`
- Create: `docs/production-acceptance.md`

**Interfaces:**
- Consumes: audited sources, a seeded local database, user-approved filters, and macOS Notification Center.
- Produces: verified laptop delivery and a controlled progression from suppressed mode to monitoring.

- [ ] **Step 1: Implement the macOS notifier (test first)**

`MacOSNotifier` (`provider = "macos"`) runs `terminal-notifier` when it is on `PATH` (clicking the
alert opens the posting) and otherwise `osascript` (the banner shows the posting link as its subtitle;
clicking cannot open it). It never uses a shell: titles and bodies are untrusted provider text, so the
AppleScript receives them as `argv` items, never interpolated into the script. Exit 0 is delivered;
a non-zero exit is retryable; a timeout is retryable and `uncertain`; a non-macOS host is a
configuration error. `HIGH` priority plays the default sound.

- [ ] **Step 2: Make `macos` the default provider and narrow the coverage gate**

`collect`, `run`, `notify-test`, `retry-notifications`, and `report health` default to
`--provider macos`; `pushover` and `ntfy` remain selectable. `_require_live_activation` still refuses
LIVE without an enabled notifying filter, applies the 90% gate only when an enabled notifying filter
is broad, and refuses an allowlist naming a company that has no enabled source (it could never alert).

- [ ] **Step 3: Install as a LaunchAgent**

`scripts/macos_launch_agent.py install|status|uninstall|print` renders
`~/Library/LaunchAgents/com.fastjobalert.monitor.plist` with `RunAtLoad`, `KeepAlive`, and a
five-minute restart throttle, and loads it with `launchctl bootstrap gui/<uid>`. *Found on first
install:* macOS privacy protection (TCC) blocks a launchd agent from executing or reading anything
under `~/Documents`, where the checkout lives. So `install` stages a runtime in
`~/Library/Application Support/FastJobAlert`: a venv built from `uv.lock` with the package installed
non-editable, a copy of `config/`, the database, reports, and logs. A database already seeded in the
checkout is moved in once. Granting Full Disk Access to a shell or interpreter was rejected as too
broad. The mode is an argument
(default `live-suppressed`). Tests cover the rendered plist only; they never call `launchctl`.

- [ ] **Step 4: Send the explicit test notification**

Run `uv run job-alert notify-test --provider macos`. Expected: `TEST — Fast Job Alert` appears in
Notification Center. If it does not, allow notifications for the delivering app (Script Editor for
`osascript`, terminal-notifier otherwise) in System Settings › Notifications. Record the result in
`docs/production-acceptance.md`.

- [ ] **Step 5: Obtain and encode the user's production filter choices**

Before changing `notify`, obtain explicit values for title keywords, excluded keywords, locations,
remote/hybrid preferences, experience level, and priority companies. Enter them in
`production-matches` with an `include_companies` allowlist of up to five canary companies covering at
least two adapter families, and keep `notify: false` for the first canary collection. Validate with
`uv run job-alert collect --all --mode live-suppressed --report reports/filter-canary.json` and review
every would-notify event.

- [ ] **Step 6: Run the canary on the laptop**

Baseline (already seeded sources stay seeded), set `notify: true`, install the LaunchAgent with
`--mode live`, and run for 24 hours of laptop uptime. Acceptance: no duplicate notifications, no
baseline notifications, links open the posting, no unresolved source-health incident. A sleeping
laptop does not poll; cycles resume on wake and the first cycle after wake detects what appeared.

- [ ] **Step 7: Expand with checkpoints**

Widen the allowlist one category at a time in this order: Robotics/Autonomy, Big Tech/AI,
Automotive/Mobility, Industrial Automation, Aerospace/Defense, Medical Robotics. Removing the
allowlist entirely is broad activation and stays refused until direct coverage reaches 90%. Stop on
any duplicate, missing link, mass-drop quarantine, or repeated source error.

- [ ] **Step 8: Verify freshness and record acceptance**

Report detection delay by tier (sample count, median, p95, maximum) from `first_seen_at` versus
delivery time, noting that laptop sleep is excluded from the objectives. Record evidence for every
spec criterion that applies to laptop delivery in `docs/production-acceptance.md`; Android-specific
criteria are carried to Task 28. Run the full quality gate and live probes, verify `config/filters.yaml`
holds no personal data or secrets, and commit:

```bash
git add config/filters.yaml docs/operator-runbook.md docs/production-acceptance.md
git commit -m "ops: approve laptop job alert rollout"
```

### Task 27: Secondary Coverage Cross-Checks Without Duplicate Alerts

**Files:**
- Create: `config/secondary_sources.yaml`
- Create: `src/job_alert/coverage_crosscheck.py`
- Create: `tests/unit/test_coverage_crosscheck.py`
- Modify: `src/job_alert/scheduler.py`
- Modify: `docs/operator-runbook.md`

**Interfaces:**
- Consumes: audited official sitemap, RSS/Atom, JSON-LD, or separately approved documented aggregator sources and the primary job store.
- Produces: `CoverageGap` records and a slow coverage report; never creates user-facing new-job events.

- [ ] **Step 1: Write failing gap-confirmation tests**

```python
# tests/unit/test_coverage_crosscheck.py
def test_secondary_job_requires_two_observations_before_gap() -> None:
    first = compare_secondary(
        primary_urls=set(),
        secondary_urls={"https://example.com/jobs/123"},
        prior_counts={},
    )
    assert first.gaps == []
    second = compare_secondary(
        primary_urls=set(),
        secondary_urls={"https://example.com/jobs/123"},
        prior_counts=first.counts,
    )
    assert [gap.url for gap in second.gaps] == ["https://example.com/jobs/123"]


def test_secondary_source_never_creates_job_notification() -> None:
    result = build_crosscheck_events([confirmed_gap])
    assert all(event.kind == "coverage_gap" for event in result)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/unit/test_coverage_crosscheck.py -v`

Expected: FAIL because `job_alert.coverage_crosscheck` does not exist.

- [ ] **Step 3: Configure only audited secondary sources**

`secondary_sources.yaml` uses the same company IDs and records source type, endpoint, interval minutes constrained to 60–240, and evidence date. Seed only official sitemaps, RSS/Atom feeds, or JSON-LD pages discovered during Task 11. Leave companies without a legitimate secondary source absent rather than adding Google search URLs. A documented paid aggregator can be added only after its API terms, credentials, rate limits, and delay are recorded in `docs/source-audit.md`.

- [ ] **Step 4: Implement URL-level coverage comparison**

Canonicalize secondary URLs with Task 4's function. Match primary records by canonical posting/apply URL and provider/requisition metadata when available. A secondary-only URL must appear in two consecutive complete secondary checks before becoming a `coverage_gap`. Gaps enter the health/daily digest with company, URL, first observed, confirmations, and secondary source; they cannot enter `job_events` or notification delivery.

- [ ] **Step 5: Schedule slow checks and document triage**

Run secondary checks every 1–4 hours based on configuration. The operator runbook requires inspecting a confirmed gap, correcting the primary source or audit classification, and closing the gap after two complete checks agree. It explicitly prohibits turning a secondary gap into a direct job alert without first adding a verified primary/fallback adapter.

- [ ] **Step 6: Run tests and commit**

Run: `uv run pytest tests/unit/test_coverage_crosscheck.py tests/unit/test_scheduler.py -v && uv run mypy src`.

```bash
git add config/secondary_sources.yaml src/job_alert/coverage_crosscheck.py src/job_alert/scheduler.py tests/unit/test_coverage_crosscheck.py docs/operator-runbook.md
git commit -m "feat: add secondary source coverage checks"
```

### Task 28: Android (Pushover) Delivery — Deferred

Deferred on 2026-10-14 at the user's request until laptop delivery is accepted. This is the original
Task 26: configure Pushover on Android without committing credentials, send `notify-test --provider
pushover`, run the canary and category expansion with `--provider pushover` (natively or through the
Task 25 container with `JOB_ALERT_MODE=live`), verify Android delivery and direct links, and record the
Android acceptance criteria (delivery delay, quota headroom, secrets absent from Git history) in
`docs/production-acceptance.md`. `PushoverNotifier` and its tests already exist (Task 20).

### Task 29: Phased Company Expansion — FAANG Tier, Then Fortune 100, Then Beyond

Requested 2026-10-14: extend alerts gradually, starting with FAANG-equivalent companies, then the
Fortune 100, and so on. The alert target is unchanged: internships in robotics and mechanical
engineering, currently "every internship" at each allowlisted company (see `config/filters.yaml`).

**Two lists grow, in this order for every company:** first the *source* (a verified, enabled
collection), then the *alert allowlist* (`include_companies` in both `production-matches` filters).
A company is never allowlisted before its source passes the gate below, and LIVE already refuses an
allowlist naming a company without an enabled source.

**Which filter a company joins (decided 2026-10-14):** pure robotics companies join the
`production-matches` pair (every internship). Every other expansion company joins the
`expansion-matches` pair, which alerts only on internships whose title also names robotics,
mechanical, or adjacent engineering work (`require_title_keywords` ANDs the two title lists),
because "every internship" at a retailer or bank is noise for this search.

**Per-company gate (applies in every phase):**

1. Probe the official careers site's public endpoint with ordinary HTTPS. A challenge, login, or
   `403` records the company `BLOCKED` with evidence; no evasion, no browser.
2. Capture a sanitized fixture with a provenance README, write failing adapter tests, implement.
3. Live probe twice: complete, identical keys, stable ids, direct HTTPS links, declared total met.
4. Enable the source at its class tier; the first complete snapshot seeds silently.
5. Two consecutive complete scheduler cycles, then add the company to the allowlist and reinstall
   the LaunchAgent. Record it in `docs/collection-verification.md` (expansion log).

**Scoped sources.** Some boards cannot be enumerated whole: amazon.jobs caps `hits` at 10,000 and
Workday CXS reports at most `total=2000`; others would cost 50+ requests a cycle. Such a source may
be scoped to a provider-side query (for example `intern`). The scope lives in the source's
`parameters`, is named in the audit evidence, and completeness is judged against the *scoped*
declared total. This relaxes the Global Constraint "ingest all jobs" for those sources only, and
only because the full board is unreachable or impolite; filters still run after ingestion.

**Phase 1 — FAANG tier** (probed 2026-10-14):

| Company | Public endpoint found | Plan |
| --- | --- | --- |
| Amazon | `amazon.jobs/en/search.json`, JSON, capped at 10,000 hits; `base_query=intern` gives 428 in 5 pages | `amazon` adapter, scoped, `company_json` tier |
| NVIDIA | Workday CXS (`POST …/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs`), `externalPath` per posting | `workday` adapter (Task 13 gate now passes), scoped |
| Apple | `jobs.apple.com/en-us/search` server-rendered HTML | investigate embedded data; adapter only if complete and paginable |
| Alphabet/Google | `google.com/about/careers/applications/jobs/results` server-rendered (`AF_initDataCallback`) | investigate as Apple |
| Meta | `metacareers.com/jobs` redirects to a client-rendered search | probe; likely `BLOCKED`/`FALLBACK_ONLY` |
| Microsoft | `careers.microsoft.com` → challenged Eightfold app; legacy API host certificate mismatch | probe; likely stays `FALLBACK_ONLY` |
| Netflix | Eightfold, access challenge at audit | stays `BLOCKED` unless a public endpoint answers |

**Phase 2 — Workday unlock of the existing registry:** the `workday` adapter from Phase 1 is applied to
the eight audit-evidenced Workday candidates (Boston Dynamics, Boeing, Stryker, Rockwell Automation,
Intel, Samsung, CrowdStrike, Zebra), robotics and aerospace first.

**Phase 3 — Fortune 100:** add the current Fortune 100 to `config/company_targets.yaml` under a
`fortune-100` category (companies already present just gain the category), run `job-alert audit run`
for them, and enable every company whose provider has an adapter. Order: engineering-heavy employers
first (aerospace/defense, automotive, industrial, semiconductors), finance/retail/health last. New
enterprise adapters (SuccessFactors, Oracle, iCIMS, Eightfold) are built only when at least two
Fortune 100 companies need the same one (the Task 13-17 gates).

**Phase 4 — Fortune 500 and beyond:** repeat Phase 3 in rank order, in batches of about 25.

**Status is tracked** in the expansion log in `docs/collection-verification.md`; this task's
checkboxes are per phase:

- [ ] Phase 1: FAANG tier — sources done 2026-10-14 for Amazon, NVIDIA, Apple, Google, Netflix (allowlisting waits for two clean cycles); Microsoft deferred (429 rate limit, needs a paced Eightfold PCSX source); Meta has no public endpoint
- [ ] Phase 2: Workday registry unlock — 7 of 8 sources done 2026-10-14 (Boston Dynamics, Rockwell, Zebra, Boeing, Intel, Samsung, CrowdStrike); Stryker blocked by a stub posting; allowlisting waits for two clean cycles
- [ ] Phase 3: Fortune 100 — inventory and audit done 2026-10-14; 8 sources enabled (Galaxy Digital, Target, Nike, Coca-Cola, Elevance, Bank of America, Comcast, Allstate); next: Eightfold endpoints for 5 companies, then a SuccessFactors adapter (3 here + 7 earlier candidates), Oracle (3 + 2), iCIMS (2 + 2)
- [ ] Phase 4: Fortune 500 batches

## Execution Checkpoints

1. **Foundation checkpoint — Tasks 1–6:** Review model boundaries, SQLite safety, identity, diff semantics, HTTP controls, and adapter contract before any broad internet probing.
2. **Documented API checkpoint — Tasks 7–10:** Confirm fixtures and one live representative per documented provider.
3. **Source-audit checkpoint — Tasks 11–12:** Review the exact company coverage matrix, unresolved companies, access limitations, and the 90% gate before enterprise adapter work.
4. **Enterprise adapter checkpoint — Tasks 13–18:** Accept each adapter only with live public evidence and complete pagination; explicitly retain degraded statuses elsewhere.
5. **Event pipeline checkpoint — Tasks 19–23:** Verify filters, durable notification events, baseline behavior, quarantine rules, health suppression, scheduler, and safety gates.
6. **All-company checkpoint — Task 24:** Require a clean baseline and immediate repeat before any real job alert is delivered.
7. **Deployment checkpoint — Task 25:** Verify persistence, backup/restore, non-root container, and secret handling.
8. **Laptop and production checkpoint — Task 26:** Require the explicit test notification, five-company canary, category-by-category rollout, and written acceptance evidence. Android delivery follows in Task 28.
9. **Coverage cross-check checkpoint — Task 27:** Add only legitimate audited secondary sources, confirm gaps twice, and prove they cannot create duplicate job notifications.
10. **Expansion checkpoints — Task 29:** one per phase; each company passes the per-company gate before it is allowlisted.

## Completion Definition

Implementation is complete only when Task 26's production acceptance document contains evidence for every acceptance criterion in the specification and Task 27's secondary checks cannot create job notifications. A working prototype against one provider, a successful Android test message, or a partial company registry does not constitute completion.
