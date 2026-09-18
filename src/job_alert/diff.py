from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast


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
    """Classify a snapshot. Absence only counts when the snapshot is complete."""
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
            # The stored state is SQLite's integer column; cast only to satisfy strict mypy.
            stored = cast("int | str", previous[key].get("missing_complete_runs", 0))
            count = int(stored) + 1
            (closed if count >= close_after else missing).append(key)
    return SnapshotDiff(new, updated, unchanged, reopened, missing, closed)
