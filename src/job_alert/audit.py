"""Company-by-company source audit for the fast job alert monitor.

The audit answers one question per canonical company: can this monitor read that company's public
job listings directly from an official endpoint, with complete pagination, stable posting
identity, and a direct application link?

What the probe does
-------------------
``probe_source()`` fetches the company's official careers landing URL once with the shared
``HttpFetcher``: ordinary HTTP, bounded redirects, a short read timeout, no browser, no challenge
evasion, no credentials. It fingerprints the provider from positive evidence only, namely the
final URL and the links the page publishes, matched against the provider markers in
``fingerprint_provider()``. Ambiguous evidence leaves the fingerprint empty, because a company is
never assigned a provider from reputation or from an unverified third-party directory.

When the fingerprint names a provider whose adapter exists in ``PROVIDER_ADAPTERS`` the probe
derives that provider's public list endpoint and runs the adapter twice. Only a complete,
repeatable collection earns ``SUPPORTED``, or ``SUPPORTED_UNDOCUMENTED`` for a public endpoint the
provider does not document. Everything else is recorded as ``HTML_ONLY``, ``FALLBACK_ONLY``,
``BLOCKED``, or ``NEEDS_MANUAL_REVIEW`` together with the evidence that produced the verdict.

Resumability
------------
``run_audit()`` persists one record per company to the results file (default
``reports/source-audit.json``) as soon as that company finishes and skips companies that are
already recorded, so an interrupted audit continues instead of restarting. ``limit`` bounds one
chunk. A company that has not been probed keeps ``NEEDS_MANUAL_REVIEW`` and carries no audit date,
which is what ``build_report()`` surfaces as outstanding.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter, ValidationError

from job_alert.adapters.adp import AdpAdapter
from job_alert.adapters.amazon import AmazonAdapter
from job_alert.adapters.apple import AppleAdapter
from job_alert.adapters.ashby import AshbyAdapter
from job_alert.adapters.bamboohr import BambooHrAdapter
from job_alert.adapters.base import (
    AdapterError,
    PartialCollectionError,
    SourceAdapter,
    job_metrics,
    validate_collection,
)
from job_alert.adapters.breezy import BreezyAdapter
from job_alert.adapters.eightfold import EightfoldAdapter
from job_alert.adapters.email_alerts import EmailAlertAdapter
from job_alert.adapters.freehire import FreehireAdapter
from job_alert.adapters.generic_json import GenericJsonAdapter
from job_alert.adapters.google import GoogleAdapter
from job_alert.adapters.greenhouse import GreenhouseAdapter
from job_alert.adapters.hrmdirect import HrmDirectAdapter
from job_alert.adapters.icims import IcimsAdapter
from job_alert.adapters.jazzhr import JazzHrAdapter
from job_alert.adapters.jibe import JibeAdapter
from job_alert.adapters.jsonld import JsonLdAdapter
from job_alert.adapters.lever import LeverAdapter
from job_alert.adapters.oracle import OracleAdapter
from job_alert.adapters.phenom import PhenomAdapter
from job_alert.adapters.pinpoint import PinpointAdapter
from job_alert.adapters.radancy import RadancyAdapter
from job_alert.adapters.rippling import RipplingAdapter
from job_alert.adapters.smartrecruiters import SmartRecruitersAdapter
from job_alert.adapters.successfactors import SuccessFactorsAdapter
from job_alert.adapters.taleo import TaleoAdapter
from job_alert.adapters.tesla_relay import TeslaRelayAdapter
from job_alert.adapters.ukg import UkgAdapter
from job_alert.adapters.workable import WorkableAdapter
from job_alert.adapters.workday import WorkdayAdapter
from job_alert.config import AppConfig, load_app_config
from job_alert.fetch import (
    ChallengeDetected,
    FetchFailure,
    HttpFetcher,
    HttpPayload,
    PayloadTooLarge,
    RequestSpec,
)
from job_alert.models import (
    TIER_INTERVAL_MINUTES,
    CollectionResult,
    CompanyTarget,
    HttpsUrl,
    SourceClass,
    SourceConfig,
    SupportStatus,
)

# Provider name to adapter instance. Task 12 owns the registry that dispatches by source provider;
# it imports this mapping instead of rebuilding one, which is what keeps `audit set-source` able to
# validate an endpoint without depending on a registry that does not exist yet.
PROVIDER_ADAPTERS: dict[str, SourceAdapter] = {
    "greenhouse": GreenhouseAdapter(),
    "lever": LeverAdapter(),
    "ashby": AshbyAdapter(),
    "smartrecruiters": SmartRecruitersAdapter(),
    "amazon": AmazonAdapter(),
    "apple": AppleAdapter(),
    "google": GoogleAdapter(),
    "eightfold": EightfoldAdapter(),
    "email-alerts": EmailAlertAdapter(),
    "freehire": FreehireAdapter(),
    "tesla-relay": TeslaRelayAdapter(),
    "workday": WorkdayAdapter(),
    "generic-json": GenericJsonAdapter(),
    "json-ld": JsonLdAdapter(),
    "successfactors": SuccessFactorsAdapter(),
    "rippling": RipplingAdapter(),
    "workable": WorkableAdapter(),
    "bamboohr": BambooHrAdapter(),
    "pinpoint": PinpointAdapter(),
    "oracle": OracleAdapter(),
    "ukg": UkgAdapter(),
    "icims": IcimsAdapter(),
    "phenom": PhenomAdapter(),
    "jibe": JibeAdapter(),
    "taleo": TaleoAdapter(),
    "adp": AdpAdapter(),
    "hrmdirect": HrmDirectAdapter(),
    "radancy": RadancyAdapter(),
    "breezy": BreezyAdapter(),
    "jazzhr": JazzHrAdapter(),
}

# Public endpoints that a provider documents earn SUPPORTED; a public endpoint the provider does
# not document earns SUPPORTED_UNDOCUMENTED. The value is also the source class the audit writes to
# a verified registry record.
PROVIDER_SOURCE_CLASSES: dict[str, SourceClass] = {
    "greenhouse": SourceClass.DOCUMENTED_API,
    "lever": SourceClass.DOCUMENTED_API,
    "smartrecruiters": SourceClass.DOCUMENTED_API,
    "ashby": SourceClass.PUBLIC_ATS,
    "workday": SourceClass.PUBLIC_ATS,
    "successfactors": SourceClass.PUBLIC_ATS,
    "icims": SourceClass.PUBLIC_ATS,
    "eightfold": SourceClass.PUBLIC_ATS,
    "oracle": SourceClass.PUBLIC_ATS,
    "rippling": SourceClass.PUBLIC_ATS,
    "workable": SourceClass.PUBLIC_ATS,
    "bamboohr": SourceClass.PUBLIC_ATS,
    "pinpoint": SourceClass.PUBLIC_ATS,
    "ukg": SourceClass.PUBLIC_ATS,
    "phenom": SourceClass.PUBLIC_ATS,
    "jibe": SourceClass.PUBLIC_ATS,
    "taleo": SourceClass.PUBLIC_ATS,
    "adp": SourceClass.PUBLIC_ATS,
    "hrmdirect": SourceClass.PUBLIC_ATS,
    "radancy": SourceClass.PUBLIC_ATS,
    "breezy": SourceClass.PUBLIC_ATS,
    "jazzhr": SourceClass.PUBLIC_ATS,
    "freehire": SourceClass.FALLBACK,
    "tesla-relay": SourceClass.COMPANY_JSON,
    "generic-json": SourceClass.COMPANY_JSON,
    "json-ld": SourceClass.HTML,
}

DOCUMENTED_PROVIDERS = frozenset({"greenhouse", "lever", "smartrecruiters"})
DISCOVERY_PROVIDER = "discovery"
WORKDAY_PROVIDER = "workday"
JSONLD_PROVIDER = "json-ld"

# Parameters a probe writes about its own verdict. A re-probe drops them before writing its own, so
# a superseded verdict can never survive as evidence. `audit_note` (a human resolution carried into
# the audit) and `audit_reason` (an operator's disable reason) are not probe output and are kept.
AUDIT_VERDICT_PARAMETERS: tuple[str, ...] = (
    "audit_status",
    "audit_probed_at",
    "audit_evidence",
    "audit_provider",
    "audit_endpoint",
    "audit_http_status",
    "audit_final_url",
    "audit_notes",
)

# Adapter parameters the audit itself writes for a provider (Workday today). They belong to one
# provider's source identity, so they are cleared wherever that identity is superseded and are
# listed here with the rest of the probe-written keys.
PROBE_ADAPTER_PARAMETERS: tuple[str, ...] = ("page_size", "locale", "detail_base_url")

PROBE_EVIDENCE_PARAMETERS: tuple[str, ...] = (
    *AUDIT_VERDICT_PARAMETERS,
    *PROBE_ADAPTER_PARAMETERS,
)

# Parameters the audit never treats as probe output: a human resolution and an operator's reason.
HUMAN_PARAMETERS: frozenset[str] = frozenset({"audit_note", "audit_reason"})

# Every status a company can end the audit with, best first. A company with several sources is
# counted once, by the best status any of its sources earned.
STATUS_PRECEDENCE: tuple[SupportStatus, ...] = (
    SupportStatus.SUPPORTED,
    SupportStatus.SUPPORTED_UNDOCUMENTED,
    SupportStatus.HTML_ONLY,
    SupportStatus.FALLBACK_ONLY,
    SupportStatus.BLOCKED,
    SupportStatus.NEEDS_MANUAL_REVIEW,
)

# A direct source is a public endpoint this monitor reads itself, with no browser and no fallback.
DIRECT_STATUSES = frozenset({SupportStatus.SUPPORTED, SupportStatus.SUPPORTED_UNDOCUMENTED})

BROAD_ACTIVATION_THRESHOLD = 0.90

PROBE_TIMEOUT_SECONDS = 12.0
DEFAULT_AUDIT_CONCURRENCY = 4
MAX_AUDIT_CONCURRENCY = 4
COMPANY_PROBE_BUDGET_SECONDS = 180.0

WORKDAY_PAGE_SIZE = 20
# Workday reports the board's size as ``total`` on the first page only: later pages answer with
# ``total: 0``, so the audit keeps the first declaration and walks at most this many pages before it
# stops calling the prefix a complete collection.
WORKDAY_PAGE_CAP = 5

# A careers page can be large; the provider markers are looked for in the links it publishes, and a
# half-megabyte sample is enough for that while keeping the scan cheap and predictable.
BODY_SCAN_BYTES = 512 * 1024
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>\\)]+")
_HREF_IN_TEXT = re.compile(r"href=[\"']([^\"']+)[\"']", re.IGNORECASE)
CAREER_LINK_MARKERS = (
    "career",
    "jobs",
    "/job",
    "job-",
    "position",
    "opening",
    "opportunit",
    "hiring",
    "recruit",
)
# Shared asset hosts and paths never prove a provider. `*.successfactors.*` serves UI bundles and
# `*.oraclecloud.com` serves images, and the audit only records positive careers-site evidence.
ASSET_HOST_LABELS = frozenset(
    {"assets", "static", "cdn", "img", "images", "errors", "vs-errors", "fonts", "js"}
)
ASSET_PATH_SUFFIXES = (
    ".js",
    ".css",
    ".png",
    ".jpg",
    ".jpeg",
    ".svg",
    ".gif",
    ".ico",
    ".map",
    ".woff",
    ".woff2",
)
ASSET_PATH_SEGMENTS = (
    "/images",
    "/img/",
    "/assets/",
    "/static/",
    "/fonts/",
    "/extlib/",
    "/resources/",
    "/verp/",
)
SECOND_HOP_LIMIT = 2
_JSONLD_JOB_POSTING = re.compile(rb"\"@type\"\s*:\s*\"JobPosting\"", re.IGNORECASE)

SOURCES_HEADER = (
    "# Audited company job sources.\n"
    "#\n"
    "# One record per canonical company. A record the Task 11 source audit verified carries the\n"
    "# provider, the public list endpoint, the evidence date, and the support status that earned\n"
    "# it; a record still awaiting audit stays on provider `discovery` with\n"
    "# support_status NEEDS_MANUAL_REVIEW, no audit_date, and enabled: false.\n"
)

SOURCE_KEY_ORDER: tuple[str, ...] = (
    "id",
    "company_id",
    "provider",
    "source_class",
    "support_status",
    "endpoint",
    "interval_minutes",
    "tenant",
    "parameters",
    "headers",
    "enabled",
    "audit_date",
)

CATEGORY_ORDER: tuple[str, ...] = (
    "big-tech-ai",
    "robotics-autonomy-physical-ai",
    "industrial-automation-advanced-manufacturing",
    "automotive-ev-autonomous-mobility",
    "aerospace-defense",
    "medical-robotics",
)

# Companies whose discovery records carried a verification obligation into this task: three with an
# `audit_note` because no official careers landing page could be confirmed, and two that were set
# but never machine-verified. The report states what the audit found for each of them.
CARRIED_OVER_COMPANIES: tuple[str, ...] = (
    "agibot",
    "saic-motor",
    "chery-automobile",
    "denso-robotics",
    "geely-holding",
)

DEFAULT_RESULTS_PATH = Path("reports/source-audit.json")


class AuditError(RuntimeError):
    """The audit cannot complete the requested operation."""


class ProviderFingerprint(BaseModel):
    """Positive evidence for one provider, or nothing when the evidence is ambiguous."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str | None = None
    evidence: str | None = None
    host: str | None = None
    tenant: str | None = None
    board: str | None = None
    feed_url: str | None = None


