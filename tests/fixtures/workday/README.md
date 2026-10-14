# Workday fixture: `jobs.json`

Recorded from the public Workday candidate-experience (CXS) search endpoint that NVIDIA's official
careers site calls, so the adapter tests exercise the real field shapes. The committed file is one
live response with `jobPostings` trimmed to two entries and the large `facets` array emptied.

## Provenance

- Captured: `POST https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs`
  with body `{"limit":3,"offset":0,"appliedFacets":{"workerSubType":["0c40f6bd1d8f10adf6dae42e46d44a17"]},"searchText":""}`
  (the facet value is NVIDIA's `Intern (Fixed Term)` worker subtype)
- Captured: 2026-10-14T10:25:41Z, HTTP 200, `application/json`
- `total` is the declared size of the scoped search (163); the unscoped board reports `total=2000`,
  Workday's cap, which is why Workday sources are scoped.

## Kept postings

| `bulletFields[0]` | `title` | `locationsText` | Why it is kept |
| --- | --- | --- | --- |
| JR2027948 | Data Processing Developer Technology Intern - 2027 | 2 Locations | a count, not a place |
| JR2025948 | System Software Engineer - Base OS (RDSS Intern) | Taiwan, Taipei | a real location |

The list payload carries no description and no person: no recruiter or hiring-manager field exists.
`postedOn` is relative ("Posted Today") and is not stored as a timestamp.
