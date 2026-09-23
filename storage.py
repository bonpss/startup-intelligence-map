import contextvars
import os
import random
import re
import threading
from datetime import datetime, timezone
from urllib.parse import urlparse
import httpx
from postgrest.exceptions import APIError
from supabase import create_client
from dotenv import load_dotenv
from tenacity import stop_after_attempt, wait_exponential

from pricing import compute_cost_usd
from retry import build_retry
from taxonomy import TAXONOMY

load_dotenv()

# Minimum pair score for two companies to be saved as competitors.
# Validation showed 0.75-0.84 pairs are mostly false positives; 0.85+ all held up.
#
# MISTRAL-SCALE ONLY. This constant governs the Mistral scoring path
# (competitor.py::score_candidates/save_competitors, still the live default
# called from main.ingest() as of 2026-09-17) and save_relationships()'s
# default `threshold` param below. It is NOT on the same scale as Jev's
# scores -- see JEV_ACCEPT_THRESHOLD/JEV_REVIEW_FLOOR just below, which
# govern the separate, not-yet-default Jev scoring path
# (competitor.py::score_candidates_jev/save_competitors_jev). Do not repoint
# this constant to a Jev-scale value while the Mistral path is still live --
# that would silently break Mistral's own threshold, not "migrate" it.
COMPETITOR_THRESHOLD = 0.85

# Jev's three-zone decision (competitor.py::score_candidates_jev), decided
# 2026-09-17 after the LOOCV/prompt-iteration/z-score investigation
# documented in jev_manual_labels.json and loocv_jev_threshold_report.json:
#   score >= JEV_ACCEPT_THRESHOLD            -> confirmed competitor, saved
#   JEV_REVIEW_FLOOR <= score < ACCEPT       -> needs human review, not auto-saved
#   score < JEV_REVIEW_FLOOR                 -> rejected, not saved
# Jev's scale is NOT comparable to Mistral's (COMPETITOR_THRESHOLD above) --
# confirmed true positives in testing landed anywhere from 0.52 to 0.92,
# nowhere near Mistral's 0.85+ range for the same kind of pair.
JEV_ACCEPT_THRESHOLD = 0.50
JEV_REVIEW_FLOOR = 0.40

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

# Set by main.ingest() for the duration of a single ingest (same asyncio.to_thread
# propagation as INTERACTIVE_REQUEST above), read by log_api_call() below so every
# Mistral call site in extractor.py/competitor.py/embeddings.py can attribute its
# cost to the right ingestion_queue row without an extra parameter threaded through
# every function signature. {"ingestion_queue_id": int | None, "label": str} --
# label is the url (known before the startup's name is), a fallback for reading
# raw api_call_log rows with no ingestion_queue_id, not used by the dashboard's
# aggregation itself. None (the default) for any call made outside main.ingest() --
# a backfill/fix-up script importing these modules directly -- log_api_call()
# still logs the call, just with ingestion_queue_id=None.
CURRENT_API_CALL_CONTEXT: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "current_api_call_context", default=None
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


def _fine_subsector_own_labels(
    sectors: list[str], subsectors: list[str], own_sub_subsectors: list[str]
) -> dict[str, set[str]]:
    """Subsectors in `subsectors` that TAXONOMY further breaks into
    sub_subsectors for at least one of `sectors` ("fine" subsectors), mapped
    to the subset of `own_sub_subsectors` that belongs to each -- an empty
    set means the subsector is fine but nothing survived step2c's confidence
    threshold for it. Subsectors with no sub_subsectors defined anywhere in
    TAXONOMY are absent from the result entirely (not "fine").

    Reads TAXONOMY dynamically -- no subsector name is hardcoded, so this
    keeps working as the taxonomy grows new fine-grained subsectors.
    """
    own = set(own_sub_subsectors or [])
    result: dict[str, set[str]] = {}
    for sub in subsectors:
        defined: set[str] = set()
        for sec in sectors:
            defined |= set(TAXONOMY.get(sec, {}).get(sub, []))
        if defined:
            result[sub] = own & defined
    return result


_CANDIDATE_POOL_CAP = 100


