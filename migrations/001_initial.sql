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

-- Single-use marker: a finalized complete run may apply its absence and reopening decisions
-- once, and the primary key makes every repeat application a no-op.
CREATE TABLE IF NOT EXISTS snapshot_applications (
  run_id INTEGER PRIMARY KEY REFERENCES poll_runs(id),
  source_id TEXT NOT NULL,
  applied_at TEXT NOT NULL
);
