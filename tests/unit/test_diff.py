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


def test_complete_snapshot_classifies_every_transition() -> None:
    diff = compare_snapshot(
        previous={
            "same": {"content_hash": "one"},
            "edited": {"content_hash": "one"},
            "back": {"content_hash": "one", "closed_at": "2026-09-01T00:00:00+00:00"},
        },
        current={
            "same": {"content_hash": "one"},
            "edited": {"content_hash": "two"},
            "back": {"content_hash": "two"},
            "fresh": {"content_hash": "one"},
        },
        complete=True,
        close_after=2,
    )
    assert diff.new == ["fresh"]
    assert diff.updated == ["edited"]
    assert diff.unchanged == ["same"]
    # A previously closed job returning is a reopening even when its content also changed.
    assert diff.reopened == ["back"]
    assert diff.missing == []
    assert diff.closed == []


def test_job_stays_missing_until_the_close_after_threshold() -> None:
    diff = compare_snapshot(
        previous={"a": {"content_hash": "one", "missing_complete_runs": 1}},
        current={},
        complete=True,
        close_after=3,
    )
    assert diff.missing == ["a"]
    assert diff.closed == []


def test_diff_output_is_sorted_deterministically() -> None:
    diff = compare_snapshot(
        previous={"b": {"content_hash": "one"}, "d": {"content_hash": "one"}},
        current={"c": {"content_hash": "one"}, "a": {"content_hash": "one"}},
        complete=True,
        close_after=1,
    )
    assert diff.new == ["a", "c"]
    assert diff.closed == ["b", "d"]
