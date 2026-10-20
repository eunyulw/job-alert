"""The openings page: every open posting an alerting filter matches, and how long it has been open.

A posting's opening time is the best evidence the monitor has, and the page says which it used:

- ``detected``: the posting appeared after monitoring began, so it opened at most one poll interval
  before the first cycle that saw it. This is the accurate case.
- ``posted``: the posting was already open when monitoring began, and the provider published a
  posting time.
- ``before monitoring``: already open when monitoring began, with no published time, so the page
  can only say it has been open *at least* since it was first seen.

The page is a single static HTML file with no external requests. Every value from a provider is
escaped, links are kept only when they are absolute HTTP(S) URLs, and a short inline script keeps
the "hours open" column current between regenerations.
"""

from __future__ import annotations

import html
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from job_alert.config import FilterConfig
from job_alert.db import Repository
from job_alert.filters import evaluate_filters
from job_alert.models import CompanyTarget, SourceConfig

DETECTED = "detected"
POSTED = "posted"
BEFORE_MONITORING = "before monitoring"
REFRESH_SECONDS = 300
#: The page warns when no enabled source has completed a collection for this long.
STALE_AFTER = timedelta(minutes=30)
#: The page's own script warns when the page itself has not been rewritten for this long.
PAGE_STALE_SECONDS = 900


@dataclass(frozen=True, slots=True)
class Opening:
    """One open posting an alerting filter matches."""

    company: str
    title: str
    url: str
    locations: tuple[str, ...]
    opened_at: datetime
    basis: str
    filter_id: str

    def hours_open(self, now: datetime) -> float:
        return max(0.0, (now - self.opened_at).total_seconds() / 3600)


def collect_openings(
    repository: Repository,
    *,
    sources: Sequence[SourceConfig],
    filters: Sequence[FilterConfig],
    companies: Sequence[CompanyTarget],
    now: datetime,
) -> list[Opening]:
    """Open postings from enabled sources that an alerting filter matches, newest opening first."""
    alerting = [record for record in filters if record.enabled and record.notify]
    if not alerting:
        return []
    names = {company.id: company.name for company in companies}
    categories = {company.id: tuple(company.categories) for company in companies}
    detections = repository.first_detections()
    openings: list[Opening] = []
    for source in sources:
        if not source.enabled:
            continue
        for job in repository.load_active_jobs(source.id):
            decisions = evaluate_filters(
                job, alerting, company_categories=categories.get(job.company_id, ()), now=now
            )
            matched = next((decision for decision in decisions if decision.notify), None)
            if matched is None:
                continue
            detected = detections.get(job.canonical_job_key)
            if detected is not None:
                opened_at, basis = detected, DETECTED
            elif job.source_published_at is not None:
                opened_at, basis = job.source_published_at, POSTED
            else:
                opened_at, basis = job.first_seen_at, BEFORE_MONITORING
            openings.append(
                Opening(
                    company=names.get(job.company_id, job.company_id),
                    title=" ".join(job.title.split()),
                    url=str(job.posting_url),
                    locations=tuple(job.locations),
                    opened_at=opened_at.astimezone(UTC),
                    basis=basis,
                    filter_id=matched.filter_id,
                )
            )
    # Detected and posted times are real openings, newest first; "before monitoring" rows only
    # have a lower bound, so they go last.
    openings.sort(key=lambda item: (item.basis == BEFORE_MONITORING, -item.opened_at.timestamp()))
    return openings


def _safe_href(url: str) -> str | None:
    return url if url.startswith(("https://", "http://")) else None


def _hours_text(opening: Opening, now: datetime) -> str:
    hours = opening.hours_open(now)
    prefix = "≥ " if opening.basis == BEFORE_MONITORING else ""
    return f"{prefix}{hours:.1f}" if hours < 100 else f"{prefix}{hours:.0f}"


def _stale_notice(last_complete_at: datetime | None, generated_at: datetime) -> str:
    """A warning when the monitor has stopped collecting, so stale rows are not read as current."""
    if last_complete_at is None:
        return "No source has completed a collection yet, so this list may be incomplete."
    if generated_at - last_complete_at < STALE_AFTER:
        return ""
    since = last_complete_at.astimezone(UTC)
    return (
        f"No source has completed a collection since {since:%Y-%m-%d %H:%M} UTC."
        " The monitor is offline or stuck, so postings opened since then are missing."
    )


