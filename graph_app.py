# pip install fastapi uvicorn supabase python-dotenv
# python graph_app.py  →  open http://localhost:8000

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from postgrest.exceptions import APIError
from starlette.middleware.sessions import SessionMiddleware
from storage import _client, enqueue_ingestion, mark_processing, mark_done, mark_error, get_pending_ingestions, list_ingestions, get_ingestion, retry_ingestion, delete_ingestion, mark_done_rows_seen, get_ingestion_summary, get_users_by_ids, create_user, get_user_by_email, normalize_domain
from main import ingest as ingest_startup
import auth
import dashboard
import net_security
from rate_limit import SlidingWindowRateLimiter

load_dotenv()

# In-process ingestion queue (Epic 6, Story 6.1) -- /api/ingest enqueues here and
# returns immediately instead of awaiting main.ingest() directly. Holds
# (row_id, url, requested_by_user_id) tuples. asyncio.Queue() doesn't need a running event loop to construct on
# Python 3.11 (the old loop-binding-at-construction behavior was removed), so a
# module-level instance is safe here.
_ingestion_queue: asyncio.Queue = asyncio.Queue()

# Row ids currently sitting in _ingestion_queue or being processed by the
# worker -- lets _ingestion_reconciler tell "genuinely in flight" apart from
# "orphaned" among the DB's 'queued'/'processing' rows (see that function).
_tracked_ingestion_ids: set[int] = set()

# Holds the worker's asyncio.Task so it isn't garbage-collected mid-run --
# asyncio.create_task() only keeps a *weak* reference internally; a Task with no
# other strong reference can be silently collected before it finishes.
_ingestion_worker_task: asyncio.Task | None = None
_ingestion_reconciler_task: asyncio.Task | None = None

_RECONCILE_INTERVAL_S = 300


async def _ingestion_worker() -> None:
    """Consumes _ingestion_queue one row at a time (concurrency=1) -- exactly one
    worker coroutine, so Mistral rate limits are absorbed by this serialization
    rather than hit concurrently by overlapping background ingests (AC #3). Every
    status transition is written to the DB immediately via storage.py (AD-1) --
    the DB is the source of truth, this in-process queue is just a low-latency
    trigger and is disposable (see _lifespan's startup-recovery sweep, AC #4).

    Code review (2026-08-28): the whole loop body is wrapped in try/except so a
    transient DB/network error on one row can't kill the sole worker coroutine
    for the rest of the process's life -- previously mark_processing/mark_done/
    mark_error could raise straight out of the loop with nothing to catch it.
    Supabase calls are wrapped in asyncio.to_thread since they're synchronous
    and would otherwise block the event loop for every other request.
    """
    while True:
        row_id, url, requested_by_user_id = await _ingestion_queue.get()
        try:
            try:
                await asyncio.to_thread(mark_processing, row_id)
            except Exception as e:
                # Code review (2026-08-29): without this fallback, a row whose
                # mark_processing write fails is dropped from the in-memory
                # queue but left at 'queued' (or 'processing' if the write
                # actually landed) in the DB -- neither a fresh /api/ingest
                # (which refuses to re-push an already-queued/processing row)
                # nor the Story 6.3 retry button (which only accepts 'error'
                # rows) can ever recover it; only a full app restart's
                # recovery sweep can. Marking it 'error' instead makes the
                # failure visible and retriable from the UI.
                print(f"[ingestion worker] mark_processing failed for row {row_id} ({url}), marking as error instead of leaving it stuck at 'queued'/'processing': {e}")
                await asyncio.to_thread(mark_error, row_id, f"Failed to mark as processing: {e}")
                continue
            try:
                # interactive=False: no browser is waiting on a background job,
                # so it can use main.ingest()'s more patient batch retry/timeout
                # budget instead of the tight one meant to keep a browser client
                # from stalling -- the default (interactive=True) would give up
                # on a transient Mistral rate limit faster than necessary here.
                result = await ingest_startup(url, interactive=False, added_by_user_id=requested_by_user_id, ingestion_queue_id=row_id)
            except Exception as e:
                await asyncio.to_thread(mark_error, row_id, str(e))
            else:
                try:
                    await asyncio.to_thread(mark_done, row_id, result)
                except Exception as e:
                    # Code review (2026-08-29): ingest_startup already succeeded
                    # but persisting 'done' failed (storage._set_ingestion_status
                    # already retries transient errors -- this is the case where
                    # even that gave up). Without this fallback the row stays at
                    # 'processing' forever with no requeue, showing a permanently
                    # spinning badge. Marking it 'error' instead makes the failure
                    # visible and stops the next restart's recovery sweep from
                    # silently re-running the whole pipeline for a startup that
                    # already finished.
                    print(f"[ingestion worker] mark_done failed for row {row_id} ({url}), marking as error instead of leaving it stuck at 'processing': {e}")
                    await asyncio.to_thread(mark_error, row_id, f"Ingestion succeeded but saving the result failed: {e}")
        except Exception as e:
            print(f"[ingestion worker] unexpected failure on row {row_id} ({url}): {e}")
        finally:
            _tracked_ingestion_ids.discard(row_id)
            _ingestion_queue.task_done()