class ProbeResult(BaseModel):
    """Everything one company's audit probe observed, plus the verdict it earned."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    support_status: SupportStatus
    evidence_at: datetime
    http_status: int | None = None
    final_url: str | None = None
    media_type: str | None = None
    provider_fingerprint: str | None = None
    provider_evidence: str | None = None
    provider_feed_url: str | None = None
    challenge_detected: bool = False
    elapsed_ms: int = 0
    parsed_job_count: int = 0
    pagination_complete: bool | None = None
    stable_id_percent: float | None = None
    direct_url_percent: float | None = None
    notes: list[str] = Field(default_factory=list)


class CoverageSummary(BaseModel):
    """Per-company support coverage across the canonical registry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_companies: int
    by_status: dict[SupportStatus, int]
    direct_supported: int
    direct_coverage_percent: float
    broad_activation_allowed: bool
    unaudited_companies: list[str]


@dataclass(frozen=True, slots=True)
class _Verification:
    """What running a provider adapter (or the Workday walk) proved about one feed."""

    status: SupportStatus
    job_count: int = 0
    pagination_complete: bool | None = None
    stable_id_percent: float | None = None
    direct_url_percent: float | None = None
    notes: Sequence[str] = ()


class AuditStore(BaseModel):
    """The persisted, resumable audit state: one record per audited company."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    companies: dict[str, dict[str, Any]] = Field(default_factory=dict)


_HTTPS_URL = TypeAdapter(HttpsUrl)


def to_https_url(value: str) -> HttpUrl:
    """Validate an endpoint string as the HTTPS-only URL the registry stores."""
    try:
        return _HTTPS_URL.validate_python(value)
    except ValidationError as exc:
        raise AuditError(f"{value} is not a valid HTTPS endpoint: {exc}") from exc


def now_utc() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------------------------
# Provider fingerprinting
# --------------------------------------------------------------------------------------------


def fingerprint_provider(
    *, final_url: str, media_type: str | None, body: bytes
) -> ProviderFingerprint:
    """Identify a provider from positive evidence, or return an empty fingerprint.

    The final URL is the strongest evidence, so it is classified first. When that says nothing,
    the links published by the landing page are classified, because a careers page usually links
    straight at the board that serves its postings. JSON-LD ``JobPosting`` documents are evidence
    of a structured HTML listing, not of a provider, so they fingerprint as ``json-ld``.
    """
    match = _classify_url(final_url)
    if match is not None:
        return match

    text = _decode_body(body, media_type)
    for candidate in _URL_IN_TEXT.findall(text):
        match = _classify_url(candidate)
        if match is not None:
            return match.model_copy(update={"evidence": f"careers page links to {candidate}"})

    if _JSONLD_JOB_POSTING.search(body[:BODY_SCAN_BYTES]) is not None:
        return ProviderFingerprint(
            provider=JSONLD_PROVIDER,
            evidence="careers page publishes application/ld+json JobPosting documents",
        )
    return ProviderFingerprint()


def _decode_body(body: bytes, media_type: str | None) -> str:
    if media_type is not None and not (
        media_type.startswith("text/") or media_type.endswith(("json", "xml", "javascript"))
    ):
        return ""
    return body[:BODY_SCAN_BYTES].decode("utf-8", "replace")


def _career_links(base_url: str, body: bytes) -> list[str]:
    """Career-page links worth one more request, best first.

    A landing page frequently points at the board that serves its postings, either on another host
    such as ``jobs.example.com`` or at a path such as ``/careers/openings``. The audit follows at
    most ``SECOND_HOP_LIMIT`` of those links, so the discovery stays bounded and each request is an
    ordinary GET of a page the company publishes.
    """
    text = body[:BODY_SCAN_BYTES].decode("utf-8", "replace")
    base_host = urlsplit(base_url).netloc.lower()
    seen: set[str] = set()
    scored: list[tuple[int, str]] = []
    for raw in [*_HREF_IN_TEXT.findall(text), *_URL_IN_TEXT.findall(text)]:
        try:
            candidate = urljoin(base_url, raw)
            parts = urlsplit(candidate)
        except ValueError:
            continue
        if parts.scheme != "https" or not parts.netloc:
            continue
        host = parts.netloc.lower()
        marker_text = f"{host}{parts.path}".lower()
        if not any(marker in marker_text for marker in CAREER_LINK_MARKERS):
            continue
        target = parts._replace(fragment="").geturl()
        if target in seen or target.rstrip("/") == base_url.rstrip("/"):
            continue
        seen.add(target)
        # A different host that names careers or jobs is the strongest hint; a same-host path is
        # next; a bare segment somewhere else on the original host comes last.
        score = 0 if host != base_host and ("career" in host or "job" in host) else 1
        scored.append((score, target))
    scored.sort(key=lambda item: (item[0], len(item[1])))
    return [target for _, target in scored[:SECOND_HOP_LIMIT]]


def _classify_url(url: str) -> ProviderFingerprint | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        # A malformed link in a careers page must never abort the audit.
        return None
    host = parts.netloc.split("@")[-1].split(":")[0].lower()
    path = parts.path
    query = parse_qs(parts.query)

    if host == "boards-api.greenhouse.io":
        token = _first_match(path, r"/v1/boards/([^/]+)/jobs")
        if token is not None:
            return _fingerprint("greenhouse", host, f"final URL host {host}", tenant=token)
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        token = _greenhouse_board_token(path, query)
        if token is not None:
            return _fingerprint("greenhouse", host, f"final URL host {host}", tenant=token)

    if host == "api.lever.co" or host == "api.eu.lever.co":
        site = _first_match(path, r"/v0/postings/([^/]+)")
        if site is not None:
            return _fingerprint("lever", host, f"final URL host {host}", tenant=site)
    if host in {"jobs.lever.co", "jobs.eu.lever.co"}:
        site = _first_path_segment(path)
        if site is not None:
            return _fingerprint("lever", host, f"final URL host {host}", tenant=site)

    if host == "api.ashbyhq.com":
        board = _first_match(path, r"/posting-api/job-board/([^/]+)")
        if board is not None:
            return _fingerprint("ashby", host, f"final URL host {host}", tenant=board)
    if host == "jobs.ashbyhq.com":
        board = _first_path_segment(path)
        if board is not None:
            return _fingerprint("ashby", host, f"final URL host {host}", tenant=board)

    if host == "api.smartrecruiters.com":
        identifier = _first_match(path, r"/v1/companies/([^/]+)/postings")
        if identifier is not None:
            return _fingerprint(
                "smartrecruiters", host, f"final URL host {host}", tenant=identifier
            )
    if host in {"jobs.smartrecruiters.com", "careers.smartrecruiters.com"}:
        identifier = _first_path_segment(path)
        if identifier is not None:
            return _fingerprint(
                "smartrecruiters", host, f"final URL host {host}", tenant=identifier
            )

    workday = _classify_workday(host, path)
    if workday is not None:
        return workday

    # The enterprise markers below name a careers site, so a generic host suffix or a shared asset
    # path is not evidence: `*.successfactors.com` serves jQuery bundles and `*.oraclecloud.com`
    # serves images, and neither says anything about where the company publishes postings.
    if _is_asset_url(host, path):
        return None
    if re.search(r"^career[a-z0-9-]*\.successfactors\.(com|eu)$", host) is not None or (
        "careersitecompanyid" in url.lower()
    ):
        return _fingerprint("successfactors", host, f"final URL host {host}")
    if host.endswith(".icims.com") and "/jobs/" in path:
        return _fingerprint("icims", host, f"final URL host {host}")
    if (host == "eightfold.ai" or host.endswith(".eightfold.ai")) and "/careers" in path:
        return _fingerprint("eightfold", host, f"final URL host {host}")
    if host.endswith(".oraclecloud.com") and "/hcmUI/CandidateExperience" in path:
        return _fingerprint("oracle", host, f"final URL host {host}")
    if host.endswith(".taleo.net") and re.search(r"/(careersection|ats|careers)/", path, re.I):
        return _fingerprint("oracle", host, f"final URL host {host}")
    return None


def _is_asset_url(host: str, path: str) -> bool:
    """Whether a URL names a shared asset rather than a careers page."""
    if host.split(".")[0] in ASSET_HOST_LABELS:
        return True
    lowered = path.lower()
    if lowered.endswith(ASSET_PATH_SUFFIXES):
        return True
    return any(segment in lowered for segment in ASSET_PATH_SEGMENTS)


def _classify_workday(host: str, path: str) -> ProviderFingerprint | None:
    cxs = re.search(r"/wday/cxs/(?P<tenant>[^/]+)/(?P<site>[^/]+)/jobs", path)
    if cxs is not None:
        return _fingerprint(
            WORKDAY_PROVIDER,
            host,
            f"final URL path /wday/cxs/{cxs.group('tenant')}/{cxs.group('site')}/jobs",
            tenant=cxs.group("tenant"),
            board=cxs.group("site"),
        )
    match = re.search(r"^(?P<tenant>[a-z0-9-]+)\.(?:wd\d+\.)?myworkdayjobs\.com$", host)
    if match is None:
        return None
    segments = [segment for segment in path.split("/") if segment]
    if segments and re.fullmatch(r"[a-z]{2}-[A-Za-z]{2}", segments[0]) is not None:
        segments = segments[1:]
    if not segments:
        return None
    # Workday career sites are mounted at /{locale}/{site}; anything after the site names a page or
    # a posting, so the first remaining segment is the site.
    site = segments[0]
    return _fingerprint(
        WORKDAY_PROVIDER,
        host,
        f"final URL host {host}",
        tenant=match.group("tenant"),
        board=site,
    )


def _fingerprint(
    provider: str,
    host: str,
    evidence: str,
    *,
    tenant: str | None = None,
    board: str | None = None,
) -> ProviderFingerprint:
    return ProviderFingerprint(
        provider=provider,
        evidence=evidence,
        host=host,
        # A board token is percent-encoded in the link and decoded for identity, which is the form
        # the adapters put in their canonical job keys; the feed URL keeps the encoded token.
        tenant=unquote(tenant) if tenant is not None else None,
        board=unquote(board) if board is not None else None,
        feed_url=_feed_url(provider, tenant=tenant, board=board, host=host),
    )


def _feed_url(
    provider: str, *, tenant: str | None, board: str | None, host: str
) -> str | None:
    if provider == "greenhouse" and tenant is not None:
        return f"https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs?content=true"
    if provider == "lever" and tenant is not None:
        return f"https://api.lever.co/v0/postings/{tenant}?mode=json"
    if provider == "ashby" and tenant is not None:
        return f"https://api.ashbyhq.com/posting-api/job-board/{tenant}"
    if provider == "smartrecruiters" and tenant is not None:
        return f"https://api.smartrecruiters.com/v1/companies/{tenant}/postings"
    if provider == WORKDAY_PROVIDER and tenant is not None and board is not None:
        return f"https://{host}/wday/cxs/{tenant}/{board}/jobs"
    return None


def _greenhouse_board_token(path: str, query: Mapping[str, list[str]]) -> str | None:
    if "/embed/job_board" in path:
        values = query.get("for")
        if values and values[0].strip():
            return values[0].strip()
    for segment in path.split("/"):
        if segment and segment not in {"embed", "job_board", "jobs"}:
            return segment
    return None


def _first_path_segment(path: str) -> str | None:
    for segment in path.split("/"):
        if segment and segment not in {"embed", "api", "v1", "v0", "posting-api", "job-board"}:
            return segment
    return None


def _first_match(path: str, pattern: str) -> str | None:
    match = re.search(pattern, path)
    return match.group(1) if match is not None else None


# --------------------------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------------------------


def _media_type(headers: Mapping[str, str]) -> str | None:
    for name, value in headers.items():
        if name.lower() == "content-type":
            return value.split(";", maxsplit=1)[0].strip().lower() or None
    return None


def _http_status(message: str) -> int | None:
    """The status code ``HttpFetcher`` reports for a non-retryable response.

    ``FetchFailure`` carries the code in its message rather than as a field, and the audit records
    that code as evidence, so the message is parsed deliberately and a missing code stays unknown.
    """
    match = re.search(r"HTTP (\d{3})", message)
    return int(match.group(1)) if match is not None else None


def _blocked_status(http_status: int | None) -> SupportStatus:
    if http_status in {401, 403, 451}:
        return SupportStatus.BLOCKED
    return SupportStatus.NEEDS_MANUAL_REVIEW


async def probe_source(source: SourceConfig, fetcher: HttpFetcher) -> ProbeResult:
    """Probe one company's official careers landing page and classify what it publishes."""
    started = time.perf_counter()
    evidence_at = now_utc()

    def elapsed_ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    def result(**fields: Any) -> ProbeResult:
        return ProbeResult(
            source_id=source.id, evidence_at=evidence_at, elapsed_ms=elapsed_ms(), **fields
        )

    try:
        payload = await fetcher.get(
            RequestSpec(
                url=str(source.endpoint),
                headers=dict(source.headers),
                expected="bytes",
                timeout_seconds=PROBE_TIMEOUT_SECONDS,
            )
        )
    except ChallengeDetected:
        return result(
            support_status=SupportStatus.BLOCKED,
            final_url=str(source.endpoint),
            challenge_detected=True,
            notes=["access challenge detected on the official careers landing page"],
        )
    except PayloadTooLarge:
        return result(
            support_status=SupportStatus.NEEDS_MANUAL_REVIEW,
            final_url=str(source.endpoint),
            notes=["careers landing page exceeded the fetch byte limit"],
        )
    except FetchFailure as exc:
        http_status = _http_status(str(exc))
        return result(
            support_status=_blocked_status(http_status),
            final_url=str(source.endpoint),
            http_status=http_status,
            challenge_detected=http_status in {401, 403, 451},
            notes=[f"careers landing page request failed: {exc}"],
        )

    media_type = _media_type(payload.headers)
    fingerprint = fingerprint_provider(
        final_url=payload.final_url, media_type=media_type, body=payload.body
    )
    hop_notes: list[str] = []
    if fingerprint.provider is None and payload.status == 200 and media_type == "text/html":
        fingerprint, hop_notes = await _second_hop_fingerprint(payload, fingerprint, fetcher)
    html_only = _html_job_posting_count(payload.body) if fingerprint.provider is None else 0

    verification = await _verify_provider(source, fingerprint, fetcher)
    notes = [*hop_notes, *verification.notes]
    status = verification.status
    if status is SupportStatus.NEEDS_MANUAL_REVIEW and fingerprint.provider is None:
        if html_only > 0:
            status = SupportStatus.HTML_ONLY
            notes.append(
                f"careers page publishes {html_only} JobPosting JSON-LD documents and no provider "
                "feed, so only an HTML adapter can read it"
            )
        elif payload.status == 200:
            status = SupportStatus.FALLBACK_ONLY
            notes.append(
                "official careers landing page responded but publishes no provider feed and no "
                "machine-readable job documents, so a fallback source is needed"
            )

    return result(
        support_status=status,
        http_status=payload.status,
        final_url=payload.final_url,
        media_type=media_type,
        provider_fingerprint=fingerprint.provider,
        provider_evidence=fingerprint.evidence,
        provider_feed_url=fingerprint.feed_url,
        parsed_job_count=verification.job_count,
        pagination_complete=verification.pagination_complete,
        stable_id_percent=verification.stable_id_percent,
        direct_url_percent=verification.direct_url_percent,
        notes=notes,
    )


