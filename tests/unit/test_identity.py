import pytest

from job_alert.identity import (
    build_dedupe_key,
    build_job_key,
    canonicalize_url,
    content_hash,
)


def test_canonicalize_url_removes_tracking_but_preserves_job_id() -> None:
    assert canonicalize_url(
        "https://jobs.example.com/opening?id=123&utm_source=linkedin&gh_src=abc#apply"
    ) == "https://jobs.example.com/opening?id=123"


@pytest.mark.parametrize(
    "parameter",
    [
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gh_src",
        "lever-source",
        "source",
    ],
)
def test_canonicalize_url_strips_every_allowlisted_parameter(parameter: str) -> None:
    assert canonicalize_url(f"https://jobs.example.com/j?{parameter}=x&id=7") == (
        "https://jobs.example.com/j?id=7"
    )


def test_canonicalize_url_is_case_stable_and_order_independent() -> None:
    assert canonicalize_url("https://Jobs.Example.COM/j?b=2&a=1") == "https://jobs.example.com/j?a=1&b=2"
    assert canonicalize_url("https://jobs.example.com/j?b=2&a=1") == canonicalize_url(
        "https://jobs.example.com/j?a=1&b=2"
    )
    # Only the allowlisted names are removed; every other parameter survives verbatim.
    assert canonicalize_url("https://jobs.example.com/j?ref=keep&source=drop") == (
        "https://jobs.example.com/j?ref=keep"
    )


def test_build_job_key_prefers_provider_tenant_and_source_job_id() -> None:
    assert (
        build_job_key(
            provider="greenhouse",
            tenant="openai",
            source_job_id="123",
            company_id="openai",
            title="Research Engineer",
            locations=["San Francisco, CA"],
            apply_url="https://jobs.example.com/opening?utm_source=linkedin",
        )
        == "greenhouse:openai:123"
    )


def test_build_job_key_falls_back_to_a_stable_fingerprint() -> None:
    key = build_job_key(
        provider="html",
        tenant="site",
        source_job_id=None,
        company_id="acme",
        title="  Robotics   Engineer ",
        locations=["Tokyo", " Osaka "],
        apply_url="https://jobs.example.com/apply?id=9&utm_medium=email",
    )
    assert key.startswith("sha256:")
    assert key == build_job_key(
        provider="html",
        tenant="site",
        source_job_id=None,
        company_id="acme",
        title="robotics engineer",
        locations=["osaka", "TOKYO"],
        apply_url="https://jobs.example.com/apply?utm_source=x&id=9",
    )
    assert key != build_job_key(
        provider="html",
        tenant="site",
        source_job_id=None,
        company_id="acme",
        title="robotics engineer",
        locations=["osaka", "TOKYO"],
        apply_url="https://jobs.example.com/apply?id=10",
    )


def test_build_dedupe_key_prefers_requisition_then_url_then_fingerprint() -> None:
    def build(requisition_id: str | None, apply_url: str) -> str:
        return build_dedupe_key(
            company_id="acme",
            requisition_id=requisition_id,
            apply_url=apply_url,
            title="Robotics Engineer",
            locations=["Tokyo"],
            department="Hardware",
        )

    assert build("req-1", "https://jobs.example.com/apply?id=9") == "requisition:acme:req-1"
    assert build(None, "https://jobs.example.com/apply?id=9&utm_source=x#top") == (
        "url:acme:https://jobs.example.com/apply?id=9"
    )
    fingerprint = build(None, "")
    assert fingerprint.startswith("sha256:")
    assert fingerprint == build_dedupe_key(
        company_id="acme",
        requisition_id=None,
        apply_url="",
        title="robotics engineer",
        locations=["tokyo"],
        department=" Hardware ",
    )


def test_content_hash_tracks_real_edits_and_ignores_cosmetics() -> None:
    digest = content_hash("Robotics Engineer", ["Tokyo"], "Build robots.")
    assert len(digest) == 64
    assert digest == content_hash(" robotics   engineer ", ["tokyo"], "Build robots.")
    assert digest != content_hash("Robotics Engineer", ["Tokyo"], "Build robots!")
    # An absent description hashes the same as an empty one.
    assert content_hash("Robotics Engineer", ["Tokyo"], None) == content_hash(
        "Robotics Engineer", ["Tokyo"], ""
    )
