import os

import pytest

import auth
import storage


# ── hash_password / verify_password ───────────────────────────────────────────

def test_hash_password_roundtrips_with_verify_password():
    hashed = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("correct horse battery staple", hashed)


def test_verify_password_rejects_wrong_password():
    hashed = auth.hash_password("correct horse battery staple")
    assert not auth.verify_password("wrong password", hashed)


def test_hash_password_never_stores_plaintext():
    hashed = auth.hash_password("hunter2")
    assert "hunter2" not in hashed


def test_verify_password_fails_closed_on_malformed_hash():
    # A corrupt/foreign hash string must return False, not raise -- a 500
    # would be worse than a clean 401 here.
    assert not auth.verify_password("anything", "not-a-real-bcrypt-hash")


# ── normalize_email ────────────────────────────────────────────────────────────

def test_normalize_email_trims_and_lowercases():
    assert auth.normalize_email("  Foo@Bar.COM  ") == "foo@bar.com"


def test_normalize_email_handles_none():
    assert auth.normalize_email(None) == ""


# ── is_owner_email ──────────────────────────────────────────────────────────────

def test_is_owner_email_matches_case_insensitively(monkeypatch):
    monkeypatch.setenv("OWNER_EMAIL", "Julien@Example.com")
    assert auth.is_owner_email("julien@example.com")
    assert auth.is_owner_email("JULIEN@EXAMPLE.COM")
    assert auth.is_owner_email("  julien@example.com  ")


def test_is_owner_email_rejects_non_matching_email(monkeypatch):
    monkeypatch.setenv("OWNER_EMAIL", "julien@example.com")
    assert not auth.is_owner_email("someone-else@example.com")


def test_is_owner_email_false_when_owner_email_unset(monkeypatch):
    monkeypatch.delenv("OWNER_EMAIL", raising=False)
    assert not auth.is_owner_email("anyone@example.com")


def test_is_owner_email_false_when_owner_email_blank(monkeypatch):
    monkeypatch.setenv("OWNER_EMAIL", "   ")
    assert not auth.is_owner_email("anyone@example.com")


# ── signup_cap_reached (MAX_USERS) ───────────────────────────────────────────

def test_signup_cap_reached_true_at_or_above_max_users(monkeypatch):
    monkeypatch.setenv("MAX_USERS", "5")
    monkeypatch.setattr(storage, "count_non_owner_users", lambda: 5)
    assert auth.signup_cap_reached()


def test_signup_cap_reached_true_above_max_users(monkeypatch):
    monkeypatch.setenv("MAX_USERS", "5")
    monkeypatch.setattr(storage, "count_non_owner_users", lambda: 6)
    assert auth.signup_cap_reached()


def test_signup_cap_not_reached_below_max_users(monkeypatch):
    monkeypatch.setenv("MAX_USERS", "5")
    monkeypatch.setattr(storage, "count_non_owner_users", lambda: 4)
    assert not auth.signup_cap_reached()


def test_signup_cap_reached_when_max_users_unset(monkeypatch):
    # Unset MAX_USERS reads as 0 -- fail closed (signup effectively capped at
    # zero seats) rather than raising or reading as unlimited.
    monkeypatch.delenv("MAX_USERS", raising=False)
    monkeypatch.setattr(storage, "count_non_owner_users", lambda: 0)
    assert auth.signup_cap_reached()


def test_signup_cap_reached_when_max_users_malformed(monkeypatch):
    # A non-numeric MAX_USERS (typo'd env var) must also fail closed, not
    # raise ValueError and 500 every signup attempt (step-04 review).
    monkeypatch.setenv("MAX_USERS", "five")
    monkeypatch.setattr(storage, "count_non_owner_users", lambda: 0)
    assert auth.signup_cap_reached()


# ── get_current_user / require_owner (session-cookie reads, no DB I/O) ──────

class _FakeRequest:
    def __init__(self, session):
        self.session = session


def test_get_current_user_returns_none_without_session_user():
    assert auth.get_current_user(_FakeRequest({})) is None


def test_get_current_user_returns_session_user_dict():
    user = {"id": 1, "email": "a@b.com", "is_owner": False}
    assert auth.get_current_user(_FakeRequest({"user": user})) == user


def test_require_owner_true_for_owner_session():
    user = {"id": 1, "email": "owner@example.com", "is_owner": True}
    assert auth.require_owner(_FakeRequest({"user": user}))


def test_require_owner_false_for_non_owner_session():
    user = {"id": 2, "email": "guest@example.com", "is_owner": False}
    assert not auth.require_owner(_FakeRequest({"user": user}))


def test_require_owner_false_with_no_session():
    assert not auth.require_owner(_FakeRequest({}))


# ── storage.create_user / get_user_by_email / count_non_owner_users ─────────
# Same minimal-fake convention as test_storage.py's ingestion_queue tests.

class _FakeUserSelectQuery:
    def __init__(self, rows):
        self.request = type("FakeRequest", (), {"http_method": "GET"})()
        self._rows = list(rows)

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def execute(self):
        return type("FakeResponse", (), {"data": self._rows})()


