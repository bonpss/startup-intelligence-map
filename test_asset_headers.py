"""Verifies /assets responses carry the headers that stop a served file from
ever executing in the app's own origin (BUG 2, option a, requirement 1).
Writes a real, temporary .svg file under the actual assets/logos/ directory
StaticFiles serves from (no way to test this against a mocked filesystem --
StaticFiles is bound to a real directory at mount time), and removes it
again in teardown regardless of test outcome.
"""

import os

os.environ.setdefault("SESSION_SECRET_KEY", "test-only-secret-not-used-in-prod")

import pytest
from fastapi.testclient import TestClient

import graph_app


_TEST_ASSET_PATH = "assets/logos/__test_asset_headers__.svg"


@pytest.fixture
def real_svg_asset():
    os.makedirs("assets/logos", exist_ok=True)
    with open(_TEST_ASSET_PATH, "wb") as f:
        f.write(b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="1"/></svg>')
    try:
        yield "/" + _TEST_ASSET_PATH
    finally:
        if os.path.exists(_TEST_ASSET_PATH):
            os.remove(_TEST_ASSET_PATH)


def test_real_svg_response_has_nosniff_and_sandboxed_csp(real_svg_asset):
    client = TestClient(graph_app.app)

    resp = client.get(real_svg_asset)

    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-security-policy"] == "sandbox; default-src 'none'"


def test_missing_asset_404_still_has_the_headers():
    # These headers matter most on a served file, but the middleware applies
    # to every response under /assets/ regardless of status -- a stale link
    # to a since-deleted file shouldn't fall through the guard either.
    client = TestClient(graph_app.app)

    resp = client.get("/assets/logos/__definitely_does_not_exist__.svg")

    assert resp.status_code == 404
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-security-policy"] == "sandbox; default-src 'none'"


def test_non_asset_response_does_not_get_the_asset_headers():
    # Sanity check that the middleware is scoped to /assets/ -- these
    # headers have no business on, say, a JSON API response.
    client = TestClient(graph_app.app)

    resp = client.get("/api/graph/all", follow_redirects=False)

    assert "content-security-policy" not in {k.lower() for k in resp.headers}


def test_headers_still_present_if_auth_gate_ever_short_circuits_an_asset_path(monkeypatch):
    """/assets/ is allowlisted in auth_gate today, so it always falls
    through to StaticFiles -- but asset_security_headers must still apply
    even if that ever stopped being true (a future bug/change to the
    allowlist). Simulated here by making _is_allowlisted lie about an
    /assets/ path so auth_gate takes its normal "no session -> redirect"
    branch instead, without ever reaching call_next() into StaticFiles.
    This only passes if asset_security_headers is registered as the
    OUTERMOST middleware (see its docstring) -- an inner middleware never
    runs when an outer one returns without calling call_next().
    """
    real_is_allowlisted = graph_app._is_allowlisted
    monkeypatch.setattr(
        graph_app, "_is_allowlisted",
        lambda path: False if path.startswith("/assets/") else real_is_allowlisted(path),
    )
    client = TestClient(graph_app.app)

    resp = client.get("/assets/logos/whatever.png", follow_redirects=False)

    assert resp.status_code == 303  # auth_gate's redirect-to-/login branch, never reached StaticFiles
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-security-policy"] == "sandbox; default-src 'none'"
