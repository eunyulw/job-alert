# Greenhouse fixture: `jobs.json`

Recorded from the public Greenhouse Job Board API so adapter tests exercise the provider's real
field shapes. The committed file is a trimmed subset of one live response, not a hand-written
stand-in.

## Provenance

- Board token: `vercel`
- Captured URL: `https://boards-api.greenhouse.io/v1/boards/vercel/jobs?content=true`
- Captured: 2026-09-18T01:41:54Z, HTTP 200, response media type `application/json`
  (rechecked at 2026-09-18T06:44:13Z: HTTP 200, `content-type: application/json`)
- The board advertised 84 published postings; the committed fixture keeps exactly two.

The captured board does not belong to one of the companies in `config/company_targets.yaml`. That
is intentional: this fixture only pins the provider's response shape. Task 11 audits each registry
company and may replace this file with a response from an enabled Greenhouse source.

## Kept postings

| `id` | `title` | `location.name` | `requisition_id` |
| --- | --- | --- | --- |
| 6199608004 | Software Engineer, Agentic Infrastructure | Hybrid - San Francisco, New York City | 824 |
| 6142097004 | Scaled Commercial Account Executive, Install base  | Hybrid - San Francisco, New York City, Austin | 1318 |

The two postings differ on purpose: the first has one office, the second has three, and the second
keeps the trailing space the board really publishes in its title.

## What changed from the live response

- `jobs` reduced to the two postings above, and `meta.total` set to `2` so the fixture describes a
  complete board that contains exactly what it ships with. The live response said `84`.
- Each posting's `content` was cut at a paragraph boundary (`&lt;/p&gt;`) to keep the fixture small:
  job 6199608004 from 7313 to 2479 characters, job 6142097004 from 9724 to 2867 characters.
- Three keys that only carried board-wide legal boilerplate were dropped:
  `ai_disclaimer`, `include_ai_disclaimer`, and `ai_opt_out_request_url`.

Every other key is byte-identical to the live response, including provider quirks worth keeping:
`content` is an HTML-escaped document (`&lt;div ...&gt;`), `application_deadline` is `null`,
`offices[*].name` uses board labels such as `Office - Austin`, and timestamps carry a `-04:00`
offset rather than `Z`.

## Sanitization

- No recruiter names, personal names, e-mail addresses, phone numbers, or candidate data appear in
  the captured payload. An e-mail scan across the descriptions of all 84 postings returned none,
  and the two kept postings mention recruiting only generically ("your recruiter can share more
  details").
- Nothing was replaced or invented: fields were kept verbatim or dropped whole, and only trailing
  description text was removed.
- The request is an unauthenticated public GET. No request credentials, session identifiers, or
  identifying headers were recorded or committed.