def _cap_candidate_pool(candidates: list[dict], query_subsectors: list[str], startup_name: str) -> list[dict]:
    """Cap an oversized candidate pool at _CANDIDATE_POOL_CAP, keeping the
    candidates that share the most subsectors with the querying startup.

    Ties at the cap boundary are broken with a Random seeded on startup_name
    (not unseeded randomness) so re-running the same startup's ingestion/
    reprocess always yields the same kept set.
    """
    if len(candidates) <= _CANDIDATE_POOL_CAP:
        return candidates

    qs = set(query_subsectors)
    scored = [(len(set(c.get("subsectors") or []) & qs), c) for c in candidates]
    scored.sort(key=lambda pair: pair[0], reverse=True)

    boundary_score = scored[_CANDIDATE_POOL_CAP - 1][0]
    kept = [c for score, c in scored if score > boundary_score]
    tied = [c for score, c in scored if score == boundary_score]
    remaining_slots = _CANDIDATE_POOL_CAP - len(kept)

    # Sort tied candidates by name first so the sample doesn't depend on the
    # DB's (arbitrary) row order, then sample deterministically off a seed
    # tied to the querying startup.
    tied_sorted = sorted(tied, key=lambda c: c.get("name") or "")
    rng = random.Random(startup_name)
    kept.extend(rng.sample(tied_sorted, remaining_slots))

    print(f"[get_by_subsectors] candidate pool cap activated for {startup_name!r}: {len(candidates)} -> {len(kept)}")
    return kept


def get_by_subsectors(
    subsectors: list[str],
    sectors: list[str],
    exclude_name: str,
    sub_subsectors: list[str] = [],
    include_embedding: bool = True,
    exclude_id: str | None = None,
) -> list[dict]:
    """Return rows from compspro overlapping both subsectors AND sectors.

    For "fine" subsectors -- ones TAXONOMY further breaks into sub_subsectors,
    e.g. "Productivity Tools" -- matching is tightened per subsector:
    - if the querying startup has its own sub_subsectors for that fine
      subsector, a candidate must share at least one of them via that same
      subsector to count as a match through it. A candidate with an empty
      sub_subsectors list no longer gets a free pass just because it has
      none (that was letting through e.g. Fluidstack/Rad AI/Garner Health --
      sharing "Productivity Tools" with Freestyle but nothing else -- as
      false-positive competitor candidates).
    - if the querying startup itself has no sub_subsectors for that fine
      subsector (nothing survived step2c), that subsector contributes no
      candidates at all until the startup is reprocessed with a real
      sub_subsector -- chosen policy is to exclude rather than fall back to
      the full subsector pool.

    Subsectors with no sub_subsectors defined in TAXONOMY (the majority) are
    unaffected -- matching stays at the subsector level, exactly as before.

    include_embedding defaults to True (competitor.py's compare() needs it for
    prefilter_by_embedding). Callers that never touch the field -- e.g.
    backfill_competitors.py, which scores candidates via score_candidates()
    without any embedding prefilter -- should pass False: the column is a
    ~20KB-per-row pgvector serialized as JSON, and pulling it for hundreds of
    candidates across a large backlog is real PostgREST egress for no benefit
    (2026-09-04 incident: an unthrottled --dry-run backlog scan alone drove a
    single day's egress past the project's entire monthly quota).

    exclude_id, when given, excludes the querying startup by compspro.id
    instead of by name -- two startups can legitimately share a display name
    (e.g. "Corma" at corma.io and corma.ai), so a name-based exclude would
    wrongly hide a real candidate. exclude_name is still required (used by
    _cap_candidate_pool's deterministic tie-break seed) and is the fallback
    exclusion key for callers that don't yet have an id (backfill_competitors.py).
    """
    if not subsectors or not sectors:
        return []
    client = _client()
    columns = "id, name, sectors, subsectors, sub_subsectors, description"
    columns += ", embedding" if include_embedding else ""
    query = (
        client.table("compspro")
        .select(columns)
        .overlaps("sectors", sectors)
        .overlaps("subsectors", subsectors)
    )
    query = query.neq("id", exclude_id) if exclude_id is not None else query.neq("name", exclude_name)
    rows = _execute(query).data or []

    fine_own = _fine_subsector_own_labels(sectors, subsectors, sub_subsectors)
    if not fine_own:
        return _cap_candidate_pool(rows, subsectors, exclude_name)

    coarse_subsectors = set(subsectors) - set(fine_own)

    kept = []
    for r in rows:
        r_subsectors = set(r.get("subsectors") or [])
        if r_subsectors & coarse_subsectors:
            kept.append(r)
            continue
        r_sub_subs = set(r.get("sub_subsectors") or [])
        for sub, own_labels in fine_own.items():
            if not own_labels or sub not in r_subsectors:
                continue
            if own_labels & r_sub_subs:
                kept.append(r)
                break
    return _cap_candidate_pool(kept, subsectors, exclude_name)


