# Amazon fixture: `search.json`

Recorded from the public search endpoint that the amazon.jobs site itself calls, so the adapter
tests exercise the real field shapes. The committed file is one live response with its `jobs` array
trimmed to two postings; it is not a hand-written stand-in.

## Provenance

- Captured URL: `https://www.amazon.jobs/en/search.json?base_query=intern&sort=recent&offset=0&result_limit=3`
- Captured: 2026-10-14T10:22:02Z, HTTP 200, `application/json; charset=utf-8`, ETag
  `W/"8a1662c1fba916e0c6d340f4060af5a8"`
- `hits` is the scoped total the provider declared for the query (428); the committed `jobs` array
  keeps two of the three returned postings.

## Kept postings

| `id_icims` | `title` | `locations` entries | Why it is kept |
| --- | --- | --- | --- |
| 10578321 | DCO Tech Intern | 1 (Cape Town) | single location |
| 10578090 | Hardware Development Engineer Intern, Summer 2027 | 3 (Sunnyvale, North Reading, Hawthorne) | multi-location, the shape a robotics intern search returns |

`locations` is a list of JSON-encoded strings, not objects, which the adapter has to decode.
`is_intern` and `university_job` are `null` even on intern titles, so the adapter does not rely on
them. The payload names no person: no recruiter, hiring manager, or contact field is present.
