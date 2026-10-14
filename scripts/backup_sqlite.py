"""Consistent SQLite backup and restore for the job alert state database.

    python scripts/backup_sqlite.py backup DATABASE BACKUP_DIRECTORY
    python scripts/backup_sqlite.py restore BACKUP DATABASE

``backup`` copies a live database through the sqlite3 online-backup API, so it is safe while the
monitor is writing in WAL mode, verifies the copy with ``PRAGMA integrity_check``, keeps the newest
14 ``job-alert-*.sqlite3`` files in the directory, and prints the new file's path.

``restore`` verifies a backup the same way and then copies it over the database through the same
API. Stop the monitor first: a restore replaces every job, event, and delivery record.

Only the standard library is used, so the script runs inside the container (which has no ``sqlite3``
CLI) and on any host with Python 3. Exit code 2 is a usage error, 1 a failed integrity check.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

KEEP = 14
PREFIX = "job-alert-"
SUFFIX = ".sqlite3"
USAGE = (
    "usage: backup_sqlite.py backup DATABASE BACKUP_DIRECTORY\n"
    "       backup_sqlite.py restore BACKUP DATABASE"
)


def _usage(message: str | None = None) -> int:
    if message:
        print(message, file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 2


def _copy(source: Path, target: Path) -> None:
    reader = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        writer = sqlite3.connect(target)
        try:
            reader.backup(writer)
        finally:
            writer.close()
    finally:
        reader.close()


def _intact(path: Path) -> bool:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        return False


def backup(database: Path, directory: Path) -> int:
    if not database.is_file():
        return _usage(f"database does not exist: {database}")
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"{PREFIX}{stamp}{SUFFIX}"
    _copy(database, target)
    if not _intact(target):
        print(f"backup integrity check failed: {target}", file=sys.stderr)
        return 1
    backups = sorted(
        directory.glob(f"{PREFIX}*{SUFFIX}"), key=lambda path: path.stat().st_mtime, reverse=True
    )
    for old in backups[KEEP:]:
        if old != target:
            old.unlink()
    print(target)
    return 0


def restore(source: Path, database: Path) -> int:
    if not source.is_file():
        return _usage(f"backup does not exist: {source}")
    if not _intact(source):
        print(f"refusing to restore: {source} fails its integrity check", file=sys.stderr)
        return 1
    database.parent.mkdir(parents=True, exist_ok=True)
    _copy(source, database)
    print(f"restored {database} from {source}")
    return 0


def main(arguments: list[str]) -> int:
    if len(arguments) != 3:
        return _usage()
    command, first, second = arguments
    if not first or not second:
        return _usage("paths must not be empty")
    if command == "backup":
        if Path(second).resolve() == Path("/"):
            return _usage("refusing to write backups into the filesystem root")
        return backup(Path(first), Path(second))
    if command == "restore":
        return restore(Path(first), Path(second))
    return _usage(f"unknown command: {command}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
