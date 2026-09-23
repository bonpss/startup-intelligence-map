"""Session/security logic for the capped-signup public-demo auth
(spec-public-demo-auth.md). Deliberately isolated from storage.py (no
Supabase access here beyond the one call into storage.count_non_owner_users())
and from graph_app.py's route/middleware wiring -- this module owns password
hashing, email normalization, owner detection, and the signup cap check, all
of which are pure logic (or a single read) safe to unit test without a
running app.

No new session table: a session is a signed httponly cookie (Starlette
SessionMiddleware, secret from SESSION_SECRET_KEY) carrying the logged-in
user's {id, email, is_owner} directly, so get_current_user() below is a
zero-I/O read off request.session -- no DB round-trip per request, per the
spec's Design Notes.
"""

import os
from datetime import datetime, timedelta, timezone

import bcrypt

import storage


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt. Returns a str (utf-8 decoded)
    so it's directly storable in users.password_hash (a text column) --
    callers never see or persist the plaintext.
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against a bcrypt hash. Fails closed
    (returns False) rather than raising on a malformed/foreign hash string,
    so a corrupt DB value can't turn into a 500 instead of a clean 401.
    """
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def normalize_email(email: str | None) -> str:
    """Trim + lowercase -- the one place email normalization happens (mirrors
    storage.normalize_domain's single-normalization-point convention, AD-8),
    so login/signup/OWNER_EMAIL comparisons can't drift on case or whitespace
    independently.
    """
    return (email or "").strip().lower()


def is_owner_email(email: str) -> bool:
    """Case-insensitive match against OWNER_EMAIL (I/O matrix: 'Signup, owner
    email' -- is_owner=true iff the signup email matches OWNER_EMAIL).
    An unset/blank OWNER_EMAIL never matches anything, so a misconfigured
    deploy fails closed (no accidental owner) rather than open.
    """
    owner_email = os.environ.get("OWNER_EMAIL", "")
    if not owner_email.strip():
        return False
    return normalize_email(email) == normalize_email(owner_email)


def _max_users() -> int:
    """MAX_USERS env var as an int; unset/blank/non-numeric all read as 0
    (signup closed by default) rather than raising, since a missing or
    malformed cap should fail closed, not 500 every signup attempt (step-04
    review, iteration 1 -- the previous version guarded blank but not
    malformed, e.g. a typo'd non-numeric value).
    """
    raw = os.environ.get("MAX_USERS", "").strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def signup_cap_reached() -> bool:
    """True once storage.count_non_owner_users() >= MAX_USERS -- the I/O
    matrix's 'Signup, cap reached' row. Only meaningful for non-owner
    signups; the owner account is exempt (checked by the caller before this
    is consulted, not inside it -- keeps this function's one job to the cap
    arithmetic, not owner detection).

    Known race, accepted per the spec's Design Notes: this check and the
    users insert that follows it are not atomic, so two signups landing in
    the same window right as the cap is reached could both pass. Acceptable
    for a cost-guardrail cap on a demo, not a strict security boundary.
    """
    return storage.count_non_owner_users() >= _max_users()


# Cost/abuse guardrail on /api/ingest (each ingestion runs a real browser
# render plus several Mistral calls): a non-owner user is capped at this many
# ingestions per rolling 24h window. Unlike MAX_USERS/signup, this does NOT
# fail closed to 0 on a missing/malformed env var -- an unset cap here would
# silently block every non-owner ingestion rather than just leaving signups
# closed, which is a functionality bug this project would rather avoid than
# trade for defense-in-depth that main.py's SSRF guard (net_security.py)
# already provides independently of this quota.
_DEFAULT_MAX_INGESTIONS_PER_USER_PER_DAY = 20


def _max_ingestions_per_user_per_day() -> int:
    raw = os.environ.get("MAX_INGESTIONS_PER_USER_PER_DAY", "").strip()
    if not raw:
        return _DEFAULT_MAX_INGESTIONS_PER_USER_PER_DAY
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_MAX_INGESTIONS_PER_USER_PER_DAY


class QuotaCheckError(RuntimeError):
    """Raised by ingestion_quota_reached() when the underlying count itself
    couldn't be determined (e.g. a Supabase outage) -- deliberately distinct
    from a real "quota reached" result. The caller (graph_app.py's
    api_ingest/api_retry_ingestion) must fail closed on this for non-owner
    users (refuse the ingestion) rather than let it surface as an
    unhandled 500, but it's still worth telling apart from an actual quota
    hit: one is "you've used your 20 today", the other is "we couldn't even
    check, try again shortly" -- collapsing them into a false "quota
    reached" would misreport a transient outage as the user's own usage.
    """


def ingestion_quota_reached(user_id: int) -> bool:
    """True once `user_id` has requested >= MAX_INGESTIONS_PER_USER_PER_DAY
    ingestions in the last 24h. Rolling window (not calendar-day), so a
    burst right at UTC midnight can't double a user's effective allowance.

    Owner-exemption is the caller's responsibility (graph_app.py's api_ingest
    checks is_owner before calling this), same division as
    signup_cap_reached()/is_owner_email() -- this function only does the
    quota arithmetic.

    Raises QuotaCheckError (instead of returning a value) if the count
    itself couldn't be read -- see that class's docstring for why the
    caller must treat this as "refuse" for non-owners, not "allow".
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    try:
        count = storage.count_user_ingestions_since(user_id, since)
    except Exception as e:
        raise QuotaCheckError(f"Could not verify ingestion quota for user {user_id}: {e}") from e
    return count >= _max_ingestions_per_user_per_day()


def get_current_user(request) -> dict | None:
    """Read the logged-in user straight out of the signed session cookie --
    {'id', 'email', 'is_owner'}, written at login/signup time. Returns None
    if there is no session or it carries no user. No DB round-trip.
    """
    return request.session.get("user")


def require_owner(request) -> bool:
    """True only when the current session belongs to the owner account. Used
    by graph_app.py's single auth-gating middleware to decide the /graph,
    /api/graph/all block -- not called per-route (spec: gating stays in one
    choke point, not per-route Depends).
    """
    user = get_current_user(request)
    return bool(user and user.get("is_owner"))
