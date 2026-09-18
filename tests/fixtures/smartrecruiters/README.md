# SmartRecruiters fixtures: `postings-page-1.json` and `postings-page-2.json`

Recorded from the public SmartRecruiters Posting API
(`GET /v1/companies/{companyIdentifier}/postings`) so adapter tests exercise the provider's real
pagination envelope and field shapes. Each committed file is one live response page, not a
hand-written stand-in.

## Provenance

- Company identifier: `Ubisoft2` (the board publishes `company.identifier: "Ubisoft2"` with
  `company.name: "Ubisoft"`).
- Page 1: `https://api.smartrecruiters.com/v1/companies/Ubisoft2/postings?offset=0&limit=2` —
  captured 2026-09-18T22:23:33Z (response `date` header), HTTP 200, response media type
  `application/json; charset=utf-8`, weak ETag `W/"139e-J1q2YnDxETWHI/Vke9573ySNPRg"`.
- Page 2: `https://api.smartrecruiters.com/v1/companies/Ubisoft2/postings?offset=2&limit=2` —
  captured 2026-09-18T22:23:34Z (response `date` header), HTTP 200, response media type
  `application/json; charset=utf-8`, weak ETag `W/"1581-XLNvPWBe/YxEHbrxvw929ovOEcI"`.
- Both pages declared `totalFound: 300`, which is 150 pages of this size. `limit=2` is not the
  provider's default; it was chosen because it makes two genuine pages small enough to commit.
- Page-limit evidence: a request for `limit=1000` against the same board returned `offset: 0`,
  `limit: 100`, `totalFound: 300` and exactly 100 records (HTTP 200,
  `application/json; charset=utf-8`, weak ETag `W/"3dd13-eanOb8Hr+8JQg+Jg5PjGkBQK/qU"`, captured
  2026-09-18T22:25:25Z). The provider caps a page at 100 records whatever `limit` is requested,
  which is why the adapter must not read a short page as the end of the board.

The `Ubisoft2` board does not belong to a company in `config/company_targets.yaml`. That is
intentional: this fixture only pins the provider's response shape. Task 11 audits each registry
company and may replace these files with responses from an enabled SmartRecruiters source.

## Kept pages

| file | `offset` | `limit` | `totalFound` | `content` (id, name) |
| --- | --- | --- | --- | --- |
| `postings-page-1.json` | 0 | 2 | 300 | `744000150454779` Team Lead Level Designer [Splinter Cell Remake]; `744000150453690` Paie Maître |
| `postings-page-2.json` | 2 | 2 | 300 | `744000150333355` Problem Management Specialist; `744000150335344` Application Specialist (ERP Finance) |

Each file is exactly what the provider returned for its URL: the two records of that page in board
order, inside the live `offset`/`limit`/`totalFound`/`content` envelope. The pages do not overlap,
which is what lets the unit tests walk them as two steps of one board. Every kept record carries a
structured `location` with `city`, `region` and a lower-case ISO 3166-1 alpha-2 `country`, and
every kept record publishes `department.label: "Ubisoft"` with
`typeOfEmployment.label: "Full-time"`.

## What the list endpoint did not publish

Across the 100 records of the `limit=1000` page the list response published `id`, `name`, `uuid`,
`jobAdId`, `defaultJobAd`, `refNumber`, `company`, `releasedDate`, `location`, `industry`,
`department`, `function`, `typeOfEmployment`, `experienceLevel`, `customField`, `visibility`, `ref`
and `language`. It published no public posting URL and no application URL: `ref` is the API
resource URL (`https://api.smartrecruiters.com/v1/companies/Ubisoft2/postings/{id}`), not a public
posting link. The public posting URL shape comes from the provider's own detail response:

- `https://api.smartrecruiters.com/v1/companies/Ubisoft2/postings/744000150454779` (captured
  2026-09-18T22:23:34Z, HTTP 200, `application/json; charset=utf-8`) published
  `postingUrl: "https://jobs.smartrecruiters.com/Ubisoft2/744000150454779-team-lead-level-designer-splinter-cell-remake-"`
  and
  `applyUrl: "https://jobs.smartrecruiters.com/Ubisoft2/744000150454779-team-lead-level-designer-splinter-cell-remake-?oga=true"`.

Both start with `https://jobs.smartrecruiters.com/{companyIdentifier}/{postingId}`, which is the
derivation the adapter documents and uses when a list page carries no URL. That detail response is
evidence for the derivation and is not committed. The list endpoint also published no job ad
(`jobAd` appears only in the detail response), so `description_text` stays empty for this provider.

## What changed from the live responses

- Nothing inside the two committed pages was added, replaced, shortened, or removed. Each file is
  the provider's own response for its `offset`/`limit`, rendered with two-space indentation and a
  trailing newline; every key and value is otherwise byte-identical, including the provider's
  UTF-8 text (`Paie Maître`).
- The board's other 296 postings are simply not part of these two pages.

## Sanitization

- A scan of both pages for e-mail addresses and telephone numbers returned no matches. Every
  candidate from a broad digit pattern was a posting id, a UUID, a latitude/longitude, a
  cost-centre code, or an ISO timestamp.
- Nothing was redacted, because the records contain no personal data.
- The captures were unauthenticated public GETs. No request credentials, cookies, session
  identifiers, or identifying headers were recorded or committed.
