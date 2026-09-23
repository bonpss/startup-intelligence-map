"""Endpoint-level security tests for graph_app.py -- SSRF/quota guards on
/api/ingest and /api/ingestion-queue/{id}/retry, and the /api/login rate
limiter's IP-spoofing resistance. Same no-lifespan TestClient convention as
test_auth.py's test_middleware_order_session_available_before_auth_gate
(plain `TestClient(app)`, no `with` block, so the real Supabase-backed
lifespan never runs) -- storage/auth calls are monkeypatched instead of
hitting a real DB.
"""

import os

# Must run before `import graph_app` -- that import fails fast at module
# level if SESSION_SECRET_KEY isn't already set (graph_app.py's own
# fail-closed guard against a blank signing secret), and a fixture can't
# help here since fixtures only run after collection-time imports have
# already executed. setdefault so a real environment's own value (e.g. a
# local .env loaded by graph_app's load_dotenv()) still wins.
os.environ.setdefault("SESSION_SECRET_KEY", "test-only-secret-not-used-in-prod")

import pytest
from fastapi.testclient import TestClient

import auth
import graph_app
import net_security


def _fake_user(is_owner: bool = False, user_id: int = 1) -> dict:
    return {"id": user_id, "email": "u@example.com", "is_owner": is_owner}


