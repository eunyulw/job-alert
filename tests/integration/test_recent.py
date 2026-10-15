"""The last-24-hours page: which detections it lists, which it flags, and how it renders them."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from job_alert.config import FilterConfig
from job_alert.recent import (
    RecentPosting,
    collect_recent,
    render_recent_page,
    write_recent_page,
)
from job_alert.service import RunMode
from tests.integration.harness import FIXED_NOW, ServiceHarness


@pytest.mark.asyncio
async def test_only_detections_inside_the_window_are_listed_and_matches_are_flagged(
    service_harness: ServiceHarness,
) -> None:
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.clock.advance(hours=1)
    service_harness.adapter.set_fixture("three-jobs")
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.LIVE)
    filters = (
        FilterConfig(
            id="only-three", notify=True, include_companies=["acme"],
            include_title_keywords=["engineer 3"],
        ),
    )
    company = [service_harness.company]

    inside = collect_recent(
        service_harness.repo, filters=filters, companies=company,
        now=FIXED_NOW + timedelta(hours=5),
    )
    outside = collect_recent(
        service_harness.repo, filters=filters, companies=company,
        now=FIXED_NOW + timedelta(hours=26),
    )

    # The two baseline postings were seeded, never detected, so only the new one is listed.
    assert [(posting.title, posting.matched, posting.closed) for posting in inside] == [
        ("Robotics Software Engineer 3", True, False)
    ]
    assert inside[0].company == "Acme Robotics"
    assert inside[0].detected_at == FIXED_NOW + timedelta(hours=1)
    assert outside == []


def test_the_page_escapes_provider_text_and_marks_matches_and_closings() -> None:
    hostile = RecentPosting(
        company="Acme <b>",
        title='<script>alert("x")</script>',
        url="javascript:alert(1)",
        locations=("Palo Alto & Remote",),
        detected_at=FIXED_NOW - timedelta(minutes=90),
        matched=True,
        closed=True,
    )

    page = render_recent_page([hostile], generated_at=FIXED_NOW)

    assert '<script>alert("x")</script>' not in page
    assert "javascript:alert" not in page
    assert "Acme &lt;b&gt;" in page and "Palo Alto &amp; Remote" in page
    assert '<tr class="match closed"' in page
    assert '<td class="ago">1h 30m</td>' in page
    assert "1 postings detected in the last 24 hours, 1 matching your filters." in page
    empty = render_recent_page([], generated_at=FIXED_NOW)
    assert "Nothing detected in the last 24 hours." in empty


@pytest.mark.asyncio
async def test_the_page_is_written_atomically(
    service_harness: ServiceHarness, tmp_path: Path
) -> None:
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    target = tmp_path / "reports" / "recent.html"

    count = write_recent_page(
        target, service_harness.repo, filters=service_harness.filters,
        companies=[service_harness.company], now=FIXED_NOW,
    )

    assert count == 0
    assert target.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert not target.with_name("recent.html.tmp").exists()