def render_openings_page(
    openings: Sequence[Opening],
    *,
    generated_at: datetime,
    last_complete_at: datetime | None = None,
) -> str:
    """The page as one self-contained HTML document."""
    rows: list[str] = []
    for opening in openings:
        href = _safe_href(opening.url)
        title = html.escape(opening.title)
        role = title
        if href:
            role = f'<a href="{html.escape(href)}" rel="noopener noreferrer">{title}</a>'
        location = html.escape("; ".join(opening.locations) or "—")
        rows.append(
            "<tr"
            f' data-opened="{html.escape(opening.opened_at.isoformat())}"'
            f' data-lower-bound="{"1" if opening.basis == BEFORE_MONITORING else "0"}">'
            f'<td class="hours">{html.escape(_hours_text(opening, generated_at))}</td>'
            f"<td>{html.escape(opening.company)}</td>"
            f"<td>{role}</td>"
            f"<td>{location}</td>"
            f'<td class="basis">{html.escape(opening.basis)}</td>'
            "</tr>"
        )
    fresh = sum(1 for opening in openings if opening.basis == DETECTED)
    summary = (
        f"{len(openings)} matching openings, {fresh} detected since monitoring began."
        f" Updated {generated_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC."
    )
    empty = '<tr><td colspan="5" class="empty">No matching openings yet.</td></tr>'
    body = "\n".join(rows) or empty
    generated = json.dumps(generated_at.astimezone(UTC).isoformat())
    notice = _stale_notice(last_complete_at, generated_at)
    hidden = "" if notice else " hidden"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<title>Job Openings</title>
<style>
:root {{ --bg: #ffffff; --fg: #1d1d1f; --muted: #6e6e73; --line: #e5e5ea; --accent: #0a66c2;
  --new: #e8f3ff; --warn: #fff1d6; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #1c1c1e; --fg: #f2f2f7; --muted: #a1a1a6; --line: #38383a; --accent: #64a8ff;
    --new: #1f2d3d; --warn: #4a3a12; }}
}}
body {{ margin: 0; padding: 16px; background: var(--bg); color: var(--fg);
  font: 14px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
h1 {{ font-size: 18px; margin: 0 0 4px; }}
p {{ margin: 0 0 12px; color: var(--muted); }}
.wrap {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line);
  vertical-align: top; }}
th {{ font-weight: 600; color: var(--muted); white-space: nowrap; }}
td.hours {{ font-variant-numeric: tabular-nums; white-space: nowrap; text-align: right; }}
td.basis, td.empty {{ color: var(--muted); }}
tr[data-lower-bound="0"] {{ background: var(--new); }}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
#stale {{ margin: 0 0 12px; padding: 8px 12px; border-radius: 6px; color: var(--fg);
  background: var(--warn); }}
</style>
</head>
<body>
<h1>Job openings</h1>
<p id="summary">{html.escape(summary)}</p>
<p id="stale" role="alert"{hidden}>{html.escape(notice)}</p>
<div class="wrap"><table>
<thead><tr><th>Hours open</th><th>Company</th><th>Role</th><th>Location</th><th>Opened</th>
</tr></thead>
<tbody>
{body}
</tbody>
</table></div>
<script>
(function () {{
  var generated = new Date({generated});
  function refresh() {{
    var now = Date.now();
    document.querySelectorAll("tr[data-opened]").forEach(function (row) {{
      var hours = Math.max(0, (now - new Date(row.dataset.opened).getTime()) / 36e5);
      var text = hours < 100 ? hours.toFixed(1) : hours.toFixed(0);
      row.querySelector("td.hours").textContent =
        (row.dataset.lowerBound === "1" ? "\\u2265 " : "") + text;
    }});
  }}
  refresh();
  setInterval(refresh, 60000);
  function checkPage() {{
    if (Date.now() - generated.getTime() < {PAGE_STALE_SECONDS * 1000}) return;
    var stale = document.getElementById("stale");
    stale.textContent = "This page has not been rewritten since " + generated.toLocaleString() +
      ". The monitor is not running, so postings opened since then are missing.";
    stale.hidden = false;
  }}
  checkPage();
  setInterval(checkPage, 60000);
  var summary = document.getElementById("summary");
  summary.textContent = summary.textContent.replace(/Updated .*$/,
    "Updated " + generated.toLocaleString() + ".");
}})();
</script>
</body>
</html>
"""


def write_openings_page(
    path: Path,
    repository: Repository,
    *,
    sources: Sequence[SourceConfig],
    filters: Sequence[FilterConfig],
    companies: Sequence[CompanyTarget],
    now: datetime,
) -> int:
    """Write the page atomically and return how many openings it lists."""
    openings = collect_openings(
        repository, sources=sources, filters=filters, companies=companies, now=now
    )
    completions = [
        state.last_complete_at
        for source in sources
        if source.enabled
        and (state := repository.source_health(source.id)) is not None
        and state.last_complete_at is not None
    ]
    last_complete_at = max(
        (moment if moment.tzinfo else moment.replace(tzinfo=UTC) for moment in completions),
        default=None,
    )
    page = render_openings_page(openings, generated_at=now, last_complete_at=last_complete_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.name + ".tmp")
    staging.write_text(page, encoding="utf-8")
    staging.replace(path)
    return len(openings)
