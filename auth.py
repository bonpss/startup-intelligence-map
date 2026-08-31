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
