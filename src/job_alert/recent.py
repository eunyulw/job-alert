"""The last-24-hours page: every posting detected as new or reopened in the window, matched or not.

Postings a baseline seeded are not listed: they were already open when monitoring began, so they
were never detected. A posting that matches an alerting filter is highlighted, and a checkbox hides
the rest. A posting that closed again inside the window stays listed, marked closed.

Like the openings page, this is one static HTML file with no external requests; provider text is
escaped and only absolute HTTP(S) links are kept.
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
from job_alert.models import CompanyTarget
from job_alert.openings import REFRESH_SECONDS

WINDOW = timedelta(hours=24)
# The scheduler writes this page next to the openings page.
RECENT_PAGE_NAME = "recent.html"


@dataclass(frozen=True, slots=True)
class RecentPosting:
    """One posting detected inside the window."""

    company: str
    title: str
    url: str
    locations: tuple[str, ...]
    detected_at: datetime
    matched: bool
    closed: bool


def collect_recent(
    repository: Repository,
    *,
    filters: Sequence[FilterConfig],
    companies: Sequence[CompanyTarget],
    now: datetime,
) -> list[RecentPosting]:
    """Postings detected in the 24 hours before ``now``, newest first."""
    alerting = [record for record in filters if record.enabled and record.notify]
    names = {company.id: company.name for company in companies}
    categories = {company.id: tuple(company.categories) for company in companies}
    postings: list[RecentPosting] = []
    for job, detected_at, closed in repository.detections_since(now - WINDOW):
        decisions = evaluate_filters(
            job, alerting, company_categories=categories.get(job.company_id, ()), now=now
        )
        postings.append(
            RecentPosting(
                company=names.get(job.company_id, job.company_id),
                title=" ".join(job.title.split()),
                url=str(job.posting_url),
                locations=tuple(job.locations),
                detected_at=detected_at.astimezone(UTC),
                matched=any(decision.notify for decision in decisions),
                closed=closed,
            )
        )
    return postings


def _ago(moment: datetime, now: datetime) -> str:
    minutes = max(0, int((now - moment).total_seconds() // 60))
    return f"{minutes}m" if minutes < 60 else f"{minutes // 60}h {minutes % 60:02d}m"


def render_recent_page(postings: Sequence[RecentPosting], *, generated_at: datetime) -> str:
    """The page as one self-contained HTML document."""
    rows: list[str] = []
    for posting in postings:
        title = html.escape(posting.title)
        role = title
        if posting.url.startswith(("https://", "http://")):
            role = f'<a href="{html.escape(posting.url)}" rel="noopener noreferrer">{title}</a>'
        if posting.closed:
            role += ' <span class="tag">closed</span>'
        classes = " ".join(
            name for name, on in (("match", posting.matched), ("closed", posting.closed)) if on
        )
        rows.append(
            f'<tr class="{classes}" data-detected="{html.escape(posting.detected_at.isoformat())}">'
            f'<td class="ago">{_ago(posting.detected_at, generated_at)}</td>'
            f"<td>{html.escape(posting.company)}</td>"
            f"<td>{role}</td>"
            f"<td>{html.escape('; '.join(posting.locations) or '—')}</td>"
            "</tr>"
        )
    matched = sum(1 for posting in postings if posting.matched)
    summary = (
        f"{len(postings)} postings detected in the last 24 hours, {matched} matching your filters."
        f" Updated {generated_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC."
    )
    empty = '<tr><td colspan="4" class="empty">Nothing detected in the last 24 hours.</td></tr>'
    body = "\n".join(rows) or empty
    generated = json.dumps(generated_at.astimezone(UTC).isoformat())
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<title>Last 24 Hours</title>
<style>
:root {{ --bg: #ffffff; --fg: #1d1d1f; --muted: #6e6e73; --line: #e5e5ea; --accent: #0a66c2;
  --match: #e8f3ff; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #1c1c1e; --fg: #f2f2f7; --muted: #a1a1a6; --line: #38383a; --accent: #64a8ff;
    --match: #1f2d3d; }}
}}
body {{ margin: 0; padding: 16px; background: var(--bg); color: var(--fg);
  font: 14px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
h1 {{ font-size: 18px; margin: 0 0 4px; }}
p {{ margin: 0 0 8px; color: var(--muted); }}
label {{ display: inline-block; margin-bottom: 12px; cursor: pointer; }}
.wrap {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line);
  vertical-align: top; }}
th {{ font-weight: 600; color: var(--muted); white-space: nowrap; }}
td.ago {{ font-variant-numeric: tabular-nums; white-space: nowrap; text-align: right;
  color: var(--muted); }}
td.empty, .tag {{ color: var(--muted); }}
.tag {{ font-size: 12px; }}
tr.match {{ background: var(--match); }}
tr.closed {{ opacity: 0.55; }}
body.only-matches tbody tr:not(.match) {{ display: none; }}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<h1>Last 24 hours</h1>
<p id="summary">{html.escape(summary)}</p>
<label><input type="checkbox" id="only"> Matches only</label>
<div class="wrap"><table>
<thead><tr><th>Ago</th><th>Company</th><th>Role</th><th>Location</th></tr></thead>
<tbody>
{body}
</tbody>
</table></div>
<script>
(function () {{
  var only = document.getElementById("only");
  try {{ only.checked = localStorage.getItem("onlyMatches") === "1"; }} catch (e) {{}}
  function apply() {{
    document.body.classList.toggle("only-matches", only.checked);
    try {{ localStorage.setItem("onlyMatches", only.checked ? "1" : "0"); }} catch (e) {{}}
  }}
  only.addEventListener("change", apply);
  apply();
  function refresh() {{
    var now = Date.now();
    document.querySelectorAll("tr[data-detected]").forEach(function (row) {{
      var minutes = Math.max(0, Math.floor((now - new Date(row.dataset.detected)) / 6e4));
      row.querySelector("td.ago").textContent = minutes < 60 ? minutes + "m"
        : Math.floor(minutes / 60) + "h " + String(minutes % 60).padStart(2, "0") + "m";
    }});
  }}
  refresh();
  setInterval(refresh, 60000);
  var summary = document.getElementById("summary");
  summary.textContent = summary.textContent.replace(/Updated .*$/,
    "Updated " + new Date({generated}).toLocaleString() + ".");
}})();
</script>
</body>
</html>
"""


def write_recent_page(
    path: Path,
    repository: Repository,
    *,
    filters: Sequence[FilterConfig],
    companies: Sequence[CompanyTarget],
    now: datetime,
) -> int:
    """Write the page atomically and return how many postings it lists."""
    postings = collect_recent(repository, filters=filters, companies=companies, now=now)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.name + ".tmp")
    staging.write_text(render_recent_page(postings, generated_at=now), encoding="utf-8")
    staging.replace(path)
    return len(postings)
