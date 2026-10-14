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
  error_message TEXT,
  retry_after_seconds REAL,
  quota_limit INTEGER,
  quota_remaining INTEGER,
  quota_reset_at TEXT
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
  health_alert_open INTEGER NOT NULL DEFAULT 0,
  last_run_at TEXT,
  first_failure_at TEXT,
  last_error TEXT,
  health_opened_at TEXT,
  health_reason TEXT
);

-- Single-use marker: a finalized complete run may apply its absence and reopening decisions
-- once, and the primary key makes every repeat application a no-op.
CREATE TABLE IF NOT EXISTS snapshot_applications (
  run_id INTEGER PRIMARY KEY REFERENCES poll_runs(id),
  source_id TEXT NOT NULL,
  applied_at TEXT NOT NULL
);

-- A complete candidate withheld because it dropped more than half of the previous complete
-- snapshot. The stored key set is the exact board a second, independently fetched collection has
-- to reproduce before any absence or new-job transition from it may be applied. At most one
-- candidate is pending per source.
CREATE TABLE IF NOT EXISTS quarantined_snapshots (
  run_id INTEGER PRIMARY KEY REFERENCES poll_runs(id),
  source_id TEXT NOT NULL,
  quarantined_at TEXT NOT NULL,
  previous_job_count INTEGER NOT NULL,
  candidate_job_count INTEGER NOT NULL,
  candidate_keys_json TEXT NOT NULL
);

-- Columns added after the first release. A database created before them keeps its rows: the
-- repository applies only the additions a table does not already have, so this file stays the
-- single source of the DDL and initialize() stays idempotent on both fresh and existing files.
-- The retry, quota, and incident figures live here because the digest reports them.
ALTER TABLE notification_attempts ADD COLUMN retry_after_seconds REAL;
ALTER TABLE notification_attempts ADD COLUMN quota_limit INTEGER;
ALTER TABLE notification_attempts ADD COLUMN quota_remaining INTEGER;
ALTER TABLE notification_attempts ADD COLUMN quota_reset_at TEXT;
ALTER TABLE source_state ADD COLUMN last_run_at TEXT;
ALTER TABLE source_state ADD COLUMN first_failure_at TEXT;
ALTER TABLE source_state ADD COLUMN last_error TEXT;
ALTER TABLE source_state ADD COLUMN health_opened_at TEXT;
ALTER TABLE source_state ADD COLUMN health_reason TEXT;