def get_known_competitors(company_id: str) -> list[dict]:
    """Return [{id, name}, ...] for every company linked to company_id in
    competitors (either direction) -- keyed by company_a_id/company_b_id,
    never the company_a/company_b name-text columns (two unrelated startups
    can share a display name, e.g. "Corma" at corma.io and corma.ai)."""
    client = _client()
    as_a = _execute(client.table("competitors").select("company_b_id, company_b").eq("company_a_id", company_id))
    as_b = _execute(client.table("competitors").select("company_a_id, company_a").eq("company_b_id", company_id))
    return (
        [{"id": r["company_b_id"], "name": r["company_b"]} for r in (as_a.data or []) if r["company_b_id"] is not None]
        + [{"id": r["company_a_id"], "name": r["company_a"]} for r in (as_b.data or []) if r["company_a_id"] is not None]
    )


def get_company(name: str) -> dict | None:
    """Fetch a single startup's data from compspro by name.

    Caveat: two startups can share a display name (e.g. "Corma" at corma.io
    and corma.ai) -- this returns whichever row Postgres happens to return
    first, with no tiebreaker. Kept for human-supervised maintenance scripts
    (delete_stale_competitor_pairs.py); the pipeline itself uses
    get_company_by_id() instead, which has no such ambiguity.
    """
    client = _client()
    response = _execute(
        client.table("compspro")
        .select("name, sectors, subsectors, sub_subsectors, description, website, flaticon_url")
        .eq("name", name)
        .limit(1)
    )
    return response.data[0] if response.data else None


def get_company_by_id(company_id: str) -> dict | None:
    """Fetch a single startup's data from compspro by id -- unambiguous even
    when two rows share a display name, unlike get_company(name)."""
    client = _client()
    response = _execute(
        client.table("compspro")
        .select("id, name, sectors, subsectors, sub_subsectors, description, website, flaticon_url, domain")
        .eq("id", company_id)
        .limit(1)
    )
    return response.data[0] if response.data else None


def relationship_exists(company_a_id: str, company_b_id: str) -> bool:
    """Check if the exact (company_a_id, company_b_id) row exists in competitors."""
    client = _client()
    response = _execute(
        client.table("competitors")
        .select("id")
        .eq("company_a_id", company_a_id)
        .eq("company_b_id", company_b_id)
        .limit(1)
    )
    return bool(response.data)


def save_relationships(company_a_id: str, company_a_name: str, results: list[dict], threshold: float = COMPETITOR_THRESHOLD, scorer: str | None = None) -> list[dict]:
    """Insert (company_a_id, company_b_id) rows for results with score >= threshold.

    threshold defaults to COMPETITOR_THRESHOLD (Mistral's scale, the existing
    call site in competitor.py::save_competitors doesn't pass it explicitly).
    competitor.py::save_competitors_jev passes JEV_ACCEPT_THRESHOLD explicitly
    instead -- the two scorers' scores are not on the same scale, see
    JEV_ACCEPT_THRESHOLD's comment above COMPETITOR_THRESHOLD.

    scorer, when given, is stamped onto competitors.scorer ('mistral' or
    'jev', migration 2026-09-17). None (the default) leaves the column NULL
    -- the existing Mistral call site doesn't pass it, so its rows stay NULL
    (implicitly mistral) rather than every historical row needing a backfill.

    Skips if the exact (company_a_id, company_b_id) pair already exists.
    company_a/company_b (name) are written alongside the ids as human-readable
    labels only -- never read back for lookups, so a later rename or a shared
    display name can't cause a mismatch.
    Returns list of dicts {company_a_id, company_a, company_b_id, company_b, score} that were inserted.
    """
    candidates = [r for r in results if r.get("score", 0) >= threshold]
    if not candidates:
        return []

    client = _client()
    saved = []

    for r in candidates:
        company_b_id = r["id"]
        if not relationship_exists(company_a_id, company_b_id):
            row = {
                "company_a_id": company_a_id,
                "company_a": company_a_name,
                "company_b_id": company_b_id,
                "company_b": r["name"],
                "score": r["score"],
            }
            if scorer is not None:
                row["scorer"] = scorer
            _execute(client.table("competitors").insert(row))
            saved.append(row)

    return saved


