import contextvars
import os
import re
import threading
from urllib.parse import urlparse
import httpx
from supabase import create_client
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from taxonomy import TAXONOMY

load_dotenv()

# Minimum pair score for two companies to be saved as competitors.
# Validation showed 0.75-0.84 pairs are mostly false positives; 0.85+ all held up.
COMPETITOR_THRESHOLD = 0.85

# Set True by main.ingest() for the duration of a single interactive ingest (web
# UI / CLI), read by competitor.py and extractor.py to pick a tighter Mistral
# retry/timeout budget than the batch/backfill scripts use. Lives here (not in
# main.py) because both competitor.py and extractor.py already import from
# storage.py -- main.py importing back from either would be circular.
# asyncio.to_thread() propagates the calling coroutine's contextvars.Context into
# the worker thread, so a flag set in ingest() before its to_thread() call is
# visible inside _ingest_sync() -> extract()/compare() with no parameter threaded
# through every intermediate function signature.
INTERACTIVE_REQUEST: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "interactive_request", default=False
)

# Shared interactive-vs-batch Mistral retry/timeout config, read by both
# competitor.py's and extractor.py's chat-call dispatch (Story 5.2). Defined once
# here rather than duplicated per module, so a future budget tuning can't be
# applied to one file and silently miss the other. The batch-side wait/stop
# config stays local to each module (competitor.py's _retry, extractor.py's
# @retry(...)) -- factoring that one too is Story 5.3's separately-scoped job.
RETRY_INTERACTIVE_WAIT = wait_exponential(multiplier=2, min=4, max=20)
RETRY_INTERACTIVE_STOP = stop_after_attempt(3)
BATCH_TIMEOUT_MS = 120_000
INTERACTIVE_TIMEOUT_MS = 30_000

# Same rationale as competitor.py's Mistral retry: transient connection-level
# failures (e.g. RemoteProtocolError from a dropped HTTP/2 stream) shouldn't
# abort a whole ingest run -- retry them instead. Scoped to GET/HEAD only (see
# _execute below) on the general "only retry idempotent requests" principle --
# NOT because it mirrors postgrest-py's own retry: the installed postgrest-py
# (2.30.1) retries a different failure class entirely (HTTP 503/520 responses,
# not httpx.TransportError), and its own idempotency check has a bug (compares
# against the literal string "HTTP", not "HEAD" -- verified in
# postgrest/base_request_builder.py:102), so it doesn't actually retry HEAD
# either. Coincidentally similar scoping, unrelated mechanism.
_retry = retry(
    retry=retry_if_exception(lambda exc: isinstance(exc, httpx.TransportError)),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    stop=stop_after_attempt(5),
    reraise=True,
)


@_retry
def _execute_retryable(query):
    return query.execute()


def _execute(query):
    """Run a postgrest query. Retries transient httpx.TransportError, but only for
    idempotent (GET/HEAD) requests -- a write (POST/PATCH/DELETE) is not retried.

    Retrying a write risks a silent duplicate: httpx.RemoteProtocolError (a
    TransportError subtype) can fire after the server already committed the
    request but before the response was read back -- an "in-doubt write". Since
    quality_review_log deliberately has no unique constraint on
    (review_type, subject) (history, not dedup -- see Story 1.1), a retried
    duplicate insert would be silently indistinguishable from a real one. A
    visible failure the caller can re-run manually is safer than that.
    """
    if query.request.http_method in ("GET", "HEAD"):
        return _execute_retryable(query)
    return query.execute()

# quality_review_log contract (AD-4, AD-7): review_type is a small, fixed,
# application-validated vocabulary -- not a DB enum, so scraping_diagnostic's
# still-evolving failure-type verdicts aren't blocked by a premature schema constraint.
# See ARCHITECTURE-SPINE.md AD-1/AD-2/AD-4/AD-7/AD-8 for the full rationale.
_KNOWN_REVIEW_TYPES = {"taxonomy_split", "scraping_diagnostic", "redundant_uncategorized_cleanup", "empty_subsectors_backfill"}
_TAXONOMY_SPLIT_VERDICTS = {"isolated mis-tag", "structural gap", "scraping artifact", "ambiguous"}
_TAXONOMY_SUBSECTORS = {sub for sector_subs in TAXONOMY.values() for sub in sector_subs}


_supabase = None
_supabase_lock = threading.Lock()