def _html_job_posting_count(body: bytes) -> int:
    return len(_JSONLD_JOB_POSTING.findall(body[:BODY_SCAN_BYTES]))


async def _second_hop_fingerprint(
    payload: HttpPayload, fingerprint: ProviderFingerprint, fetcher: HttpFetcher
) -> tuple[ProviderFingerprint, list[str]]:
    """Follow at most ``SECOND_HOP_LIMIT`` links the careers page publishes.

    The landing page of a JavaScript-driven careers site often names its provider only on a
    deeper page, such as ``jobs.example.com`` that redirects to a board. Following those links is
    still ordinary HTTP against pages the company publishes; a challenge or an error on a hop is
    recorded as evidence and never retried.
    """
    notes: list[str] = []
    for link in _career_links(payload.final_url, payload.body):
        try:
            hop = await fetcher.get(
                RequestSpec(url=link, expected="bytes", timeout_seconds=PROBE_TIMEOUT_SECONDS)
            )
        except ChallengeDetected:
            notes.append(f"linked careers page {link} served an access challenge")
            continue
        except FetchFailure as exc:
            notes.append(f"linked careers page {link} could not be read: {exc}")
            continue
        except PayloadTooLarge:
            notes.append(f"linked careers page {link} exceeded the fetch byte limit")
            continue
        hop_fingerprint = fingerprint_provider(
            final_url=hop.final_url,
            media_type=_media_type(hop.headers),
            body=hop.body,
        )
        if hop_fingerprint.provider is None:
            continue
        return hop_fingerprint.model_copy(
            update={
                "evidence": (
                    f"the official careers page links to {link}, which resolves to "
                    f"{hop.final_url} ({hop_fingerprint.evidence})"
                )
            }
        ), notes
    return fingerprint, notes


