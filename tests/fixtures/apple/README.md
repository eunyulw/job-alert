# Apple fixture: `search.html`

A server-rendered search page from jobs.apple.com, the official careers site. The page carries its
results as `window.__staticRouterHydrationData = JSON.parse("…")`, which is what the adapter reads.

## Provenance

- Captured URL: `https://jobs.apple.com/en-us/search?team=internships-STDNT-INTRN&sort=newest&page=1`
- Captured: 2026-10-14 (UTC), HTTP 200, `text/html`; the live page declared `totalRecords: 101`
- **Re-encoded, not byte-for-byte:** the hydration JSON was decoded, trimmed to two postings and to
  the `root.locale` and `search` keys the adapter reads, `totalRecords` was rewritten to 2 so the
  fixture is a self-consistent board, and it was re-encoded the same way the page encodes it. The
  surrounding page markup was reduced to a minimal shell.

## Kept postings

| `id` | `postingTitle` | Location |
| --- | --- | --- |
| 200688776-2611 | Intern - Full Stack Engineer | Prague, Czechia |
| 200685035-4170 | Internship - Machine Learning (Foundation Models) | Zurich, Switzerland |

The records name no person; `jobSummary` is the public posting summary.
