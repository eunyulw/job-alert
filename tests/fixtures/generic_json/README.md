# Generic JSON fixture: `jobs.json`

**This fixture is synthetic.** It is hand-written for the registry-driven generic JSON adapter and
is not a capture of any live endpoint. No company publishes this document, and no real company's
board, token, posting id, or career URL appears in it.

## What it stands for

A company careers site that serves its own JSON search endpoint, which is exactly the shape a
registry entry for the generic JSON adapter has to describe: a nested collection of job records
(`results.jobs`), one record id per posting (`jobId`), a display title (`name`), a location list, a
public posting URL, a separate application URL, an HTML description, an ISO 8601 publish time, and
a next-page reference that the adapter follows (`nextPageUrl`, `null` here because this document
describes a single-page board).

The company name (Northwind Robotics) and the `northwind.example` domain are invented. The
`.example` top-level domain is reserved for documentation, so the URLs cannot resolve to a real
site.

## Sanitization

- No credentials, session identifiers, request headers, or personal names appear in the payload.
- No key carries a person's name: there is no `creator`, `author`, `hiringManager`, `recruiter`,
  `contact`, or `owner` field, and none of those keys is present with an empty value either.
- The file is valid JSON so the fixture provenance scan can read every key it contains.

## `tests/fixtures/jsonld/jobs.html`

The JSON-LD fixture is synthetic in the same way and is described in its own README.