async def _verify_provider(
    source: SourceConfig, fingerprint: ProviderFingerprint, fetcher: HttpFetcher
) -> _Verification:
    provider = fingerprint.provider
    if provider is None:
        return _Verification(
            status=SupportStatus.NEEDS_MANUAL_REVIEW,
            notes=["no positive provider evidence on the official careers landing page"],
        )
    if provider == JSONLD_PROVIDER:
        return _Verification(
            status=SupportStatus.HTML_ONLY,
            notes=["careers page publishes application/ld+json JobPosting documents only"],
        )

    # Workday has an adapter, but a Workday source must be scoped (its search caps at 2,000) and
    # only an operator can say which facet is the scope, so the audit gathers evidence and stops;
    # `audit set-source --parameters` promotes a scoped source.
    if provider == WORKDAY_PROVIDER:
        return await _verify_workday(source, fingerprint, fetcher)
    adapter = PROVIDER_ADAPTERS.get(provider)
    if adapter is None:
        return _Verification(
            status=SupportStatus.NEEDS_MANUAL_REVIEW,
            notes=[
                f"{provider} provider evidence found ({fingerprint.evidence}); no adapter exists "
                "yet, so pagination, stable identity, and direct links stay unverified"
            ],
        )

    if fingerprint.feed_url is None:
        return _Verification(
            status=SupportStatus.NEEDS_MANUAL_REVIEW,
            notes=[f"{provider} evidence found but no list endpoint could be derived"],
        )

    candidate = _candidate_source(source, provider, fingerprint)
    try:
        first = await adapter.collect(candidate, fetcher)
        second = await adapter.collect(candidate, fetcher)
    except ChallengeDetected:
        return _Verification(
            status=SupportStatus.BLOCKED,
            notes=[f"{provider} list endpoint served an access challenge"],
        )
    except PayloadTooLarge:
        return _Verification(
            status=SupportStatus.NEEDS_MANUAL_REVIEW,
            notes=[f"{provider} list response exceeded the fetch byte limit"],
        )
    except FetchFailure as exc:
        http_status = _http_status(str(exc))
        return _Verification(
            status=_blocked_status(http_status),
            notes=[f"{provider} list request failed: {exc}"],
        )
    except AdapterError as exc:
        return _Verification(
            status=SupportStatus.NEEDS_MANUAL_REVIEW,
            notes=[f"{provider} adapter could not normalize the board: {exc}"],
        )

    notes: list[str] = []
    try:
        validate_collection(first)
        validate_collection(second)
    except PartialCollectionError:
        notes.append("adapter reported an incomplete collection")

    complete = first.complete and second.complete
    if first.declared_total is not None:
        complete = complete and first.declared_total == len(first.jobs)
    first_keys = sorted(job.canonical_job_key for job in first.jobs)
    second_keys = sorted(job.canonical_job_key for job in second.jobs)
    repeated = first_keys == second_keys
    if not repeated:
        notes.append("repeating the collection changed the canonical job keys")

    stable_id_percent, direct_url_percent = job_metrics(first)
    status = SupportStatus.NEEDS_MANUAL_REVIEW
    if not first.jobs:
        notes.append("board returned no postings, so stable identity and direct links are unproven")
    elif not complete:
        notes.append("board did not prove a complete, repeated pagination walk")
    elif not repeated:
        notes.append("board did not prove repeated no-change behavior")
    elif stable_id_percent != 100.0 or direct_url_percent != 100.0:
        notes.append("some postings lack a stable id or a direct HTTPS application link")
    else:
        status = (
            SupportStatus.SUPPORTED
            if provider in DOCUMENTED_PROVIDERS
            else SupportStatus.SUPPORTED_UNDOCUMENTED
        )
        notes.append(
            f"{provider} adapter collected {len(first.jobs)} postings twice with identical "
            f"canonical keys across {first.page_count} page(s)"
        )

    if provider == "smartrecruiters" and first.jobs:
        notes.append(await _check_smartrecruiters_posting_url(first, fetcher))

    return _Verification(
        status=status,
        job_count=len(first.jobs),
        pagination_complete=complete,
        stable_id_percent=stable_id_percent,
        direct_url_percent=direct_url_percent,
        notes=tuple(notes),
    )


async def _check_smartrecruiters_posting_url(
    result: CollectionResult, fetcher: HttpFetcher
) -> str:
    """Prove the slug-less derived posting URL resolves for one real posting.

    The adapter derives ``https://jobs.smartrecruiters.com/{companyIdentifier}/{postingId}`` from
    the list payload, which publishes no public link. The audit checks one real posting so the
    report can state whether the derived shape still resolves instead of assuming it does.
    """
    posting_url = str(result.jobs[0].posting_url)
    try:
        payload = await fetcher.get(
            RequestSpec(url=posting_url, expected="bytes", timeout_seconds=PROBE_TIMEOUT_SECONDS)
        )
    except FetchFailure as exc:
        return f"derived SmartRecruiters posting URL {posting_url} did not resolve: {exc}"
    return f"derived SmartRecruiters posting URL {posting_url} returned HTTP {payload.status}"


