# Eightfold fixture: `jobs.json`

Recorded from the public Eightfold v2 jobs API that Netflix's official careers site
(explore.jobs.netflix.net) calls, so the adapter tests exercise the real field shapes.

## Provenance

- Captured URL: `https://explore.jobs.netflix.net/api/apply/v2/jobs?domain=netflix.com&query=intern&start=0&num=10`
- Captured: 2026-10-14 (UTC), HTTP 200, `application/json`; the live response declared `count: 4`
- **Trimmed:** the envelope keeps only `domain`, `positions`, and `count`; `positions` keeps two of
  the four entries and `count` was rewritten to 2 so the fixture is a self-consistent board.

## Kept positions

| `id` | `name` | `locations` |
| --- | --- | --- |
| 790317917022 | Machine Learning/AI Infrastructure Engineering Intern (AI Platform) PhD, Winter 2027 | Los Gatos |
| 790317300617 | CG Experimental Artist- Ink | Los Angeles, Vancouver, Los Gatos |

The second entry shows that `query=intern` matches loosely; the notification filter, not the source
scope, decides what alerts. The payload names no person.
