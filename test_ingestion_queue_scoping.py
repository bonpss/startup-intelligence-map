"""Endpoint-level tests for per-user ingestion_queue visibility: user A vs.
user B vs. the owner, across all five affected endpoints, plus the
enqueue-collision case (a duplicate-domain submission must never hand a
non-owner someone else's row id). Same no-lifespan TestClient convention as
test_graph_app_security.py -- storage calls are monkeypatched, no real DB.
"""

import os

os.environ.setdefault("SESSION_SECRET_KEY", "test-only-secret-not-used-in-prod")

import pytest
from fastapi.testclient import TestClient

import auth
import graph_app
import net_security


USER_A = {"id": 1, "email": "a@example.com", "is_owner": False}
USER_B = {"id": 2, "email": "b@example.com", "is_owner": False}
OWNER = {"id": 99, "email": "owner@example.com", "is_owner": True}

ROW_A = {"id": 10, "url": "https://a-startup.com", "domain": "a-startup.com", "status": "error",
         "requested_by_user_id": USER_A["id"], "created_at": "2026-09-01T00:00:00+00:00"}
ROW_B = {"id": 11, "url": "https://b-startup.com", "domain": "b-startup.com", "status": "error",
         "requested_by_user_id": USER_B["id"], "created_at": "2026-09-01T00:01:00+00:00"}
ROW_ORPHAN = {"id": 12, "url": "https://orphan-startup.com", "domain": "orphan-startup.com", "status": "done",
              "requested_by_user_id": None, "created_at": "2026-09-01T00:02:00+00:00", "result": {"name": "Orphan"}, "seen": False}


@pytest.fixture
def client():
    return TestClient(graph_app.app)


def _as(monkeypatch, user):
    monkeypatch.setattr(auth, "get_current_user", lambda request: user)


def _boom(*a, **k):
    raise AssertionError("this must not be called once the ownership guard has rejected the request")


# ── GET /api/ingestion-queue ──────────────────────────────────────────────────

def test_user_a_only_sees_their_own_rows(monkeypatch, client):
    _as(monkeypatch, USER_A)
    captured = {}

    def fake_list_ingestions(status=None, requested_by_user_id=None):
        captured["requested_by_user_id"] = requested_by_user_id
        return [ROW_A]

    monkeypatch.setattr(graph_app, "list_ingestions", fake_list_ingestions)

    resp = client.get("/api/ingestion-queue")

    assert resp.status_code == 200
    assert captured["requested_by_user_id"] == USER_A["id"]
    assert [r["id"] for r in resp.json()] == [ROW_A["id"]]


def test_user_a_scope_param_is_ignored_and_cannot_widen_their_view(monkeypatch, client):
    """A non-owner passing scope=all must not get an unfiltered query --
    the query param can only ever widen what the OWNER sees."""
    _as(monkeypatch, USER_A)
    captured = {}

    def fake_list_ingestions(status=None, requested_by_user_id=None):
        captured["requested_by_user_id"] = requested_by_user_id
        return []

    monkeypatch.setattr(graph_app, "list_ingestions", fake_list_ingestions)

    client.get("/api/ingestion-queue", params={"scope": "all"})

    assert captured["requested_by_user_id"] == USER_A["id"]


def test_owner_sees_everything_including_orphaned_rows_with_requester_email(monkeypatch, client):
    _as(monkeypatch, OWNER)
    captured = {}

    def fake_list_ingestions(status=None, requested_by_user_id=None):
        captured["requested_by_user_id"] = requested_by_user_id
        return [dict(ROW_A), dict(ROW_B), dict(ROW_ORPHAN)]

    monkeypatch.setattr(graph_app, "list_ingestions", fake_list_ingestions)
    monkeypatch.setattr(graph_app, "get_users_by_ids", lambda ids: {USER_A["id"]: USER_A["email"], USER_B["id"]: USER_B["email"]})

    resp = client.get("/api/ingestion-queue")

    assert resp.status_code == 200
    assert captured["requested_by_user_id"] is None  # unfiltered
    rows_by_id = {r["id"]: r for r in resp.json()}
    assert rows_by_id[ROW_A["id"]]["requester_email"] == USER_A["email"]
    assert rows_by_id[ROW_B["id"]]["requester_email"] == USER_B["email"]
    assert rows_by_id[ROW_ORPHAN["id"]]["requester_email"] is None


