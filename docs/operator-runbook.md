# Operator runbook

Every command runs from the repository root on the deployment host. `dc` below is shorthand for:

```sh
alias dc='docker compose -f deploy/compose.yaml --env-file deploy/job-alert.env'
```

Inside the container the state database is `/app/data/job-alert.sqlite3` on the `job-alert-data`
volume, the registry is the repository's `config/` mounted read-only at `/app/config`, and the root
filesystem is read-only. The process runs as the unprivileged `jobalert` user and publishes no port.

## Initial deployment

1. Create the secret file, which is ignored by git, and keep it readable only by you:

   ```sh
   cp deploy/job-alert.env.example deploy/job-alert.env
   chmod 600 deploy/job-alert.env
   ```

   Leave `JOB_ALERT_MODE=live-suppressed` until [live enablement](#live-enablement). The Pushover
   values may stay empty until then.
2. Build the image and create the schema. The schema is idempotent, and every command that opens the
   database applies it; `init-db` only does it explicitly.

   ```sh
   dc build
   dc run --rm job-alert job-alert init-db --db /app/data/job-alert.sqlite3
   ```

3. Seed the baseline before the loop starts, and check that it sent nothing:

   ```sh
   dc run --rm job-alert job-alert baseline --all --confirm-seed \
     --config /app/config --db /app/data/job-alert.sqlite3 \
     --report /app/data/reports/baseline.json
   ```

   Every enabled source should be `complete`. A source that is not stays unseeded. That is safe:
   its first complete snapshot in any mode seeds it without events (reported as `seeded`), so a
   source is never able to announce the postings it already had. That also holds for a source
   enabled later.
4. Start the loop and watch the first wake:

   ```sh
   dc up -d
   dc logs -f job-alert
   ```

The container is healthy once some source has completed within 65 minutes (`deploy/healthcheck.py`,
15-minute start period). `docker inspect --format '{{.State.Health.Status}}' <container>` shows it.

## Pushover test

Fill `PUSHOVER_APP_TOKEN` and `PUSHOVER_USER_KEY` in `deploy/job-alert.env`, then:

```sh
dc run --rm job-alert job-alert notify-test --provider pushover
```

The phone receives `TEST — Fast Job Alert`. No job, event, or delivery record is read or written.
Exit code 2 means a credential is missing; 1 means Pushover refused the message.

## Live enablement

LIVE is refused (exit 2) unless an enabled filter in `config/filters.yaml` has `notify: true`, the
Pushover credentials are present, and direct coverage passes the 90% broad-activation gate. As of
2026-10-14, coverage is 10 of 148 companies, so LIVE is refused. See
`docs/collection-verification.md`, and the open decision on canary activation in Task 26 of the plan.

Once the gates pass:

```sh
# deploy/job-alert.env: JOB_ALERT_MODE=live
dc up -d            # recreates the container with the new mode
dc logs -f job-alert
```

To stop alerting without stopping collection, set `JOB_ALERT_MODE=live-suppressed` and run
`dc up -d` again. Events recorded while suppressed stay pending, and LIVE delivers them later, so
review `would_deliver` in a suppressed report before switching.

## Logs and reports

```sh
dc logs --since 1h job-alert
dc exec job-alert job-alert report health --config /app/config --db /app/data/job-alert.sqlite3
dc exec job-alert job-alert report health --json --config /app/config --db /app/data/job-alert.sqlite3
uv run job-alert report coverage --markdown reports/coverage.md   # on the host, from the registry
dc exec job-alert cat /app/data/reports/daily-digest.md            # written daily at 18:00 Chicago
```

Logs rotate at 10 MiB, three files.

## Backup

`scripts/backup_sqlite.py` copies the database through the SQLite online-backup API, which is safe
while the loop writes. It verifies the copy with `PRAGMA integrity_check` and keeps the newest 14
backups. A backup inside the volume does not survive losing the volume, so copy it out:

```sh
dc exec -T job-alert python /app/scripts/backup_sqlite.py backup \
  /app/data/job-alert.sqlite3 /app/data/backups
mkdir -p backups && dc cp job-alert:/app/data/backups/. ./backups
```

Schedule it daily from the host's crontab, for example at 03:00 (replace the path):

```cron
0 3 * * * cd /srv/job-alert && docker compose -f deploy/compose.yaml --env-file deploy/job-alert.env exec -T job-alert python /app/scripts/backup_sqlite.py backup /app/data/job-alert.sqlite3 /app/data/backups && docker compose -f deploy/compose.yaml --env-file deploy/job-alert.env cp job-alert:/app/data/backups/. ./backups
```

## Restore

A restore replaces every job, event, and delivery record, so stop the loop first. The script refuses a
backup that fails its integrity check, and leaves the database untouched when it does.

```sh
dc stop job-alert
dc run --rm --no-deps -v "$PWD/backups:/restore:ro" job-alert \
  python /app/scripts/backup_sqlite.py restore /restore/job-alert-YYYYMMDDTHHMMSSZ.sqlite3 \
  /app/data/job-alert.sqlite3
dc up -d
```

The restored database knows nothing that happened after the backup. A posting first seen after the
backup is new to it again, so the next cycle announces it once if a filter notifies on it, even if it
was already announced before the restore. Postings announced before the backup keep their delivery
records and are not repeated.

## Rotating Pushover credentials

Create the new application token in Pushover, put it (and a new user key, if that changed) in
`deploy/job-alert.env`, run `dc up -d` to recreate the container, and run the [Pushover
test](#pushover-test). Then revoke the old token in Pushover. Never commit either value; `git grep`
for the token before every push if in doubt.

## Disabling one broken source

On the host, record the reason with the evidence the audit holds, then restart so the loop reloads the
registry:

```sh
uv run job-alert audit disable-source --source SOURCE_ID --status NEEDS_MANUAL_REVIEW \
  --reason "what failed, when, and the evidence"
dc restart job-alert
```

The company stays in the registry and in the coverage report. Commit the `config/sources.yaml` change.

## Handling `429 Too Many Requests`

The fetcher retries a `429`, honouring `Retry-After`. If the retries run out, the cycle fails
closed: a failed cycle never closes a job. Three consecutive failures open one health incident, which
is pushed in LIVE mode. Slow the source by raising
its `interval_minutes` in `config/sources.yaml`. Slower than its class tier is allowed; faster is
refused at load. Then run `dc restart job-alert`. `bosch-smartrecruiters` is the heaviest source: 51
requests per cycle at the 5-minute tier.

## Handling a provider schema change

A payload the adapter cannot read is an adapter error, and a challenge page is never read as an empty
board, so the cycle fails closed and opens a health incident after repeated failures. Do not loosen
validation to make the incident go away. Either:

1. capture a sanitized fixture of the new payload, write a failing adapter test, fix the adapter,
   and redeploy; or
2. [disable the source](#disabling-one-broken-source) with the evidence until that is done.

## Upgrading and rolling back the image

The database lives on the volume and survives any image change. Take a [backup](#backup) before an
upgrade, then:

```sh
git pull && dc up -d --build
```

To roll back, check out the previous release and rebuild; the volume is reused as it is:

```sh
git checkout PREVIOUS_SHA && dc up -d --build
```

The schema has a single idempotent migration. A future release that changes the schema must document
whether its database can be read by the release before it; until then, restoring the pre-upgrade
backup is the rollback path for state.
