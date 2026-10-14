# Operator runbook

The current release delivers **laptop notifications**: the monitor runs natively on your Mac as a
LaunchAgent and posts to Notification Center. The container deployment further down is kept for the
later always-on and mobile (Pushover) phase, Task 28 of the plan.

## Laptop (macOS)

All commands run from the repository root.

1. **Install and check delivery.**

   ```sh
   uv sync
   brew install terminal-notifier   # optional: makes a click open the posting
   # if brew refuses with "untrusted tap osrf/simulation", run `brew trust osrf/simulation` first
   # (only if you trust that tap), then re-run `install` so the agent picks terminal-notifier up
   uv run job-alert notify-test     # provider macos is the default
   ```

   `TEST — Fast Job Alert` should appear. If it does not, open System Settings › Notifications and
   allow notifications for **terminal-notifier**, or for **Script Editor** if you skipped the
   install. Without terminal-notifier, a banner shows the posting link as its subtitle, but clicking it
   opens Script Editor rather than the link.
2. **Seed the baseline**, so the postings already on every board are never announced:

   ```sh
   uv run job-alert baseline --all --confirm-seed --report reports/baseline.json
   # once the agent is installed, add --db ~/Library/Application\ Support/FastJobAlert/data/job-alert.sqlite3
   ```

3. **Choose what alerts you.** Edit the `production-matches` filter in `config/filters.yaml` (title
   keywords, exclusions, locations, workplace types, priority) and give it an `include_companies`
   allowlist. Keep `notify: false` at first and review what it would send:

   ```sh
   uv run job-alert collect --all --mode live-suppressed --report reports/filter-canary.json
   ```

4. **Start the background monitor.**

   ```sh
   python3 scripts/macos_launch_agent.py install --mode live-suppressed   # collect, no alerts
   python3 scripts/macos_launch_agent.py install --mode live              # alerts on
   python3 scripts/macos_launch_agent.py status                           # state and last log lines
   python3 scripts/macos_launch_agent.py page                             # open the openings page
   python3 scripts/macos_launch_agent.py uninstall                        # stop (state is kept)
   ```

   macOS privacy protection stops background agents from reading `~/Documents`, so `install` stages
   a self-contained runtime in `~/Library/Application Support/FastJobAlert`. That runtime holds a venv
   built from `uv.lock`, a copy of `config/`, the database `data/job-alert.sqlite3`, `reports/`, and
   `logs/`. **Run `install` again after changing code or `config/`**; the database is kept. On the
   first install, a database seeded in the checkout's `data/` is moved into the runtime.

   The agent starts at login, restarts if it exits (at most once every five minutes), and a sleeping
   laptop catches up on the first cycle after wake. If LIVE is refused, the reason is the last line
   of `logs/job-alert.err.log`. Point manual commands at the runtime database, for example:

   ```sh
   uv run job-alert report health --db ~/Library/Application\ Support/FastJobAlert/data/job-alert.sqlite3
   ```

