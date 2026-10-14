"""Container health check: healthy while some source completed a collection recently.

Exit 0 when the newest ``source_state.last_complete_at`` is within
``JOB_ALERT_HEALTH_MAX_AGE_MINUTES`` (default 65); otherwise exit non-zero with the reason on
stderr. The database is opened read-only and never created, so a mistyped path cannot pass by
producing an empty file. Per-source health is the monitor's own job; this only proves the loop is
alive and collecting.
"""

import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

database = Path(os.getenv("JOB_ALERT_DB", "/app/data/job-alert.sqlite3"))
maximum_age = timedelta(minutes=int(os.getenv("JOB_ALERT_HEALTH_MAX_AGE_MINUTES", "65")))
if not database.is_file():
    sys.exit(f"database missing: {database}")
try:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT MAX(last_complete_at) FROM source_state WHERE last_complete_at IS NOT NULL"
        ).fetchone()
    finally:
        connection.close()
except sqlite3.Error as exc:
    sys.exit(f"cannot read source state from {database}: {exc}")
if row is None or row[0] is None:
    sys.exit("no complete source run yet")
last_complete = datetime.fromisoformat(row[0]).astimezone(UTC)
age = datetime.now(UTC) - last_complete
if age > maximum_age:
    sys.exit(f"source state is stale: newest complete run was {age} ago")
sys.exit(0)
