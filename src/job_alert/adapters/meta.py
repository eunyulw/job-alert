"""Meta careers adapter (``www.metacareers.com``).

Meta's job search is a Relay page: the list comes from one GraphQL query,
``CareersJobSearchResultsV2DataQuery``, which the page posts to ``/graphql`` with the ``lsd`` token
it was served. The query answers every matching posting in one response (``all_jobs``), so a read
is two requests: ``GET /jobsearch/`` for the token, then the query. Both are what the page itself
sends; nothing is authenticated.

The source's endpoint is ``https://www.metacareers.com/graphql``. Parameters: ``doc_id``, the
persisted query id (default ``DEFAULT_DOC_ID``, read from the page's own request on 2026-10-22),
and ``roles`` (default ``["Internship"]``), the employment types the search is scoped to. Meta
rotates persisted query ids now and then; a stale id answers an ``errors`` document, which fails
the read with an error that says so rather than passing for an empty board.

Mapping: ``id`` is the posting id and identity is ``meta:metacareers:{id}``; ``locations`` are
copied as published (``Menlo Park, CA``, ``Remote, US``); the first of ``teams`` is the
department. The posting link is ``https://www.metacareers.com/profile/job_details/{id}``, the
page's own link. The search publishes no description, date or total.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import collect_document, make_job, unique, utcnow
from job_alert.adapters.common import optional_text
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "meta"
TENANT = "metacareers"
ORIGIN = "https://www.metacareers.com"
ENDPOINT = f"{ORIGIN}/graphql"
SEARCH_PAGE = f"{ORIGIN}/jobsearch/"
QUERY_NAME = "CareersJobSearchResultsV2DataQuery"
DEFAULT_DOC_ID = "27129360303422352"
DEFAULT_ROLES = ("Internship",)
# metacareers.com answers a request without fetch metadata with HTTP 400; these are the headers a
# browser navigation sends, nothing more, and the query below carries the page's own fetch's.
PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)"
        " Chrome/141.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Site": "none",
}
_LSD = re.compile(r'"LSD",\[\],\{"token":"([^"]+)"')
_DOC_ID = re.compile(r"^\d{5,30}$")
_JOB_ID = re.compile(r"^\d{5,30}$")


class MetaAdapter:
    """Normalize Meta's job search for the configured roles, in one query."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        if str(source.endpoint) != ENDPOINT:
            raise AdapterError(f"meta endpoint {source.endpoint} is not {ENDPOINT}")
        doc_id, roles = _parameters(source)
        page = await fetcher.get(
            RequestSpec(url=SEARCH_PAGE, headers=PAGE_HEADERS, expected="html")
        )
        token = _LSD.search(page.body.decode("utf-8", errors="replace"))
        if token is None:
            raise AdapterError("the Meta job search page no longer carries an lsd token")
        variables = {
            "search_input": {
                "q": None,
                "divisions": [],
                "offices": [],
                "roles": list(roles),
                "leadership_levels": [],
                "saved_jobs": [],
                "saved_searches": [],
                "sub_teams": [],
                "teams": [],
                "is_leadership": False,
                "is_remote_only": False,
                "sort_by_new": True,
                "results_per_page": None,
            },
            "viewasUserID": None,
            "isLoggedIn": False,
        }
        payload = await fetcher.post(
            RequestSpec(
                url=ENDPOINT,
                headers={
                    "User-Agent": PAGE_HEADERS["User-Agent"],
                    "Accept": "*/*",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-Dest": "empty",
                    "Origin": ORIGIN,
                    "Referer": SEARCH_PAGE,
                    "X-FB-LSD": token.group(1),
                    "X-FB-Friendly-Name": QUERY_NAME,
                },
                expected="json",
                form_body={
                    "av": "0",
                    "__user": "0",
                    "__a": "1",
                    "__comet_req": "31",
                    "lsd": token.group(1),
                    "fb_api_caller_class": "RelayModern",
                    "fb_api_req_friendly_name": QUERY_NAME,
                    "server_timestamps": "true",
                    "variables": json.dumps(variables, separators=(",", ":")),
                    "doc_id": doc_id,
                },
                idempotent=True,
            )
        )
        records = _all_jobs(payload.body)
        first_seen_at = self._now()

        def normalize(record: Mapping[str, Any]) -> NormalizedJob:
            job_id = optional_text(record.get("id"))
            if job_id is not None and not _JOB_ID.match(job_id):
                job_id = None
            places = record.get("locations")
            teams = record.get("teams")
            return make_job(
                provider=PROVIDER,
                tenant=TENANT,
                source=source,
                job_id=job_id,
                title=record.get("title"),
                posting_url=f"{ORIGIN}/profile/job_details/{job_id}" if job_id else None,
                first_seen_at=first_seen_at,
                locations=unique(optional_text(place) for place in places if isinstance(place, str))
                if isinstance(places, list)
                else [],
                department=teams[0] if isinstance(teams, list) and teams else None,
            )

        return collect_document(source=source, records=records, normalize=normalize)


def _parameters(source: SourceConfig) -> tuple[str, tuple[str, ...]]:
    doc_id = source.parameters.get("doc_id", DEFAULT_DOC_ID)
    if not isinstance(doc_id, str) or not _DOC_ID.match(doc_id):
        raise AdapterError(f"meta source {source.id} has an unusable doc_id {doc_id!r}")
    roles = source.parameters.get("roles", list(DEFAULT_ROLES))
    if (
        not isinstance(roles, list)
        or not roles
        or not all(isinstance(role, str) and role.strip() for role in roles)
    ):
        raise AdapterError(f"meta source {source.id} needs roles as a list of names")
    return doc_id, tuple(roles)


def _all_jobs(body: bytes) -> list[object]:
    # The response is one JSON document per line; the first holds the query result.
    first = body.decode("utf-8", errors="replace").split("\n", 1)[0]
    try:
        document = json.loads(first)
    except json.JSONDecodeError as exc:
        raise AdapterError(f"meta search answered something that is not JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise AdapterError("meta search answered something that is not a JSON object")
    if document.get("errors"):
        raise AdapterError(
            "meta search answered an error; the persisted query id (doc_id) has probably"
            " rotated: read the current one from the job search page's own request"
        )
    data = document.get("data")
    search = data.get("job_search_with_featured_jobs_v2") if isinstance(data, Mapping) else None
    records = search.get("all_jobs") if isinstance(search, Mapping) else None
    if not isinstance(records, list):
        raise AdapterError("meta search answered no all_jobs list")
    if not records:
        # Meta always lists internships; an empty list is a changed query, not an empty board.
        raise AdapterError("meta search answered an empty all_jobs list")
    return records
