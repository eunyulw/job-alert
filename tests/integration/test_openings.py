"""The openings page: which postings it lists, how it dates them, and how safely it renders them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from job_alert.config import FilterConfig
from job_alert.openings import (
    BEFORE_MONITORING,
    DETECTED,
    Opening,
    collect_openings,
    render_openings_page,
    write_openings_page,
)
from job_alert.service import RunMode
from tests.integration.harness import FIXED_NOW, ServiceHarness


def _collect(harness: ServiceHarness, now: datetime) -> list[Opening]:
    return collect_openings(
        harness.repo,
        sources=[harness.source],
        filters=harness.filters,
        companies=[harness.company],
        now=now,
    )


@pytest.mark.asyncio
async def test_a_detected_posting_is_dated_by_its_detection_and_listed_first(
    service_harness: ServiceHarness,
) -> None:
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.clock.advance(hours=1)
    service_harness.adapter.set_fixture("three-jobs")
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.LIVE)

    later = FIXED_NOW + timedelta(hours=4)
    openings = _collect(service_harness, later)

    assert [opening.title for opening in openings] == [
        "Robotics Software Engineer 3",
        "Robotics Software Engineer 1",
        "Robotics Software Engineer 2",
    ]
    fresh = openings[0]
    assert fresh.basis == DETECTED
    assert fresh.hours_open(later) == pytest.approx(3.0)
    assert fresh.company == "Acme Robotics"
    assert fresh.url == "https://jobs.acme-robotics.com/postings/1003"
    # The baseline postings were already open; the page can only give a lower bound.
    assert {opening.basis for opening in openings[1:]} == {BEFORE_MONITORING}


@pytest.mark.asyncio
async def test_only_postings_an_alerting_filter_matches_are_listed(
    service_harness: ServiceHarness,
) -> None:
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    service_harness.set_filters(
        (
            FilterConfig(
                id="only-one", notify=True, include_companies=["acme"],
                include_title_keywords=["engineer 1"],
            ),
            FilterConfig(id="silent", notify=False),
        )
    )

    openings = _collect(service_harness, FIXED_NOW)

    assert [opening.title for opening in openings] == ["Robotics Software Engineer 1"]
    assert openings[0].filter_id == "only-one"


def test_the_page_escapes_provider_text_and_refuses_unsafe_links() -> None:
    hostile = Opening(
        company="Acme <b>",
        title='<script>alert("x")</script>',
        url="javascript:alert(1)",
        locations=("Palo Alto & Remote",),
        opened_at=FIXED_NOW - timedelta(hours=2),
        basis=DETECTED,
        filter_id="f",
        first_seen_at=datetime(2026, 9, 18, 10, 5, 7, tzinfo=UTC),
    )

    page = render_openings_page([hostile], generated_at=FIXED_NOW)

    assert '<script>alert("x")</script>' not in page
    assert "&lt;script&gt;" in page
    assert "javascript:alert" not in page
    assert "Acme &lt;b&gt;" in page and "Palo Alto &amp; Remote" in page
    assert '<td class="hours">2.0</td>' in page
    assert '<td class="seen">2026-09-18 10:05:07</td>' in page
    assert "1 matching openings, 1 detected since monitoring began" in page


def test_a_lower_bound_is_marked_and_an_empty_page_says_so() -> None:
    old = Opening(
        company="Acme", title="Intern", url="https://acme.example/1", locations=(),
        opened_at=FIXED_NOW - timedelta(hours=250), basis=BEFORE_MONITORING, filter_id="f",
        first_seen_at=FIXED_NOW - timedelta(hours=250),
    )
    assert '<td class="hours">≥ 250</td>' in render_openings_page([old], generated_at=FIXED_NOW)
    assert "No matching openings yet." in render_openings_page([], generated_at=FIXED_NOW)


@pytest.mark.asyncio
async def test_the_page_is_written_atomically(
    service_harness: ServiceHarness, tmp_path: Path
) -> None:
    await service_harness.service.collect_source(service_harness.source, mode=RunMode.BASELINE)
    target = tmp_path / "reports" / "openings.html"

    count = write_openings_page(
        target,
        service_harness.repo,
        sources=[service_harness.source],
        filters=service_harness.filters,
        companies=[service_harness.company],
        now=datetime(2026, 9, 18, 13, 0, tzinfo=UTC),
    )

    assert count == 2
    assert target.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert not target.with_name("openings.html.tmp").exists()


def test_the_page_warns_when_no_source_has_completed_for_half_an_hour() -> None:
    fresh = render_openings_page(
        [], generated_at=FIXED_NOW, last_complete_at=FIXED_NOW - timedelta(minutes=29)
    )
    stale = render_openings_page(
        [], generated_at=FIXED_NOW, last_complete_at=FIXED_NOW - timedelta(hours=22)
    )

    assert '<p id="stale" role="alert" hidden></p>' in fresh
    assert "No source has completed a collection since" in stale
    assert 'role="alert" hidden' not in stale
    assert "No source has completed a collection yet" in render_openings_page(
        [], generated_at=FIXED_NOW, last_complete_at=None
    )
