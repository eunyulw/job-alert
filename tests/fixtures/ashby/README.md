# Ashby fixture: `job_board.json`

Recorded from the public Ashby Job Postings API (`GET /posting-api/job-board/{board}`) so adapter
tests exercise the provider's real field shapes. The committed file is a trimmed subset of one live
response, not a hand-written stand-in.

## Provenance

- Board token: `openai`
- Captured URL: `https://api.ashbyhq.com/posting-api/job-board/openai`
- Captured: 2026-09-18T22:11:29Z (response `date` header), HTTP 200, response media type
  `application/json; charset=utf-8`, 13,587,074 bytes, weak ETag
  `W/"job-board:c885280d18d9c5b24f96555371870779157c207239921f66b25b1b2e22880365"`
- The board published 817 postings; the committed fixture keeps two.

The captured board belongs to OpenAI, not to a company in `config/company_targets.yaml`. That is
intentional: this fixture only pins the provider's response shape. Task 11 audits each registry
company and may replace this file with a response from an enabled Ashby source.

## Kept postings

| `id` | `title` | `location` | `secondaryLocations` | `workplaceType` | `publishedAt` |
| --- | --- | --- | --- | --- | --- |
| f873dd71-46c9-4be1-8580-bc67c56b4cad | Full-Stack Software Engineer, Applied Foundations | San Francisco | Mountain View | Hybrid | 2026-05-07T19:45:42.110+00:00 |
| b60d3040-38e4-4abe-8af8-881286b10d50 | Researcher, Safety Training, National Security | San Francisco | Washington, DC, US - Remote | `null` | 2026-09-12T03:21:15.873+00:00 |

The two postings differ on purpose: the first carries one additional site and a `workplaceType`,
the second carries two additional sites and publishes `workplaceType` and `isRemote` as `null`,
which is how 247 of the 817 live postings answered. Every secondary location keeps its nested
`address.postalAddress` object, including the second record's remote site, whose address has no
`addressRegion` or `addressLocality`.

## What changed from the live response

- `jobs` was reduced to the two postings above. The envelope key `apiVersion` (`"1"`) is kept.
- Each record's two description fields were cut to keep the fixture small: `descriptionPlain`
  f873dd71… 4536 → 1151 characters and b60d3040… 4733 → 1033 characters, each cut at the next
  paragraph break; `descriptionHtml` f873dd71… 5882 → 1372 characters and b60d3040… 6113 → 1451
  characters, each cut after the next closing `</p>`. Both fields therefore remain exact prefixes
  of the live values.
- Nothing else was added, replaced, or shortened. Every other key and value is byte-identical to
  the live response, including the `null` values, the nested address objects, and the trailing
  space the board publishes inside the first posting's description text.

## What the endpoint did not publish

The live response for this board has no total count field and no update timestamp, so the adapter
cannot declare a total and leaves `source_updated_at` empty. It also never omitted a key the
adapter reads: across all 817 postings, `id`, `title`, `location`, `department`, `team`,
`employmentType`, `publishedAt`, `jobUrl`, `applyUrl`, `descriptionPlain` and
`descriptionHtml` were always present and never blank, while `isRemote` was `null` on 247
postings and `workplaceType` was `null` on the same 247. Cases the live board never showed, such
as a record without a department or without plain text, are therefore pinned by synthetic records
inside `tests/unit/adapters/test_ashby.py` rather than by invented fixture content. The unrecognized
top-level `apiVersion` key is not mapped into a job.

## Sanitization

- A scan of all 817 postings' `descriptionPlain` and `descriptionHtml` for e-mail addresses and
  telephone numbers returned no matches, and the two kept postings contain no personal data to
  remove.
- Nothing was redacted and no value was invented.
- The capture was an unauthenticated public GET. No request credentials, cookies, session
  identifiers, or identifying headers were recorded or committed.