@pytest.fixture(autouse=True)
def _session_secret(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-only-secret-not-used-in-prod")


@pytest.fixture
def client():
    return TestClient(graph_app.app)


def _boom(*args, **kwargs):
    raise AssertionError("this must not be called once the guard has rejected the request")


# ── /api/ingest: SSRF guard runs before enqueue ──────────────────────────────

def test_api_ingest_rejects_unsafe_url_before_enqueue(monkeypatch, client):
    monkeypatch.setattr(auth, "get_current_user", lambda request: _fake_user())
    monkeypatch.setattr(graph_app, "enqueue_ingestion", _boom)

    resp = client.post("/api/ingest", params={"url": "http://127.0.0.1:9/"})

    assert resp.status_code == 400


def test_api_ingest_allows_safe_url_through_to_enqueue(monkeypatch, client):
    monkeypatch.setattr(auth, "get_current_user", lambda request: _fake_user())
    monkeypatch.setattr(net_security, "assert_safe_url", lambda url: "example.com")
    monkeypatch.setattr(auth, "ingestion_quota_reached", lambda user_id: False)
    monkeypatch.setattr(graph_app, "enqueue_ingestion", lambda url, user_id: ({"id": 7}, True))

    resp = client.post("/api/ingest", params={"url": "https://example.com/"})

    assert resp.status_code == 202
    assert resp.json() == {"id": 7}


# ── /api/ingest: quota guard, owner exemption, fail-closed ──────────────────

def test_api_ingest_blocks_non_owner_at_quota(monkeypatch, client):
    monkeypatch.setattr(auth, "get_current_user", lambda request: _fake_user(is_owner=False))
    monkeypatch.setattr(net_security, "assert_safe_url", lambda url: "example.com")
    monkeypatch.setattr(auth, "ingestion_quota_reached", lambda user_id: True)
    monkeypatch.setattr(graph_app, "enqueue_ingestion", _boom)

    resp = client.post("/api/ingest", params={"url": "https://example.com/"})

    assert resp.status_code == 429


def test_api_ingest_owner_is_exempt_from_quota_check(monkeypatch, client):
    monkeypatch.setattr(auth, "get_current_user", lambda request: _fake_user(is_owner=True))
    monkeypatch.setattr(net_security, "assert_safe_url", lambda url: "example.com")
    checked = []
    monkeypatch.setattr(auth, "ingestion_quota_reached", lambda user_id: checked.append(user_id) or True)
    monkeypatch.setattr(graph_app, "enqueue_ingestion", lambda url, user_id: ({"id": 1}, True))

    resp = client.post("/api/ingest", params={"url": "https://example.com/"})

    assert resp.status_code == 202
    assert checked == []  # the owner's quota is never even evaluated


def test_api_ingest_fails_closed_for_non_owner_when_quota_check_errors(monkeypatch, client):
    monkeypatch.setattr(auth, "get_current_user", lambda request: _fake_user(is_owner=False))
    monkeypatch.setattr(net_security, "assert_safe_url", lambda url: "example.com")

    def _raise(user_id):
        raise auth.QuotaCheckError("Supabase unreachable")

    monkeypatch.setattr(auth, "ingestion_quota_reached", _raise)
    monkeypatch.setattr(graph_app, "enqueue_ingestion", _boom)

    resp = client.post("/api/ingest", params={"url": "https://example.com/"})

    assert resp.status_code == 503


# ── /api/ingestion-queue/{id}/retry: same SSRF + quota guards as /api/ingest ─

def test_api_retry_ingestion_rejects_unsafe_url(monkeypatch, client):
    monkeypatch.setattr(auth, "get_current_user", lambda request: _fake_user())
    monkeypatch.setattr(
        graph_app, "get_ingestion",
        lambda row_id, requested_by_user_id=None: {"id": row_id, "url": "http://169.254.169.254/", "status": "error", "requested_by_user_id": 1},
    )
    monkeypatch.setattr(graph_app, "retry_ingestion", _boom)

    resp = client.post("/api/ingestion-queue/42/retry")

    assert resp.status_code == 400


def test_api_retry_ingestion_blocks_non_owner_caller_at_quota(monkeypatch, client):
    # A non-owner retrying their OWN row (test_ingestion_queue_scoping.py
    # separately covers the owner-retries-someone-else's-row case, where the
    # quota check doesn't even run since the owner is exempt).
    monkeypatch.setattr(auth, "get_current_user", lambda request: _fake_user(is_owner=False, user_id=2))
    monkeypatch.setattr(
        graph_app, "get_ingestion",
        lambda row_id, requested_by_user_id=None: {"id": row_id, "url": "https://example.com/", "status": "error", "requested_by_user_id": 2},
    )
    monkeypatch.setattr(net_security, "assert_safe_url", lambda url: "example.com")
    checked_user_ids = []
    monkeypatch.setattr(
        auth, "ingestion_quota_reached",
        lambda user_id: checked_user_ids.append(user_id) or True,
    )
    monkeypatch.setattr(graph_app, "retry_ingestion", _boom)

    resp = client.post("/api/ingestion-queue/42/retry")

    assert resp.status_code == 429
    assert checked_user_ids == [2]


def test_api_retry_ingestion_returns_404_for_missing_row(monkeypatch, client):
    monkeypatch.setattr(auth, "get_current_user", lambda request: _fake_user())
    monkeypatch.setattr(graph_app, "get_ingestion", lambda row_id, requested_by_user_id=None: None)
    monkeypatch.setattr(graph_app, "retry_ingestion", _boom)

    resp = client.post("/api/ingestion-queue/999/retry")

    assert resp.status_code == 404


# ── /api/login: IP rate limit resists a spoofed X-Forwarded-For ─────────────

def test_login_rate_limit_ignores_spoofed_x_forwarded_for(monkeypatch, client):
    """A client cannot escape the per-IP login throttle by sending a
    different X-Forwarded-For on every request -- graph_app.py never reads
    that (or any other client-supplied) header to decide which bucket to
    charge, only request.client.host (the actual TCP peer, which the test
    client keeps fixed across these calls regardless of the header).
    """
    monkeypatch.setattr(graph_app, "get_user_by_email", lambda email: None)
    graph_app._LOGIN_IP_RATE_LIMITER._hits.clear()
    graph_app._LOGIN_EMAIL_IP_FAILURE_LIMITER._hits.clear()

    max_calls = graph_app._LOGIN_IP_RATE_LIMITER.max_calls
    statuses = []
    for i in range(max_calls + 5):
        resp = client.post(
            "/api/login",
            json={"email": f"user{i}@example.com", "password": "wrong"},
            headers={"X-Forwarded-For": f"1.2.3.{i}"},
        )
        statuses.append(resp.status_code)

    assert statuses[:max_calls] == [401] * max_calls
    assert statuses[max_calls:] == [429] * 5


def test_login_email_ip_failure_limiter_does_not_charge_successful_logins(monkeypatch):
    """Only failed attempts count against the per-(email, ip) limiter -- a
    legitimate user logging in repeatedly (e.g. multiple tabs) must never
    burn their own budget just by succeeding. A fresh TestClient per
    iteration sidesteps session-cookie reuse (the module-level rate
    limiters are shared regardless of which client instance is used).
    """
    graph_app._LOGIN_IP_RATE_LIMITER._hits.clear()
    graph_app._LOGIN_EMAIL_IP_FAILURE_LIMITER._hits.clear()

    user = {"id": 1, "email": "real@example.com", "password_hash": auth.hash_password("correct horse")}
    monkeypatch.setattr(graph_app, "get_user_by_email", lambda email: user)

    max_email_calls = graph_app._LOGIN_EMAIL_IP_FAILURE_LIMITER.max_calls
    for _ in range(max_email_calls + 3):
        resp = TestClient(graph_app.app).post("/api/login", json={"email": "real@example.com", "password": "correct horse"})
        assert resp.status_code == 200