**The openings page.** The agent keeps `reports/openings.html` in the runtime folder current: every
open posting an alerting filter matches, newest first, with how many hours it has been open. It is
rewritten after any cycle that finds new or closed postings and at least every five minutes, and the
hours tick in the browser between rewrites. "Opened" says how the time is known: `detected` (posted
after monitoring began, accurate to one poll interval), `posted` (the provider's own publish time),
or `before monitoring` (already open when monitoring began, so the hours are a lower bound, shown
with ≥). `uv run job-alert report openings --html FILE --db DB` writes the same page on demand.

**Tesla through Gmail job alerts.** Tesla's careers site refuses automated access, so Tesla
postings come from LinkedIn job-alert emails in your Gmail (plan Task 30). One-time setup:

1. On LinkedIn, search jobs for `Tesla intern` in United States, filter Company = Tesla, and turn
   on a job alert (email, daily or as often as offered) delivered to your Gmail.
2. In your Google Account, turn on 2-Step Verification if it is off, create an app password
   (Security > App passwords), and store it in the login keychain; the command prompts for the
   password so it never appears in your shell history:

   ```sh
   security add-generic-password -s job-alert-gmail -a you@gmail.com -w
   ```

3. After the first alert email arrives, check what the source reads without changing anything:

   ```sh
   uv run job-alert collect --source tesla-email-alerts --mode dry-run \
     --db ~/Library/Application\ Support/FastJobAlert/data/job-alert.sqlite3
   ```

   Then the source is enabled and `tesla` joins the expansion allowlist (plan Task 30 gate).

The mailbox is opened read-only; nothing is marked read, moved, or deleted. To revoke access,
delete the app password in your Google Account and run `security delete-generic-password -s
job-alert-gmail`.

**What LIVE allows.** A notifying filter with an `include_companies` allowlist is a canary, and it
may alert below the 90% coverage gate, as long as every company it names has an enabled source.
Removing the allowlist is broad activation. That stays refused until direct coverage reaches 90%
(10 of 148 companies on 2026-10-14). Health incidents and the 18:00 Chicago daily digest also arrive
as laptop notifications in LIVE mode.

Reports, backups, source disabling, `429` handling, and schema changes work as described below; on the
laptop, run each command directly with `uv run job-alert …` and `--db` pointed at the runtime database,
and use `python3 scripts/backup_sqlite.py` without Docker. After editing `config/`, run `install` again.

## Server container (later: always-on and mobile)

Every command below runs from the repository root on the deployment host. `dc` is shorthand for:

```sh
alias dc='docker compose -f deploy/compose.yaml --env-file deploy/job-alert.env'
```

Inside the container the state database is `/app/data/job-alert.sqlite3` on the `job-alert-data`
volume, the registry is the repository's `config/` mounted read-only at `/app/config`, and the root
filesystem is read-only. The process runs as the unprivileged `jobalert` user and publishes no port.
A container cannot post macOS notifications, so it pairs with `--provider pushover` (set
`--provider` through the Compose command once Task 28 lands).

### Initial deployment

1. Create the secret file, which is ignored by git, and keep it readable only by you:

   ```sh
   cp deploy/job-alert.env.example deploy/job-alert.env
   chmod 600 deploy/job-alert.env
   ```

   Leave `JOB_ALERT_MODE=live-suppressed` until live enablement. The Pushover
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

### Pushover test

Fill `PUSHOVER_APP_TOKEN` and `PUSHOVER_USER_KEY` in `deploy/job-alert.env`, then:

```sh
dc run --rm job-alert job-alert notify-test --provider pushover
```

The phone receives `TEST — Fast Job Alert`. No job, event, or delivery record is read or written.
Exit code 2 means a credential is missing; 1 means Pushover refused the message.

### Live enablement

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
`dc up -d` again. Events a notifying filter approved while suppressed stay pending, and LIVE delivers
them later, so review `would_deliver` in a suppressed report before switching. An event no notifying
filter approved is closed as `filtered` when it is evaluated, so widening a filter later never
announces old postings.

### Logs and reports

```sh
dc logs --since 1h job-alert
dc exec job-alert job-alert report health --config /app/config --db /app/data/job-alert.sqlite3
dc exec job-alert job-alert report health --json --config /app/config --db /app/data/job-alert.sqlite3
uv run job-alert report coverage --markdown reports/coverage.md   # on the host, from the registry
dc exec job-alert cat /app/data/reports/daily-digest.md            # written daily at 18:00 Chicago
```

Logs rotate at 10 MiB, three files.

### Backup

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

### Restore

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

### Rotating Pushover credentials

Create the new application token in Pushover, put it (and a new user key, if that changed) in
`deploy/job-alert.env`, run `dc up -d` to recreate the container, and run the Pushover test. Then revoke the old token in Pushover. Never commit either value; `git grep`
for the token before every push if in doubt.

### Disabling one broken source

On the host, record the reason with the evidence the audit holds, then restart so the loop reloads the
registry:

```sh
uv run job-alert audit disable-source --source SOURCE_ID --status NEEDS_MANUAL_REVIEW \
  --reason "what failed, when, and the evidence"
dc restart job-alert
```

The company stays in the registry and in the coverage report. Commit the `config/sources.yaml` change.

### Handling `429 Too Many Requests`

The fetcher retries a `429`, honouring `Retry-After`. If the retries run out, the cycle fails
closed: a failed cycle never closes a job. Three consecutive failures open one health incident, which
is pushed in LIVE mode. Slow the source by raising
its `interval_minutes` in `config/sources.yaml`. Slower than its class tier is allowed; faster is
refused at load. Then run `dc restart job-alert`. `bosch-smartrecruiters` is the heaviest source: 51
requests per cycle at the 5-minute tier.

### Handling a provider schema change

A payload the adapter cannot read is an adapter error, and a challenge page is never read as an empty
board, so the cycle fails closed and opens a health incident after repeated failures. Do not loosen
validation to make the incident go away. Either:

1. capture a sanitized fixture of the new payload, write a failing adapter test, fix the adapter,
   and redeploy; or
2. disable the source with the evidence until that is done.

### Upgrading and rolling back the image

The database lives on the volume and survives any image change. Take a backup before an
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