def _candidate_source(
    source: SourceConfig, provider: str, fingerprint: ProviderFingerprint
) -> SourceConfig:
    """The verified source record a provider feed would be collected from."""
    return SourceConfig(
        id=f"{source.company_id}-{provider}",
        company_id=source.company_id,
        provider=provider,
        source_class=PROVIDER_SOURCE_CLASSES.get(provider, SourceClass.PUBLIC_ATS),
        support_status=(
            SupportStatus.SUPPORTED
            if provider in DOCUMENTED_PROVIDERS
            else SupportStatus.SUPPORTED_UNDOCUMENTED
        ),
        endpoint=to_https_url(fingerprint.feed_url or str(source.endpoint)),
        interval_minutes=source.interval_minutes,
        tenant=fingerprint.tenant,
        headers=dict(source.headers),
        enabled=False,
    )


async def _verify_workday(
    source: SourceConfig, fingerprint: ProviderFingerprint, fetcher: HttpFetcher
) -> _Verification:
    """Walk a Workday CXS feed without a Workday adapter.

    Workday serves one page of ``jobPostings`` per POST and declares the board's size as ``total``.
    The audit walks up to ``WORKDAY_PAGE_CAP`` pages twice and compares the posting identities. A
    listing alone is not support: the CXS list payload publishes posting ids (``externalPath``) and
    no application link, so the walk also looks for a per-posting HTTPS application URL, and a
    Workday source only earns ``SUPPORTED_UNDOCUMENTED`` when that evidence is present. It normally
    is not, which is why a Workday sources stays ``NEEDS_MANUAL_REVIEW`` until Task 13's adapter
    supplies the real link shape. Nothing here derives or guesses an application URL.
    """
    feed_url = fingerprint.feed_url
    if feed_url is None or fingerprint.host is None or fingerprint.board is None:
        return _Verification(
            status=SupportStatus.NEEDS_MANUAL_REVIEW,
            notes=["workday evidence found but no CXS site could be derived"],
        )

    notes: list[str] = []
    try:
        first = await _walk_workday(feed_url, fingerprint, fetcher)
        second = await _walk_workday(feed_url, fingerprint, fetcher)
    except ChallengeDetected:
        return _Verification(
            status=SupportStatus.BLOCKED, notes=["workday CXS endpoint served an access challenge"]
        )
    except FetchFailure as exc:
        http_status = _http_status(str(exc))
        return _Verification(
            status=_blocked_status(http_status),
            notes=[f"workday CXS request failed: {exc}"],
        )
    except (ValueError, KeyError, TypeError) as exc:
        return _Verification(
            status=SupportStatus.NEEDS_MANUAL_REVIEW,
            notes=[f"workday CXS response could not be interpreted: {exc}"],
        )

    postings, total, capped = first
    ids = [posting_id for posting_id, _ in postings]
    repeated = ids == [posting_id for posting_id, _ in second[0]]
    complete = total is not None and len(ids) == total and not capped
    stable_id_percent = 100.0 if ids else None
    direct_url_percent = _direct_url_percent(postings)
    status = SupportStatus.NEEDS_MANUAL_REVIEW
    if not ids:
        notes.append("workday board returned no postings, so stable identity is unproven")
    elif not complete:
        declared = "unknown" if total is None else str(total)
        notes.append(
            f"workday CXS confirmed {len(ids)} unique postings of {declared} declared, so "
            "pagination completeness is unproven until a scoped source is promoted with"
            " `audit set-source --parameters`"
        )
    elif not repeated:
        notes.append("repeating the workday walk changed the posting identities")
    elif direct_url_percent != 100.0:
        notes.append(
            "workday CXS publishes posting ids but no direct application URL for any posting, so "
            "the per-posting application-link evidence the audit requires is missing until a "
            "scoped source is promoted with `audit set-source --parameters`"
        )
    else:
        status = SupportStatus.SUPPORTED_UNDOCUMENTED
        notes.append(
            f"workday CXS declared total={total} and served {len(ids)} unique postings twice, "
            "each with a direct HTTPS application URL"
        )

    return _Verification(
        status=status,
        job_count=len(ids),
        pagination_complete=complete,
        stable_id_percent=stable_id_percent,
        direct_url_percent=direct_url_percent,
        notes=tuple(notes),
    )


async def _walk_workday(
    feed_url: str, fingerprint: ProviderFingerprint, fetcher: HttpFetcher
) -> tuple[list[tuple[str, str | None]], int | None, bool]:
    postings: list[tuple[str, str | None]] = []
    total: int | None = None
    capped = False
    for page in range(WORKDAY_PAGE_CAP):
        payload = await fetcher.post(
            RequestSpec(
                url=feed_url,
                expected="json",
                idempotent=True,
                timeout_seconds=PROBE_TIMEOUT_SECONDS,
                json_body={
                    "appliedFacets": {},
                    "limit": WORKDAY_PAGE_SIZE,
                    "offset": page * WORKDAY_PAGE_SIZE,
                    "searchText": "",
                },
            )
        )
        document = json.loads(payload.body)
        if not isinstance(document, dict):
            raise ValueError("CXS response is not a JSON object")
        declared = document.get("total")
        if page == 0 and isinstance(declared, int):
            total = int(declared)
        records = document.get("jobPostings")
        if not isinstance(records, list):
            raise ValueError("CXS response has no jobPostings list")
        if not records:
            break
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("CXS posting is not a JSON object")
            path = record.get("externalPath")
            if not isinstance(path, str) or not path.strip():
                raise ValueError("CXS posting has no externalPath")
            postings.append((path, _posting_url(record)))
        if total is not None and len({posting_id for posting_id, _ in postings}) >= total:
            break
        if page == WORKDAY_PAGE_CAP - 1:
            capped = True
    unique: dict[str, tuple[str, str | None]] = {}
    for posting in postings:
        unique.setdefault(posting[0], posting)
    ordered = list(unique.values())
    return ordered, total, capped and (total is None or len(ordered) < total)


def _posting_url(record: Mapping[str, Any]) -> str | None:
    """A direct application URL the Workday payload itself published, never a derived one."""
    for name in ("externalUrl", "applyUrl", "jobPostingUrl", "url"):
        value = record.get(name)
        if isinstance(value, str) and value.startswith("https://"):
            return value
    return None


def _direct_url_percent(postings: Sequence[tuple[str, str | None]]) -> float | None:
    """Share of postings with a real, provider-published HTTPS application link."""
    if not postings:
        return None
    with_url = sum(1 for _, url in postings if url is not None and url.startswith("https://"))
    return round(100.0 * with_url / len(postings), 2)


# --------------------------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------------------------


def coverage_summary(config: AppConfig) -> CoverageSummary:
    """Count each company once, by the best status any of its sources earned."""
    best: dict[str, SupportStatus] = {}
    audited: set[str] = set()
    for source in config.sources:
        current = best.get(source.company_id)
        if current is None or _rank(source.support_status) < _rank(current):
            best[source.company_id] = source.support_status
        if source.audit_date is not None:
            audited.add(source.company_id)

    by_status = dict.fromkeys(STATUS_PRECEDENCE, 0)
    for company in config.companies:
        status = best.get(company.id, SupportStatus.NEEDS_MANUAL_REVIEW)
        by_status[status] = by_status.get(status, 0) + 1

    total = len(config.companies)
    direct_supported = sum(by_status[status] for status in DIRECT_STATUSES)
    direct_coverage = direct_supported / total if total else 0.0
    return CoverageSummary(
        total_companies=total,
        by_status=by_status,
        direct_supported=direct_supported,
        direct_coverage_percent=round(100.0 * direct_coverage, 2),
        broad_activation_allowed=direct_coverage >= BROAD_ACTIVATION_THRESHOLD,
        unaudited_companies=[
            company.id for company in config.companies if company.id not in audited
        ],
    )


def _rank(status: SupportStatus) -> int:
    return STATUS_PRECEDENCE.index(status)


def company_sort_key(company: CompanyTarget) -> tuple[int, str, str]:
    """Deterministic category/name order for the audit walk."""
    ranks = [
        CATEGORY_ORDER.index(category) if category in CATEGORY_ORDER else len(CATEGORY_ORDER)
        for category in company.categories
    ]
    return (min(ranks) if ranks else len(CATEGORY_ORDER), company.name.casefold(), company.id)


# --------------------------------------------------------------------------------------------
# Registry file I/O
# --------------------------------------------------------------------------------------------


