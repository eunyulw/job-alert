from job_alert.identity import canonicalize_url


def test_canonicalize_url_removes_tracking_but_preserves_job_id() -> None:
    assert canonicalize_url(
        "https://jobs.example.com/opening?id=123&utm_source=linkedin&gh_src=abc#apply"
    ) == "https://jobs.example.com/opening?id=123"