def test_owner_scope_mine_narrows_to_their_own_rows(monkeypatch, client):
    _as(monkeypatch, OWNER)
    captured = {}

    def fake_list_ingestions(status=None, requested_by_user_id=None):
        captured["requested_by_user_id"] = requested_by_user_id
        return []

    monkeypatch.setattr(graph_app, "list_ingestions", fake_list_ingestions)
    monkeypatch.setattr(graph_app, "get_users_by_ids", lambda ids: {})

    client.get("/api/ingestion-queue", params={"scope": "mine"})

    assert captured["requested_by_user_id"] == OWNER["id"]


def test_non_owner_response_has_no_requester_email_field(monkeypatch, client):
    _as(monkeypatch, USER_A)
    monkeypatch.setattr(graph_app, "list_ingestions", lambda status=None, requested_by_user_id=None: [dict(ROW_A)])

    resp = client.get("/api/ingestion-queue")

    assert "requester_email" not in resp.json()[0]


# ── GET /api/ingestion-queue/summary ─────────────────────────────────────────

def test_summary_scoped_to_user_a(monkeypatch, client):
    _as(monkeypatch, USER_A)
    captured = {}

    def fake_summary(requested_by_user_id=None):
        captured["requested_by_user_id"] = requested_by_user_id
        return {"error_count": 0, "unseen_done_count": 0}

    monkeypatch.setattr(graph_app, "get_ingestion_summary", fake_summary)

    client.get("/api/ingestion-queue/summary")

    assert captured["requested_by_user_id"] == USER_A["id"]


def test_summary_unscoped_for_owner_by_default(monkeypatch, client):
    _as(monkeypatch, OWNER)
    captured = {}

    def fake_summary(requested_by_user_id=None):
        captured["requested_by_user_id"] = requested_by_user_id
        return {"error_count": 0, "unseen_done_count": 0}

    monkeypatch.setattr(graph_app, "get_ingestion_summary", fake_summary)

    client.get("/api/ingestion-queue/summary")

    assert captured["requested_by_user_id"] is None


# ── POST /api/ingestion-queue/mark-seen ──────────────────────────────────────

def test_mark_seen_scoped_to_caller_and_never_includes_orphans_for_non_owner(monkeypatch, client):
    _as(monkeypatch, USER_A)
    captured = {}

    def fake_mark_seen(requested_by_user_id=None, include_orphaned=False):
        captured["requested_by_user_id"] = requested_by_user_id
        captured["include_orphaned"] = include_orphaned
        return 1

    monkeypatch.setattr(graph_app, "mark_done_rows_seen", fake_mark_seen)

    client.post("/api/ingestion-queue/mark-seen")

    assert captured["requested_by_user_id"] == USER_A["id"]
    assert captured["include_orphaned"] is False


def test_mark_seen_includes_orphans_for_owner(monkeypatch, client):
    _as(monkeypatch, OWNER)
    captured = {}

    def fake_mark_seen(requested_by_user_id=None, include_orphaned=False):
        captured["requested_by_user_id"] = requested_by_user_id
        captured["include_orphaned"] = include_orphaned
        return 1

    monkeypatch.setattr(graph_app, "mark_done_rows_seen", fake_mark_seen)

    client.post("/api/ingestion-queue/mark-seen")

    assert captured["requested_by_user_id"] == OWNER["id"]
    assert captured["include_orphaned"] is True


# ── POST /api/ingestion-queue/{id}/retry ─────────────────────────────────────