def _client():
    """Lazily create and cache a single Supabase client. Locked because
    graph_app.py's sync route handlers run concurrently in Starlette's thread
    pool -- without the lock, two threads racing on the very first call could
    each construct a client, one silently discarded. httpx.Client itself is
    documented thread-safe, so the cached client is safe to share once built;
    only the lazy-init check+set needed synchronizing.
    """
    global _supabase
    if _supabase is None:
        with _supabase_lock:
            if _supabase is None:
                key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_KEY"]
                _supabase = create_client(os.environ["SUPABASE_URL"], key)
    return _supabase


def normalize_domain(url: str) -> str:
    """Extract and normalize just the domain from a URL: strip scheme, userinfo,
    path, query, port, IPv6 brackets, and leading www.; lowercase. AD-8 -- the
    one place domain normalization happens, so quality_review_log lookups never
    silently miss an entry because two URLs of the same site (e.g. with
    different paths, or one missing a scheme) produce different subjects.
    Uses urlparse().hostname (not .netloc) so userinfo/port/IPv6 are stripped
    correctly rather than hand-rolled -- .netloc alone mishandles all three.
    """
    url = url.strip()
    if url.startswith("//"):
        # Protocol-relative ("//example.com/path") -- prepending "https://" would
        # produce "https:////..." (a malformed URL whose .hostname is None); only
        # the scheme itself is missing here, not the "//" authority marker.
        url = "https:" + url
    elif not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", url):
        # Checks whether url STARTS WITH a scheme, not whether "://" appears
        # anywhere in it -- a scheme-less URL with a redirect/next-URL query
        # param (e.g. "example.com/path?redirect=http://other.com") contains
        # "://" but isn't itself schemed, and "://" not in url would wrongly
        # skip prepending "https://", leaving urlparse() unable to find a host.
        url = "https://" + url
    netloc = urlparse(url).hostname or ""
    if netloc.startswith("www."):
        netloc = netloc[len("www."):]
    # Strip a trailing "." (valid absolute-FQDN DNS notation, e.g. "example.com.")
    # so it normalizes identically to "example.com".
    return netloc.rstrip(".")


def _validate_review(review_type: str, subject: str, verdict: str) -> str:
    """Validate the quality_review_log write contract (AD-4, AD-7).

    Returns the normalized subject. Raises ValueError on any violation.
    No I/O -- safe to unit test without a Supabase connection.
    """
    if review_type not in _KNOWN_REVIEW_TYPES:
        raise ValueError(f"Unknown review_type: {review_type!r}. Must be one of {sorted(_KNOWN_REVIEW_TYPES)}")

    if not verdict or not verdict.strip():
        raise ValueError("verdict must not be blank")

    if review_type == "taxonomy_split":
        # Exact subsector match, no normalization -- deliberately opposite
        # leniency from scraping_diagnostic below: these are different kinds
        # of identifier (a closed taxonomy vocabulary vs. a free-form URL).
        if subject not in _TAXONOMY_SUBSECTORS:
            raise ValueError(f"subject must be an exact TAXONOMY subsector name for taxonomy_split, got {subject!r}")
        if verdict not in _TAXONOMY_SPLIT_VERDICTS:
            raise ValueError(f"verdict must be one of {sorted(_TAXONOMY_SPLIT_VERDICTS)} for taxonomy_split, got {verdict!r}")
        return subject

    if review_type in ("redundant_uncategorized_cleanup", "empty_subsectors_backfill"):
        # subject is a compspro startup name, not a domain -- no normalization
        if not subject or not subject.strip():
            raise ValueError("subject must not be blank")
        return subject.strip()

    # scraping_diagnostic: subject is a normalized domain, verdict is free text (AD-4)
    normalized = normalize_domain(subject)
    if not normalized:
        raise ValueError(f"could not extract a domain from subject: {subject!r}")
    return normalized


def save_quality_review(
    review_type: str,
    subject: str,
    verdict: str,
    source_snapshot: dict | None = None,
    resolution: str | None = None,
    notes: str | None = None,
) -> dict:
    """Insert a row into quality_review_log. Validates the review_type/subject/verdict
    contract before touching the database (AD-4, AD-7) -- extends the single Supabase
    access point in this module (AD-1, AD-2).
    """
    subject = _validate_review(review_type, subject, verdict)

    payload = {"review_type": review_type, "subject": subject, "verdict": verdict}
    if source_snapshot is not None:
        payload["source_snapshot"] = source_snapshot
    if resolution is not None:
        payload["resolution"] = resolution
    if notes is not None:
        payload["notes"] = notes

    client = _client()
    response = _execute(client.table("quality_review_log").insert(payload))
    return response.data[0] if response.data else {}


