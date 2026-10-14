# Google fixture: `results.html`

A server-rendered results page from google.com/about/careers, the official careers site. The page
carries its results in `AF_initDataCallback({key: 'ds:1', … data: [jobs, null, total, page_size]})`,
positional arrays that the adapter validates field by field.

## Provenance

- Captured URL: `https://www.google.com/about/careers/applications/jobs/results/?employment_type=INTERN`
- Captured: 2026-10-14 (UTC), HTTP 200, `text/html`; the live page declared a total of 77, 20 per page
- **Re-encoded, not byte-for-byte:** the `ds:1` data array was decoded, trimmed to two postings with
  every positional field intact, its total rewritten to 2 so the fixture is a self-consistent board,
  and written back into a minimal page holding only that one `AF_initDataCallback` block.

## Kept postings

| Index 0 (id) | Index 1 (title) | Index 9 (locations) |
| --- | --- | --- |
| 93402210922046150 | Finance MBA Intern, 2027 | Dublin, Ireland |
| 115691171337904838 | Forward Deployed Engineer Intern, 2027 | Singapore |

Index 2 is a sign-in apply link carrying an opaque token, so the adapter does not store it; the
posting link is built from the id and title. The records name no person.