def test_user_a_cannot_retry_user_b_row_gets_404_not_403(monkeypatch, client):
    _as(monkeypatch, USER_A)
    captured = {}

    def fake_get_ingestion(row_id, requested_by_user_id=None):
        captured["requested_by_user_id"] = requested_by_user_id
        return None  # ownership filter excludes B's row

    monkeypatch.setattr(graph_app, "get_ingestion", fake_get_ingestion)
    monkeypatch.setattr(graph_app, "retry_ingestion", _boom)

    resp = client.post(f"/api/ingestion-queue/{ROW_B['id']}/retry")

    assert resp.status_code == 404
    assert captured["requested_by_user_id"] == USER_A["id"]


def test_user_a_can_retry_their_own_row(monkeypatch, client):
    _as(monkeypatch, USER_A)
    monkeypatch.setattr(graph_app, "get_ingestion", lambda row_id, requested_by_user_id=None: dict(ROW_A))
    monkeypatch.setattr(net_security, "assert_safe_url", lambda url: "a-startup.com")
    monkeypatch.setattr(auth, "ingestion_quota_reached", lambda user_id: False)

    retry_calls = []

    def fake_retry(row_id, requested_by_user_id=None):
        retry_calls.append((row_id, requested_by_user_id))
        return dict(ROW_A, status="queued")

    monkeypatch.setattr(graph_app, "retry_ingestion", fake_retry)

    resp = client.post(f"/api/ingestion-queue/{ROW_A['id']}/retry")

    assert resp.status_code == 202
    assert retry_calls == [(ROW_A["id"], USER_A["id"])]


def test_owner_can_retry_any_users_row(monkeypatch, client):
    _as(monkeypatch, OWNER)
    captured = {}

    def fake_get_ingestion(row_id, requested_by_user_id=None):
        captured["requested_by_user_id"] = requested_by_user_id
        return dict(ROW_B)

    monkeypatch.setattr(graph_app, "get_ingestion", fake_get_ingestion)
    monkeypatch.setattr(net_security, "assert_safe_url", lambda url: "b-startup.com")
    monkeypatch.setattr(auth, "ingestion_quota_reached", lambda user_id: False)
    monkeypatch.setattr(graph_app, "retry_ingestion", lambda row_id, requested_by_user_id=None: dict(ROW_B, status="queued"))

    resp = client.post(f"/api/ingestion-queue/{ROW_B['id']}/retry")

    assert resp.status_code == 202
    assert captured["requested_by_user_id"] is None


def test_retry_charges_quota_against_the_caller_not_the_original_submitter(monkeypatch, client):
    """The row was submitted by user B; the OWNER retrying it must have the
    quota check (if it applied) run against the owner, not user B -- but the
    owner is exempt entirely, so ingestion_quota_reached must not even be
    called. This documents/locks that behavior for the retry path.
    """
    _as(monkeypatch, OWNER)
    monkeypatch.setattr(graph_app, "get_ingestion", lambda row_id, requested_by_user_id=None: dict(ROW_B))
    monkeypatch.setattr(net_security, "assert_safe_url", lambda url: "b-startup.com")
    monkeypatch.setattr(auth, "ingestion_quota_reached", _boom)
    monkeypatch.setattr(graph_app, "retry_ingestion", lambda row_id, requested_by_user_id=None: dict(ROW_B, status="queued"))

    resp = client.post(f"/api/ingestion-queue/{ROW_B['id']}/retry")

    assert resp.status_code == 202


# ── DELETE /api/ingestion-queue/{id} ─────────────────────────────────────────

def test_user_a_cannot_delete_user_b_row_gets_404(monkeypatch, client):
    _as(monkeypatch, USER_A)
    captured = {}

    def fake_delete(row_id, requested_by_user_id=None):
        captured["requested_by_user_id"] = requested_by_user_id
        raise ValueError("not found or not owned")

    monkeypatch.setattr(graph_app, "delete_ingestion", fake_delete)

    resp = client.delete(f"/api/ingestion-queue/{ROW_B['id']}")

    assert resp.status_code == 404
    assert captured["requested_by_user_id"] == USER_A["id"]