def get_quality_reviews(review_type: str, subject: str | None = None) -> list[dict]:
    """Query quality_review_log by review_type (+ optional subject, normalized
    the same way as writes). Validates review_type/subject as strictly as the
    write path (AD-7) -- a typo here should raise, not silently return [].
    """
    if review_type not in _KNOWN_REVIEW_TYPES:
        raise ValueError(f"Unknown review_type: {review_type!r}. Must be one of {sorted(_KNOWN_REVIEW_TYPES)}")

    if subject is not None:
        if review_type == "taxonomy_split":
            if subject not in _TAXONOMY_SUBSECTORS:
                raise ValueError(f"subject must be an exact TAXONOMY subsector name for taxonomy_split, got {subject!r}")
        else:
            subject = normalize_domain(subject)
            if not subject:
                raise ValueError(f"could not extract a domain from subject: {subject!r}")

    client = _client()
    query = client.table("quality_review_log").select("*").eq("review_type", review_type)
    if subject is not None:
        query = query.eq("subject", subject)
    return _execute(query).data or []


def get_by_subsectors(
    subsectors: list[str],
    sectors: list[str],
    exclude_name: str,
    sub_subsectors: list[str] = [],
) -> list[dict]:
    """Return rows from compspro overlapping both subsectors AND sectors.

    If sub_subsectors is provided, also require at least one shared sub_subsector,
    making candidate matching more precise.
    """
    if not subsectors or not sectors:
        return []
    client = _client()
    query = (
        client.table("compspro")
        .select("name, sectors, subsectors, sub_subsectors, description")
        .overlaps("sectors", sectors)
        .overlaps("subsectors", subsectors)
        .neq("name", exclude_name)
    )
    rows = _execute(query).data or []

    if not sub_subsectors:
        return rows

    return [
        r for r in rows
        if not r.get("sub_subsectors")
        or bool(set(r.get("sub_subsectors") or []) & set(sub_subsectors))
    ]


def get_known_competitors(name: str) -> list[str]:
    """Return all company names linked to name in competitors (either direction)."""
    client = _client()
    as_a = _execute(client.table("competitors").select("company_b").eq("company_a", name))
    as_b = _execute(client.table("competitors").select("company_a").eq("company_b", name))
    return (
        [r["company_b"] for r in (as_a.data or [])]
        + [r["company_a"] for r in (as_b.data or [])]
    )


def get_company(name: str) -> dict | None:
    """Fetch a single startup's data from compspro."""
    client = _client()
    response = _execute(
        client.table("compspro")
        .select("name, sectors, subsectors, sub_subsectors, description, website, flaticon_url")
        .eq("name", name)
        .limit(1)
    )
    return response.data[0] if response.data else None


def relationship_exists(company_a: str, company_b: str) -> bool:
    """Check if the exact (company_a, company_b) row exists in competitors."""
    client = _client()
    response = _execute(
        client.table("competitors")
        .select("id")
        .eq("company_a", company_a)
        .eq("company_b", company_b)
        .limit(1)
    )
    return bool(response.data)


def save_relationships(company_a: str, results: list[dict]) -> list[dict]:
    """Insert (company_a, company_b) rows for results with score >= COMPETITOR_THRESHOLD.

    Skips if the exact (company_a, company_b) pair already exists.
    Returns list of dicts {company_a, company_b, score} that were inserted.
    """
    candidates = [r for r in results if r.get("score", 0) >= COMPETITOR_THRESHOLD]
    if not candidates:
        return []

    client = _client()
    saved = []

    for r in candidates:
        company_b = r["name"]
        if not relationship_exists(company_a, company_b):
            _execute(client.table("competitors").insert({
                "company_a": company_a,
                "company_b": company_b,
                "score": r["score"],
            }))
            saved.append({"company_a": company_a, "company_b": company_b, "score": r["score"]})

    return saved


def save_startup(data: dict) -> str:
    """Insert or update a startup. Returns 'saved' or 'updated'."""
    name = data.get("name")
    if not name:
        raise ValueError("Cannot save startup without a name")

    client = _client()

    existing = None
    website = data.get("website")
    if website:
        by_website = _execute(
            client.table("compspro")
            .select("id, name")
            .eq("website", website)
            .limit(1)
        )
        if by_website.data:
            existing = by_website.data[0]

    if not existing:
        by_name = _execute(
            client.table("compspro")
            .select("id, name")
            .eq("name", name)
            .limit(1)
        )
        if by_name.data:
            existing = by_name.data[0]

    if existing:
        _execute(client.table("compspro").update({**data, "taxonomy_version": "v2"}).eq("id", existing["id"]))
        return "updated"
    else:
        _execute(client.table("compspro").insert({**data, "taxonomy_version": "v2"}))
        return "saved"
