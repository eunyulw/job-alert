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