def test_user_a_can_delete_their_own_row(monkeypatch, client):
    _as(monkeypatch, USER_A)
    captured = {}

    def fake_delete(row_id, requested_by_user_id=None):
        captured["requested_by_user_id"] = requested_by_user_id
        return dict(ROW_A)

    monkeypatch.setattr(graph_app, "delete_ingestion", fake_delete)

    resp = client.delete(f"/api/ingestion-queue/{ROW_A['id']}")

    assert resp.status_code == 204
    assert captured["requested_by_user_id"] == USER_A["id"]


def test_owner_can_delete_any_users_row(monkeypatch, client):
    _as(monkeypatch, OWNER)
    captured = {}

    def fake_delete(row_id, requested_by_user_id=None):
        captured["requested_by_user_id"] = requested_by_user_id
        return dict(ROW_B)

    monkeypatch.setattr(graph_app, "delete_ingestion", fake_delete)

    resp = client.delete(f"/api/ingestion-queue/{ROW_B['id']}")

    assert resp.status_code == 204
    assert captured["requested_by_user_id"] is None


# ── POST /api/ingest: duplicate-domain collision must not leak another user's row ──

def test_duplicate_domain_from_another_user_hides_the_row_and_id(monkeypatch, client):
    _as(monkeypatch, USER_A)
    monkeypatch.setattr(net_security, "assert_safe_url", lambda url: "b-startup.com")
    monkeypatch.setattr(auth, "ingestion_quota_reached", lambda user_id: False)
    # enqueue_ingestion reuses B's already-active row for this domain.
    monkeypatch.setattr(graph_app, "enqueue_ingestion", lambda url, user_id: (dict(ROW_B), False))

    resp = client.post("/api/ingest", params={"url": "https://b-startup.com"})

    assert resp.status_code == 409
    assert "id" not in resp.json()
    assert str(ROW_B["id"]) not in resp.text


def test_duplicate_domain_from_the_same_user_returns_their_own_id(monkeypatch, client):
    _as(monkeypatch, USER_A)
    monkeypatch.setattr(net_security, "assert_safe_url", lambda url: "a-startup.com")
    monkeypatch.setattr(auth, "ingestion_quota_reached", lambda user_id: False)
    monkeypatch.setattr(graph_app, "enqueue_ingestion", lambda url, user_id: (dict(ROW_A), False))

    resp = client.post("/api/ingest", params={"url": "https://a-startup.com"})

    assert resp.status_code == 202
    assert resp.json() == {"id": ROW_A["id"]}


def test_duplicate_domain_from_another_user_is_transparent_to_the_owner(monkeypatch, client):
    _as(monkeypatch, OWNER)
    monkeypatch.setattr(net_security, "assert_safe_url", lambda url: "b-startup.com")
    monkeypatch.setattr(graph_app, "enqueue_ingestion", lambda url, user_id: (dict(ROW_B), False))

    resp = client.post("/api/ingest", params={"url": "https://b-startup.com"})

    assert resp.status_code == 202
    assert resp.json() == {"id": ROW_B["id"]}


def test_new_row_for_this_user_is_returned_normally(monkeypatch, client):
    _as(monkeypatch, USER_A)
    monkeypatch.setattr(net_security, "assert_safe_url", lambda url: "new-startup.com")
    monkeypatch.setattr(auth, "ingestion_quota_reached", lambda user_id: False)
    new_row = {"id": 20, "url": "https://new-startup.com", "requested_by_user_id": USER_A["id"]}
    monkeypatch.setattr(graph_app, "enqueue_ingestion", lambda url, user_id: (new_row, True))

    resp = client.post("/api/ingest", params={"url": "https://new-startup.com"})

    assert resp.status_code == 202
    assert resp.json() == {"id": 20}