async def _ingestion_reconciler() -> None:
    """Periodic version of _lifespan's startup-recovery sweep -- catches a row
    that gets orphaned *while the app keeps running*, not just across a
    restart.

    A 'queued' row can lose its spot in the in-memory _ingestion_queue without
    ever reaching the worker: api_ingest's insert-then-push is two steps
    (storage.enqueue_ingestion's DB insert, then queue.put()), and if the
    insert's response is lost to a transient error (this environment's
    Supabase client hits httpx.RemoteProtocolError: Server disconnected
    often enough to see in practice) after the insert already committed
    server-side, the exception fires before queue.put() ever runs. asyncio.
    shield (see api_ingest) only protects against the *caller* disconnecting
    mid-request; it does nothing for this case. Previously the only recovery
    was a full app restart (_lifespan's sweep) -- meaning a demo user's
    ingestion could silently vanish behind a permanently-spinning "queued"
    badge with no way to retry it (api_retry_ingestion only accepts
    status='error' rows, deliberately, since re-pushing a row that's still
    legitimately in flight would run main.ingest() twice).

    _tracked_ingestion_ids is how this tells "genuinely in flight" apart from
    "orphaned": every push (here, _lifespan's sweep, api_ingest,
    api_retry_ingestion) adds its row id before/with the queue.put(), and the
    worker discards it once the row leaves 'queued'/'processing'. A pending
    DB row whose id isn't in that set was never pushed (or its push was lost)
    and is safe to push now without risking a duplicate run.
    """
    while True:
        await asyncio.sleep(_RECONCILE_INTERVAL_S)
        try:
            pending = await asyncio.to_thread(get_pending_ingestions)
        except Exception as e:
            print(f"[ingestion reconciler] sweep failed, will retry next interval: {e}")
            continue
        for row in pending:
            if row["id"] in _tracked_ingestion_ids:
                continue
            print(f"[ingestion reconciler] recovering orphaned row {row['id']} ({row['url']}), stuck at '{row['status']}' with no in-memory tracker")
            _tracked_ingestion_ids.add(row["id"])
            await _ingestion_queue.put((row["id"], row["url"], row.get("requested_by_user_id")))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup-recovery sweep (AC #4): re-enqueue any row still 'queued'/
    'processing' from a previous run before the worker starts consuming new
    requests -- a crash between items must not silently strand a row forever.
    Then start the single worker coroutine (concurrency=1, AC #3) and the
    periodic reconciler that catches the same kind of orphaning without
    requiring a restart (see _ingestion_reconciler).

    Code review (2026-08-28): the sweep is wrapped in try/except so a Supabase
    outage at boot doesn't prevent the whole app (not just ingestion) from
    starting -- it logs and continues with an empty pending list instead.
    """
    global _ingestion_worker_task, _ingestion_reconciler_task
    try:
        pending = await asyncio.to_thread(get_pending_ingestions)
    except Exception as e:
        print(f"[ingestion worker] startup-recovery sweep failed, continuing with an empty queue: {e}")
        pending = []
    for row in pending:
        _tracked_ingestion_ids.add(row["id"])
        await _ingestion_queue.put((row["id"], row["url"], row.get("requested_by_user_id")))
    _ingestion_worker_task = asyncio.create_task(_ingestion_worker())
    _ingestion_reconciler_task = asyncio.create_task(_ingestion_reconciler())
    yield


app = FastAPI(lifespan=_lifespan)
os.makedirs("assets/logos", exist_ok=True)  # fresh clone: git doesn't track empty dirs
app.mount("/assets", StaticFiles(directory="assets"), name="assets")


# Auth-gating allowlist: exact-path or prefix match only (not regex), to keep
# it auditable at a glance (Design Notes). Everything not listed here requires
# a valid session.
_ALLOWLIST_EXACT = {"/login", "/signup", "/api/login", "/api/signup"}
_ALLOWLIST_PREFIXES = ("/assets/",)


def _is_allowlisted(path: str) -> bool:
    return path in _ALLOWLIST_EXACT or any(path.startswith(p) for p in _ALLOWLIST_PREFIXES)


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """Single choke point for auth (spec-public-demo-auth.md) instead of
    per-route Depends, to keep the diff small across the app's 12 pre-existing
    routes. Order of checks: allowlist -> session presence -> owner-only
    /graph, /api/graph/all block. /api/graph/{domain} (per-startup neighborhood,
    used by /startup/{domain}) is deliberately NOT blocked here -- the PRD marks
    /startup/{domain} as eligible for public/beta opening, unlike the full graph.

    request.state.user is set here (to the session's user dict, or None) so
    index()/startup_page() can read it synchronously afterwards to decide
    whether to render the Graph nav link, without a second DB round-trip --
    by the time either handler runs, a None user has already been redirected
    away by this middleware, so in practice they only ever see a real user.
    """
    path = request.url.path
    if _is_allowlisted(path):
        # The "already logged in" redirect below only makes sense for the
        # auth-FORM routes (_ALLOWLIST_EXACT) -- an already-logged-in caller
        # hitting /login or /signup would otherwise silently spawn a second
        # account or clobber their own session, since these paths never reach
        # the checks below (step-04 review). It must NOT apply to the
        # _ALLOWLIST_PREFIXES bucket (/assets/*): those are static files that
        # have to be served unconditionally regardless of session state, or
        # every logged-in request for a startup's favicon gets redirected to
        # "/" (HTML) instead of the image, breaking every icon whose browser
        # cache doesn't already have a copy from an earlier session.
        if path in _ALLOWLIST_EXACT:
            user = auth.get_current_user(request)
            if user is not None:
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "Déjà connecté."}, status_code=400)
                return RedirectResponse(url="/", status_code=303)
        return await call_next(request)

    user = auth.get_current_user(request)
    request.state.user = user

    if user is None:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Authentification requise."}, status_code=401)
        return RedirectResponse(url="/login", status_code=303)

    if not user.get("is_owner") and path in ("/graph", "/api/graph/all", "/admin", "/api/dashboard"):
        return JSONResponse({"detail": "Accès réservé."}, status_code=403)

    return await call_next(request)


# Server-side sessions (spec-public-demo-auth.md): a signed httponly cookie,
# no new session table -- the cookie itself carries {id, email, is_owner}.
# Registered AFTER auth_gate: Starlette's add_middleware()/@app.middleware("http")
# both insert at the front of the middleware stack, so whichever is registered
# LAST ends up OUTERMOST (runs first). SessionMiddleware must run before
# auth_gate reads request.session, so it must be added after auth_gate is
# registered above -- registering it earlier (as originally written) put
# auth_gate outside SessionMiddleware and crashed every request with
# "SessionMiddleware must be installed to access request.session".
#
# Fails fast at import time on a blank secret (step-04 review) -- unlike
# OWNER_EMAIL/MAX_USERS, an empty SESSION_SECRET_KEY isn't a "closed" state,
# it's a weak, guessable signing key silently accepted by itsdangerous, so
# this must refuse to start rather than degrade quietly.
_session_secret = os.environ["SESSION_SECRET_KEY"]
if not _session_secret.strip():
    raise RuntimeError("SESSION_SECRET_KEY must not be blank.")
# SESSION_COOKIE_HTTPS_ONLY defaults to false because no TLS-terminated host
# is chosen yet (Verification is run over http://localhost) -- flip it to
# "true" once the demo is deployed behind HTTPS. max_age is shortened from
# Starlette's 14-day default to 7 days, more appropriate for a time-boxed demo.
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    https_only=os.environ.get("SESSION_COOKIE_HTTPS_ONLY", "false").strip().lower() == "true",
    max_age=7 * 24 * 60 * 60,
)


@app.middleware("http")
async def asset_security_headers(request: Request, call_next):
    """Every response under /assets/ -- 200s, 304s, 404s, and even a
    hypothetical redirect/401 that some future change to auth_gate's
    allowlist logic might produce for this prefix -- gets nosniff and a
    strict per-file CSP, so a saved file (a favicon/logo this app downloaded
    from an arbitrary third-party site) can never execute in this app's own
    origin no matter how it's reached:
      - X-Content-Type-Options: nosniff stops the browser from ignoring our
        asserted Content-Type based on sniffing the file's actual bytes --
        StaticFiles derives Content-Type from the file extension, which is
        itself app-controlled (main.py's _classify_downloaded_image), but a
        browser's own content-sniffing heuristic is a second, independent
        thing this closes off.
      - Content-Security-Policy: sandbox; default-src 'none' treats a direct
        navigation to (or <iframe>/<object> embed of) one of these files as
        coming from a unique, script-disabled origin with no ability to load
        further resources. This does NOT affect an <img> reference to the
        same URL from a normal page -- <img> never executes an SVG's
        embedded script or evaluates its own CSP as a document in the first
        place, so every existing flaticon_url/logo_url <img> usage keeps
        working exactly as before.

    Registered LAST (after auth_gate and SessionMiddleware above), so it's
    the OUTERMOST middleware -- Starlette's add_middleware()/
    @app.middleware("http") both insert at the front of the stack, meaning
    whichever is registered last wraps every other layer (same rule the
    SessionMiddleware comment above documents). This matters here
    specifically: /assets/ is allowlisted in auth_gate today and always
    falls through to StaticFiles, but if that allowlist were ever
    registered BEFORE auth_gate, a redirect/401 auth_gate might someday
    produce for an /assets/ path (e.g. a future bug in the allowlist check)
    would bypass it entirely, since an inner middleware never runs unless
    the outer one calls call_next(). Being outermost means every response
    for this prefix passes through here regardless of what any inner layer
    -- auth_gate, StaticFiles' own 404/405 HTTPExceptions, anything -- does
    with the request.
    """
    response = await call_next(request)
    if request.url.path.startswith("/assets/"):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return response


SECTOR_COLORS_JS = """
const SECTOR_COLORS = {
  "AI & Machine Learning":      "#4e79a7",
  "Cybersecurity":              "#e15759",
  "SaaS & Enterprise Software": "#59a14f",
  "FinTech":                    "#f28e2b",
  "HealthTech":                 "#ff9da7",
  "Developer Tools":            "#9c755f",
  "Robotics":                   "#bab0ac",
  "CleanTech":                  "#76b7b2",
  "EdTech":                     "#edc948",
  "E-commerce & Retail":        "#b07aa1",
  "Marketing Tech":             "#d4a6c8",
  "Deep Tech":                  "#499894",
  "Life Sciences":              "#86bcb6",
  "Aerospace":                  "#8cd17d",
  "Energy":                     "#f1ce63",
  "Mobility":                   "#a0cbe8",
  "SpaceTech":                  "#c7a8d8",
  "Defense":                    "#d7b5a6",
};
const DEFAULT_COLOR = "#777777";
function sectorColor(sectors) {
  return SECTOR_COLORS[(sectors || [])[0]] || DEFAULT_COLOR;
}
"""


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/search")
def api_search(q: str = ""):
    q = q.strip()
    if len(q) < 2:
        return []
    db = _client()
    # Matched against the normalized `domain` column, not a raw substring of
    # `website` -- an arbitrary "%domain_q%" ILIKE matches any stored domain
    # that merely *contains* domain_q anywhere (e.g. querying "ed.ai" used to
    # match "nevermined.ai", since it literally ends in "...ed.ai"), which
    # wrongly told the caller the startup already existed and hid the
    # "Ajouter" button. Anchored as a prefix ("domain_q%") instead: still
    # matches while the user is progressively typing a domain (e.g. "neo"
    # against "neo.ai"), but a candidate domain can only match query strings
    # it actually starts with.
    domain_q = normalize_domain(q)
    filters = [f"name.ilike.%{q}%"]
    if domain_q:
        filters.append(f"domain.ilike.{domain_q}%")
    rows = (
        db.table("compspro")
        .select("name, sectors, subsectors, description, flaticon_url, website, domain")
        .or_(",".join(filters))
        .limit(10)
        .execute()
    )
    return rows.data or []


async def _validate_ingest_url(url: str) -> None:
    """SSRF guard (net_security.py): scheme/credentials/IP-literal checks
    plus a DNS resolution of the hostname, rejecting anything that resolves
    to loopback/private/link-local/CGNAT/multicast/unspecified. Shared by
    api_ingest and api_retry_ingestion -- a retry re-runs the full
    scrape/LLM pipeline against a URL submitted (possibly long) in the past,
    and DNS can change between then and now, so a retry needs this exact
    same check, not just the original submission.

    This is a point-in-time check -- main.scrape() (run by the background
    worker, not this handler) re-validates every redirect and subresource at
    fetch time regardless, since DNS can also change between this check and
    the moment of actual connection.
    """
    try:
        await asyncio.to_thread(net_security.assert_safe_url, url)
    except net_security.UnsafeURLError as e:
        raise HTTPException(status_code=400, detail=f"URL refusée : {e}")


async def _enforce_ingestion_quota(user: dict) -> None:
    """Cost/abuse guardrail shared by api_ingest and api_retry_ingestion:
    each ingestion runs a real browser render plus several Mistral calls.
    Owner is exempt, same convention as the /graph, /api/graph/all, /admin,
    /api/dashboard owner-only block in auth_gate above.

    Fails closed on a quota-check failure (auth.QuotaCheckError, e.g. a
    Supabase outage): a non-owner user is refused with 503 rather than let
    the ingestion through because the count couldn't be verified, or let the
    exception surface as an opaque 500. The owner is never subject to this
    check at all, so an outage never blocks them.
    """
    if user.get("is_owner"):
        return
    try:
        quota_reached = await asyncio.to_thread(auth.ingestion_quota_reached, user["id"])
    except auth.QuotaCheckError as e:
        print(f"[ingestion quota] check failed, failing closed for user {user['id']}: {e}")
        raise HTTPException(status_code=503, detail="Impossible de vérifier le quota, réessayez plus tard.")
    if quota_reached:
        raise HTTPException(status_code=429, detail="Quota quotidien d'ingestions atteint, réessayez demain.")


@app.post("/api/ingest", status_code=202)
async def api_ingest(url: str, request: Request):
    """Enqueue a startup for background ingestion and return immediately (Epic 6,
    Story 6.1) -- does not await main.ingest() directly anymore, so adding a
    startup never blocks the caller for the whole scrape/extract/score pipeline.
    Status/result/error are tracked in ingestion_queue, not this response.

    request.state.user is set by auth_gate (this path isn't allowlisted, so
    it's always a logged-in user by the time this handler runs) -- its id is
    stored as ingestion_queue.requested_by_user_id and later becomes
    compspro.added_by_user_id once the ingestion completes.
    """
    url = url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL manquante.")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    await _validate_ingest_url(url)

    user = request.state.user
    user_id = user["id"]
    await _enforce_ingestion_quota(user)

    async def _enqueue_and_push() -> tuple[dict, bool]:
        row, is_new = await asyncio.to_thread(enqueue_ingestion, url, user_id)
        if is_new:
            # A reused row (already queued or actively processing for this same
            # URL) is not re-pushed -- the worker already has it, or will pick it
            # up via the startup-recovery sweep. Pushing it again would let the
            # worker run main.ingest() twice for one row (code review, 2026-08-28).
            _tracked_ingestion_ids.add(row["id"])
            await _ingestion_queue.put((row["id"], url, user_id))
        return row, is_new

    try:
        # Shielded (code review 2026-09-04): a client disconnect (closed tab,
        # navigation, flaky network) cancels this handler's task at whatever
        # await point it's sitting on. asyncio.to_thread's DB insert isn't
        # interruptible and lands regardless, but without shielding, the
        # cancellation can strike between that insert and the queue.put()
        # below -- leaving a 'queued' row in the DB that never reaches the
        # in-memory worker queue. Since enqueue_ingestion treats an existing
        # 'queued' row as already handled, resubmitting that same URL later
        # just finds the stuck row and no-ops; only an app restart's
        # startup-recovery sweep would ever pick it back up. Shielding keeps
        # insert+push atomic from the caller's perspective regardless of
        # disconnect.
        row, is_new = await asyncio.shield(_enqueue_and_push())
    except ValueError as e:
        # Code review (2026-08-29): enqueue_ingestion now rejects a
        # malformed/host-less url (empty normalize_domain()) with a
        # ValueError -- a bad request from this endpoint's own caller, not a
        # server-side failure, so 400 rather than 502.
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Échec de la mise en file d'attente : {e}")

    # A reused row (is_new=False) submitted by someone else must not be
    # exposed to this caller at all -- not its id, not its status -- for a
    # non-owner: with per-user visibility now enforced everywhere else (see
    # api_ingestion_queue et al.), handing back an id the caller can never
    # subsequently see or act on would just be a confusing dead end, and it
    # still leaks the fact that this exact domain is already tracked by
    # *someone*. The owner is exempt (they can see/reuse any row, matching
    # their unrestricted visibility elsewhere).
    if not is_new and not user.get("is_owner") and row.get("requested_by_user_id") != user_id:
        raise HTTPException(status_code=409, detail="Cette URL est déjà en cours de traitement.")

    return {"id": row["id"]}


def _ingestion_scope_owner_filter(user: dict, scope: str) -> int | None:
    """The requested_by_user_id to filter ingestion_queue on for a read
    (list/summary), or None for "no filter" (unrestricted, including
    orphaned rows) -- the legacy/default behavior, only ever valid for the
    owner.

    A non-owner ALWAYS gets their own id back regardless of `scope` -- the
    query param can only ever WIDEN what the owner sees (all vs. mine), it
    can never be used by a non-owner to widen their own view beyond their
    own rows. scope="mine" narrows the owner down to the same "own rows
    only" view a non-owner always gets; anything else (including the
    default "all") is unrestricted.
    """
    if not user.get("is_owner"):
        return user["id"]
    return user["id"] if scope == "mine" else None


def _ingestion_owner_scope_id(user: dict) -> int | None:
    """The requested_by_user_id ownership filter for a single-row mutation
    (retry/delete) -- None for the owner (unrestricted, matches their
    unrestricted read access), else the caller's own id. Unlike the read-side
    scope filter above, mutations have no "all" vs. "mine" toggle: the owner
    can always act on any row, a non-owner only ever their own.
    """
    return None if user.get("is_owner") else user["id"]


@app.get("/api/ingestion-queue")
def api_ingestion_queue(request: Request, status: str | None = None, scope: str = "all"):
    """ingestion_queue rows, most recent first, for the "En attente" tab
    (Epic 6, Story 6.2). Plain `def` like /api/search -- FastAPI runs it in its
    own thread pool automatically, no asyncio.to_thread needed here.

    status (currently only 'error' is used, by the drawer's Échecs tab) is
    passed straight through to storage.list_ingestions -- an uncapped view so
    an old failure can't drop off the panel just because enough newer rows of
    other statuses exist (see that function's docstring).

    Per-user visibility: a non-owner only ever sees their own rows (never an
    orphaned row with no requested_by_user_id, which only the owner can see)
    -- enforced by storage.list_ingestions' query-level filter, not by
    fetching everything and discarding rows here. The owner sees everything
    by default (scope="all", matching pre-existing behavior) or can narrow
    to their own rows with scope="mine". The owner's response additionally
    carries requester_email per row (None for an orphaned row) -- a non-owner
    never needs it, since every row they see is already their own.
    """
    user = request.state.user
    requested_by_user_id = _ingestion_scope_owner_filter(user, scope)
    try:
        rows = list_ingestions(status=status, requested_by_user_id=requested_by_user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if user.get("is_owner"):
        emails = get_users_by_ids([r.get("requested_by_user_id") for r in rows])
        for r in rows:
            r["requester_email"] = emails.get(r.get("requested_by_user_id"))
    return rows


@app.post("/api/ingestion-queue/{id}/retry", status_code=202)
async def api_retry_ingestion(id: int, request: Request):
    """Reset an errored ingestion_queue row to 'queued' and re-push it onto
    the worker's queue (Epic 6, Story 6.3) -- writing 'queued' to the DB alone
    doesn't wake the worker, since it consumes the in-memory _ingestion_queue,
    not a DB poll. async def like api_ingest, since it awaits queue.put().

    Re-validated exactly like a fresh /api/ingest submission (SSRF guard +
    daily quota) before the row is actually re-queued -- without this, a
    "Relancer" click on an old failure would (a) skip the SSRF check
    entirely for a URL that may have started resolving somewhere unsafe
    since it was first submitted, and (b) be a free way to keep re-running
    the full scrape/LLM pipeline forever without ever touching the daily
    quota, since it doesn't go through api_ingest at all. Charged against
    the CALLER's quota (request.state.user), not the original row's
    requested_by_user_id -- it's the caller's click driving this run.

    Ownership: get_ingestion/retry_ingestion both take the caller's ownership
    scope (None for the owner, else their own id) as a query-level filter --
    a non-owner's row lookup for someone else's (or an orphaned) row simply
    finds nothing, indistinguishable from a bad id, so this returns 404
    (never 403) either way.
    """
    owner_scope_id = _ingestion_owner_scope_id(request.state.user)
    row = await asyncio.to_thread(get_ingestion, id, owner_scope_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Élément introuvable ou n'est pas en échec.")

    await _validate_ingest_url(row["url"])
    await _enforce_ingestion_quota(request.state.user)

    async def _retry_and_push() -> dict:
        row = await asyncio.to_thread(retry_ingestion, id, owner_scope_id)
        _tracked_ingestion_ids.add(row["id"])
        await _ingestion_queue.put((row["id"], row["url"], row.get("requested_by_user_id")))
        return row

    try:
        # Shielded for the same reason as api_ingest above: a client
        # disconnect between the DB write (retry_ingestion, not
        # interruptible mid-flight via to_thread) and the queue.put() below
        # would otherwise strand the row at 'queued' with nothing to ever
        # push it onto the in-memory worker queue again.
        row = await asyncio.shield(_retry_and_push())
    except ValueError:
        raise HTTPException(status_code=404, detail="Élément introuvable ou n'est pas en échec.")
    except APIError as e:
        if e.code == "23505":
            raise HTTPException(status_code=409, detail="Cette URL est déjà en cours de traitement.")
        raise HTTPException(status_code=502, detail=f"Échec de la relance : {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Échec de la relance : {e}")
    return {"id": row["id"]}


@app.delete("/api/ingestion-queue/{id}", status_code=204)
async def api_delete_ingestion(id: int, request: Request):
    """Permanently remove an errored ingestion_queue row so the "En attente"
    tab can be cleared of stale failures. async def to match the retry
    endpoint's shape, even though this one never touches _ingestion_queue.

    Ownership: same query-level filter as the retry endpoint -- a non-owner
    deleting someone else's (or an orphaned) row matches nothing and gets a
    404, never a 403.
    """
    owner_scope_id = _ingestion_owner_scope_id(request.state.user)
    try:
        await asyncio.to_thread(delete_ingestion, id, owner_scope_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Élément introuvable ou n'est pas en échec.")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Échec de la suppression : {e}")


@app.get("/api/ingestion-queue/summary")
def api_ingestion_queue_summary(request: Request, scope: str = "all"):
    """Counts backing the "En attente" tab's two notification badges (Epic 6,
    Story 6.4). Plain `def` like /api/search/GET /api/ingestion-queue --
    doesn't touch _ingestion_queue, nothing to await.

    Scoped exactly like GET /api/ingestion-queue above -- a non-owner's
    badges only ever count rows they can actually see.
    """
    user = request.state.user
    requested_by_user_id = _ingestion_scope_owner_filter(user, scope)
    return get_ingestion_summary(requested_by_user_id=requested_by_user_id)


@app.post("/api/ingestion-queue/mark-seen")
def api_mark_ingestion_seen(request: Request):
    """Bulk-marks currently-done rows as seen (Epic 6, Story 6.4), called once
    when the "En attente" tab opens. Plain `def`, no request body.

    Always scoped to the caller's own rows -- one user's "seen" state must
    never dismiss another user's unseen-done badge. The owner's call
    additionally covers orphaned rows (no requested_by_user_id at all): an
    orphaned row is only ever visible to the owner (see
    api_ingestion_queue), so nobody else could ever otherwise clear its
    unseen flag.
    """
    user = request.state.user
    marked = mark_done_rows_seen(requested_by_user_id=user["id"], include_orphaned=bool(user.get("is_owner")))
    return {"marked": marked}


@app.get("/api/dashboard")
def api_dashboard():
    """Aggregated stats for the owner-only /admin page (2026-09-04 conversation):
    startup/link/user counts, data completeness, sector breakdown, ingestion
    health, Mistral cost, and a recent-additions feed. Owner-gating is enforced
    by auth_gate (Design Notes), not here. Plain `def` like the other read-only
    endpoints above -- every storage.py call underneath is synchronous.
    """
    return dashboard.get_dashboard()


@app.get("/api/graph/all")
def api_graph_all():
    db = _client()

    # Supabase caps responses at 1000 rows — paginate to fetch every startup
    startups: list[dict] = []
    page, size = 0, 1000
    while True:
        batch = (
            db.table("compspro")
            .select("id, name, domain, sectors, flaticon_url, logo_url, description, website, linkedin_url")
            .range(page, page + size - 1)
            .execute()
            .data or []
        )
        startups.extend(batch)
        if len(batch) < size:
            break
        page += size

    # Same 1000-row cap as the startups query above — paginate or links get silently truncated
    links_raw: list[dict] = []
    page, size = 0, 1000
    while True:
        batch = (
            db.table("competitors")
            .select("company_a_id, company_b_id, score")
            .eq("active", True)
            .range(page, page + size - 1)
            .execute()
            .data or []
        )
        links_raw.extend(batch)
        if len(batch) < size:
            break
        page += size
    # Nodes are identified by domain (unique) rather than name (two startups
    # can share a display name) -- a row with no domain yet (pre-migration,
    # no website) can't be placed unambiguously, so it's skipped here.
    nodes = [
        {
            "domain":      s["domain"],
            "name":        s["name"],
            "sectors":     s.get("sectors")     or [],
            "flaticon_url":    s.get("flaticon_url")    or "",
            "logo_url":    s.get("logo_url")    or "",
            "description": s.get("description") or "",
            "website":     s.get("website")     or "",
            "linkedin_url": s.get("linkedin_url") or "",
        }
        for s in startups if s.get("domain")
    ]
    id_to_domain = {s["id"]: s["domain"] for s in startups if s.get("domain")}
    # Drop links whose endpoints are missing from nodes — one bad link
    # would make d3.forceLink throw and blank the whole graph
    node_domains = {n["domain"] for n in nodes}
    links = [
        {"source": id_to_domain[r["company_a_id"]], "target": id_to_domain[r["company_b_id"]], "score": r.get("score") or 0}
        for r in links_raw
        if r.get("company_a_id") in id_to_domain and r.get("company_b_id") in id_to_domain
        and id_to_domain[r["company_a_id"]] in node_domains and id_to_domain[r["company_b_id"]] in node_domains
    ]
    return {"nodes": nodes, "links": links}


@app.get("/api/graph/{domain}")
def api_graph(domain: str):
    db = _client()

    center_rows = (
        db.table("compspro")
        .select("id, name, domain, sectors, subsectors, description, website, flaticon_url, logo_url, linkedin_url")
        .eq("domain", domain)
        .limit(1)
        .execute()
        .data or []
    )
    if not center_rows:
        return {"center": None, "nodes": [], "links": []}
    center_row = center_rows[0]
    center_id = center_row["id"]

    as_a = db.table("competitors").select("company_b_id, score").eq("company_a_id", center_id).eq("active", True).execute().data or []
    as_b = db.table("competitors").select("company_a_id, score").eq("company_b_id", center_id).eq("active", True).execute().data or []

    neighbor_ids: set[str] = set()
    edges: list[tuple[str, str, float]] = []
    seen_pairs: set[frozenset] = set()

    for row in as_a:
        b_id = row["company_b_id"]
        if b_id is None:
            continue
        neighbor_ids.add(b_id)
        pair = frozenset({center_id, b_id})
        if pair not in seen_pairs:
            seen_pairs.add(pair)
            edges.append((center_id, b_id, row.get("score", 0)))

    for row in as_b:
        a_id = row["company_a_id"]
        if a_id is None:
            continue
        neighbor_ids.add(a_id)
        pair = frozenset({center_id, a_id})
        if pair not in seen_pairs:
            seen_pairs.add(pair)
            edges.append((a_id, center_id, row.get("score", 0)))

    neighbors = (
        db.table("compspro")
        .select("id, name, domain, sectors, subsectors, description, website, flaticon_url, logo_url, linkedin_url")
        .in_("id", list(neighbor_ids))
        .execute()
        .data or []
    ) if neighbor_ids else []
    by_id = {r["id"]: r for r in neighbors}
    by_id[center_id] = center_row

    def node(company_id: str) -> dict:
        s = by_id.get(company_id, {})
        return {
            "domain":      s.get("domain")      or "",
            "name":        s.get("name")        or "",
            "sectors":     s.get("sectors")     or [],
            "subsectors":  s.get("subsectors")  or [],
            "description": s.get("description") or "",
            "website":     s.get("website")     or "",
            "flaticon_url":    s.get("flaticon_url")    or "",
            "logo_url":    s.get("logo_url")    or "",
            "linkedin_url": s.get("linkedin_url") or "",
        }

    links = [
        {"source": by_id[a]["domain"], "target": by_id[b]["domain"], "score": score}
        for a, b, score in edges
        if a in by_id and b in by_id
    ]

    return {
        "center": node(center_id),
        "nodes":  [node(nid) for nid in neighbor_ids if nid in by_id],
        "links":  links,
    }


# ── Auth ──────────────────────────────────────────────────────────────────────
# JSON request bodies (not query params, unlike /api/ingest's url: str) so
# passwords never land in a URL query string -- which uvicorn's access log,
# any reverse proxy, and browser history would otherwise capture, at odds
# with "passwords ... never logged" (spec Boundaries & Constraints).

class SignupRequest(BaseModel):
    email: str
    password: str = Field(min_length=8)


class LoginRequest(BaseModel):
    email: str
    password: str


def _looks_like_email(email: str) -> bool:
    """Cheap shape check, not full RFC validation -- deliberately avoids
    pulling in a new dependency (pydantic's EmailStr needs email-validator)
    for a demo signup form. Just enough to reject "not-an-email" (step-04
    review), which would otherwise be silently accepted and stored.
    """
    local, _, domain = email.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".") and not domain.endswith(".")


@app.post("/api/signup", status_code=201)
def api_signup(body: SignupRequest, request: Request):
    """I/O matrix: seats free -> 201 + session, cap reached -> 403 (owner
    exempt), owner email -> 403 reserved (owner is seeded out-of-band by
    seed_owner.py, not created here -- step-04 review, iteration 1), duplicate
    email -> 409.
    """
    email = auth.normalize_email(body.email)
    password = body.password
    if not email or not password.strip():
        raise HTTPException(status_code=400, detail="Email et mot de passe requis.")
    if not _looks_like_email(email):
        raise HTTPException(status_code=400, detail="Adresse email invalide.")
    if auth.is_owner_email(email):
        raise HTTPException(status_code=403, detail="Cette adresse est réservée.")

    if auth.signup_cap_reached():
        raise HTTPException(status_code=403, detail="Inscriptions fermées.")

    try:
        password_hash = auth.hash_password(password)
    except ValueError:
        raise HTTPException(status_code=400, detail="Mot de passe invalide.")

    try:
        user = create_user(email, password_hash, is_owner=False)
    except APIError as e:
        if e.code == "23505":
            raise HTTPException(status_code=409, detail="Un compte existe déjà avec cet email.")
        print(f"[auth] signup insert failed for {email}: {e!r}")
        raise HTTPException(status_code=502, detail="Échec de l'inscription, réessayez.")
    except Exception as e:
        print(f"[auth] signup insert failed for {email}: {e!r}")
        raise HTTPException(status_code=502, detail="Échec de l'inscription, réessayez.")

    if not user or not user.get("id"):
        print(f"[auth] signup insert returned no row for {email}")
        raise HTTPException(status_code=502, detail="Échec de l'inscription, réessayez.")

    request.session["user"] = {"id": user["id"], "email": email, "is_owner": False}
    return {"email": email, "is_owner": False}


# bcrypt hash of a fixed dummy password, computed once at import time so
# api_login can spend equivalent time on both branches below (step-04
# review) -- checking an unknown email against this instead of short-
# circuiting keeps "unknown email" and "known email, wrong password"
# roughly indistinguishable by response time.
_DUMMY_PASSWORD_HASH = auth.hash_password("not-a-real-password-timing-decoy")

# Login brute-force throttle: two independent, differently-scoped limits --
# neither alone is a full defense.
#
# _LOGIN_IP_RATE_LIMITER counts EVERY attempt (success or failure), keyed by
# request.client.host -- the TCP peer address Starlette/uvicorn records from
# the actual socket, never a client-supplied header (a forged
# X-Forwarded-For is never read anywhere in this file, so it cannot move
# which bucket an attacker is charged against). Catches a single attacker
# hammering many different email addresses from one place.
#
# _LOGIN_EMAIL_IP_FAILURE_LIMITER counts only FAILED attempts, keyed by
# (email, ip) together rather than email alone. Email-alone would let an
# attacker who merely knows a victim's address lock that victim out just by
# submitting wrong passwords for it (an availability attack that needs no
# real guessing at all); keying on the pair instead means that only repeated
# failures from the SAME source against that email are throttled, while a
# legitimate user's own correct logins never count against it at all (only
# failures do). Distributed brute force (many IPs, one target email) isn't
# caught by this pair-keyed limiter, but it still has to get past the
# per-IP limiter on every one of those IPs individually.
#
# Both in-process, resets on restart -- see rate_limit.py's docstring for
# why that's an accepted tradeoff here.
_LOGIN_IP_RATE_LIMITER = SlidingWindowRateLimiter(max_calls=20, window_seconds=60)
_LOGIN_EMAIL_IP_FAILURE_LIMITER = SlidingWindowRateLimiter(max_calls=5, window_seconds=60)


def _login_email_ip_key(email: str, client_ip: str) -> str:
    # "|" can't appear in a normalized email or an IP literal, so this can't
    # collide two distinct (email, ip) pairs onto the same key.
    return f"{email}|{client_ip}"


@app.post("/api/login")
def api_login(body: LoginRequest, request: Request):
    """I/O matrix: valid creds -> 200 + session; invalid creds (wrong
    password or unknown email) -> 401 with the same generic message either
    way, so the response can't be used to enumerate registered emails.
    """
    email = auth.normalize_email(body.email)
    client_ip = request.client.host if request.client else "unknown"
    email_ip_key = _login_email_ip_key(email, client_ip) if email else None

    # The per-IP limiter charges this attempt immediately (every attempt
    # counts, per its docstring above). The per-(email,ip) limiter is only
    # PEEKED here (check(), not allow()/record()) -- it must not charge a
    # hit until we actually know the attempt failed, below.
    ip_ok = _LOGIN_IP_RATE_LIMITER.allow(client_ip)
    email_ip_ok = _LOGIN_EMAIL_IP_FAILURE_LIMITER.check(email_ip_key) if email_ip_key else True
    if not ip_ok or not email_ip_ok:
        raise HTTPException(status_code=429, detail="Trop de tentatives, réessayez plus tard.")

    user = get_user_by_email(email) if email else None
    if user:
        password_ok = auth.verify_password(body.password, user["password_hash"])
    else:
        auth.verify_password(body.password, _DUMMY_PASSWORD_HASH)
        password_ok = False
    if not user or not password_ok:
        if email_ip_key:
            _LOGIN_EMAIL_IP_FAILURE_LIMITER.record(email_ip_key)
        raise HTTPException(status_code=401, detail="Email ou mot de passe incorrect.")

    is_owner = bool(user.get("is_owner"))
    request.session["user"] = {"id": user["id"], "email": user["email"], "is_owner": is_owner}
    return {"email": user["email"], "is_owner": is_owner}


@app.post("/api/logout")
def api_logout(request: Request):
    request.session.clear()
    return {"ok": True}


# ── Pages ─────────────────────────────────────────────────────────────────────

AUTH_PAGE_STYLE = """
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #0f0f0f; color: #e0e0e0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      min-height: 100vh; display: flex; flex-direction: column;
      align-items: center; justify-content: center; padding: 40px 20px;
    }
    h1 { font-size: 1.8rem; font-weight: 700; color: #fff; margin-bottom: 28px; }
    form { width: 100%; max-width: 360px; display: flex; flex-direction: column; gap: 12px; }
    input {
      padding: 14px 16px; font-size: 16px;
      background: #1a1a1a; border: 1px solid #2e2e2e; border-radius: 10px;
      color: #eee; outline: none; transition: border-color 0.15s;
    }
    @media (max-width: 480px) {
      body { padding: 24px 16px; }
      h1 { font-size: 1.5rem; margin-bottom: 20px; }
    }
    input::placeholder { color: #444; }
    input:focus { border-color: #555; }
    button {
      padding: 14px; font-size: 15px; font-weight: 600;
      background: #1a1a1a; border: 1px solid #2e2e2e; border-radius: 10px;
      color: #eee; cursor: pointer; transition: border-color 0.15s, background 0.15s, opacity 0.15s;
    }
    button:hover { border-color: #555; background: #222; }
    button:disabled { cursor: default; opacity: 0.6; }
    .error { color: #d16565; font-size: 13px; text-align: center; min-height: 1.2em; }
    .switch { color: #555; font-size: 13px; text-align: center; margin-top: 8px; }
    .switch a { color: #888; text-decoration: none; }
    .switch a:hover { color: #ccc; }
"""

LOGIN_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Se connecter — Smap</title>
  <style>{AUTH_PAGE_STYLE}</style>
</head>
<body>
  <h1>Smap</h1>
  <form id="auth-form">
    <input id="email" type="email" placeholder="Email" autocomplete="username" required>
    <input id="password" type="password" placeholder="Mot de passe" autocomplete="current-password" required>
    <div class="error" id="error"></div>
    <button id="submit-btn" type="submit">Se connecter</button>
    <div class="switch">Pas de compte ? <a href="/signup">Créer un compte</a></div>
  </form>
  <script>
const form = document.getElementById("auth-form");
const errorEl = document.getElementById("error");
const btn = document.getElementById("submit-btn");

form.addEventListener("submit", e => {{
  e.preventDefault();
  errorEl.textContent = "";
  btn.disabled = true;
  btn.textContent = "Connexion…";
  fetch("/api/login", {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{
      email: document.getElementById("email").value,
      password: document.getElementById("password").value,
    }}),
  }})
    .then(async r => {{
      const body = await r.json();
      if (!r.ok) throw new Error(body.detail || "Erreur inconnue");
      return body;
    }})
    .then(() => {{ window.location.href = "/"; }})
    .catch(err => {{
      btn.disabled = false;
      btn.textContent = "Se connecter";
      errorEl.textContent = err.message;
    }});
}});
  </script>
</body>
</html>"""


SIGNUP_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Créer un compte — Smap</title>
  <style>{AUTH_PAGE_STYLE}</style>
</head>
<body>
  <h1>Smap</h1>
  <form id="auth-form">
    <input id="email" type="email" placeholder="Email" autocomplete="username" required>
    <input id="password" type="password" placeholder="Mot de passe" autocomplete="new-password" required>
    <div class="error" id="error"></div>
    <button id="submit-btn" type="submit">Créer un compte</button>
    <div class="switch">Déjà un compte ? <a href="/login">Se connecter</a></div>
  </form>
  <script>
const form = document.getElementById("auth-form");
const errorEl = document.getElementById("error");
const btn = document.getElementById("submit-btn");

form.addEventListener("submit", e => {{
  e.preventDefault();
  errorEl.textContent = "";
  btn.disabled = true;
  btn.textContent = "Création…";
  fetch("/api/signup", {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{
      email: document.getElementById("email").value,
      password: document.getElementById("password").value,
    }}),
  }})
    .then(async r => {{
      const body = await r.json();
      if (!r.ok) throw new Error(body.detail || "Erreur inconnue");
      return body;
    }})
    .then(() => {{ window.location.href = "/"; }})
    .catch(err => {{
      btn.disabled = false;
      btn.textContent = "Créer un compte";
      errorEl.textContent = err.message;
    }});
}});
  </script>
</body>
</html>"""


@app.get("/login")
def login_page():
    return HTMLResponse(content=LOGIN_HTML)


@app.get("/signup")
def signup_page():
    return HTMLResponse(content=SIGNUP_HTML)


SEARCH_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Smap</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Inter+Tight:wght@600;700&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0b0b0c; color: #e8e4dc;
      font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      min-height: 100vh; display: flex; flex-direction: column;
      align-items: center; justify-content: center; padding: 40px 20px 40px;
    }}
    h1 {{ font-family: "Inter Tight", "Inter", sans-serif; font-size: 1.7rem; font-weight: 700; letter-spacing: -0.02em; color: #e8e4dc; }}
    .subtitle {{ color: #8a8680; font-size: 0.95rem; margin-bottom: 48px; }}
    #header-row {{
      width: 100%; max-width: 600px; display: flex; align-items: baseline;
      justify-content: space-between; gap: 12px; margin-bottom: 28px;
    }}
    #nav-actions {{ display: flex; align-items: center; gap: 16px; }}
    #nav-divider {{ width: 1px; height: 14px; background: #2a2a27; }}
    #queue-toggle-btn {{
      position: relative; display: flex; align-items: center; justify-content: center;
      width: 30px; height: 30px; background: none; border: 1px solid transparent; border-radius: 8px;
      color: #8a8680; cursor: pointer; transition: color 0.15s, border-color 0.15s, background 0.15s;
    }}
    #queue-toggle-btn:hover {{ color: #e8e4dc; background: #161614; border-color: #2a2a27; }}
    #queue-toggle-btn.open {{ color: #f2b33d; background: #161614; border-color: #2a2a27; }}
    .queue-dots {{ position: absolute; top: -3px; right: -3px; display: flex; gap: 2px; }}
    .queue-dot {{
      min-width: 14px; height: 14px; padding: 0 3px; border-radius: 999px;
      font-size: 9px; font-weight: 700; color: #0b0b0c; line-height: 14px;
      display: none;
    }}
    .queue-dot.show {{ display: block; }}
    .queue-dot.dot-error {{ background: #d16565; }}
    .queue-dot.dot-unseen {{ background: #7cb8e8; }}
    #search-wrap {{ width: 100%; max-width: 600px; display: flex; gap: 10px; }}
    #search {{
      flex: 1; min-width: 0; padding: 16px 20px; font-size: 16px;
      background: #161614; border: 1px solid #2a2a27; border-radius: 10px;
      color: #e8e4dc; outline: none; transition: border-color 0.15s, box-shadow 0.15s;
    }}
    #search::placeholder {{ color: #8a8680; }}
    #search:focus {{ border-color: #f2b33d; box-shadow: 0 0 0 3px rgba(242, 179, 61, 0.15); }}
    #add-btn {{
      display: none; flex-shrink: 0; padding: 0 22px; font-size: 15px; font-weight: 600;
      background: #161614; border: 1px solid #2a2a27; border-radius: 10px;
      color: #e8e4dc; cursor: pointer; transition: border-color 0.15s, background 0.15s, opacity 0.15s;
    }}
    #add-btn:hover {{ border-color: #f2b33d; background: #1c1c19; }}
    #add-btn:disabled {{ cursor: default; opacity: 0.6; }}
    .error {{ color: #d16565; font-size: 14px; text-align: center; padding: 24px 0; }}
    #results {{ width: 100%; max-width: 600px; margin-top: 12px; display: flex; flex-direction: column; gap: 10px; }}
    .card {{
      background: #161614; border: 1px solid #2a2a27; border-radius: 10px;
      padding: 16px; cursor: pointer; transition: border-color 0.15s, background 0.15s;
      display: flex; gap: 14px; align-items: flex-start;
    }}
    .card:hover {{ border-color: #3a3a35; background: #1a1a17; }}
    .card-logo {{
      width: 40px; height: 40px; border-radius: 50%; object-fit: cover; flex-shrink: 0;
      background: #1a1a17;
    }}
    .card-logo-initial {{
      width: 40px; height: 40px; border-radius: 50%; flex-shrink: 0;
      display: flex; align-items: center; justify-content: center;
      font-family: "Inter Tight", "Inter", sans-serif; font-size: 15px; font-weight: 700;
    }}
    .card-body {{ flex: 1; min-width: 0; }}
    .card-name {{ font-family: "Inter Tight", "Inter", sans-serif; font-size: 16px; font-weight: 600; color: #e8e4dc; margin-bottom: 8px; }}
    .badges {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }}
    .badge {{ font-size: 11px; letter-spacing: 0.03em; text-transform: uppercase; padding: 3px 8px; border-radius: 5px; font-weight: 500; white-space: nowrap; }}
    .card-desc {{
      font-size: 13px; color: #8a8680; line-height: 1.5;
      display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
    }}
    .empty {{ color: #8a8680; font-size: 14px; text-align: center; padding: 24px 0; }}
    #graph-link, #admin-link {{ color: #8a8680; font-size: 13px; text-decoration: none; transition: color 0.15s; white-space: nowrap; flex-shrink: 0; }}
    #graph-link:hover, #admin-link:hover {{ color: #c7c3bb; }}
    #logout-link {{
      display: flex; align-items: center; gap: 6px; color: #65625c; font-size: 13px;
      text-decoration: none; transition: color 0.15s; white-space: nowrap; flex-shrink: 0;
    }}
    #logout-link:hover {{ color: #a8a49c; }}
    #logout-link svg {{ flex-shrink: 0; }}
    #queue-backdrop {{
      position: fixed; inset: 0; background: rgba(0, 0, 0, 0.5);
      opacity: 0; pointer-events: none; transition: opacity 0.2s; z-index: 20;
    }}
    #queue-backdrop.open {{ opacity: 1; pointer-events: auto; }}
    #queue-drawer {{
      position: fixed; top: 0; right: 0; height: 100vh; width: 360px; max-width: 90vw;
      background: #101010; border-left: 1px solid #2a2a27; box-shadow: -8px 0 24px rgba(0, 0, 0, 0.35);
      display: flex; flex-direction: column; padding: 20px;
      transform: translateX(100%); transition: transform 0.25s ease; z-index: 21;
    }}
    #queue-drawer.open {{ transform: translateX(0); }}
    #queue-drawer-header {{
      display: flex; align-items: center; justify-content: space-between;
      margin-bottom: 20px; flex-shrink: 0;
    }}
    #queue-drawer-header span {{ font-family: "Inter Tight", "Inter", sans-serif; font-size: 15px; font-weight: 600; color: #e8e4dc; }}
    #queue-close-btn {{
      background: none; border: none; color: #8a8680; font-size: 20px; line-height: 1;
      cursor: pointer; padding: 4px; transition: color 0.15s;
    }}
    #queue-close-btn:hover {{ color: #e8e4dc; }}
    #queue-tabs {{ display: flex; gap: 8px; margin-bottom: 14px; flex-shrink: 0; }}
    .queue-tab {{
      display: flex; align-items: center; gap: 6px; padding: 6px 12px; font-size: 13px;
      font-weight: 600; color: #8a8680; background: #161614; border: 1px solid #2a2a27;
      border-radius: 20px; cursor: pointer; transition: color 0.15s, border-color 0.15s;
    }}
    .queue-tab:hover {{ color: #e8e4dc; }}
    .queue-tab.active {{ color: #e8e4dc; border-color: #4a4a45; }}
    .queue-tab-count {{
      font-size: 11px; font-weight: 700; color: #d16565; background: rgba(209, 101, 101, 0.15);
      border-radius: 10px; padding: 1px 6px; display: none;
    }}
    .queue-tab-count.show {{ display: inline-block; }}
    #queue-scope-toggle {{ display: flex; gap: 8px; margin-bottom: 14px; flex-shrink: 0; }}
    .queue-scope-btn {{
      padding: 5px 10px; font-size: 12px; font-weight: 600; color: #8a8680; background: none;
      border: 1px solid #2a2a27; border-radius: 20px; cursor: pointer; transition: color 0.15s, border-color 0.15s;
    }}
    .queue-scope-btn:hover {{ color: #e8e4dc; }}
    .queue-scope-btn.active {{ color: #e8e4dc; border-color: #4a4a45; }}
    #queue-panel {{ display: flex; width: 100%; flex-direction: column; gap: 10px; overflow-y: auto; }}
    .queue-row {{
      background: #161614; border: 1px solid #2a2a27; border-radius: 10px;
      padding: 14px 16px; display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 8px 12px;
    }}
    .queue-row-label {{ font-size: 14px; color: #e8e4dc; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .queue-row-requester {{ font-size: 11px; color: #8a8680; display: block; margin-top: 2px; }}
    .queue-status {{
      display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end;
      gap: 8px; min-width: 0; max-width: 100%;
    }}
    .queue-badge {{ font-size: 12px; font-weight: 600; white-space: nowrap; display: flex; align-items: center; gap: 6px; flex-shrink: 0; max-width: 100%; }}
    .queue-badge.queued {{ color: #f2c94c; }}
    .queue-badge.processing {{ color: #7cb8e8; }}
    .queue-badge.done {{ color: #6fcf6f; }}
    .queue-badge.error {{ color: #d16565; display: block; white-space: normal; }}
    .queue-badge-msg {{
      min-width: 0; overflow: hidden; text-overflow: ellipsis;
      display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
      word-break: break-word; cursor: help;
    }}
    .retry-btn, .delete-btn {{
      flex-shrink: 0; padding: 4px 12px; font-size: 12px; font-weight: 600;
      background: #161614; border: 1px solid #2a2a27; border-radius: 8px;
      color: #e8e4dc; cursor: pointer; transition: border-color 0.15s, background 0.15s, opacity 0.15s;
    }}
    .retry-btn:hover {{ border-color: #f2b33d; background: #1c1c19; }}
    .retry-btn:disabled, .delete-btn:disabled {{ cursor: default; opacity: 0.6; }}
    .delete-btn {{ color: #d16565; }}
    .delete-btn:hover {{ border-color: #d16565; background: #2a1616; }}
    .retry-error {{ color: #d16565; font-size: 11px; flex-basis: 100%; }}
    .spinner {{
      width: 12px; height: 12px; border-radius: 50%;
      border: 2px solid #2a2a27; border-top-color: #7cb8e8;
      animation: spin 0.7s linear infinite;
    }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    @media (max-width: 480px) {{
      body {{ padding: 24px 16px 32px; }}
      h1 {{ font-size: 1.4rem; }}
      .subtitle {{ margin-bottom: 32px; }}
      #header-row {{ flex-wrap: wrap; row-gap: 10px; }}
      #nav-actions {{ gap: 12px; }}
      #search-wrap {{ flex-wrap: wrap; }}
      #add-btn {{ flex: 1 1 100%; padding: 12px; }}
      .card {{ padding: 14px; gap: 10px; }}
      #queue-drawer {{ width: 100%; max-width: 100vw; padding: 16px; }}
    }}
  </style>
</head>
<body>
  <div id="header-row">
    <h1>Smap</h1>
    <div id="nav-actions">
      __GRAPH_NAV_LINK__
      <button id="queue-toggle-btn" type="button" aria-label="En attente">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
          <circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2"/>
          <path d="M12 7v5l3.5 2" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <span class="queue-dots">
          <span id="queue-dot-error" class="queue-dot dot-error"></span>
          <span id="queue-dot-unseen" class="queue-dot dot-unseen"></span>
        </span>
      </button>
      <div id="nav-divider"></div>
      <a href="#" id="logout-link" onclick="fetch('/api/logout', {{method: 'POST'}}).then(() => location.href = '/login'); return false;">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
          <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M16 17l5-5-5-5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M21 12H9" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <span>Déconnexion</span>
      </a>
    </div>
  </div>
  <div id="search-wrap">
    <input id="search" type="text" placeholder="Rechercher une startup…" autocomplete="off">
    <button id="add-btn" type="button">Ajouter</button>
  </div>
  <div id="results"></div>
  <div id="queue-backdrop"></div>
  <div id="queue-drawer">
    <div id="queue-drawer-header">
      <span>En attente</span>
      <button id="queue-close-btn" type="button" aria-label="Fermer">×</button>
    </div>
    <div id="queue-tabs">
      <button class="queue-tab active" data-status="" type="button">Tous</button>
      <button class="queue-tab" data-status="error" type="button">Échecs<span id="queue-tab-error-count" class="queue-tab-count"></span></button>
    </div>
    __QUEUE_SCOPE_TOGGLE__
    <div id="queue-panel"></div>
  </div>

  <script>
{SECTOR_COLORS_JS}

const searchEl  = document.getElementById("search");
const resultsEl = document.getElementById("results");
const addBtn    = document.getElementById("add-btn");
let timer;
let lastQuery = "";

searchEl.addEventListener("input", () => {{
  clearTimeout(timer);
  timer = setTimeout(() => doSearch(searchEl.value.trim()), 300);
}});

resultsEl.addEventListener("click", e => {{
  const card = e.target.closest(".card");
  if (card) go(card.dataset.domain);
}});

// The "Ajouter" button posts lastQuery straight to /api/ingest as a URL --
// only makes sense to show it when the query actually looks like one, since a
// plain startup name (no dot, has spaces) would just fail the ingest call.
function looksLikeUrl(q) {{
  return /^https?:\/\//i.test(q) || /^[a-z0-9-]+(\.[a-z0-9-]+)+(\/\S*)?$/i.test(q);
}}

function doSearch(q) {{
  lastQuery = q;
  if (q.length < 2) {{ resultsEl.innerHTML = ""; addBtn.style.display = "none"; return; }}
  fetch("/api/search?q=" + encodeURIComponent(q))
    .then(r => r.json())
    .then(render);
}}

function render(data) {{
  if (!data.length) {{
    if (looksLikeUrl(lastQuery)) {{
      resultsEl.innerHTML = '<div class="empty">No startup found</div>';
      addBtn.style.display = "inline-block";
    }} else {{
      resultsEl.innerHTML = '<div class="empty">No startup found. Enter the startup’s website URL to add it.</div>';
      addBtn.style.display = "none";
    }}
    return;
  }}
  addBtn.style.display = "none";
  resultsEl.innerHTML = data.map(s => {{
    const badges = (s.sectors || []).map(sec => {{
      const c = SECTOR_COLORS[sec] || DEFAULT_COLOR;
      return `<span class="badge" style="background:${{c}}26; color:${{c}}">${{sec}}</span>`;
    }}).join("");
    const color   = sectorColor(s.sectors);
    const initial = (s.name || "?")[0].toUpperCase();
    const logoHtml = s.flaticon_url
      ? `<img class="card-logo" src="${{s.flaticon_url}}" alt="" onerror="handleLogoImgError(this)" data-fallback-class="card-logo-initial" data-fallback-bg="${{color}}26" data-fallback-color="${{color}}" data-fallback-initial="${{initial}}">`
      : `<div class="card-logo-initial" style="background:${{color}}26; color:${{color}}">${{initial}}</div>`;
    return `<div class="card" data-domain="${{s.domain}}">
      ${{logoHtml}}
      <div class="card-body">
        <div class="card-name">${{s.name}}</div>
        <div class="badges">${{badges}}</div>
        <div class="card-desc">${{s.description || ""}}</div>
      </div>
    </div>`;
  }}).join("");
}}

function go(domain) {{
  window.location.href = "/startup/" + encodeURIComponent(domain);
}}

const queuePanel     = document.getElementById("queue-panel");
const queueDrawer    = document.getElementById("queue-drawer");
const queueBackdrop  = document.getElementById("queue-backdrop");
const queueToggleBtn = document.getElementById("queue-toggle-btn");
const queueCloseBtn  = document.getElementById("queue-close-btn");
const queueTabs      = document.querySelectorAll(".queue-tab");
const queueScopeBtns = document.querySelectorAll(".queue-scope-btn");
let queuePollTimer;
let queueStatusFilter = "";  // "" = Tous, "error" = Échecs tab
// Owner-only ("all" vs "mine") -- absent for a non-owner, whose view is
// always restricted to their own rows server-side regardless of this value.
let queueScope = "all";

queueTabs.forEach(tab => tab.addEventListener("click", () => {{
  queueStatusFilter = tab.dataset.status;
  queueTabs.forEach(t => t.classList.toggle("active", t === tab));
  pollQueue();
}}));

queueScopeBtns.forEach(btn => btn.addEventListener("click", () => {{
  queueScope = btn.dataset.scope;
  queueScopeBtns.forEach(b => b.classList.toggle("active", b === btn));
  pollQueue();
  pollSummary();
}}));

function openQueueDrawer() {{
  queueDrawer.classList.add("open");
  queueBackdrop.classList.add("open");
  queueToggleBtn.classList.add("open");
  pollQueue();
  if (queuePollTimer) clearInterval(queuePollTimer);
  queuePollTimer = setInterval(pollQueue, 2000);
  fetch("/api/ingestion-queue/mark-seen", {{ method: "POST" }})
    .then(() => pollSummary())
    .catch(() => {{}});
}}

function closeQueueDrawer() {{
  queueDrawer.classList.remove("open");
  queueBackdrop.classList.remove("open");
  queueToggleBtn.classList.remove("open");
  if (queuePollTimer) clearInterval(queuePollTimer);
}}

queueToggleBtn.addEventListener("click", openQueueDrawer);
queueCloseBtn.addEventListener("click", closeQueueDrawer);
queueBackdrop.addEventListener("click", closeQueueDrawer);

function pollQueue() {{
  const params = new URLSearchParams();
  if (queueStatusFilter) params.set("status", queueStatusFilter);
  if (queueScopeBtns.length) params.set("scope", queueScope);
  const qs = params.toString() ? "?" + params.toString() : "";
  fetch("/api/ingestion-queue" + qs)
    .then(r => r.json())
    .then(renderQueue)
    .catch(err => {{
      queuePanel.innerHTML = `<div class="error">${{err.message}}</div>`;
    }});
}}

const queueDotError      = document.getElementById("queue-dot-error");
const queueDotUnseen     = document.getElementById("queue-dot-unseen");
const queueTabErrorCount = document.getElementById("queue-tab-error-count");

function pollSummary() {{
  const qs = queueScopeBtns.length ? "?scope=" + encodeURIComponent(queueScope) : "";
  fetch("/api/ingestion-queue/summary" + qs)
    .then(r => r.json())
    .then(data => {{
      queueDotError.textContent = data.error_count;
      queueDotError.classList.toggle("show", data.error_count > 0);
      queueDotUnseen.textContent = data.unseen_done_count;
      queueDotUnseen.classList.toggle("show", data.unseen_done_count > 0);
      queueTabErrorCount.textContent = data.error_count;
      queueTabErrorCount.classList.toggle("show", data.error_count > 0);
    }})
    .catch(() => {{ /* transient failure -- leave badges at their last-known values */ }});
}}

pollSummary();
setInterval(pollSummary, 5000);

function escapeHtml(str) {{
  const map = {{ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }};
  return String(str == null ? "" : str).replace(/[&<>"']/g, ch => map[ch]);
}}

// Shared onerror fallback for every <img> pointing at a flaticon_url/logo_url
// -- a missing file (StaticFiles 404) or an unreadable one otherwise shows
// the browser's native broken-image glyph, in cards and in the graph alike.
// Reads its replacement's styling off data-fallback-* attributes (set by
// each caller) and builds it via DOM APIs, not string concatenation --
// textContent is used for the initial, so this is safe regardless of what
// characters a startup's name happens to start with.
function handleLogoImgError(imgEl) {{
  const div = document.createElement("div");
  div.className = imgEl.dataset.fallbackClass;
  if (imgEl.dataset.fallbackBg) div.style.background = imgEl.dataset.fallbackBg;
  if (imgEl.dataset.fallbackColor) div.style.color = imgEl.dataset.fallbackColor;
  div.textContent = imgEl.dataset.fallbackInitial || "";
  imgEl.replaceWith(div);
}}

// Error messages can be arbitrarily long (raw API error bodies) -- clamp what's
// shown inline so a single row can't blow out the card's width; the full text
// is still reachable via the title tooltip set alongside this in QUEUE_BADGES.error.
function truncate(str, n) {{
  return str.length > n ? str.slice(0, n - 1) + "…" : str;
}}

const QUEUE_BADGES = {{
  queued:     () => `<span class="queue-badge queued">🟡 En attente</span>`,
  processing: () => `<span class="queue-badge processing"><span class="spinner"></span> Traitement…</span>`,
  done:       row => `<span class="queue-badge done">✅ Terminé — ${{escapeHtml((row.result || {{}}).name || "")}} ajouté</span>`,
  error:      row => {{
    const msg = row.error_message || "";
    return `<span class="queue-badge error"><span class="queue-badge-msg" title="${{escapeHtml(msg)}}">🔴 Échec — ${{escapeHtml(truncate(msg, 160))}}</span></span>`
      + `<button class="retry-btn" data-id="${{row.id}}">Relancer</button>`
      + `<button class="delete-btn" data-id="${{row.id}}">Supprimer</button>`;
  }},
}};

function renderQueue(data) {{
  if (!data.length) {{
    queuePanel.innerHTML = queueStatusFilter === "error"
      ? '<div class="empty">Aucun échec.</div>'
      : '<div class="empty">Aucun élément en file d’attente.</div>';
    return;
  }}
  queuePanel.innerHTML = data.map(row => {{
    const label = row.status === "done" ? ((row.result || {{}}).name || row.url) : row.url;
    const badge = (QUEUE_BADGES[row.status] || QUEUE_BADGES.error)(row);
    // requester_email is only ever present in the owner's response (see
    // api_ingestion_queue) -- a non-owner's rows are always their own, so
    // there's nothing useful to label there.
    const requester = row.requester_email
      ? `<span class="queue-row-requester">${{escapeHtml(row.requester_email)}}</span>`
      : (("requester_email" in row) ? `<span class="queue-row-requester">(non attribué)</span>` : "");
    return `<div class="queue-row">
      <div class="queue-row-label">${{escapeHtml(label)}}${{requester}}</div>
      <div class="queue-status">${{badge}}</div>
    </div>`;
  }}).join("");
}}

queuePanel.addEventListener("click", e => {{
  const retryBtn = e.target.closest(".retry-btn");
  if (retryBtn) {{
    const id = retryBtn.dataset.id;
    retryBtn.disabled = true;
    retryBtn.textContent = "Relance…";
    fetch(`/api/ingestion-queue/${{id}}/retry`, {{ method: "POST" }})
      .then(async r => {{
        const body = await r.json();
        if (!r.ok) throw new Error(body.detail || "Erreur inconnue");
        return body;
      }})
      .then(() => pollQueue())
      .catch(err => {{
        retryBtn.disabled = false;
        retryBtn.textContent = "Relancer";
        retryBtn.insertAdjacentHTML("afterend", `<span class="retry-error">${{escapeHtml(err.message)}}</span>`);
      }});
    return;
  }}

  const deleteBtn = e.target.closest(".delete-btn");
  if (deleteBtn) {{
    const id = deleteBtn.dataset.id;
    deleteBtn.disabled = true;
    deleteBtn.textContent = "Suppression…";
    fetch(`/api/ingestion-queue/${{id}}`, {{ method: "DELETE" }})
      .then(async r => {{
        if (!r.ok) {{
          const body = await r.json().catch(() => ({{}}));
          throw new Error(body.detail || "Erreur inconnue");
        }}
      }})
      .then(() => {{ pollQueue(); pollSummary(); }})
      .catch(err => {{
        deleteBtn.disabled = false;
        deleteBtn.textContent = "Supprimer";
        deleteBtn.insertAdjacentHTML("afterend", `<span class="retry-error">${{escapeHtml(err.message)}}</span>`);
      }});
  }}
}});

addBtn.addEventListener("click", () => {{
  const url = lastQuery;
  if (!url) return;
  addBtn.disabled = true;
  addBtn.textContent = "Ajout en cours…";
  fetch("/api/ingest?url=" + encodeURIComponent(url), {{ method: "POST" }})
    .then(async r => {{
      const body = await r.json();
      if (!r.ok) throw new Error(body.detail || "Erreur inconnue");
      return body;
    }})
    .then(() => {{
      // Enqueued for background processing (Epic 6, Story 6.1). The item's
      // live status badge is shown in the "En attente" drawer (Story 6.2) --
      // stay on the search view instead of jumping there automatically.
      addBtn.disabled = false;
      addBtn.textContent = "Ajouter";
      addBtn.style.display = "none";
      searchEl.value = "";
      lastQuery = "";
      resultsEl.innerHTML = '<div class="empty">Startup ajoutée — suivez son traitement dans « En attente ».</div>';
      pollSummary();
    }})
    .catch(err => {{
      addBtn.disabled = false;
      addBtn.textContent = "Ajouter";
      resultsEl.innerHTML = `<div class="error">${{err.message}}</div>`;
    }});
}});
  </script>
</body>
</html>"""


GRAPH_HTML_TEMPLATE = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title id="page-title">Loading…</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0f0f0f; color: #e0e0e0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      height: 100vh; overflow: hidden; display: flex; flex-direction: column;
    }}

    #topbar {{
      height: 48px; background: #111; border-bottom: 1px solid #1e1e1e;
      display: flex; align-items: center; padding: 0 16px; gap: 14px; flex-shrink: 0;
    }}
    #back {{ color: #888; text-decoration: none; font-size: 20px; line-height: 1; transition: color 0.15s; }}
    #back:hover {{ color: #fff; }}
    #page-name {{ font-size: 14px; font-weight: 600; color: #ccc; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}

    #main {{ flex: 1; display: flex; overflow: hidden; }}

    #graph-col {{ flex: 0 0 100%; position: relative; }}
    svg#graph {{ width: 100%; height: 100%; display: block; }}
    #empty-msg {{
      position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%);
      color: #444; font-size: 14px; text-align: center;
    }}

    #panel {{
      flex: 0 0 30%; background: #111; border-left: 1px solid #1e1e1e;
      padding: 24px 20px; overflow-y: auto; display: none; flex-direction: column; gap: 16px;
      position: relative;
    }}
    #panel-close {{
      display: none; position: absolute; top: 16px; right: 16px; width: 28px; height: 28px;
      align-items: center; justify-content: center;
      background: transparent; border: none; border-radius: 50%;
      color: #666; font-size: 18px; line-height: 1; cursor: pointer;
      transition: background 0.15s, color 0.15s;
    }}
    #panel-close:hover {{ background: #222; color: #fff; }}
    #panel-logo-row {{ display: flex; align-items: center; gap: 10px; align-self: flex-start; }}
    #panel-logo {{
      width: 80px; height: 80px; border-radius: 12px; object-fit: contain;
      background: #1a1a1a; border: 1px solid #222; padding: 6px;
    }}
    #panel-logo-initial {{
      width: 80px; height: 80px; border-radius: 12px;
      display: none; align-items: center; justify-content: center;
      font-size: 32px; font-weight: 700;
    }}
    #panel-logo-download {{
      display: none; width: 28px; height: 28px; align-items: center; justify-content: center;
      background: #1a1a1a; border: 1px solid #2e2e2e; border-radius: 8px;
      color: #888; text-decoration: none; font-size: 15px; flex-shrink: 0;
      transition: background 0.15s, color 0.15s;
    }}
    #panel-logo-download:hover {{ background: #222; color: #fff; }}
    #panel-name {{ font-size: 20px; font-weight: 700; color: #fff; line-height: 1.3; }}
    #panel-link {{ font-size: 12px; color: #555; text-decoration: none; word-break: break-all; transition: color 0.15s; }}
    #panel-link:hover {{ color: #888; }}
    .badges {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .badge {{ font-size: 11px; padding: 3px 9px; border-radius: 20px; font-weight: 500; color: #111; white-space: nowrap; }}
    #panel-subsectors {{ display: flex; flex-direction: column; gap: 4px; }}
    .subsector-tag {{ font-size: 12px; color: #888; }}
    #panel-desc {{ font-size: 13px; color: #aaa; line-height: 1.6; }}
    #panel-view-btn {{
      display: none; margin-top: 8px;
      background: #1e1e1e; border: 1px solid #333; border-radius: 8px;
      color: #ccc; padding: 10px 16px; font-size: 13px; cursor: pointer;
      text-align: center; text-decoration: none; transition: background 0.15s, color 0.15s;
    }}
    #panel-view-btn:hover {{ background: #2a2a2a; color: #fff; }}

    .node circle {{ cursor: pointer; stroke-width: 2px; transition: stroke 0.15s; }}
    .node circle.selected {{ stroke: #fff !important; stroke-width: 3px; }}
    .node text {{ font-size: 11px; fill: #ccc; pointer-events: none; }}
    .link {{ stroke: #aaa; fill: none; }}
    @media (max-width: 768px) {{
      #panel {{
        position: fixed; left: 0; right: 0; bottom: 0; top: auto;
        flex: none !important; width: 100%; max-height: 65vh;
        border-left: none; border-top: 1px solid #1e1e1e;
        border-radius: 16px 16px 0 0;
        box-shadow: 0 -8px 24px rgba(0, 0, 0, 0.5);
        z-index: 15;
      }}
      #panel-close {{ display: flex; }}
      #panel-name {{ padding-right: 24px; }}
    }}
  </style>
</head>
<body>
  <div id="topbar">
    <a id="back" href="/">&#8592;</a>
    <span id="page-name">Loading…</span>
  </div>
  <div id="main">
    <div id="graph-col">
      <svg id="graph"></svg>
      <div id="empty-msg" style="display:none">No competitors found in the graph.</div>
    </div>
    <div id="panel">
      <button id="panel-close" aria-label="Close" title="Close">&#10005;</button>
      <div id="panel-logo-row">
        <img id="panel-logo" src="" alt="" style="display:none">
        <div id="panel-logo-initial"></div>
        <a id="panel-logo-download" href="#" download title="Télécharger le logo">&#8681;</a>
      </div>
      <div id="panel-name">—</div>
      <a id="panel-link" href="#" target="_blank" style="display:none"></a>
      <a id="panel-linkedin" href="#" target="_blank" style="display:none; font-size:12px; color:#0a66c2;">LinkedIn →</a>
      <div class="badges" id="panel-badges"></div>
      <div id="panel-subsectors"></div>
      <div id="panel-desc"></div>
      <a id="panel-view-btn">View graph →</a>
    </div>
  </div>

  <script src="https://d3js.org/d3.v7.min.js"></script>
  <script>
{SECTOR_COLORS_JS}

const STARTUP_DOMAIN = __STARTUP_DOMAIN_JSON__;

document.getElementById("page-title").textContent = STARTUP_DOMAIN;
document.getElementById("page-name").textContent  = STARTUP_DOMAIN;

const isMobile = () => window.matchMedia("(max-width: 768px)").matches;
let nodeSel = null;

document.getElementById("panel-close").addEventListener("click", () => {{
  document.getElementById("panel").style.display = "none";
  document.getElementById("graph-col").style.flex = "0 0 100%";
  if (nodeSel) nodeSel.selectAll("circle").classed("selected", false);
}});

function showPanel(node, isCenter) {{
  document.getElementById("panel").style.display = "flex";
  if (!isMobile()) document.getElementById("graph-col").style.flex = "0 0 70%";
  const logoEl        = document.getElementById("panel-logo");
  const logoDlEl      = document.getElementById("panel-logo-download");
  const logoInitialEl = document.getElementById("panel-logo-initial");
  const logoSrc = node.logo_url || node.flaticon_url;
  const color   = sectorColor(node.sectors);
  const initial = (node.name || "?")[0].toUpperCase();
  function showLogoInitial() {{
    logoEl.style.display = "none";
    logoDlEl.style.display = "none";
    logoInitialEl.style.background = color + "26";
    logoInitialEl.style.color = color;
    logoInitialEl.textContent = initial;
    logoInitialEl.style.display = "flex";
  }}
  logoEl.onerror = showLogoInitial;
  if (logoSrc) {{
    logoInitialEl.style.display = "none";
    logoEl.src           = logoSrc;
    logoEl.style.display = "";
    logoDlEl.href        = logoSrc;
    logoDlEl.download    = node.name.replace(/[^a-z0-9]+/gi, "_") + "_logo" + logoSrc.slice(logoSrc.lastIndexOf("."));
    logoDlEl.style.display = "flex";
  }} else {{
    showLogoInitial();
  }}

  document.getElementById("panel-name").textContent = node.name;

  const linkEl = document.getElementById("panel-link");
  if (node.website) {{
    linkEl.href = node.website; linkEl.textContent = node.website; linkEl.style.display = "";
  }} else {{
    linkEl.style.display = "none";
  }}

  const linkedinEl = document.getElementById("panel-linkedin");
  if (node.linkedin_url) {{
    linkedinEl.href = node.linkedin_url; linkedinEl.style.display = "";
  }} else {{
    linkedinEl.style.display = "none";
  }}

  document.getElementById("panel-badges").innerHTML = (node.sectors || []).map(s =>
    `<span class="badge" style="background:${{sectorColor([s])}}; color:#111">${{s}}</span>`
  ).join("");

  document.getElementById("panel-subsectors").innerHTML = (node.subsectors || []).map(s =>
    `<span class="subsector-tag">· ${{s}}</span>`
  ).join("");

  document.getElementById("panel-desc").textContent = node.description || "";

  const btn = document.getElementById("panel-view-btn");
  if (!isCenter) {{
    btn.style.display = "";
    btn.onclick = () => {{ window.location.href = "/startup/" + encodeURIComponent(node.domain); }};
  }} else {{
    btn.style.display = "none";
  }}
}}

fetch("/api/graph/" + encodeURIComponent(STARTUP_DOMAIN))
  .then(r => r.json())
  .then(data => {{
    const {{ center, nodes, links }} = data;

    document.getElementById("page-title").textContent = center.name;
    document.getElementById("page-name").textContent  = center.name;

    showPanel(center, true);

    if (!nodes.length) {{
      document.getElementById("empty-msg").style.display = "";
      return;
    }}

    const W   = document.getElementById("graph-col").clientWidth;
    const H   = document.getElementById("graph-col").clientHeight;
    const svg = d3.select("svg#graph").attr("width", W).attr("height", H);

    const defs    = svg.append("defs");
    const allNodes = [{{ ...center, _isCenter: true }}, ...nodes.map(n => ({{ ...n, _isCenter: false }}))];
    const allLinks = links;

    // Works before and after D3 resolves string refs to objects. Keyed by
    // domain, not name -- two startups can share a display name.
    const domainOf = x => (typeof x === "object" ? x.domain : x);

    const scoreOf = n => {{
      const link = allLinks.find(l =>
        (domainOf(l.source) === n.domain || domainOf(l.target) === n.domain) &&
        (domainOf(l.source) === center.domain || domainOf(l.target) === center.domain)
      );
      return link ? (link.score || 0) : 0;
    }};

    const sim = d3.forceSimulation(allNodes)
      .force("link",      d3.forceLink(allLinks).id(d => d.domain).distance(160))
      .force("charge",    d3.forceManyBody().strength(-350))
      .force("center",    d3.forceCenter(W / 2, H / 2))
      .force("collision", d3.forceCollide().radius(d => d._isCenter ? 36 : 14 + scoreOf(d) * 14));

    const linkSel = svg.append("g")
      .selectAll("line")
      .data(allLinks)
      .join("line")
        .attr("class", "link")
        .attr("stroke-width",   d => 1.5 + d.score * 5)
        .attr("stroke-opacity", d => 0.15 + d.score * 0.45);

    nodeSel = svg.append("g")
      .selectAll("g")
      .data(allNodes)
      .join("g")
        .attr("class", "node")
        .call(d3.drag()
          .on("start", (e, d) => {{ if (!e.active) sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; }})
          .on("drag",  (e, d) => {{ d.fx = e.x; d.fy = e.y; }})
          .on("end",   (e, d) => {{ if (!e.active) sim.alphaTarget(0); d.fx = null; d.fy = null; }})
        );

    // Circle — always rendered as click target and selection ring
    nodeSel.append("circle")
      .attr("r",      d => d._isCenter ? 30 : 10 + scoreOf(d) * 12)
      .attr("fill",   d => d.flaticon_url ? "none" : (d._isCenter ? "#ffffff" : sectorColor(d.sectors)))
      .attr("stroke", d => d._isCenter ? "#fff" : (d.flaticon_url ? sectorColor(d.sectors) : "#0f0f0f"))
      .style("pointer-events", "all")
      .on("click", (e, d) => {{
        nodeSel.selectAll("circle").classed("selected", false);
        d3.select(e.currentTarget).classed("selected", true);
        showPanel(d, d._isCenter);
      }});

    // Colored-initial fallback, shown whenever there's no logo image (never
    // had one, or its <image> below failed to load) -- sits under the image
    // in paint order, invisible whenever the image successfully renders.
    nodeSel.append("text")
      .attr("class",  "node-initial")
      .attr("x", 0).attr("y", 4)
      .attr("text-anchor", "middle")
      .style("font-weight", "700")
      .style("font-size", d => (d._isCenter ? 30 : 10 + scoreOf(d) * 12) * 0.7 + "px")
      .style("fill", d => d._isCenter ? "#111" : "#0f0f0f")
      .style("pointer-events", "none")
      .style("display", d => d.flaticon_url ? "none" : null)
      .text(d => (d.name || "?")[0].toUpperCase());

    // Circular logo images for nodes that have flaticon_url
    nodeSel.each(function(d, i) {{
      if (!d.flaticon_url) return;
      const r = d._isCenter ? 30 : 10 + scoreOf(d) * 12;
      defs.append("clipPath")
        .attr("id", "logo-clip-" + i)
        .append("circle").attr("r", r);
      d3.select(this).append("image")
        .attr("href", d.flaticon_url)
        .attr("x", -r).attr("y", -r)
        .attr("width",  r * 2).attr("height", r * 2)
        .attr("clip-path", "url(#logo-clip-" + i + ")")
        .attr("preserveAspectRatio", "xMidYMid slice")
        .style("pointer-events", "none")
        .on("error", function() {{
          // Missing/unreadable file -- fall back to the same plain colored
          // circle + initial a node with no flaticon_url at all would show.
          const g = d3.select(this.parentNode);
          d3.select(this).remove();
          g.select("circle")
            .attr("fill", d._isCenter ? "#ffffff" : sectorColor(d.sectors))
            .attr("stroke", d._isCenter ? "#fff" : "#0f0f0f");
          g.select(".node-initial").style("display", null);
        }});
    }});

    nodeSel.append("text")
      .attr("x",            0)
      .attr("y",            d => (d._isCenter ? 30 : 10 + scoreOf(d) * 12) + 16)
      .attr("text-anchor",  "middle")
      .attr("font-weight",  d => d._isCenter ? "700" : "400")
      .style("fill",        "#ffffff")
      .style("font-size",   "11px")
      .style("stroke",         "#000000")
      .style("stroke-width",   "3px")
      .style("paint-order",    "stroke")
      .style("pointer-events", "none")
      .text(d => d.name.length > 20 ? d.name.slice(0, 18) + "…" : d.name);

    sim.on("tick", () => {{
      linkSel
        .attr("x1", d => d.source.x).attr("y1", d => d.source.y)
        .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
      nodeSel.attr("transform", d => `translate(${{d.x}},${{d.y}})`);
    }});
  }});
  </script>
</body>
</html>"""


GLOBAL_GRAPH_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Full Startup Graph</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0f0f0f; color: #e0e0e0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      height: 100vh; overflow: hidden; display: flex; flex-direction: column;
    }}
    #topbar {{
      height: 48px; background: #111; border-bottom: 1px solid #1e1e1e;
      display: flex; align-items: center; padding: 0 16px; gap: 14px; flex-shrink: 0;
    }}
    #back {{ color: #888; text-decoration: none; font-size: 20px; line-height: 1; transition: color 0.15s; }}
    #back:hover {{ color: #fff; }}
    #topbar-search {{ position: relative; margin-left: auto; width: 260px; }}
    #topbar-search-input {{
      width: 100%; padding: 8px 12px; font-size: 13px;
      background: #1a1a1a; border: 1px solid #2e2e2e; border-radius: 8px;
      color: #eee; outline: none; transition: border-color 0.15s;
    }}
    #topbar-search-input::placeholder {{ color: #444; }}
    #topbar-search-input:focus {{ border-color: #555; }}
    #topbar-search-results {{
      position: absolute; top: calc(100% + 6px); right: 0; width: 100%;
      max-height: 320px; overflow-y: auto; z-index: 10;
      background: #161616; border: 1px solid #2a2a2a; border-radius: 10px;
      display: none; flex-direction: column;
    }}
    #topbar-search-results.open {{ display: flex; }}
    .search-result {{
      padding: 9px 12px; cursor: pointer; display: flex; align-items: center; gap: 10px;
      border-bottom: 1px solid #222; transition: background 0.15s;
    }}
    .search-result:last-child {{ border-bottom: none; }}
    .search-result:hover {{ background: #222; }}
    .search-result-logo {{
      width: 24px; height: 24px; border-radius: 50%; object-fit: cover; flex-shrink: 0;
    }}
    .search-result-logo-initial {{
      width: 24px; height: 24px; border-radius: 50%; flex-shrink: 0;
      display: flex; align-items: center; justify-content: center;
      font-size: 11px; font-weight: 700; color: #111;
    }}
    .search-result-name {{ font-size: 13px; color: #eee; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .search-result-empty {{ padding: 12px; font-size: 12px; color: #555; text-align: center; }}
    #main {{ flex: 1; display: flex; overflow: hidden; }}
    #graph-col {{ flex: 0 0 100%; position: relative; }}
    #links-canvas {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; touch-action: none; }}
    svg#graph {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: block; cursor: grab; background: transparent; touch-action: none; }}
    svg#graph:active {{ cursor: grabbing; }}
    #panel {{
      flex: 0 0 30%; background: #111; border-left: 1px solid #1e1e1e;
      padding: 24px 20px; overflow-y: auto; display: none; flex-direction: column; gap: 16px;
      position: relative;
    }}
    #panel-close {{
      position: absolute; top: 16px; right: 16px; width: 28px; height: 28px;
      display: flex; align-items: center; justify-content: center;
      background: transparent; border: none; border-radius: 50%;
      color: #666; font-size: 18px; line-height: 1; cursor: pointer;
      transition: background 0.15s, color 0.15s;
    }}
    #panel-close:hover {{ background: #222; color: #fff; }}
    #panel-logo-row {{ display: flex; align-items: center; gap: 10px; align-self: flex-start; }}
    #panel-logo {{
      width: 80px; height: 80px; border-radius: 12px; object-fit: contain;
      background: #1a1a1a; border: 1px solid #222; padding: 6px;
    }}
    #panel-logo-initial {{
      width: 80px; height: 80px; border-radius: 12px;
      display: none; align-items: center; justify-content: center;
      font-size: 32px; font-weight: 700;
    }}
    #panel-logo-download {{
      display: none; width: 28px; height: 28px; align-items: center; justify-content: center;
      background: #1a1a1a; border: 1px solid #2e2e2e; border-radius: 8px;
      color: #888; text-decoration: none; font-size: 15px; flex-shrink: 0;
      transition: background 0.15s, color 0.15s;
    }}
    #panel-logo-download:hover {{ background: #222; color: #fff; }}
    #panel-name {{ font-size: 20px; font-weight: 700; color: #fff; line-height: 1.3; padding-right: 24px; }}
    #panel-link {{ font-size: 12px; color: #555; text-decoration: none; word-break: break-all; transition: color 0.15s; }}
    #panel-link:hover {{ color: #888; }}
    .badges {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .badge {{ font-size: 11px; padding: 3px 9px; border-radius: 20px; font-weight: 500; color: #111; white-space: nowrap; }}
    #panel-desc {{ font-size: 13px; color: #aaa; line-height: 1.6; }}
    #panel-hint {{ font-size: 13px; color: #444; }}
    #panel-view-btn {{
      display: none; margin-top: 8px;
      background: #1e1e1e; border: 1px solid #333; border-radius: 8px;
      color: #ccc; padding: 10px 16px; font-size: 13px;
      text-align: center; text-decoration: none; transition: background 0.15s, color 0.15s;
    }}
    #panel-view-btn:hover {{ background: #2a2a2a; color: #fff; }}
    .node circle {{ cursor: pointer; stroke-width: 2px; transition: stroke 0.15s; }}
    .node circle.selected {{ stroke: #fff !important; stroke-width: 3px; }}
    .node text {{ font-size: 11px; fill: #ccc; pointer-events: none; }}
    .link {{ stroke: #aaa; fill: none; }}
    @media (max-width: 768px) {{
      #topbar {{ padding: 0 12px; gap: 10px; }}
      #topbar-search {{ width: 100%; }}
      #topbar-search-input {{ font-size: 16px; }}
      #panel {{
        position: fixed; left: 0; right: 0; bottom: 0; top: auto;
        flex: none !important; width: 100%; max-height: 65vh;
        border-left: none; border-top: 1px solid #1e1e1e;
        border-radius: 16px 16px 0 0;
        box-shadow: 0 -8px 24px rgba(0, 0, 0, 0.5);
        z-index: 15;
      }}
    }}
  </style>
</head>
<body>
  <div id="topbar">
    <a id="back" href="/">&#8592;</a>
    <div id="topbar-search">
      <input id="topbar-search-input" type="text" placeholder="Search a startup…" autocomplete="off">
      <div id="topbar-search-results"></div>
    </div>
  </div>
  <div id="main">
    <div id="graph-col">
      <canvas id="links-canvas"></canvas>
      <svg id="graph"></svg>
    </div>
    <div id="panel">
      <button id="panel-close" aria-label="Close" title="Close">&#10005;</button>
      <div id="panel-logo-row">
        <img id="panel-logo" src="" alt="" style="display:none">
        <div id="panel-logo-initial"></div>
        <a id="panel-logo-download" href="#" download title="Télécharger le logo">&#8681;</a>
      </div>
      <div id="panel-name">Click a node</div>
      <a id="panel-link" href="#" target="_blank" style="display:none"></a>
      <a id="panel-linkedin" href="#" target="_blank" style="display:none; font-size:12px; color:#0a66c2;">LinkedIn →</a>
      <div class="badges" id="panel-badges"></div>
      <div id="panel-desc"></div>
      <a id="panel-view-btn">View graph →</a>
    </div>
  </div>
  <script src="https://d3js.org/d3.v7.min.js"></script>
  <script>
{SECTOR_COLORS_JS}

// Shared onerror fallback for a flaticon_url/logo_url <img> in a list --
// see SEARCH_HTML's copy of this function for the full rationale (each page
// here is a fully separate document with no shared script file, hence the
// duplication).
function handleLogoImgError(imgEl) {{
  const div = document.createElement("div");
  div.className = imgEl.dataset.fallbackClass;
  if (imgEl.dataset.fallbackBg) div.style.background = imgEl.dataset.fallbackBg;
  if (imgEl.dataset.fallbackColor) div.style.color = imgEl.dataset.fallbackColor;
  div.textContent = imgEl.dataset.fallbackInitial || "";
  imgEl.replaceWith(div);
}}

const isMobile = () => window.matchMedia("(max-width: 768px)").matches;

function showPanel(node) {{
  document.getElementById("panel").style.display = "flex";
  if (!isMobile()) document.getElementById("graph-col").style.flex = "0 0 70%";
  const logoEl        = document.getElementById("panel-logo");
  const logoDlEl      = document.getElementById("panel-logo-download");
  const logoInitialEl = document.getElementById("panel-logo-initial");
  const logoSrc = node.logo_url || node.flaticon_url;
  const color   = sectorColor(node.sectors);
  const initial = (node.name || "?")[0].toUpperCase();
  function showLogoInitial() {{
    logoEl.style.display = "none";
    logoDlEl.style.display = "none";
    logoInitialEl.style.background = color + "26";
    logoInitialEl.style.color = color;
    logoInitialEl.textContent = initial;
    logoInitialEl.style.display = "flex";
  }}
  logoEl.onerror = showLogoInitial;
  if (logoSrc) {{
    logoInitialEl.style.display = "none";
    logoEl.src           = logoSrc;
    logoEl.style.display = "";
    logoDlEl.href        = logoSrc;
    logoDlEl.download    = node.name.replace(/[^a-z0-9]+/gi, "_") + "_logo" + logoSrc.slice(logoSrc.lastIndexOf("."));
    logoDlEl.style.display = "flex";
  }} else {{
    showLogoInitial();
  }}
  document.getElementById("panel-name").textContent = node.name;
  const linkEl = document.getElementById("panel-link");
  if (node.website) {{
    linkEl.href = node.website; linkEl.textContent = node.website; linkEl.style.display = "";
  }} else {{
    linkEl.style.display = "none";
  }}
  const linkedinEl = document.getElementById("panel-linkedin");
  if (node.linkedin_url) {{
    linkedinEl.href = node.linkedin_url; linkedinEl.style.display = "";
  }} else {{
    linkedinEl.style.display = "none";
  }}
  document.getElementById("panel-badges").innerHTML = (node.sectors || []).map(s =>
    `<span class="badge" style="background:${{sectorColor([s])}}; color:#111">${{s}}</span>`
  ).join("");
  document.getElementById("panel-desc").textContent = node.description || "";
  const btn = document.getElementById("panel-view-btn");
  btn.style.display = "";
  btn.onclick = () => {{ window.location.href = "/startup/" + encodeURIComponent(node.domain); }};
}}

fetch("/api/graph/all")
  .then(r => r.json())
  .then(data => {{
    const nodes = data.nodes;
    const links = data.links;
    // Keyed by domain, not name -- two startups can share a display name.
    const nodeByDomain = new Map(nodes.map(n => [n.domain, n]));

    // Compute degree before D3 resolves link source/target to objects
    const deg = {{}};
    links.forEach(l => {{
      deg[l.source] = (deg[l.source] || 0) + 1;
      deg[l.target] = (deg[l.target] || 0) + 1;
    }});
    const maxDeg = Math.max(...Object.values(deg), 1);
    const nodeR = n => 5 + ((deg[n.domain] || 0) / maxDeg) * 15;

    let W = document.getElementById("graph-col").clientWidth;
    let H = document.getElementById("graph-col").clientHeight;
    const svg = d3.select("svg#graph").attr("width", W).attr("height", H);

    // Links are drawn on a <canvas> instead of as SVG <line> elements -- with
    // ~6000 links, redrawing that many DOM nodes every simulation tick (and on
    // every pan/zoom) is what made the graph laggy. A canvas repaint of the
    // same lines is one imperative draw call per link with no DOM/layout cost.
    const linkCanvas = document.getElementById("links-canvas");
    const lctx = linkCanvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    linkCanvas.width  = W * dpr;
    linkCanvas.height = H * dpr;
    let currentTransform = d3.zoomIdentity;

    function drawLinks() {{
      lctx.save();
      lctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      lctx.clearRect(0, 0, W, H);
      lctx.translate(currentTransform.x, currentTransform.y);
      lctx.scale(currentTransform.k, currentTransform.k);
      lctx.strokeStyle = "#aaaaaa";
      for (const l of links) {{
        lctx.globalAlpha = 0.15 + (l.score || 0) * 0.45;
        lctx.lineWidth = 1 + (l.score || 0) * 4;
        lctx.beginPath();
        lctx.moveTo(l.source.x, l.source.y);
        lctx.lineTo(l.target.x, l.target.y);
        lctx.stroke();
      }}
      lctx.restore();
    }}

    // #graph-col's width changes whenever the side panel opens/closes (its flex-basis
    // flips between 100% and 70%, see showPanel()/panel-close below) and on a plain
    // window resize. svg#graph has no viewBox, so a CSS-driven box resize just clips
    // its visible area -- node positions (translate(d.x,d.y), unaffected) stay correct
    // automatically. links-canvas is a raster <canvas>, though: resizing its CSS box
    // without also resizing its width/height attributes makes the browser stretch the
    // existing bitmap to fit, scaling every already-drawn line -- producing exactly the
    // "nodes don't move but edges do (or vice versa)" mismatch this fixes. Re-reading W/H
    // and re-applying them to both svg and canvas keeps the two perfectly in sync.
    function syncGraphViewportSize() {{
      const newW = document.getElementById("graph-col").clientWidth;
      const newH = document.getElementById("graph-col").clientHeight;
      if (newW === W && newH === H) return;
      W = newW; H = newH;
      svg.attr("width", W).attr("height", H);
      linkCanvas.width  = W * dpr;
      linkCanvas.height = H * dpr;
      drawLinks();
    }}
    new ResizeObserver(syncGraphViewportSize).observe(document.getElementById("graph-col"));

    const g = svg.append("g");
    const defs = svg.append("defs");

    const zoom = d3.zoom()
      .scaleExtent([0.05, 4])
      // d3's default only boosts wheel delta 10x for ctrlKey events (how browsers
      // report a trackpad pinch) -- a plain two-finger scroll gets the un-boosted
      // 0.002 multiplier, which reads as "nothing happens". Applying the same
      // boost regardless of ctrlKey makes a two-finger scroll zoom just as
      // responsively as a pinch, without needing to pinch.
      .wheelDelta(event => -event.deltaY * (event.deltaMode === 1 ? 0.05 : event.deltaMode ? 1 : 0.002) * 10)
      .on("zoom", e => {{
        currentTransform = e.transform;
        g.attr("transform", e.transform);
        drawLinks();
      }});
    svg.call(zoom);

    const sim = d3.forceSimulation(nodes)
      .force("link",      d3.forceLink(links).id(d => d.domain).distance(120))
      .force("charge",    d3.forceManyBody().strength(-300))
      .force("center",    d3.forceCenter(W / 2, H / 2))
      .force("collision", d3.forceCollide().radius(d => nodeR(d) + 4));

    const nodeSel = g.append("g")
      .selectAll("g")
      .data(nodes)
      .join("g")
        .attr("class", "node")
        .call(d3.drag()
          .on("start", (e, d) => {{ if (!e.active) sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; }})
          .on("drag",  (e, d) => {{ d.fx = e.x; d.fy = e.y; }})
          .on("end",   (e, d) => {{ if (!e.active) sim.alphaTarget(0); d.fx = null; d.fy = null; }})
        );

    function selectNode(d) {{
      nodeSel.selectAll("circle").classed("selected", false);
      nodeSel.filter(n => n.domain === d.domain).select("circle").classed("selected", true);
      showPanel(d);
    }}

    document.getElementById("panel-close").addEventListener("click", () => {{
      document.getElementById("panel").style.display = "none";
      document.getElementById("graph-col").style.flex = "0 0 100%";
      nodeSel.selectAll("circle").classed("selected", false);
    }});

    function centerOnNode(d) {{
      const scale = Math.max(d3.zoomTransform(svg.node()).k, 1.4);
      const transform = d3.zoomIdentity
        .translate(W / 2, H / 2)
        .scale(scale)
        .translate(-d.x, -d.y);
      svg.transition().duration(600).call(zoom.transform, transform);
    }}

    nodeSel.append("circle")
      .attr("r",      nodeR)
      .attr("fill",   d => d.flaticon_url ? "none" : sectorColor(d.sectors))
      .attr("stroke", d => d.flaticon_url ? sectorColor(d.sectors) : "#0f0f0f")
      .style("pointer-events", "all")
      .on("click", (e, d) => {{
        selectNode(d);
        e.stopPropagation();
      }});

    // Colored-initial fallback, shown whenever there's no logo image (never
    // had one, or its <image> below failed to load).
    nodeSel.append("text")
      .attr("class",  "node-initial")
      .attr("x", 0).attr("y", 4)
      .attr("text-anchor", "middle")
      .style("font-weight", "700")
      .style("font-size", d => nodeR(d) * 0.7 + "px")
      .style("fill", "#0f0f0f")
      .style("pointer-events", "none")
      .style("display", d => d.flaticon_url ? "none" : null)
      .text(d => (d.name || "?")[0].toUpperCase());

    nodeSel.each(function(d, i) {{
      if (!d.flaticon_url) return;
      const r = nodeR(d);
      defs.append("clipPath")
        .attr("id", "logo-clip-" + i)
        .append("circle").attr("r", r);
      d3.select(this).append("image")
        .attr("href", d.flaticon_url)
        .attr("x", -r).attr("y", -r)
        .attr("width",  r * 2).attr("height", r * 2)
        .attr("clip-path", "url(#logo-clip-" + i + ")")
        .attr("preserveAspectRatio", "xMidYMid slice")
        .style("pointer-events", "none")
        .on("error", function() {{
          const g = d3.select(this.parentNode);
          d3.select(this).remove();
          g.select("circle").attr("fill", sectorColor(d.sectors)).attr("stroke", "#0f0f0f");
          g.select(".node-initial").style("display", null);
        }});
    }});

    nodeSel.append("text")
      .attr("x",           0)
      .attr("y",           d => nodeR(d) + 16)
      .attr("text-anchor", "middle")
      .style("fill",         "#ffffff")
      .style("font-size",    "11px")
      .style("stroke",       "#000000")
      .style("stroke-width", "3px")
      .style("paint-order",  "stroke")
      .style("pointer-events", "none")
      .text(d => d.name.length > 20 ? d.name.slice(0, 18) + "…" : d.name);

    function renderAll() {{
      drawLinks();
      nodeSel.attr("transform", d => `translate(${{d.x}},${{d.y}})`);
    }}

    // Run the layout to convergence headlessly (pure math, no DOM writes) instead
    // of animating from a random scatter -- that animation was the other big
    // source of lag, since every one of ~300 in-between frames repainted every
    // node and link. simulation.tick() applies the same alpha decay as the
    // internal timer, so this reaches the same resting layout, just instantly.
    sim.stop();
    for (let i = 0; i < 300; ++i) sim.tick();
    renderAll();

    sim.on("tick", renderAll);

    // ── Search bar ──────────────────────────────────────────────────────────
    const searchInput   = document.getElementById("topbar-search-input");
    const searchResults = document.getElementById("topbar-search-results");
    let searchTimer;

    function jumpToNode(domain) {{
      const d = nodeByDomain.get(domain);
      if (!d) return;
      searchInput.value = "";
      searchResults.classList.remove("open");
      searchResults.innerHTML = "";
      selectNode(d);
      centerOnNode(d);
    }}

    function renderSearchResults(list) {{
      if (!list.length) {{
        searchResults.innerHTML = '<div class="search-result-empty">No startup found</div>';
        searchResults.classList.add("open");
        return;
      }}
      searchResults.innerHTML = list.map(s => {{
        const color   = sectorColor(s.sectors);
        const initial = (s.name || "?")[0].toUpperCase();
        const logoHtml = s.flaticon_url
          ? `<img class="search-result-logo" src="${{s.flaticon_url}}" alt="" onerror="handleLogoImgError(this)" data-fallback-class="search-result-logo-initial" data-fallback-bg="${{color}}" data-fallback-initial="${{initial}}">`
          : `<div class="search-result-logo-initial" style="background:${{color}}">${{initial}}</div>`;
        return `<div class="search-result" data-domain="${{s.domain}}">
          ${{logoHtml}}
          <div class="search-result-name">${{s.name}}</div>
        </div>`;
      }}).join("");
      searchResults.classList.add("open");
    }}

    searchInput.addEventListener("input", () => {{
      clearTimeout(searchTimer);
      const q = searchInput.value.trim();
      if (q.length < 2) {{
        searchResults.classList.remove("open");
        searchResults.innerHTML = "";
        return;
      }}
      searchTimer = setTimeout(() => {{
        fetch("/api/search?q=" + encodeURIComponent(q))
          .then(r => r.json())
          .then(renderSearchResults);
      }}, 300);
    }});

    searchResults.addEventListener("click", e => {{
      const card = e.target.closest(".search-result");
      if (card) jumpToNode(card.dataset.domain);
    }});

    document.addEventListener("click", e => {{
      if (!document.getElementById("topbar-search").contains(e.target)) {{
        searchResults.classList.remove("open");
      }}
    }});
  }})
  .catch(err => {{
    console.error("Erreur de chargement du graphe :", err);
    document.title = "Erreur de chargement — Full Startup Graph";
  }});
  </script>
</body>
</html>"""


ADMIN_DASHBOARD_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dashboard — Smap</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0f0f0f; color: #e8e4dc;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      min-height: 100vh; padding: 28px 32px 60px;
    }}
    #dash-header {{ display: flex; align-items: center; gap: 16px; margin-bottom: 28px; }}
    #dash-header a {{ color: #8a8680; font-size: 13px; text-decoration: none; }}
    #dash-header a:hover {{ color: #c7c3bb; }}
    #dash-header h1 {{ font-size: 20px; font-weight: 700; }}
    #dash-generated {{ margin-left: auto; font-size: 12px; color: #55524c; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 16px; }}
    .card {{
      background: #161614; border: 1px solid #2a2a27; border-radius: 12px; padding: 18px 20px;
    }}
    .card h2 {{ font-size: 13px; font-weight: 600; color: #8a8680; text-transform: uppercase; letter-spacing: 0.03em; margin-bottom: 14px; }}
    .stat-row {{ display: flex; gap: 24px; flex-wrap: wrap; }}
    .stat {{ min-width: 90px; }}
    .stat-value {{ font-size: 26px; font-weight: 700; color: #fff; line-height: 1.2; }}
    .stat-label {{ font-size: 12px; color: #8a8680; margin-top: 2px; }}
    .bar-row {{ display: flex; align-items: center; gap: 10px; margin-bottom: 9px; font-size: 12.5px; }}
    .bar-row:last-child {{ margin-bottom: 0; }}
    .bar-label {{ width: 140px; flex-shrink: 0; color: #c7c3bb; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .bar-track {{ flex: 1; height: 8px; background: #201f1c; border-radius: 4px; overflow: hidden; }}
    .bar-fill {{ height: 100%; border-radius: 4px; }}
    .bar-fill.warn {{ background: #d1a565; }}
    .bar-fill.ok {{ background: #6fcf6f; }}
    .bar-value {{ width: 68px; flex-shrink: 0; text-align: right; color: #8a8680; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12.5px; }}
    th {{ text-align: left; color: #8a8680; font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.02em; padding: 0 10px 8px 0; border-bottom: 1px solid #2a2a27; }}
    td {{ padding: 9px 10px 9px 0; border-bottom: 1px solid #201f1c; color: #c7c3bb; vertical-align: middle; }}
    tr:last-child td {{ border-bottom: none; }}
    .recent-name {{ display: flex; align-items: center; gap: 8px; color: #e8e4dc; font-weight: 600; }}
    .recent-logo {{ width: 20px; height: 20px; border-radius: 50%; object-fit: cover; flex-shrink: 0; background: #201f1c; }}
    .recent-logo-initial {{ display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 700; color: #111; }}
    .mini-badge {{ font-size: 10px; padding: 2px 7px; border-radius: 10px; font-weight: 600; color: #111; white-space: nowrap; }}
    .cost-day-bars {{ display: flex; align-items: flex-end; gap: 4px; height: 60px; margin-top: 4px; }}
    .cost-day-bar {{ flex: 1; background: #7cb8e8; border-radius: 2px 2px 0 0; min-height: 2px; }}
    .empty-note {{ color: #55524c; font-size: 12.5px; }}
    #loading {{ color: #55524c; font-size: 13px; }}
    @media (max-width: 640px) {{
      body {{ padding: 20px 16px 40px; }}
      #dash-header {{ flex-wrap: wrap; row-gap: 6px; }}
      #dash-generated {{ margin-left: 0; flex-basis: 100%; }}
      .grid {{ grid-template-columns: 1fr; }}
      .bar-label {{ width: 96px; }}
      table {{ min-width: 480px; }}
    }}
  </style>
</head>
<body>
  <div id="dash-header">
    <a href="/">← Smap</a>
    <h1>Dashboard</h1>
    <span id="dash-generated"></span>
  </div>
  <div id="loading">Chargement…</div>
  <div id="dash-content" style="display:none">
    <div class="grid">
      <div class="card" id="card-overview"><h2>Vue d'ensemble</h2><div class="stat-row" id="overview-stats"></div></div>
      <div class="card" id="card-cost"><h2>Coût API Mistral</h2><div class="stat-row" id="cost-stats"></div><div class="cost-day-bars" id="cost-day-bars"></div></div>
      <div class="card" id="card-health"><h2>Santé de l'ingestion</h2><div class="stat-row" id="health-stats"></div></div>
    </div>
    <div class="grid">
      <div class="card" id="card-completeness"><h2>Complétude des données</h2><div id="completeness-bars"></div></div>
      <div class="card" id="card-sectors"><h2>Répartition par secteur</h2><div id="sector-bars"></div></div>
      <div class="card" id="card-cost-breakdown"><h2>Coût par type d'appel</h2><div id="cost-breakdown-bars"></div></div>
    </div>
    <div class="card">
      <h2>Derniers ajouts</h2>
      <div style="overflow-x:auto">
        <table>
          <thead><tr>
            <th>Startup</th><th>Secteurs</th><th>Concurrents</th><th>Candidats scorés</th><th>Coût</th><th>Ajouté par</th><th>Terminé</th>
          </tr></thead>
          <tbody id="recent-rows"></tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <h2>Échecs</h2>
      <div style="overflow-x:auto">
        <table>
          <thead><tr>
            <th>URL</th><th>Ajouté par</th><th>Erreur</th><th>Échoué</th>
          </tr></thead>
          <tbody id="failure-rows"></tbody>
        </table>
      </div>
    </div>
  </div>
  <script>
{SECTOR_COLORS_JS}

function escapeHtml(str) {{
  const map = {{ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }};
  return String(str == null ? "" : str).replace(/[&<>"']/g, ch => map[ch]);
}}

// See SEARCH_HTML's copy of this function for the full rationale.
function handleLogoImgError(imgEl) {{
  const div = document.createElement("div");
  div.className = imgEl.dataset.fallbackClass;
  if (imgEl.dataset.fallbackBg) div.style.background = imgEl.dataset.fallbackBg;
  if (imgEl.dataset.fallbackColor) div.style.color = imgEl.dataset.fallbackColor;
  div.textContent = imgEl.dataset.fallbackInitial || "";
  imgEl.replaceWith(div);
}}

function fmtUsd(n) {{
  if (n == null) return "—";
  return "$" + n.toFixed(n < 0.01 ? 5 : 4);
}}

function timeAgo(iso) {{
  if (!iso) return "—";
  const diffMs = Date.now() - new Date(iso).getTime();
  const mins = Math.round(diffMs / 60000);
  if (mins < 1) return "à l'instant";
  if (mins < 60) return mins + " min";
  const hours = Math.round(mins / 60);
  if (hours < 24) return hours + " h";
  return Math.round(hours / 24) + " j";
}}

function barRow(label, value, valueText, pct, cls) {{
  return `<div class="bar-row">
    <div class="bar-label" title="${{escapeHtml(label)}}">${{escapeHtml(label)}}</div>
    <div class="bar-track"><div class="bar-fill ${{cls || ''}}" style="width:${{pct}}%"></div></div>
    <div class="bar-value">${{escapeHtml(valueText)}}</div>
  </div>`;
}}

fetch("/api/dashboard")
  .then(r => {{
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }})
  .then(data => {{
    document.getElementById("loading").style.display = "none";
    document.getElementById("dash-content").style.display = "block";
    document.getElementById("dash-generated").textContent =
      "Généré " + new Date(data.generated_at).toLocaleString("fr-FR");

    const ov = data.overview;
    document.getElementById("overview-stats").innerHTML = `
      <div class="stat"><div class="stat-value">${{ov.total_startups.toLocaleString()}}</div><div class="stat-label">Startups</div></div>
      <div class="stat"><div class="stat-value">${{ov.total_competitor_links.toLocaleString()}}</div><div class="stat-label">Liens concurrents</div></div>
      <div class="stat"><div class="stat-value">${{ov.non_owner_users}}${{ov.max_users ? '/' + ov.max_users : ''}}</div><div class="stat-label">Comptes inscrits</div></div>
    `;

    const c = data.cost;
    document.getElementById("cost-stats").innerHTML = `
      <div class="stat"><div class="stat-value">${{fmtUsd(c.total_cost_usd)}}</div><div class="stat-label">Coût total</div></div>
      <div class="stat"><div class="stat-value">${{fmtUsd(c.avg_cost_per_startup_usd)}}</div><div class="stat-label">Coût moyen / startup</div></div>
      <div class="stat"><div class="stat-value">${{c.total_calls.toLocaleString()}}</div><div class="stat-label">Appels API</div></div>
    `;
    const days = c.cost_by_day || [];
    const maxDay = Math.max(...days.map(d => d.cost_usd), 0.000001);
    document.getElementById("cost-day-bars").innerHTML = days.length
      ? days.map(d => `<div class="cost-day-bar" style="height:${{Math.max(4, d.cost_usd / maxDay * 60)}}px" title="${{d.day}} — ${{fmtUsd(d.cost_usd)}}"></div>`).join("")
      : '<span class="empty-note">Pas encore de données de coût.</span>';

    const breakdownEl = document.getElementById("cost-breakdown-bars");
    const byType = Object.entries(c.by_call_type || {{}});
    if (!byType.length) {{
      breakdownEl.innerHTML = '<span class="empty-note">Pas encore de données de coût.</span>';
    }} else {{
      const maxCost = Math.max(...byType.map(([, v]) => v.cost_usd), 0.000001);
      breakdownEl.innerHTML = byType
        .sort((a, b) => b[1].cost_usd - a[1].cost_usd)
        .map(([type, v]) => barRow(type, v.cost_usd, fmtUsd(v.cost_usd) + " (" + v.calls + ")", v.cost_usd / maxCost * 100, "ok"))
        .join("");
    }}

    const h = data.ingestion_health;
    document.getElementById("health-stats").innerHTML = `
      <div class="stat"><div class="stat-value">${{h.success_rate_pct}}%</div><div class="stat-label">Taux de succès</div></div>
      <div class="stat"><div class="stat-value">${{h.error}}</div><div class="stat-label">Échecs</div></div>
      <div class="stat"><div class="stat-value">${{h.avg_processing_seconds != null ? Math.round(h.avg_processing_seconds) + 's' : '—'}}</div><div class="stat-label">Temps moyen</div></div>
    `;

    const compl = data.completeness;
    const fieldLabels = {{
      linkedin_url: "LinkedIn", logo_url: "Logo", flaticon_url: "Favicon",
      description: "Description", country: "Pays", sub_subsectors: "Sous-sous-secteurs", embedding: "Embedding",
    }};
    document.getElementById("completeness-bars").innerHTML = compl.fields
      .map(f => barRow(fieldLabels[f.field] || f.field, f.missing, f.missing_pct + "%", f.missing_pct, f.missing_pct > 30 ? "warn" : ""))
      .join("");

    const sectorsEl = document.getElementById("sector-bars");
    const maxSectorCount = Math.max(...data.sectors.map(s => s.count), 1);
    sectorsEl.innerHTML = data.sectors
      .map(s => barRow(s.sector, s.count, s.count + " (" + s.pct + "%)", s.count / maxSectorCount * 100))
      .join("");
    // Color each sector's bar to match the graph's own sector-color legend
    sectorsEl.querySelectorAll(".bar-row").forEach((row, i) => {{
      row.querySelector(".bar-fill").style.background = sectorColor([data.sectors[i].sector]);
    }});

    const recentEl = document.getElementById("recent-rows");
    if (!data.recent.length) {{
      recentEl.innerHTML = '<tr><td colspan="7" class="empty-note">Aucun ajout terminé pour l\\'instant.</td></tr>';
    }} else {{
      recentEl.innerHTML = data.recent.map(r => {{
        const color   = sectorColor(r.sectors);
        const initial = (r.name || "?")[0].toUpperCase();
        const logo = r.flaticon_url
          ? `<img class="recent-logo" src="${{r.flaticon_url}}" alt="" onerror="handleLogoImgError(this)" data-fallback-class="recent-logo recent-logo-initial" data-fallback-bg="${{color}}" data-fallback-initial="${{initial}}">`
          : `<div class="recent-logo recent-logo-initial" style="background:${{color}}">${{initial}}</div>`;
        const badges = (r.sectors || []).slice(0, 2).map(s =>
          `<span class="mini-badge" style="background:${{sectorColor([s])}}">${{escapeHtml(s)}}</span>`
        ).join(" ");
        return `<tr>
          <td><div class="recent-name">${{logo}}${{escapeHtml(r.name)}}</div></td>
          <td>${{badges}}</td>
          <td>${{r.competitors_found ?? "—"}}</td>
          <td>${{r.candidates_scored ?? "—"}}</td>
          <td>${{fmtUsd(r.cost_usd)}}</td>
          <td>${{escapeHtml(r.added_by || "—")}}</td>
          <td>${{timeAgo(r.completed_at)}}</td>
        </tr>`;
      }}).join("");
    }}

    const failureEl = document.getElementById("failure-rows");
    if (!data.failures.length) {{
      failureEl.innerHTML = '<tr><td colspan="4" class="empty-note">Aucun échec pour l\\'instant.</td></tr>';
    }} else {{
      failureEl.innerHTML = data.failures.map(f => `<tr>
          <td>${{escapeHtml(f.url)}}</td>
          <td>${{escapeHtml(f.added_by || "—")}}</td>
          <td>${{escapeHtml(f.error_message || "—")}}</td>
          <td>${{timeAgo(f.failed_at)}}</td>
        </tr>`).join("");
    }}
  }})
  .catch(err => {{
    document.getElementById("loading").textContent = "Erreur de chargement : " + err.message;
  }});
  </script>
</body>
</html>"""


@app.get("/admin")
def admin_page():
    return HTMLResponse(content=ADMIN_DASHBOARD_HTML)


@app.get("/")
def index(request: Request):
    # request.state.user is set by auth_gate (Design Notes) -- by the time this
    # handler runs, an unauthenticated request has already been redirected to
    # /login by the middleware, so user here is always the logged-in account.
    user = getattr(request.state, "user", None)
    is_owner = bool(user and user.get("is_owner"))
    graph_link = (
        '<a href="/graph" id="graph-link">Vue graphe complet →</a>'
        '<a href="/admin" id="admin-link">Dashboard →</a>'
        '<div id="nav-divider"></div>'
        if is_owner
        else ""
    )
    # Owner-only "Toutes / Mes ingestions" scope toggle -- a non-owner's view
    # is always restricted server-side regardless of this control, so there's
    # nothing for them to toggle (queueScopeBtns.length being 0 also tells
    # the frontend JS not to send a scope param at all for them).
    queue_scope_toggle = (
        '<div id="queue-scope-toggle">'
        '<button class="queue-scope-btn active" data-scope="all" type="button">Toutes</button>'
        '<button class="queue-scope-btn" data-scope="mine" type="button">Mes ingestions</button>'
        '</div>'
        if is_owner
        else ""
    )
    html = SEARCH_HTML.replace("__GRAPH_NAV_LINK__", graph_link).replace("__QUEUE_SCOPE_TOGGLE__", queue_scope_toggle)
    return HTMLResponse(content=html)


@app.get("/graph")
def graph_page():
    return HTMLResponse(content=GLOBAL_GRAPH_HTML)


@app.get("/startup/{domain}")
def startup_page(domain: str):
    html = GRAPH_HTML_TEMPLATE.replace("__STARTUP_DOMAIN_JSON__", json.dumps(domain))
    return HTMLResponse(content=html)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