def review_pair_exists(company_a_id: str, company_b_id: str) -> bool:
    """Check if the exact (company_a_id, company_b_id) row already exists in
    competitor_review_queue -- same dedup principle as relationship_exists()
    for `competitors`, so re-ingesting a startup doesn't queue the same pair
    for review twice.
    """
    client = _client()
    response = _execute(
        client.table("competitor_review_queue")
        .select("id")
        .eq("company_a_id", company_a_id)
        .eq("company_b_id", company_b_id)
        .limit(1)
    )
    return bool(response.data)


def save_review_queue(company_a_id: str, company_a_name: str, results: list[dict], scorer: str) -> list[dict]:
    """Insert (company_a_id, company_b_id) rows into competitor_review_queue
    (migration 2026-09-17) for pairs in a scorer's review band -- not
    filtered by any threshold here, the caller (competitor.py::
    save_competitors_jev) already selected exactly the review-zone results.
    Mirrors save_relationships()'s dedup pattern (skips an exact pair
    already queued) but writes to competitor_review_queue instead of
    `competitors`, and always stamps `scorer` (unlike save_relationships(),
    where it's optional) since a review-queue row has no other way to know
    which scorer's band it came from.

    Returns the list of dicts that were inserted (same shape as the row
    written: company_a_id, company_a, company_b_id, company_b, score, scorer).
    """
    if not results:
        return []

    client = _client()
    saved = []

    for r in results:
        company_b_id = r["id"]
        if not review_pair_exists(company_a_id, company_b_id):
            row = {
                "company_a_id": company_a_id,
                "company_a": company_a_name,
                "company_b_id": company_b_id,
                "company_b": r["name"],
                "score": r["score"],
                "scorer": scorer,
            }
            _execute(client.table("competitor_review_queue").insert(row))
            saved.append(row)

    return saved


def save_startup(data: dict, added_by_user_id: int | None = None) -> tuple[str, str, str]:
    """Insert or update a startup, keyed by normalized domain -- NEVER by name.

    Two startups can legitimately share a display name (e.g. "Corma" at
    corma.io vs corma.ai); matching by name used to silently overwrite one
    with the other's data. No match on domain -> always INSERT, never a
    name-based fallback.

    Returns (action, id, domain): action is 'saved' or 'updated', id is the
    compspro.id (uuid) of the affected row, domain is the normalized value
    written -- callers use both instead of re-deriving/re-looking-up by name.

    added_by_user_id is only written on first insert -- a re-ingestion of an
    already-existing startup (the 'updated' branch) never overwrites its
    original attribution, even if a different user happens to trigger the
    update.
    """
    name = data.get("name")
    if not name:
        raise ValueError("Cannot save startup without a name")

    website = data.get("website")
    if not website:
        raise ValueError("Cannot save startup without a website")

    domain = normalize_domain(website)
    if not domain:
        raise ValueError(f"Could not extract a domain from website: {website!r}")

    client = _client()
    payload = {**data, "domain": domain, "taxonomy_version": "v2"}

    existing = _execute(
        client.table("compspro").select("id").eq("domain", domain).limit(1)
    )
    if existing.data:
        row_id = existing.data[0]["id"]
        _execute(client.table("compspro").update(payload).eq("id", row_id))
        return "updated", row_id, domain

    response = _execute(
        client.table("compspro").insert({**payload, "added_by_user_id": added_by_user_id})
    )
    if not response.data:
        raise ValueError("compspro insert returned no row")
    return "saved", response.data[0]["id"], domain


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


