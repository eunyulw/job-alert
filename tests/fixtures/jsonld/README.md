# JSON-LD fixture: `jobs.html`

**This fixture is synthetic.** It is hand-written for the JSON-LD fallback adapter and is not a
capture of any live page. No company publishes this page, and no real company's board, posting id,
or career URL appears in it.

## What it stands for

A server-rendered careers page that publishes its openings as schema.org `JobPosting` documents in
an `application/ld+json` script block. The block's top level is an `@graph` holding one
`Organization` node and the two `JobPosting` nodes the adapter tests read, so the fixture pins
`@graph` traversal rather than a bare top-level array.

The two postings differ on purpose:

- The first publishes an `identifier` object, a `jobLocation` with a structured `PostalAddress`, an
  `applicantLocationRequirements` country, a list-valued `employmentType`, and a **relative**
  `url` (`/postings/NW-2001`) that only the audited source origin may resolve.
- The second publishes no `applicationUrl`, lists two `jobLocation` entries (one named place and one
  structured address), and carries `jobLocationType: TELECOMMUTE`.

The page also carries a `text/javascript` script block whose payload contains the word `JobPosting`
in a `title`. The adapter must ignore it: only `application/ld+json` blocks are structured data.

The company name (Northwind Robotics) and the `northwind.example` domain are invented. The
`.example` top-level domain is reserved for documentation, so the URLs cannot resolve to a real
site.

## Sanitization

- No credentials, session identifiers, or request headers appear in the page.
- No person's name appears: the document carries an organization name only, and no `creator`,
  `author`, `hiringManager`, `recruiter`, `contact`, or `owner` key exists anywhere in it.
- The description strings are short and generic, written for the fixture rather than copied from a
  live posting.
