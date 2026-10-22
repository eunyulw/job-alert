"""Kenexa BrassRing (Talent Gateway) adapter (``sjobs.brassring.com``).

A BrassRing site (``/TGnewUI/Search/Home/Home?partnerid={p}&siteid={s}``) is an Angular page that
searches with ``POST /TgNewUI/Search/Ajax/ProcessSortAndShowMoreJobs``: 50 postings a page, with
the size of the result set (``JobsCount``). The walk sorts by title (``SortType: JobTitle``): the
newest-first sort orders the many postings updated the same day differently on every request, so
its pages repeat some postings and hide others (measured 2026-10-22: 837 and 853 unique of 869),
while the title order held on repeated walks. The keyword and location field lists are sent empty
for the same reason. Like the page,
each POST carries the ``RFT`` anti-forgery token the home page renders, so a read starts with that
one request (which also sets the session cookie the token belongs to). That page carries a
login-captcha template, so the fetcher's challenge scan is off for it; a challenge served instead
renders no token, which fails the read.

Each posting is a list of ``Questions`` (name/value pairs). Mapping: ``reqid`` is the id and
identity is ``brassring:{partner}-{site}:{id}``; ``jobtitle`` is the title; ``department`` the
department; ``autoreq`` the requisition; ``lastupdated`` (``21-Oct-2026``) the publication date. The
location fields differ per site: they are the ones the home page names in
``LocationCustomSolrFields`` (``FORMTEXT4,FORMTEXT5``, state then city on General Atomics' site),
read in reverse so the city comes first, unless the source names its own ``location_fields``. The
posting link is the site's job-details link.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from job_alert.adapters.base import AdapterError
from job_alert.adapters.boards import joined, json_document, make_job, utcnow
from job_alert.adapters.common import optional_text
from job_alert.adapters.paged import UnusableRecord, walk_scoped
from job_alert.fetch import HttpFetcher, RequestSpec
from job_alert.models import CollectionResult, NormalizedJob, SourceConfig

PROVIDER = "brassring"
ORIGIN = "https://sjobs.brassring.com"
SEARCH_PATH = "/TgNewUI/Search/Ajax/ProcessSortAndShowMoreJobs"
SEARCH_CAP = 100_000
MAX_PAGES = 100
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)"
        " Chrome/141.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
_TOKEN = re.compile(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"')
_LOCATION_FIELDS = re.compile(r"LocationCustomSolrFields&quot;:&quot;([^&]*)&quot;")
_ID = re.compile(r"^\d+$")
_FIELD = re.compile(r"^[a-z0-9_]+$")


class BrassRingAdapter:
    """Normalize one BrassRing site's search in title order, one page per request."""

    provider = PROVIDER

    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self._now = now

    async def collect(self, source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
        parts = urlsplit(str(source.endpoint))
        query = {name.lower(): values[0] for name, values in parse_qs(parts.query).items()}
        partner, site = query.get("partnerid", ""), query.get("siteid", "")
        if (
            f"{parts.scheme}://{parts.netloc}" != ORIGIN
            or parts.path.lower() != "/tgnewui/search/home/home"
            or not (_ID.match(partner) and _ID.match(site))
        ):
            raise AdapterError(
                f"brassring endpoint {source.endpoint} is not a Search/Home/Home URL with"
                " partnerid and siteid"
            )
        home = await fetcher.get(
            RequestSpec(
                url=str(source.endpoint),
                headers={**HEADERS, **source.headers},
                expected="html",
                detect_challenge=False,
            )
        )
        page = home.body.decode("utf-8", errors="replace")
        token = _TOKEN.search(page)
        if token is None:
            raise AdapterError("brassring home page renders no anti-forgery token")
        location_fields = _location_fields(source, _LOCATION_FIELDS.search(page))
        first_seen_at = self._now()

        async def fetch_page(index: int, _: int) -> tuple[Sequence[object], int]:
            payload = await fetcher.post(
                RequestSpec(
                    url=f"{ORIGIN}{SEARCH_PATH}",
                    headers={
                        **HEADERS,
                        **source.headers,
                        "RFT": html.unescape(token.group(1)),
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "*/*",
                    },
                    expected="json",
                    json_body={
                        "partnerId": partner,
                        "siteId": site,
                        "keyword": "",
                        "location": "",
                        "keywordCustomSolrFields": "",
                        "locationCustomSolrFields": "",
                        "linkId": "",
                        "Latitude": 0,
                        "Longitude": 0,
                        "facetfilterfields": {"Facet": []},
                        "powersearchoptions": {"PowerSearchOption": []},
                        "SortType": "JobTitle",
                        "pageNumber": index + 1,
                        "encryptedSessionValue": "",
                    },
                    idempotent=True,
                )
            )
            document = json_document(payload, PROVIDER)
            jobs = document.get("Jobs") if isinstance(document, Mapping) else None
            records = jobs.get("Job") if isinstance(jobs, Mapping) else None
            total = document.get("JobsCount") if isinstance(document, Mapping) else None
            if not isinstance(records, list):
                raise AdapterError("brassring search answered no Jobs.Job list")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise AdapterError("brassring search answered no non-negative JobsCount")
            return records, total

        def normalize(record: object) -> NormalizedJob:
            if not isinstance(record, Mapping):
                raise UnusableRecord("record is not a JSON object")
            answers = _answers(record)
            job_id = answers.get("reqid")
            identifier = job_id if job_id and _ID.match(job_id) else None
            link = (
                f"{ORIGIN}/TGnewUI/Search/home/HomeWithPreLoad?"
                + urlencode(
                    {
                        "partnerid": partner,
                        "siteid": site,
                        "PageType": "JobDetails",
                        "jobid": identifier,
                    }
                )
                if identifier
                else None
            )
            location = joined(*(answers.get(name) for name in reversed(location_fields)))
            return make_job(
                provider=PROVIDER,
                tenant=f"{partner}-{site}",
                source=source,
                job_id=identifier,
                title=answers.get("jobtitle"),
                posting_url=link,
                first_seen_at=first_seen_at,
                locations=[location] if location else [],
                department=answers.get("department"),
                requisition_id=answers.get("autoreq"),
                published_at=_date(answers.get("lastupdated")),
            )

        return await walk_scoped(
            source_id=source.id,
            fetch_page=fetch_page,
            normalize=normalize,
            cap=SEARCH_CAP,
            max_pages=MAX_PAGES,
            cap_advice="narrow the site",
        )


def _location_fields(source: SourceConfig, found: re.Match[str] | None) -> list[str]:
    configured = source.parameters.get("location_fields")
    if configured is not None:
        if not isinstance(configured, list) or not all(
            isinstance(name, str) and _FIELD.match(name.lower()) for name in configured
        ):
            raise AdapterError(f"brassring source {source.id} has unusable location_fields")
        return [name.lower() for name in configured]
    if found is None:
        return []
    return [name.strip().lower() for name in found.group(1).split(",") if name.strip()]


def _answers(record: Mapping[str, Any]) -> dict[str, str]:
    questions = record.get("Questions")
    answers: dict[str, str] = {}
    for question in questions if isinstance(questions, list) else []:
        if not isinstance(question, Mapping):
            continue
        name, value = optional_text(question.get("QuestionName")), question.get("Value")
        text = optional_text(value) if isinstance(value, str) else None
        if name and text is not None:
            answers.setdefault(name.lower(), " ".join(text.split()))
    return answers


def _date(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%d-%b-%Y").replace(tzinfo=UTC)
    except ValueError:
        return None