def enqueue_ingestion(url: str, requested_by_user_id: int | None = None) -> tuple[dict, bool]:
    """Insert a new ingestion_queue row with status='queued', or reuse the
    existing queued/processing row for the same *domain* if one already
    exists. Returns (row, is_new) -- the caller must only push onto the
    in-process worker queue when is_new is True, so a reused row (already
    queued or actively being processed) isn't picked up and run a second time.

    requested_by_user_id is only stored on a genuinely new row -- a reused
    row keeps whichever user originally submitted it.

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
        response = _execute(client.table("ingestion_queue").insert({"url": url, "domain": domain, "status": "queued", "requested_by_user_id": requested_by_user_id}))
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


def get_ingestion(row_id, requested_by_user_id: int | None = None) -> dict | None:
    """Fetch a single ingestion_queue row by id, or None if it doesn't exist
    (or -- see requested_by_user_id below -- doesn't belong to the caller).
    Used by the retry/delete endpoints (graph_app.py) to re-validate the
    row's URL (SSRF guard) and check the daily quota before retry_ingestion()
    flips the row back to 'queued' -- checking after that transition would
    leave a rejected row stuck at 'queued' with no worker ever picking it up.

    requested_by_user_id, when given, restricts the SELECT itself to rows
    owned by that user -- a mismatched or NULL (orphaned) row simply isn't
    returned, indistinguishable from "no such id". This is the ownership
    check for non-owner callers (graph_app.py passes None for the owner, who
    is unrestricted, and their own id otherwise); it must be a query-level
    filter, not a fetch-then-compare in Python, so a non-owner's read of
    someone else's row never round-trips its data out of the DB layer at all.
    """
    client = _client()
    query = client.table("ingestion_queue").select("*").eq("id", row_id)
    if requested_by_user_id is not None:
        query = query.eq("requested_by_user_id", requested_by_user_id)
    response = _execute(query.limit(1))
    return response.data[0] if response.data else None


def retry_ingestion(row_id, requested_by_user_id: int | None = None) -> dict:
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

    requested_by_user_id, when given, is ANDed into the same WHERE clause as
    an ownership guard -- a non-owner's retry on someone else's (or an
    orphaned) row matches zero rows and raises the same ValueError as a
    nonexistent id, giving the endpoint no way to distinguish "not found"
    from "not yours" (by design: 404, not 403, per the spec). This mirrors
    get_ingestion()'s query-level filter above rather than trusting a
    Python-side check done by the caller after a separate read, so the
    UPDATE itself can never touch a row it isn't authorized to touch.

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
    query = (
        client.table("ingestion_queue")
        .update({"status": "queued", "error_message": None, "updated_at": _now_iso()})
        .eq("id", row_id)
        .eq("status", "error")
    )
    if requested_by_user_id is not None:
        query = query.eq("requested_by_user_id", requested_by_user_id)
    response = _execute(query)
    if not response.data:
        raise ValueError(f"ingestion_queue row {row_id} not found, not in 'error' status, or not owned by user {requested_by_user_id}")
    return response.data[0]


def delete_ingestion(row_id, requested_by_user_id: int | None = None) -> dict:
    """Permanently remove an errored ingestion_queue row so the "En attente"
    tab can be cleared of stale failures. Restricted to status='error', same
    as retry_ingestion -- deleting a queued/processing row would silently
    drop work still in flight, and a done row is the record of a real
    ingestion having happened.

    requested_by_user_id: same query-level ownership guard as
    retry_ingestion() -- see that function's docstring.
    """
    client = _client()
    query = (
        client.table("ingestion_queue")
        .delete()
        .eq("id", row_id)
        .eq("status", "error")
    )
    if requested_by_user_id is not None:
        query = query.eq("requested_by_user_id", requested_by_user_id)
    response = _execute(query)
    if not response.data:
        raise ValueError(f"ingestion_queue row {row_id} not found, not in 'error' status, or not owned by user {requested_by_user_id}")
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


def list_ingestions(limit: int = 50, status: str | None = None, requested_by_user_id: int | None = None) -> list[dict]:
    """All ingestion_queue rows (no status filter by default), most recent
    first, for the "En attente" tab (Story 6.2) -- unlike get_pending_ingestions(),
    this also surfaces done/error rows so their terminal badge stays visible.

    `limit` is a placeholder to keep the unfiltered panel from rendering an
    ever-growing list, not a considered retention policy -- Story 6.1's code
    review flagged that ingestion_queue has no retention/cleanup policy yet
    (deferred).

    status, when given, filters to just that status and is NOT subject to
    `limit` -- unlike the default view, a status filter (currently only
    'error', from the "En attente" panel's Échecs tab) exists specifically so
    an old failure never silently drops out of view once enough newer rows of
    any status push it past the default cap. Mirrors get_ingestion_summary's
    error_count, which already counts every error row with no cap -- before
    this, the header badge and the list it was supposed to explain could
    disagree (badge says 4 errors, list -- capped to the 50 most recent rows
    of ANY status -- shows none of them).

    requested_by_user_id, when given, restricts the result to that user's own
    rows (query-level .eq(), not a Python-side filter after the fact) --
    graph_app.py passes the caller's own id for a non-owner (who can never
    see anyone else's rows, including orphaned ones with no
    requested_by_user_id at all -- NULL never matches .eq()) or None for the
    owner viewing everything (unchanged/legacy behavior, the only path that
    still surfaces orphaned rows).
    """
    if status is not None and status not in _KNOWN_INGESTION_STATUSES:
        raise ValueError(f"Unknown ingestion status: {status!r}. Must be one of {sorted(_KNOWN_INGESTION_STATUSES)}")
    client = _client()
    query = client.table("ingestion_queue").select("*").order("created_at", desc=True)
    if requested_by_user_id is not None:
        query = query.eq("requested_by_user_id", requested_by_user_id)
    query = query.eq("status", status) if status is not None else query.limit(limit)
    return _execute(query).data or []


def mark_done_rows_seen(requested_by_user_id: int | None = None, include_orphaned: bool = False) -> int:
    """Bulk-marks currently-done-and-unseen rows as seen (Story 6.4) -- called
    once when the "En attente" tab opens, not per-row. Returns the number of
    rows updated (not required by any caller today, just an honest return
    value instead of None).

    requested_by_user_id, when given, restricts this to that user's own rows
    -- each user's "seen" state is theirs alone, so one user opening the
    drawer must never dismiss another user's unseen-done badge.
    include_orphaned additionally covers rows with no requested_by_user_id
    at all (NULL) -- graph_app.py only ever sets this for the owner, since
    an orphaned row is only ever visible to the owner in the first place
    (see list_ingestions), so nobody else could otherwise ever clear its
    "unseen" flag.

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
    query = (
        client.table("ingestion_queue")
        .update({"seen": True})
        .eq("status", "done")
        .eq("seen", False)
    )
    if requested_by_user_id is not None:
        if include_orphaned:
            query = query.or_(f"requested_by_user_id.eq.{requested_by_user_id},requested_by_user_id.is.null")
        else:
            query = query.eq("requested_by_user_id", requested_by_user_id)
    response = _execute(query)
    return len(response.data or [])


# users (spec-public-demo-auth.md): capped-signup email/password accounts.
# password_hash is a bcrypt hash produced by auth.py -- this module never
# hashes/verifies passwords itself, same separation as taxonomy validation
# living in auth.py's caller rather than here.

def create_user(email: str, password_hash: str, is_owner: bool = False) -> dict:
    """Insert a new users row. Raises postgrest APIError (code 23505) on a
    duplicate email -- the caller (graph_app.py's POST /api/signup) turns that
    into a 409, per the I/O matrix's 'Signup, duplicate email' row. Not
    wrapped in _execute's retry (a write), for the same in-doubt-write reason
    other inserts in this module aren't retried.
    """
    client = _client()
    response = _execute(client.table("users").insert({
        "email": email,
        "password_hash": password_hash,
        "is_owner": is_owner,
    }))
    return response.data[0] if response.data else {}


def get_user_by_email(email: str) -> dict | None:
    """Fetch a single users row by exact email match, or None if no account
    exists. Callers normalize (trim/lowercase) email before calling this --
    matching is exact here, same division of responsibility as
    _normalize_subject vs. the raw DB lookup elsewhere in this module.
    """
    client = _client()
    response = _execute(
        client.table("users")
        .select("id, email, password_hash, is_owner")
        .eq("email", email)
        .limit(1)
    )
    return response.data[0] if response.data else None


def count_non_owner_users() -> int:
    """Count of users where is_owner=false -- the denominator auth.py's
    MAX_USERS cap check compares against. count='exact', head=True issues an
    HTTP HEAD request with no row payload, same pattern as
    get_ingestion_summary()'s two counts below.
    """
    client = _client()
    response = _execute(
        client.table("users")
        .select("id", count="exact", head=True)
        .eq("is_owner", False)
    )
    return response.count or 0


def count_user_ingestions_since(user_id: int, since_iso: str) -> int:
    """Count of ingestion_queue rows requested by `user_id` created at/after
    since_iso -- backs auth.ingestion_quota_reached()'s rolling-24h per-user
    cap (cost/abuse guardrail on /api/ingest, non-owner users only). Every
    submission (queued/processing/done/error alike) counts, including one
    that's since errored out -- a user retrying a URL that keeps failing
    still consumed real scrape/LLM cost each time, so it still counts against
    the cap.
    """
    client = _client()
    response = _execute(
        client.table("ingestion_queue")
        .select("id", count="exact", head=True)
        .eq("requested_by_user_id", user_id)
        .gte("created_at", since_iso)
    )
    return response.count or 0


def get_ingestion_summary(requested_by_user_id: int | None = None) -> dict:
    """Counts backing the "En attente" tab's two notification badges (Story
    6.4): error_count (status='error', regardless of seen -- a failure is
    never silently dismissed) and unseen_done_count (status='done' AND
    seen=false). Two separate count="exact", head=True queries -- no row
    payload fetched, just a Content-Range-derived count -- since Postgrest
    has no single-query way to count two different filters at once. head=True
    issues an HTTP HEAD request, which _execute()'s existing
    "http_method in ('GET', 'HEAD')" retry check already covers unchanged.

    requested_by_user_id, when given, scopes both counts to that user's own
    rows -- same query-level filter as list_ingestions(), so a non-owner's
    badges only ever reflect rows they can actually see (never someone
    else's, never an orphaned row with no requested_by_user_id).
    """
    client = _client()
    error_query = (
        client.table("ingestion_queue")
        .select("id", count="exact", head=True)
        .eq("status", "error")
    )
    unseen_query = (
        client.table("ingestion_queue")
        .select("id", count="exact", head=True)
        .eq("status", "done")
        .eq("seen", False)
    )
    if requested_by_user_id is not None:
        error_query = error_query.eq("requested_by_user_id", requested_by_user_id)
        unseen_query = unseen_query.eq("requested_by_user_id", requested_by_user_id)
    error_response = _execute(error_query)
    unseen_response = _execute(unseen_query)
    return {
        "error_count": error_response.count or 0,
        "unseen_done_count": unseen_response.count or 0,
    }


# api_call_log (owner-only /admin dashboard, 2026-09-04 conversation): one row per
# Mistral API call, written by log_api_call() below and read back by dashboard.py's
# aggregation functions.

def log_api_call(call_type: str, model: str, prompt_tokens: int, completion_tokens: int, item_count: int | None = None) -> None:
    """Best-effort usage/cost log for one Mistral API call. Reads
    CURRENT_API_CALL_CONTEXT (set by main.ingest()) for the ingestion_queue_id/
    label to attribute this call to -- see that contextvar's docstring above.

    Never raises: this is instrumentation for the admin dashboard, not part of
    the ingestion pipeline's correctness -- a transient DB error here must not
    fail (or retry-slow) the Mistral call it's just finished logging.
    """
    ctx = CURRENT_API_CALL_CONTEXT.get() or {}
    try:
        client = _client()
        _execute(client.table("api_call_log").insert({
            "ingestion_queue_id": ctx.get("ingestion_queue_id"),
            "label": ctx.get("label"),
            "call_type": call_type,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "item_count": item_count,
            "cost_usd": compute_cost_usd(model, prompt_tokens, completion_tokens),
        }))
    except Exception as e:
        print(f"[log_api_call] failed to log usage ({call_type}, {model}): {e}")


# ── /admin dashboard data access (2026-09-04 conversation) ─────────────────────
# Every function below is a plain fetch -- no server-side aggregation (Postgrest/
# the supabase-py client has none to offer beyond count="exact"), so dashboard.py
# does the grouping/summing in Python once the rows are back. Table sizes here
# (thousands of rows, not millions) make that the right tradeoff for a solo admin
# page checked occasionally, over adding a second DB-access pattern (raw SQL) that
# nothing else in this project uses.

def _paginated_select(table: str, columns: str, page_size: int = 1000) -> list[dict]:
    """Fetch every row of `table`, `columns` only, across as many
    .range()-paginated requests as needed -- Postgrest caps a single response
    at 1000 rows by default. Shared by the dashboard fetchers below, all of
    which need "every row of a couple of narrow columns", never a single-page
    slice.
    """
    client = _client()
    rows: list[dict] = []
    start = 0
    while True:
        batch = (
            _execute(client.table(table).select(columns).range(start, start + page_size - 1)).data
            or []
        )
        rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return rows


def count_compspro() -> int:
    response = _execute(_client().table("compspro").select("id", count="exact", head=True))
    return response.count or 0


def count_competitors() -> int:
    response = _execute(_client().table("competitors").select("id", count="exact", head=True))
    return response.count or 0


def count_users() -> int:
    response = _execute(_client().table("users").select("id", count="exact", head=True))
    return response.count or 0


def get_data_completeness() -> dict:
    """Missing-field counts for compspro's dashboard "completeness" card. Missing
    means NULL OR empty string/array -- extractor.py's own defaults are '' and
    '{}', not NULL, so a NULL-only check would undercount every field. Same
    definition used in the 2026-09-03 manual audit this card reuses.
    """
    client = _client()

    def missing_text(col: str) -> int:
        r = _execute(client.table("compspro").select("id", count="exact", head=True).or_(f"{col}.is.null,{col}.eq."))
        return r.count or 0

    def missing_array(col: str) -> int:
        r = _execute(client.table("compspro").select("id", count="exact", head=True).or_(f"{col}.is.null,{col}.eq.{{}}"))
        return r.count or 0

    embedding_missing = _execute(client.table("compspro").select("id", count="exact", head=True).is_("embedding", "null"))

    return {
        "total":           count_compspro(),
        "linkedin_url":    missing_text("linkedin_url"),
        "logo_url":        missing_text("logo_url"),
        "flaticon_url":    missing_text("flaticon_url"),
        "description":     missing_text("description"),
        "country":         missing_text("country"),
        "sub_subsectors":  missing_array("sub_subsectors"),
        "embedding":       embedding_missing.count or 0,
    }


def get_all_compspro_sectors() -> list[list[str]]:
    """Every compspro row's `sectors` array, for the dashboard's sector-breakdown
    card (Counter'd in dashboard.py) -- fetches only that one column, not the
    full row, since compspro also holds heavy columns (embedding) this doesn't need.
    """
    rows = _paginated_select("compspro", "sectors")
    return [r.get("sectors") or [] for r in rows]


def get_ingestion_health_rows() -> list[dict]:
    """status/created_at/updated_at for every ingestion_queue row (any status,
    no cap), for the dashboard's ingestion-health card (success/error rate, avg
    processing time). Unlike list_ingestions(), never fetches `result`/
    `error_message` (unbounded text/jsonb) since this only needs the three
    columns above.
    """
    return _paginated_select("ingestion_queue", "status, created_at, updated_at")


def get_recent_done_ingestions(limit: int = 25) -> list[dict]:
    """The `limit` most recently completed ingestions, for the dashboard's
    "recent additions" feed -- unlike list_ingestions(), filtered to status='done'
    only and ordered by updated_at (completion time), not created_at (submission
    time), since a feed of "what got added" cares about when it finished.
    """
    client = _client()
    query = (
        client.table("ingestion_queue")
        .select("id, url, result, requested_by_user_id, created_at, updated_at")
        .eq("status", "done")
        .order("updated_at", desc=True)
        .limit(limit)
    )
    return _execute(query).data or []


def get_compspro_by_names(names: list[str]) -> dict[str, dict]:
    """name -> {sectors, flaticon_url} for a given set of startup names.

    Caveat: two rows can share a display name, in which case one silently
    overwrites the other in the returned dict. Kept only as a fallback for
    dashboard.get_recent_additions() rows whose stored ingestion result
    predates the "id" key (see get_compspro_by_ids, the unambiguous version).
    """
    unique = list(set(names))
    if not unique:
        return {}
    client = _client()
    rows = _execute(client.table("compspro").select("name, sectors, flaticon_url").in_("name", unique)).data or []
    return {r["name"]: r for r in rows}


def get_compspro_by_ids(ids: list[str]) -> dict[str, dict]:
    """id -> {sectors, flaticon_url} for a given set of startup ids, for the
    dashboard's "recent additions" feed to show sector badges/logos without a
    per-row round trip, and without get_compspro_by_names()'s name-collision
    risk. Empty dict for an empty input, no query.
    """
    unique = [i for i in set(ids) if i is not None]
    if not unique:
        return {}
    client = _client()
    rows = _execute(client.table("compspro").select("id, sectors, flaticon_url").in_("id", unique)).data or []
    return {r["id"]: r for r in rows}


def get_users_by_ids(ids: list[int]) -> dict[int, str]:
    """id -> email for a given set of user ids, for the dashboard's "added by"
    column (ingestion_queue.requested_by_user_id / compspro.added_by_user_id).
    Empty dict for an empty input, no query -- e.g. an all-NULL/CLI-submitted batch.
    """
    unique = [i for i in set(ids) if i is not None]
    if not unique:
        return {}
    client = _client()
    rows = _execute(client.table("users").select("id, email").in_("id", unique)).data or []
    return {r["id"]: r["email"] for r in rows}


def get_api_call_log_rows() -> list[dict]:
    """Every api_call_log row (narrow columns only, no `label`), for the
    dashboard's cost aggregates -- grouped/summed in Python by dashboard.py
    (by call_type, by model, by day, by ingestion_queue_id).
    """
    return _paginated_select(
        "api_call_log",
        "ingestion_queue_id, call_type, model, prompt_tokens, completion_tokens, cost_usd, item_count, created_at",
    )