def load_source_records(path: Path) -> list[SourceConfig]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise AuditError(f"cannot read {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise AuditError(f"{path} must contain a YAML mapping")
    raw = document.get("sources") or []
    if not isinstance(raw, list):
        raise AuditError(f"{path} must contain a sources list")
    records: list[SourceConfig] = []
    for index, entry in enumerate(raw):
        try:
            records.append(SourceConfig.model_validate(entry))
        except ValidationError as exc:
            raise AuditError(f"{path} source entry {index} is invalid: {exc}") from exc
    return records


def write_sources_yaml(path: Path, sources: Sequence[SourceConfig]) -> None:
    """Write the registry with a stable key order through an atomic temp-file rename."""
    payload = {"sources": [_serialize_source(source) for source in sources]}
    try:
        text = SOURCES_HEADER + yaml.safe_dump(
            payload, sort_keys=False, default_flow_style=False, allow_unicode=True, width=1000
        )
    except yaml.YAMLError as exc:  # pragma: no cover - defensive, the payload is plain data
        raise AuditError(f"cannot serialize {path}: {exc}") from exc

    _atomic_write(path, text)


def _atomic_write(path: Path, text: str) -> None:
    """Replace ``path`` through a temp-file rename, with the mode an ordinary write would give.

    ``mkstemp`` creates its file ``0600``, and the rename would carry that onto the registry, which
    the deployed container reads as a different user.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    umask = os.umask(0)
    os.umask(umask)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        temporary.chmod(0o666 & ~umask)
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise AuditError(f"cannot write {path}: {exc}") from exc


def _serialize_source(source: SourceConfig) -> dict[str, Any]:
    try:
        validated = SourceConfig.model_validate(source.model_dump(mode="json"))
    except ValidationError as exc:
        raise AuditError(f"source {source.id} is not writable: {exc}") from exc
    dump = validated.model_dump(mode="json")
    record: dict[str, Any] = {}
    for key in SOURCE_KEY_ORDER:
        value = dump.get(key)
        if value is None or value == {} or value == []:
            continue
        record[key] = value
    return record


def replace_source_record(path: Path, source: SourceConfig) -> list[SourceConfig]:
    """Replace a company's discovery record with a verified record, keeping one per company."""
    records = load_source_records(path)
    target = source.company_id
    index = next(
        (position for position, record in enumerate(records) if record.company_id == target),
        None,
    )
    if index is None:
        updated = [*records, source]
    else:
        updated = [*records[:index], source, *records[index + 1 :]]
    write_sources_yaml(path, updated)
    return updated


def disable_source_record(
    path: Path,
    *,
    source_id: str,
    status: SupportStatus,
    reason: str,
    evidence: Mapping[str, Any] | None = None,
    audit_date: str | None = None,
) -> SourceConfig:
    """Disable one source and keep the reason and evidence in its registry parameters."""
    records = load_source_records(path)
    index = next(
        (position for position, record in enumerate(records) if record.id == source_id), None
    )
    if index is None:
        raise AuditError(f"unknown source id {source_id}")
    record = records[index]
    parameters: dict[str, Any] = dict(record.parameters)
    parameters["audit_status"] = str(status)
    parameters["audit_reason"] = reason
    for name, value in (evidence or {}).items():
        parameters[f"audit_{name}"] = value
    disabled = record.model_copy(
        update={
            "support_status": status,
            "enabled": False,
            "parameters": parameters,
            "audit_date": audit_date or now_utc().date().isoformat(),
        }
    )
    records[index] = disabled
    write_sources_yaml(path, records)
    return disabled


# --------------------------------------------------------------------------------------------
# Persisted audit state
# --------------------------------------------------------------------------------------------


def load_audit_store(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"cannot read {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise AuditError(f"{path} must contain a JSON object")
    companies = document.get("companies") or {}
    if not isinstance(companies, dict):
        raise AuditError(f"{path} must contain a companies mapping")
    return {str(key): value for key, value in companies.items() if isinstance(value, dict)}


def save_audit_store(path: Path, companies: Mapping[str, Any], *, generated_at: datetime) -> None:
    payload = AuditStore(generated_at=generated_at, companies=dict(companies)).model_dump(
        mode="json"
    )
    text = json.dumps(payload, indent=2, sort_keys=False) + "\n"
    _atomic_write(path, text)


def audit_record(
    company: CompanyTarget,
    source: SourceConfig,
    result: ProbeResult,
    *,
    previous: Mapping[str, Any] | None = None,
    landing_source: SourceConfig | None = None,
) -> dict[str, Any]:
    careers_url = str((landing_source or source).endpoint)
    if previous is not None and _classify_url(careers_url) is not None:
        # Re-probing a verified source reads the official landing page, never its provider feed, and
        # a stored landing page is never replaced by an API endpoint.
        stored = previous.get("careers_url")
        if isinstance(stored, str) and stored:
            careers_url = stored
    return {
        "company_id": company.id,
        "name": company.name,
        "categories": list(company.categories),
        "source_id": source.id,
        # The stored provider is the fresh verdict, never the superseded record's provider: a probe
        # that found no evidence reports the neutral discovery provider.
        "provider": source.provider,
        "support_status": str(source.support_status),
        "careers_url": careers_url,
        "audited_at": result.evidence_at.date().isoformat(),
        # The probe's identity fields must name the source being recorded, not the pre-rename
        # source the probe was handed, so no persisted value can cite an id absent from the
        # registry. Every other ProbeResult field is evidence observed at the probed URL.
        "probe": result.model_copy(update={"source_id": source.id}).model_dump(mode="json"),
    }


def probe_target(source: SourceConfig, record: Mapping[str, Any] | None) -> SourceConfig:
    """The source a probe should read: the official careers landing page the audit recorded.

    A verified registry record points at the provider's list endpoint, so re-probing that record
    would send an ordinary GET to an API. The recorded landing page is what the probe reads instead.
    """
    if record is None:
        return source
    careers_url = record.get("careers_url")
    if not isinstance(careers_url, str) or not careers_url.strip():
        return source
    if careers_url == str(source.endpoint):
        return source
    return SourceConfig(
        id=source.id,
        company_id=source.company_id,
        provider=source.provider,
        source_class=source.source_class,
        support_status=source.support_status,
        endpoint=to_https_url(careers_url),
        interval_minutes=source.interval_minutes,
        tenant=source.tenant,
        parameters=dict(source.parameters),
        headers=dict(source.headers),
        enabled=source.enabled,
        audit_date=source.audit_date,
    )


def _tier_interval(source: SourceConfig, source_class: SourceClass) -> int:
    """The interval a re-probed record polls at.

    A record that changes class takes the new class's tier, so a discovery record verified as an API
    does not keep the fallback cadence. A record that keeps its class keeps its interval, which may
    be an operator's deliberate back-off, but never polls faster than the tier.
    """
    tier = TIER_INTERVAL_MINUTES[source_class]
    if source.source_class is not source_class:
        return tier
    return max(source.interval_minutes, tier)


def _updated_source(
    source: SourceConfig,
    company: CompanyTarget,
    result: ProbeResult,
    *,
    taken_ids: Iterable[str] = (),
) -> SourceConfig:
    """Fold one probe verdict back into the company's registry record.

    A fresh probe replaces the earlier verdict instead of merging with it. When the provider is
    unchanged the record keeps its identity and its non-audit parameters, so the only difference is
    the audit fields; when the provider is superseded - a different provider, or no provider at all
    - the whole source identity is replaced: provider, tenant, source id, source class, endpoint,
    and a parameters mapping that carries only the new provider's keys plus the human notes.
    """
    fresh = result.provider_fingerprint
    verified = (
        result.support_status in DIRECT_STATUSES
        and fresh is not None
        and result.provider_feed_url is not None
    )
    superseded = fresh is None or fresh != source.provider
    if superseded:
        parameters: dict[str, Any] = {
            key: value
            for key, value in source.parameters.items()
            if key in HUMAN_PARAMETERS and key not in PROBE_EVIDENCE_PARAMETERS
        }
    else:
        parameters = {
            key: value
            for key, value in source.parameters.items()
            if key not in AUDIT_VERDICT_PARAMETERS
        }

    source_id = source.id
    endpoint = _registry_endpoint(source, result, verified=verified)
    source_class = source.source_class
    provider = fresh or DISCOVERY_PROVIDER
    derived_tenant = _fingerprint_tenant(result) if fresh is not None else None
    # A superseded identity always takes the tenant of the probe that just ran, so the previous
    # provider's board token can never be resurrected; an unchanged provider keeps its own tenant
    # untouched.
    tenant = derived_tenant if superseded else source.tenant
    if fresh is None:
        # No positive provider evidence: the record goes back to the neutral discovery shape and
        # keeps only the landing page it was just probed at.
        source_class = SourceClass.FALLBACK
        source_id = _renamed_source_id(company.id, DISCOVERY_PROVIDER, source.id, taken_ids)
    elif verified:
        source_class = PROVIDER_SOURCE_CLASSES.get(provider, SourceClass.PUBLIC_ATS)
        source_id = _renamed_source_id(company.id, provider, source.id, taken_ids, superseded)
    else:
        # A provider the probe recognised but could not verify keeps the landing page as its
        # endpoint, so the registry still names where the evidence came from.
        source_class = (
            source.source_class if source.provider == provider else SourceClass.FALLBACK
        )
        source_id = _renamed_source_id(company.id, provider, source.id, taken_ids, superseded)

    parameters["audit_status"] = str(result.support_status)
    parameters["audit_probed_at"] = result.evidence_at.date().isoformat()
    if result.provider_evidence:
        parameters["audit_evidence"] = result.provider_evidence
    if fresh is not None and not verified:
        # A provider the probe recognised but could not yet verify still belongs in the registry,
        # as evidence for the adapter task that will finish the job.
        parameters["audit_provider"] = fresh
        if result.provider_feed_url:
            parameters["audit_endpoint"] = result.provider_feed_url
    if result.http_status is not None:
        parameters["audit_http_status"] = result.http_status
    if result.final_url:
        parameters["audit_final_url"] = result.final_url
    if result.notes:
        parameters["audit_notes"] = list(result.notes)
    if provider == WORKDAY_PROVIDER and verified:
        parameters.setdefault("page_size", WORKDAY_PAGE_SIZE)
        parameters.setdefault("locale", "en-US")
        parameters.setdefault(
            "detail_base_url", f"https://{_fingerprint_host(result)}/en-US/{_fingerprint_board(result)}"
        )
    return SourceConfig(
        id=source_id,
        company_id=source.company_id,
        provider=provider,
        source_class=source_class,
        support_status=result.support_status,
        endpoint=to_https_url(endpoint),
        interval_minutes=_tier_interval(source, source_class),
        tenant=tenant,
        parameters=parameters,
        headers=dict(source.headers),
        enabled=verified,
        audit_date=result.evidence_at.date().isoformat(),
    )


def _registry_endpoint(source: SourceConfig, result: ProbeResult, *, verified: bool) -> str:
    """The endpoint a record should carry after a probe.

    A verified source points at its provider feed. Otherwise the record keeps the official careers
    page the registry already audited, and only a record that still pointed at a provider endpoint
    (one an earlier probe verified) is reset to the landing page the probe just read.
    """
    if verified:
        return result.provider_feed_url or str(source.endpoint)
    current = str(source.endpoint)
    if _classify_url(current) is None:
        return current
    return result.final_url or current


def _renamed_source_id(
    company_id: str,
    provider: str,
    current_id: str,
    taken_ids: Iterable[str],
    superseded: bool = True,
) -> str:
    """The source id a probe verdict implies.

    A superseded identity is renamed to ``<company-id>-<provider>`` whatever the previous id was, so
    an id can never keep naming a provider the record no longer uses. An unchanged provider keeps
    the id it already has.
    """
    if not superseded:
        return current_id
    candidate = f"{company_id}-{provider}"
    if candidate == current_id or candidate in set(taken_ids):
        return current_id
    return candidate


def _fingerprint_tenant(result: ProbeResult) -> str | None:
    return _probe_fingerprint_field(result, "tenant")


def _fingerprint_host(result: ProbeResult) -> str:
    return _probe_fingerprint_field(result, "host") or ""


def _fingerprint_board(result: ProbeResult) -> str:
    return _probe_fingerprint_field(result, "board") or ""


def _probe_fingerprint_field(result: ProbeResult, name: str) -> str | None:
    """The fingerprint's own detail, re-derived from the recorded feed URL when needed."""
    feed = result.provider_feed_url
    if feed is None:
        return None
    if name == "host":
        return urlsplit(feed).netloc
    parts = urlsplit(feed)
    path = parts.path
    if result.provider_fingerprint == WORKDAY_PROVIDER:
        match = re.search(r"/wday/cxs/(?P<tenant>[^/]+)/(?P<site>[^/]+)/jobs", path)
        if match is not None:
            return match.group("tenant") if name == "tenant" else match.group("site")
        return None
    if name == "tenant":
        if result.provider_fingerprint == "greenhouse":
            return _first_match(path, r"/v1/boards/([^/]+)/jobs")
        if result.provider_fingerprint == "lever":
            return _first_match(path, r"/v0/postings/([^/]+)")
        if result.provider_fingerprint == "ashby":
            return _first_match(path, r"/job-board/([^/]+)")
        if result.provider_fingerprint == "smartrecruiters":
            return _first_match(path, r"/v1/companies/([^/]+)/postings")
    return None


# --------------------------------------------------------------------------------------------
# The runner
# --------------------------------------------------------------------------------------------


async def run_audit(
    config_dir: Path,
    results_path: Path = DEFAULT_RESULTS_PATH,
    *,
    limit: int | None = None,
    company_ids: Sequence[str] = (),
    category: str | None = None,
    concurrency: int = DEFAULT_AUDIT_CONCURRENCY,
    budget_seconds: float = COMPANY_PROBE_BUDGET_SECONDS,
    force: bool = False,
    retry_failed: bool = False,
    on_result: Callable[[ProbeResult], None] | None = None,
) -> list[str]:
    """Probe unaudited companies in bounded, resumable chunks.

    Each company that finishes is written to the results file and folded into
    ``config/sources.yaml`` immediately, so re-running continues with the companies that are still
    missing. ``limit`` bounds one chunk and ``company_ids``/``category`` select a slice. ``force``
    re-probes companies that already have a record, and ``retry_failed`` re-probes the recorded
    companies whose verdict was a failure (blocked or needing manual review), which is how a later
    run revisits access challenges and transient network errors.
    """
    if concurrency < 1:
        raise AuditError("concurrency must be at least one")
    concurrency = min(concurrency, MAX_AUDIT_CONCURRENCY)
    if limit is not None and limit < 1:
        raise AuditError("limit must be at least one")

    config = load_app_config(config_dir)
    sources_path = config_dir / "sources.yaml"
    records = load_source_records(sources_path)
    store = load_audit_store(results_path)
    companies = sorted(config.companies, key=company_sort_key)
    sources_by_company = {source.company_id: source for source in records}

    selected: list[CompanyTarget] = []
    for company in companies:
        recorded = store.get(company.id)
        if recorded is not None and not (force or (retry_failed and _failed_record(recorded))):
            continue
        if company_ids and company.id not in company_ids:
            continue
        if category is not None and category not in company.categories:
            continue
        if company.id not in sources_by_company:
            continue
        selected.append(company)
    if limit is not None:
        selected = selected[:limit]

    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(concurrency)
    probed: list[str] = []

    async with HttpFetcher() as fetcher:

        async def probe_company(company: CompanyTarget) -> None:
            recorded = store.get(company.id)
            source = sources_by_company[company.id]
            target = probe_target(source, recorded)
            async with semaphore:
                result = await _probe_with_budget(target, fetcher, budget_seconds)
            async with lock:
                probed.append(company.id)
                taken_ids = [
                    item.id for item in records if item.company_id != company.id
                ]
                updated_source = _updated_source(
                    source, company, result, taken_ids=taken_ids
                )
                store[company.id] = audit_record(
                    company,
                    updated_source,
                    result,
                    previous=store.get(company.id),
                    landing_source=target,
                )
                records[:] = [
                    updated_source if record.company_id == company.id else record
                    for record in records
                ]
                save_audit_store(results_path, store, generated_at=now_utc())
                write_sources_yaml(sources_path, records)
            if on_result is not None:
                on_result(result)

        await asyncio.gather(*(probe_company(company) for company in selected))

    return probed


def _failed_record(record: Mapping[str, Any]) -> bool:
    """Whether a recorded verdict is a failure worth re-probing."""
    status = str(record.get("support_status"))
    if status in {SupportStatus.BLOCKED, SupportStatus.NEEDS_MANUAL_REVIEW}:
        return True
    notes = record.get("probe", {}).get("notes", [])
    failure_markers = ("request failed", "timed out", "access challenge", "could not be read")
    return any(marker in str(note) for note in notes for marker in failure_markers)


async def _probe_with_budget(
    source: SourceConfig, fetcher: HttpFetcher, budget_seconds: float
) -> ProbeResult:
    try:
        async with asyncio.timeout(budget_seconds):
            return await probe_source(source, fetcher)
    except TimeoutError:
        return ProbeResult(
            source_id=source.id,
            support_status=SupportStatus.NEEDS_MANUAL_REVIEW,
            evidence_at=now_utc(),
            final_url=str(source.endpoint),
            notes=[f"probe exceeded its {budget_seconds:g}s budget and was stopped"],
        )
    except Exception as exc:  # noqa: BLE001 - a failed probe is evidence, not a stopped audit
        return ProbeResult(
            source_id=source.id,
            support_status=SupportStatus.NEEDS_MANUAL_REVIEW,
            evidence_at=now_utc(),
            final_url=str(source.endpoint),
            notes=[f"probe failed with {type(exc).__name__}: {exc}"],
        )


def source_for_company(config: AppConfig, company_id: str) -> SourceConfig:
    for source in config.sources:
        if source.company_id == company_id:
            return source
    raise AuditError(f"unknown company id {company_id}")


def record_probe(
    config_dir: Path, results_path: Path, source: SourceConfig, result: ProbeResult
) -> None:
    """Fold one probe into the persisted results and the source registry."""
    config = load_app_config(config_dir)
    company = next(
        (item for item in config.companies if item.id == source.company_id), None
    )
    if company is None:
        raise AuditError(f"unknown company id {source.company_id}")
    sources_path = config_dir / "sources.yaml"
    records = load_source_records(sources_path)
    taken_ids = [record.id for record in records if record.company_id != company.id]
    previous = load_audit_store(results_path).get(company.id)
    target = probe_target(source, previous)
    registry_source = next(
        (record for record in records if record.company_id == company.id), source
    )
    updated_source = _updated_source(registry_source, company, result, taken_ids=taken_ids)
    updated = [
        updated_source if record.company_id == company.id else record
        for record in records
    ]
    store = load_audit_store(results_path)
    store[company.id] = audit_record(
        company,
        updated_source,
        result,
        previous=previous,
        landing_source=target,
    )
    save_audit_store(results_path, store, generated_at=now_utc())
    write_sources_yaml(sources_path, updated)


def source_by_id(config: AppConfig, source_id: str) -> SourceConfig:
    for source in config.sources:
        if source.id == source_id:
            return source
    raise AuditError(f"unknown source id {source_id}")


async def validate_source_endpoint(source: SourceConfig, fetcher: HttpFetcher) -> CollectionResult:
    """Validate a proposed endpoint with its provider adapter before it replaces a record."""
    adapter = PROVIDER_ADAPTERS.get(source.provider)
    if adapter is None:
        raise AuditError(
            f"provider {source.provider} has no adapter, so its endpoint cannot be validated"
        )
    try:
        return validate_collection(await adapter.collect(source, fetcher))
    except AdapterError as exc:
        raise AuditError(f"endpoint validation failed for {source.id}: {exc}") from exc
    except (ChallengeDetected, FetchFailure, PayloadTooLarge) as exc:
        raise AuditError(f"endpoint validation failed for {source.id}: {exc}") from exc


# --------------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------------


def render_report(
    config: AppConfig, store: Mapping[str, Mapping[str, Any]], *, generated_at: datetime
) -> tuple[str, dict[str, Any]]:
    """Render the Markdown audit report and the machine-readable companion document."""
    summary = coverage_summary(config)
    sources_by_company = {source.company_id: source for source in config.sources}
    gate = "passes" if summary.broad_activation_allowed else "does not pass"

    lines: list[str] = [
        "# Company Source Audit",
        "",
        f"Generated {generated_at.date().isoformat()} from the persisted audit records in "
        "`reports/source-audit.json`. Every row below was produced by an ordinary HTTPS probe of "
        "the company's official careers page: no browser, no challenge evasion, no credentials.",
        "",
        "## Coverage summary",
        "",
        f"- Companies audited: {summary.total_companies - len(summary.unaudited_companies)} of "
        f"{summary.total_companies}",
        f"- Direct coverage (SUPPORTED or SUPPORTED_UNDOCUMENTED): "
        f"{summary.direct_supported}/{summary.total_companies} = "
        f"{summary.direct_coverage_percent:.2f}%, so the 90% broad-activation gate {gate}",
        "",
        "| Status | Companies |",
        "| --- | --- |",
    ]
    for status in STATUS_PRECEDENCE:
        lines.append(f"| {status} | {summary.by_status.get(status, 0)} |")
    lines.extend(
        [
            "",
            "## How a status is decided",
            "",
            "- `SUPPORTED`: a provider adapter collected a complete, repeatable listing from a "
            "documented public endpoint, with stable posting ids and direct HTTPS application "
            "links.",
            "- `SUPPORTED_UNDOCUMENTED`: the same evidence from a public endpoint that the "
            "provider does not document.",
            "- `HTML_ONLY`: the careers page publishes structured job documents (JSON-LD "
            "`JobPosting`) and no feed, so it needs an HTML adapter.",
            "- `FALLBACK_ONLY`: the official careers page answered, but publishes no provider feed "
            "and no machine-readable job document over ordinary HTTP, so covering it needs a "
            "fallback source.",
            "- `BLOCKED`: the page refused access or served a challenge at audit time. The audit "
            "never attempts to defeat a challenge.",
            "- `NEEDS_MANUAL_REVIEW`: the evidence was ambiguous, the page could not be read, or a "
            "recognised enterprise provider (Workday, SuccessFactors, iCIMS, Eightfold, Oracle) "
            "has no adapter yet, so completeness, stable identity, and direct links stay "
            "unproven.",
            "",
            "An HTTP 200 alone never earns a supported status, and no company is assigned a "
            "provider from reputation or from an unverified third-party directory.",
            "",
        ]
    )

    if summary.unaudited_companies:
        outstanding = ", ".join(f"`{company_id}`" for company_id in summary.unaudited_companies)
        lines.extend(
            [
                "## Outstanding companies",
                "",
                f"Not yet probed, so they stay `NEEDS_MANUAL_REVIEW` with no audit date: "
                f"{outstanding}.",
                "",
            ]
        )

    lines.extend(["## Carried-over audit obligations", ""])
    smartrecruiters = [
        (company_id, entry)
        for company_id, entry in sorted(store.items())
        if isinstance(entry, Mapping) and entry.get("provider") == "smartrecruiters"
    ]
    if smartrecruiters:
        for company_id, entry in smartrecruiters:
            notes = [
                str(note)
                for note in entry.get("probe", {}).get("notes", [])
                if "SmartRecruiters posting URL" in str(note)
            ]
            lines.append(
                f"- `{company_id}`: "
                + (";".join(notes) if notes else "no derived posting URL was checked")
            )
    else:
        lines.append("- No company resolved to SmartRecruiters during this audit.")
    for company_id in CARRIED_OVER_COMPANIES:
        carried = store.get(company_id)
        if not isinstance(carried, Mapping):
            lines.append(f"- `{company_id}`: not probed yet, so the obligation stays open")
            continue
        probe = carried.get("probe", {})
        final_url = probe.get("final_url")
        lines.append(
            f"- `{company_id}`: {carried.get('support_status')} from {final_url} "
            f"(HTTP {probe.get('http_status')}, {carried.get('audited_at')})"
        )
    lines.append("")

    lines.extend(
        [
            "## Company detail",
            "",
            "| Company | Categories | Careers URL | Provider | Class | Status | Jobs | Evidence "
            "date | Poll interval (min) | Limitations |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    rows: list[dict[str, Any]] = []
    for company in sorted(config.companies, key=company_sort_key):
        source = sources_by_company.get(company.id)
        record = store.get(company.id)
        probe = record.get("probe", {}) if isinstance(record, Mapping) else {}
        notes = [str(note) for note in probe.get("notes", [])] if probe else []
        provider = (
            str(record.get("provider"))
            if record is not None
            else (source.provider if source is not None else "unknown")
        )
        status_name = (
            str(record.get("support_status"))
            if record is not None
            else (str(source.support_status) if source is not None else "NEEDS_MANUAL_REVIEW")
        )
        jobs = probe.get("parsed_job_count") if probe else None
        evidence_date = str(record.get("audited_at")) if record is not None else ""
        class_name = str(source.source_class) if source is not None else ""
        interval = source.interval_minutes if source is not None else ""
        # The Careers URL column is the official landing page the company publishes, which is what
        # `audit_record` stores. A verified source's own endpoint is the provider feed, so it must
        # never replace the landing page here.
        careers_url = str(source.endpoint) if source is not None else ""
        if isinstance(record, Mapping):
            stored = record.get("careers_url") or record.get("probe", {}).get("final_url")
            if stored:
                careers_url = str(stored)
        if not notes:
            notes = ["not yet audited"] if record is None else []
        rows.append(
            {
                "company_id": company.id,
                "name": company.name,
                "categories": list(company.categories),
                "careers_url": careers_url,
                "provider": provider,
                "source_class": class_name,
                "status": status_name,
                "job_count": jobs,
                "evidence_date": evidence_date,
                "interval_minutes": interval,
                "limitations": notes,
                "probe": probe,
            }
        )
        lines.append(
            "| {name} | {categories} | {url} | {provider} | {class_name} | {status} | {jobs} | "
            "{date} | {interval} | {notes} |".format(
                name=company.name,
                categories=", ".join(company.categories),
                url=careers_url,
                provider=provider,
                class_name=class_name,
                status=status_name,
                jobs=jobs if jobs is not None else "",
                date=evidence_date or "",
                interval=interval,
                notes="; ".join(notes).replace("|", "/") or "",
            )
        )
    lines.append("")

    payload = {
        "generated_at": generated_at.isoformat(),
        "coverage": summary.model_dump(mode="json"),
        "companies": {company_id: dict(record) for company_id, record in store.items()},
        "rows": [
            {key: value for key, value in row.items() if key != "probe"} for row in rows
        ],
    }
    return "\n".join(lines) + "\n", payload
