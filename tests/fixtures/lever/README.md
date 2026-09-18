# Lever fixture: `postings.json`

Recorded from the public Lever Postings API (`GET /v0/postings/{site}?mode=json`) so adapter tests
exercise the provider's real field shapes. The committed file holds the first two postings of one
live response; it is not a hand-written stand-in.

## Provenance

- Site token: `wealthfront`
- Captured URL: `https://api.lever.co/v0/postings/wealthfront?mode=json&limit=3`
- Captured: 2026-09-18T06:57:33Z, HTTP 200, response media type `application/json; charset=utf-8`
- A re-fetch of the same URL returned the same postings byte for byte (ETag
  `W/"df0d-c/4c3rjWcZJftp7VcQGRhIx5R2s"`). The board published 23 postings in total; the committed
  fixture keeps the first two.

The `wealthfront` board does not belong to a company in `config/company_targets.yaml`. That is
intentional: this fixture only pins the provider's response shape. Task 11 audits each registry
company and may replace this file with a response from an enabled Lever source.

## Kept postings

| `id` | `text` | `categories.location` | `categories.allLocations` | `createdAt` |
| --- | --- | --- | --- | --- |
| 78d6f6d5-1f08-4d5d-87be-c4250567bfb5 | Android Engineer | Palo Alto, CA | Palo Alto, CA, Seattle, WA, San Francisco, CA, New York City, NY | 1694463796009 |
| 26d453d0-f9df-4396-a655-70f6dbceb328 | Anti-Money Laundering Monitoring Analyst | Palo Alto, CA (Open to US-based Remote) | Palo Alto, CA (Open to US-based Remote) | removed (see below) |

The first posting repeats `categories.location` as the first entry of `categories.allLocations`,
which is exactly what the adapter's case-insensitive location deduplication has to collapse. The
second posting lists a single location and is remote.

## What changed from the live response

- The array was trimmed to its first two postings (23 live, 2 committed).
- **Synthetic removal:** `createdAt` was deleted from the second posting. All 23 live postings
  carried a `createdAt`, so the brief's "posting without `createdAt`" case cannot be captured as-is;
  deleting the key from one captured posting is the smallest honest way to pin that path. No other
  key of that posting was touched and no value was invented.
- Nothing else was added, replaced, or shortened. Every remaining key and value is byte-identical to
  the live response, including `descriptionPlain` (1901 and 1062 characters), the HTML
  `description`, `lists`, `openingPlain`, and `additionalPlain`.

## Sanitization

- The two kept postings describe offices, teams, and the recruiting process generically. A scan of
  both records for e-mail addresses and telephone numbers returned no matches.
- Nothing was redacted and no personal data was removed, because the captured records contain none.
- The capture was an unauthenticated public GET. No request credentials, cookies, session
  identifiers, or identifying headers were recorded or committed.
