import contextvars
import os
import re
import threading
from datetime import datetime, timezone
from urllib.parse import urlparse
import httpx
from postgrest.exceptions import APIError
from supabase import create_client
from dotenv import load_dotenv
from tenacity import stop_after_attempt, wait_exponential

from retry import build_retry
from taxonomy import TAXONOMY

load_dotenv()

# Minimum pair score for two companies to be saved as competitors.
# Validation showed 0.75-0.84 pairs are mostly false positives; 0.85+ all held up.
COMPETITOR_THRESHOLD = 0.85

# Set by main.ingest(url, interactive=...) for the duration of a single ingest,
# read by competitor.py and extractor.py to pick a tighter Mistral retry/timeout
# budget than the batch/backfill scripts use. Since Story 6.1, no live caller
# passes interactive=True anymore -- the CLI and the web UI's background worker
# both pass False -- so this currently always reads as False in production;
# the flag/budget stay available for a future synchronous caller. Lives here
# (not in main.py) because both competitor.py and extractor.py already import
# from storage.py -- main.py importing back from either would be circular.
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
# numbers stay local to each module (competitor.py's/extractor.py's/
# competitor_validator.py's own build_retry(...) calls) -- only the
# *construction pattern* is shared (retry.py, Story 5.3), not these numbers.
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
_retry = build_retry(
    lambda exc: isinstance(exc, httpx.TransportError),
    wait_multiplier=1, wait_min=2, wait_max=20, stop_attempts=5,
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


def _normalize_subject(review_type: str, subject: str) -> str:
    """Normalize/validate `subject` per review_type's identifier contract (AD-4,
    AD-7). Shared by the write path (_validate_review -> save_quality_review)
    and the read path (get_quality_reviews) so the two can't drift into two
    independently-maintained copies of the same contract (Story 5.3).

    Returns the normalized subject. Raises ValueError on any violation.
    No I/O -- safe to unit test without a Supabase connection.
    """
    if review_type == "taxonomy_split":
        # Exact subsector match, no normalization -- deliberately opposite
        # leniency from scraping_diagnostic below: these are different kinds
        # of identifier (a closed taxonomy vocabulary vs. a free-form URL).
        if subject not in _TAXONOMY_SUBSECTORS:
            raise ValueError(f"subject must be an exact TAXONOMY subsector name for taxonomy_split, got {subject!r}")
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


def _validate_review(review_type: str, subject: str, verdict: str) -> str:
    """Validate the quality_review_log write contract (AD-4, AD-7).

    Returns the normalized subject. Raises ValueError on any violation.
    No I/O -- safe to unit test without a Supabase connection.
    """
    if review_type not in _KNOWN_REVIEW_TYPES:
        raise ValueError(f"Unknown review_type: {review_type!r}. Must be one of {sorted(_KNOWN_REVIEW_TYPES)}")

    if not verdict or not verdict.strip():
        raise ValueError("verdict must not be blank")

    subject = _normalize_subject(review_type, subject)

    if review_type == "taxonomy_split" and verdict not in _TAXONOMY_SPLIT_VERDICTS:
        raise ValueError(f"verdict must be one of {sorted(_TAXONOMY_SPLIT_VERDICTS)} for taxonomy_split, got {verdict!r}")

    return subject


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
        subject = _normalize_subject(review_type, subject)

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


# ingestion_queue contract (Epic 6, Story 6.1): status is a small, fixed,
# application-validated vocabulary -- not a DB enum/check constraint, same
# choice as quality_review_log.verdict (AD-4, AD-7).
_KNOWN_INGESTION_STATUSES = {"queued", "processing", "done", "error"}


def _now_iso() -> str:
    """UTC timestamp for ingestion_queue.updated_at -- no DB trigger sets this
    column (see migration 004's comment), so every status transition must set
    it explicitly, same convention as quality_review_log.updated_at.
    """
    return datetime.now(timezone.utc).isoformat()


def enqueue_ingestion(url: str) -> tuple[dict, bool]:
    """Insert a new ingestion_queue row with status='queued', or reuse the
    existing queued/processing row for the same *domain* if one already
    exists. Returns (row, is_new) -- the caller must only push onto the
    in-process worker queue when is_new is True, so a reused row (already
    queued or actively being processed) isn't picked up and run a second time.

    Code review (2026-08-28): prevents a double-click on "Ajouter" or a
    resubmission of the same URL from enqueueing two independent rows and
    running the full pipeline twice.

    Code review (2026-08-29 #1): the SELECT-then-INSERT above is a check-then-act
    race -- two concurrent calls for the same domain can both pass the SELECT
    before either INSERT commits. Closed at the DB level by a unique partial
    index (migrations/007, formerly migrations/006) on domain where status in
    ('queued','processing'): a second concurrent insert now fails with a
    unique_violation (23505), which is caught here and turned into a reuse of
    the row the other call just inserted, instead of two independent rows
    running the pipeline twice.

    Code review (2026-08-29 #2): dedup keys on normalize_domain(url), not the
    raw url string -- "acme.com", "https://acme.com/", and "http://acme.com"
    are the same startup and must collapse to one row (migrations/006's
    original url-keyed index missed this). url itself is still stored
    unchanged and is what gets scraped (main.ingest() needs the full path);
    only the dedup key changed.

    Code review (2026-08-29 #3): raises ValueError if normalize_domain(url)
    is empty (e.g. url="https://" or any other host-less/malformed URL that
    survives api_ingest's minimal scheme-prefix check) -- otherwise every
    such malformed submission would silently collapse onto the same
    domain="" row instead of being rejected as its own bad request. Mirrors
    _normalize_subject's existing guard for the same normalize_domain()
    empty-result case.
    """
    domain = normalize_domain(url)
    if not domain:
        raise ValueError(f"URL invalide, aucun domaine n'a pu en être extrait : {url!r}")
    client = _client()
    existing = _execute(
        client.table("ingestion_queue")
        .select("*")
        .eq("domain", domain)
        .in_("status", ["queued", "processing"])
        .limit(1)
    )
    if existing.data:
        return existing.data[0], False

    try:
        response = _execute(client.table("ingestion_queue").insert({"url": url, "domain": domain, "status": "queued"}))
    except APIError as e:
        if e.code != "23505":
            raise
        existing = _execute(
            client.table("ingestion_queue")
            .select("*")
            .eq("domain", domain)
            .in_("status", ["queued", "processing"])
            .limit(1)
        )
        if not existing.data:
            raise ValueError(f"ingestion_queue unique_violation on domain {domain!r} (url {url!r}) but no active row found on re-fetch") from e
        return existing.data[0], False

    if not response.data:
        raise ValueError("ingestion_queue insert returned no row")
    return response.data[0], True


def _set_ingestion_status(row_id, status: str, **fields) -> None:
    """Shared status-transition writer for mark_processing/mark_done/
    mark_error. Validates status against _KNOWN_INGESTION_STATUSES (code
    review, 2026-08-28: the constant existed but nothing checked against it)
    and raises if the update matched zero rows instead of silently no-op'ing.

    Code review (2026-08-29): retried via _execute_retryable despite being a
    write, unlike _execute()'s general POST/PATCH policy -- this specific
    update is safe to retry because it's keyed by row_id and reapplies the
    exact same status/fields, so an in-doubt retry after a dropped response
    just re-sets the same values rather than risking a duplicate row (the
    concern that keeps inserts from being retried). Without this, a transient
    error on mark_done/mark_error after ingest_startup already succeeded left
    the row stuck at 'processing' forever with no requeue.
    """
    if status not in _KNOWN_INGESTION_STATUSES:
        raise ValueError(f"Unknown ingestion status: {status!r}. Must be one of {sorted(_KNOWN_INGESTION_STATUSES)}")
    client = _client()
    response = _execute_retryable(
        client.table("ingestion_queue")
        .update({"status": status, "updated_at": _now_iso(), **fields})
        .eq("id", row_id)
    )
    if not response.data:
        raise ValueError(f"ingestion_queue row {row_id} not found (update matched zero rows)")


def retry_ingestion(row_id) -> dict:
    """Reset an errored ingestion_queue row back to status='queued' so the
    worker (Story 6.1) picks it up again from scratch (Story 6.3 -- no
    partial/per-step retry, full main.ingest() re-run, per the v1 scope
    decision). Clears error_message.

    Unlike _set_ingestion_status(), the WHERE clause also requires
    status='error' so the transition is atomic (not a separate read-then-
    write) and can't race a row that already left 'error' -- e.g. two browser
    tabs both showing the same stale error state, both clicking "Relancer".
    If zero rows match, the caller can't tell "no such row" from "not in
    error" without another query, so this just raises ValueError either way;
    the caller (the retry endpoint) turns that into a 404.

    Code review (2026-08-29): NOT routed through _execute_retryable, unlike
    _set_ingestion_status(). That function's update is safe to retry because
    its only WHERE clause is `id=row_id` -- reapplying the same values is a
    true no-op. This update's WHERE clause also requires `status='error'`,
    which is state-dependent: if the first attempt actually commits
    server-side but the response is lost (a dropped-stream TransportError),
    a retry re-evaluates `status='error'` against the row's *new* status
    ('queued') and matches zero rows -- raising a false "not found" even
    though the retry succeeded. Using the unretried _execute() means a
    transient error surfaces as a clean transport exception instead of a
    misleading 404.

    Can raise a postgrest APIError with code 23505: migrations/007's unique
    partial index on ingestion_queue(domain) where status in
    ('queued','processing') means this UPDATE can collide with a fresh
    queued/processing row for the same domain (e.g. resubmitted via the
    normal "Ajouter" flow while this row was still in 'error'). Deliberately
    not caught here -- unlike enqueue_ingestion's 23505 case, there's no row
    to usefully reuse, so the caller decides how to surface the conflict (a
    409, not a 404 or 502).
    """
    client = _client()
    response = _execute(
        client.table("ingestion_queue")
        .update({"status": "queued", "error_message": None, "updated_at": _now_iso()})
        .eq("id", row_id)
        .eq("status", "error")
    )
    if not response.data:
        raise ValueError(f"ingestion_queue row {row_id} not found or not in 'error' status")
    return response.data[0]


def delete_ingestion(row_id) -> dict:
    """Permanently remove an errored ingestion_queue row so the "En attente"
    tab can be cleared of stale failures. Restricted to status='error', same
    as retry_ingestion -- deleting a queued/processing row would silently
    drop work still in flight, and a done row is the record of a real
    ingestion having happened.
    """
    client = _client()
    response = _execute(
        client.table("ingestion_queue")
        .delete()
        .eq("id", row_id)
        .eq("status", "error")
    )
    if not response.data:
        raise ValueError(f"ingestion_queue row {row_id} not found or not in 'error' status")
    return response.data[0]


def mark_processing(row_id) -> None:
    """Transition an ingestion_queue row to status='processing'. Called by the
    worker right before it calls main.ingest(url) for this row.
    """
    _set_ingestion_status(row_id, "processing")


def mark_done(row_id, result: dict) -> None:
    """Transition an ingestion_queue row to status='done', storing the
    {"name","action","competitors_found"} dict main.ingest() returned.
    """
    _set_ingestion_status(row_id, "done", result=result)


def mark_error(row_id, error_message: str) -> None:
    """Transition an ingestion_queue row to status='error', storing the
    failure message (a ValueError's message, or str(e) for anything else).
    """
    _set_ingestion_status(row_id, "error", error_message=error_message)


def get_pending_ingestions() -> list[dict]:
    """Rows still status in ('queued', 'processing') from a previous run,
    oldest first. Read once at app startup to re-enqueue onto the worker's
    in-process queue -- a crash between items must not silently strand a row
    forever (Story 6.1, AC #4). The in-memory queue is disposable; this table
    is the source of truth.
    """
    client = _client()
    query = (
        client.table("ingestion_queue")
        .select("*")
        .in_("status", ["queued", "processing"])
        .order("created_at")
    )
    return _execute(query).data or []


def list_ingestions(limit: int = 50) -> list[dict]:
    """All ingestion_queue rows (no status filter), most recent first, for the
    "En attente" tab (Story 6.2) -- unlike get_pending_ingestions(), this also
    surfaces done/error rows so their terminal badge stays visible.

    `limit` is a placeholder to keep the panel from rendering an ever-growing
    list, not a considered retention policy -- Story 6.1's code review flagged
    that ingestion_queue has no retention/cleanup policy yet (deferred).
    """
    client = _client()
    query = (
        client.table("ingestion_queue")
        .select("*")
        .order("created_at", desc=True)
        .limit(limit)
    )
    return _execute(query).data or []


def mark_done_rows_seen() -> int:
    """Bulk-marks every currently-done-and-unseen row as seen (Story 6.4) --
    called once when the "En attente" tab opens, not per-row. Returns the
    number of rows updated (not required by any caller today, just an honest
    return value instead of None).

    Code review (2026-08-29): NOT routed through _execute_retryable, despite
    looking like the same "blind re-apply is a no-op" shape as
    _set_ingestion_status(). Its WHERE clause filters on `seen=false`, which
    is state-dependent: if the first attempt commits server-side but the
    response is lost (a dropped-stream TransportError), a retry re-evaluates
    `seen=false` against rows already flipped to `seen=true` and matches zero
    of them -- silently under-reporting the count (same class of bug as
    retry_ingestion()'s state-dependent WHERE clause). Using the unretried
    _execute() surfaces a transient error as a clean exception instead of a
    silently-wrong count.
    """
    client = _client()
    response = _execute(
        client.table("ingestion_queue")
        .update({"seen": True})
        .eq("status", "done")
        .eq("seen", False)
    )
    return len(response.data or [])


def get_ingestion_summary() -> dict:
    """Counts backing the "En attente" tab's two notification badges (Story
    6.4): error_count (status='error', regardless of seen -- a failure is
    never silently dismissed) and unseen_done_count (status='done' AND
    seen=false). Two separate count="exact", head=True queries -- no row
    payload fetched, just a Content-Range-derived count -- since Postgrest
    has no single-query way to count two different filters at once. head=True
    issues an HTTP HEAD request, which _execute()'s existing
    "http_method in ('GET', 'HEAD')" retry check already covers unchanged.
    """
    client = _client()
    error_response = _execute(
        client.table("ingestion_queue")
        .select("id", count="exact", head=True)
        .eq("status", "error")
    )
    unseen_response = _execute(
        client.table("ingestion_queue")
        .select("id", count="exact", head=True)
        .eq("status", "done")
        .eq("seen", False)
    )
    return {
        "error_count": error_response.count or 0,
        "unseen_done_count": unseen_response.count or 0,
    }
