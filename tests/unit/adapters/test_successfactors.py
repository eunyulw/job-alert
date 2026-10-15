"""Offline tests for the SuccessFactors career-site search adapter (``/search/``)."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
import respx

from job_alert.adapters.base import AdapterError
from job_alert.adapters.successfactors import SuccessFactorsAdapter, parse_search_page
from job_alert.fetch import HttpFetcher
from job_alert.models import SourceClass, SourceConfig, SupportStatus

SEARCH = "https://jobs.example-sf.com/search/"
ENDPOINT = f"{SEARCH}?q=&optionsFacetsDD_country=US"


def row(job_id: str, title: str, location: str, *, href: str | None = None) -> str:
    """One result row as the site renders it: a desktop and a phone copy of the link."""
    link = href or f"/job/Detroit-{title.replace(' ', '-')}-MI/{job_id}/"
    return f"""
<tr class="data-row">
  <td class="colTitle">
    <span class="jobTitle hidden-phone"><a href="{link}" class="jobTitle-link">{title}</a></span>
    <div class="jobdetail-phone visible-phone">
      <span class="jobTitle visible-phone"><a class="jobTitle-link" href="{link}">{title}</a></span>
      <span class="jobLocation visible-phone"><span class="jobLocation"> {location} </span></span>
    </div>
  </td>
  <td class="colLocation"><span class="jobLocation"> {location} </span></td>
  <td class="colDepartment"><span class="jobDepartment">Engineering</span></td>
  <td class="colFacility"><span class="jobFacility">Robotics<br>Division</span></td>
</tr>"""


def page(*rows: str, first: int = 1, total: int) -> httpx.Response:
    last = first + len(rows) - 1
    label = (
        f'<span class="paginationLabel">Results <b>{first} &ndash; {last}</b> of <b>{total}</b>'
        "</span>"
    )
    body = f"<html><body>{label}<table><tbody>{''.join(rows)}</tbody></table></body></html>"
    return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})


def source(endpoint: str = ENDPOINT) -> SourceConfig:
    return SourceConfig(
        id="acme-successfactors",
        company_id="acme",
        provider="successfactors",
        source_class=SourceClass.PUBLIC_ATS,
        support_status=SupportStatus.SUPPORTED_UNDOCUMENTED,
        endpoint=endpoint,
        interval_minutes=10,
    )


@respx.mock
@pytest.mark.asyncio
async def test_the_search_is_walked_by_startrow_until_the_declared_total() -> None:
    route = respx.get(url__startswith=SEARCH).mock(
        side_effect=[
            page(
                row("101", "Mechanical Engineering Intern", "Detroit, MI, US"),
                row("102", "Controls Co-op", "Novi, MI, US, 48375"),
                total=3,
            ),
            page(row("103", "Test Engineer", "Toledo, OH, US"), first=3, total=3),
        ]
    )

    async with HttpFetcher() as fetcher:
        result = await SuccessFactorsAdapter().collect(source(), fetcher)

    assert result.complete and result.declared_total == 3 and result.page_count == 2
    assert [job.source_job_id for job in result.jobs] == ["101", "102", "103"]
    first = result.jobs[0]
    assert first.title == "Mechanical Engineering Intern"
    assert first.locations == ["Detroit, MI, US"]
    assert first.department == "Engineering" and first.team == "Robotics Division"
    assert str(first.posting_url) == (
        "https://jobs.example-sf.com/job/Detroit-Mechanical-Engineering-Intern-MI/101/"
    )
    assert first.canonical_job_key.startswith("successfactors:")
    # The configured scope is kept and the adapter adds only startrow, advancing by rows read.
    queries = [dict(parse_qsl(urlsplit(str(call.request.url)).query, keep_blank_values=True))
               for call in route.calls]
    assert [query["startrow"] for query in queries] == ["0", "2"]
    assert all(query["optionsFacetsDD_country"] == "US" for query in queries)


@respx.mock
@pytest.mark.asyncio
async def test_a_row_without_a_usable_link_makes_the_collection_incomplete() -> None:
    respx.get(url__startswith=SEARCH).mock(
        return_value=page(
            row("201", "Robotics Intern", "Austin, TX, US"),
            row("202", "Elsewhere", "Austin, TX, US", href="https://evil.example/job/x/202/"),
            row("203", "No Id", "Austin, TX, US", href="/job/no-id-here/"),
            total=3,
        )
    )

    async with HttpFetcher() as fetcher:
        result = await SuccessFactorsAdapter().collect(source(), fetcher)

    assert not result.complete
    assert [job.source_job_id for job in result.jobs] == ["201"]
    assert any("leaves the career site" in warning for warning in result.warnings)
    assert any("no numeric id" in warning for warning in result.warnings)


def test_a_page_without_a_result_total_is_not_a_search_page() -> None:
    with pytest.raises(AdapterError, match="no result total"):
        parse_search_page("<html><body>Access denied</body></html>")
    assert parse_search_page("<p>There are currently no open positions.</p>") == ([], 0)


def test_a_thousands_separator_in_the_total_is_read() -> None:
    rows, total = parse_search_page("Results <b>1 – 25</b> of <b>1,528</b>")
    assert rows == [] and total == 1528


@pytest.mark.asyncio
async def test_an_endpoint_that_is_not_a_search_url_is_refused() -> None:
    async with HttpFetcher() as fetcher:
        with pytest.raises(AdapterError, match="not an https /search/ URL"):
            await SuccessFactorsAdapter().collect(
                source("https://jobs.example-sf.com/go/Engineering/123/"), fetcher
            )