def test_get_user_by_email_returns_matching_row(monkeypatch):
    rows = [{"id": 1, "email": "a@b.com", "password_hash": "h", "is_owner": False}]
    fake_table = type("FakeTable", (), {"select": lambda self, *a, **k: _FakeUserSelectQuery(rows)})()
    fake_client = type("FakeClient", (), {"table": lambda self, name: fake_table})()
    monkeypatch.setattr(storage, "_client", lambda: fake_client)

    assert storage.get_user_by_email("a@b.com") == rows[0]


def test_get_user_by_email_returns_none_when_no_match(monkeypatch):
    fake_table = type("FakeTable", (), {"select": lambda self, *a, **k: _FakeUserSelectQuery([])})()
    fake_client = type("FakeClient", (), {"table": lambda self, name: fake_table})()
    monkeypatch.setattr(storage, "_client", lambda: fake_client)

    assert storage.get_user_by_email("nobody@example.com") is None


def test_create_user_inserts_expected_payload(monkeypatch):
    calls = {}

    class FakeInsertQuery:
        def __init__(self, values):
            self.request = type("FakeRequest", (), {"http_method": "POST"})()
            calls["insert_values"] = values

        def execute(self):
            return type("FakeResponse", (), {"data": [{"id": 42, **calls["insert_values"]}]})()

    fake_table = type("FakeTable", (), {"insert": lambda self, values: FakeInsertQuery(values)})()
    fake_client = type("FakeClient", (), {"table": lambda self, name: fake_table})()
    monkeypatch.setattr(storage, "_client", lambda: fake_client)

    user = storage.create_user("a@b.com", "hashed", is_owner=True)

    assert calls["insert_values"] == {"email": "a@b.com", "password_hash": "hashed", "is_owner": True}
    assert user["id"] == 42


def test_create_user_defaults_is_owner_false(monkeypatch):
    calls = {}

    class FakeInsertQuery:
        def __init__(self, values):
            self.request = type("FakeRequest", (), {"http_method": "POST"})()
            calls["insert_values"] = values

        def execute(self):
            return type("FakeResponse", (), {"data": [{"id": 1, **calls["insert_values"]}]})()

    fake_table = type("FakeTable", (), {"insert": lambda self, values: FakeInsertQuery(values)})()
    fake_client = type("FakeClient", (), {"table": lambda self, name: fake_table})()
    monkeypatch.setattr(storage, "_client", lambda: fake_client)

    storage.create_user("b@c.com", "hashed")
    assert calls["insert_values"]["is_owner"] is False


class _FakeCountQuery:
    def __init__(self, count):
        self.request = type("FakeRequest", (), {"http_method": "HEAD"})()
        self._count = count

    def eq(self, *a, **k):
        return self

    def execute(self):
        return type("FakeResponse", (), {"data": [], "count": self._count})()


def test_count_non_owner_users_returns_count(monkeypatch):
    def fake_select(self, *a, **k):
        assert k.get("count") == "exact"
        assert k.get("head") is True
        return _FakeCountQuery(3)

    fake_table = type("FakeTable", (), {"select": fake_select})()
    fake_client = type("FakeClient", (), {"table": lambda self, name: fake_table})()
    monkeypatch.setattr(storage, "_client", lambda: fake_client)

    assert storage.count_non_owner_users() == 3


def test_count_non_owner_users_returns_zero_when_count_is_none(monkeypatch):
    fake_table = type("FakeTable", (), {"select": lambda self, *a, **k: _FakeCountQuery(None)})()
    fake_client = type("FakeClient", (), {"table": lambda self, name: fake_table})()
    monkeypatch.setattr(storage, "_client", lambda: fake_client)

    assert storage.count_non_owner_users() == 0


# ── graph_app.py middleware ordering (regression) ────────────────────────────
# Deliberate, narrow exception to the spec's "no TestClient" guidance: the
# implementation actually hit a bug where SessionMiddleware was registered
# before auth_gate, putting auth_gate outside it and crashing every request
# with "SessionMiddleware must be installed to access request.session" --
# invisible to every other test in this file, since none of them import
# graph_app.py. This guards against that exact regression recurring silently.
# No `with TestClient(app) as client:` block -- that would run graph_app's
# lifespan (a real Supabase read via get_pending_ingestions), which this
# project's tests never do; plain request dispatch doesn't need lifespan.

def test_middleware_order_session_available_before_auth_gate(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-only-secret-not-used-in-prod")
    monkeypatch.setenv("OWNER_EMAIL", "")
    monkeypatch.setenv("MAX_USERS", "0")
    from fastapi.testclient import TestClient
    import graph_app

    client = TestClient(graph_app.app)
    # If auth_gate ran outside SessionMiddleware, request.session would raise
    # AssertionError inside the middleware, surfacing as a 500 here.
    response = client.get("/api/graph/all", follow_redirects=False)
    assert response.status_code == 401
