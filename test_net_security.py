"""Unit tests for net_security.py's SSRF guard, in the style of test_auth.py
(pure logic / monkeypatched I/O, no real network calls except the two
deliberately-real "localhost" DNS lookups noted below).
"""

import socket

import httpx
import pytest

import net_security
import main
from net_security import UnsafeURLError


# ── validate_outbound_url: scheme / credentials ──────────────────────────────

def test_rejects_non_http_scheme():
    with pytest.raises(UnsafeURLError):
        net_security.validate_outbound_url("file:///etc/passwd")


def test_rejects_ftp_scheme():
    with pytest.raises(UnsafeURLError):
        net_security.validate_outbound_url("ftp://example.com/")


def test_rejects_userinfo_credentials_in_url():
    with pytest.raises(UnsafeURLError):
        net_security.validate_outbound_url("http://user:pass@example.com/")


def test_rejects_netloc_at_sign_used_to_smuggle_a_real_target():
    # urlsplit correctly reads "127.0.0.1" as the hostname here (not
    # "evil.com"), but the credentials check must still fire regardless of
    # which side of the '@' the real target is on.
    with pytest.raises(UnsafeURLError):
        net_security.validate_outbound_url("http://evil.com@127.0.0.1/")


def test_rejects_url_with_no_hostname():
    with pytest.raises(UnsafeURLError):
        net_security.validate_outbound_url("http:///path")


def test_allows_ordinary_https_hostname():
    # No DNS resolution happens in validate_outbound_url alone -- safe to
    # assert against an arbitrary real-looking hostname with no network I/O.
    assert net_security.validate_outbound_url("https://example.com/page") == "example.com"


# ── validate_outbound_url: direct IP literals (required test cases) ─────────

def test_rejects_loopback_ip_literal_with_unsafe_port():
    # The exact case from the 2026-09-21 report: http://127.0.0.1:9/ reached
    # Playwright's Page.goto() and was only stopped by Chromium's own
    # ERR_UNSAFE_PORT list -- the app itself filtered nothing.
    with pytest.raises(UnsafeURLError):
        net_security.validate_outbound_url("http://127.0.0.1:9/")


def test_rejects_ipv6_loopback_literal():
    with pytest.raises(UnsafeURLError):
        net_security.validate_outbound_url("http://[::1]/")


def test_rejects_cgnat_range_address():
    # 100.64.0.0/10 -- RFC 6598 Shared Address Space, includes Tailscale's
    # CGNAT range. Not covered by ipaddress.is_private, checked explicitly.
    with pytest.raises(UnsafeURLError):
        net_security.validate_outbound_url("http://100.90.233.108/")


def test_rejects_link_local_metadata_address():
    with pytest.raises(UnsafeURLError):
        net_security.validate_outbound_url("http://169.254.169.254/latest/meta-data/")


def test_rejects_multicast_address():
    with pytest.raises(UnsafeURLError):
        net_security.validate_outbound_url("http://224.0.0.1/")


def test_rejects_unspecified_ipv4_address():
    with pytest.raises(UnsafeURLError):
        net_security.validate_outbound_url("http://0.0.0.0/")


def test_rejects_unspecified_ipv6_address():
    with pytest.raises(UnsafeURLError):
        net_security.validate_outbound_url("http://[::]/")


def test_rejects_rfc1918_private_address():
    with pytest.raises(UnsafeURLError):
        net_security.validate_outbound_url("http://10.1.2.3/")


def test_rejects_ipv4_mapped_ipv6_loopback():
    # ipaddress.IPv6Address("::ffff:127.0.0.1").is_loopback is False --
    # the mapped IPv4 address must be unwrapped and checked separately.
    with pytest.raises(UnsafeURLError):
        net_security.validate_outbound_url("http://[::ffff:127.0.0.1]/")


def test_rejects_deprecated_ipv4_compatible_ipv6_loopback():
    with pytest.raises(UnsafeURLError):
        net_security.validate_outbound_url("http://[::127.0.0.1]/")


def test_allows_ordinary_public_ipv4_literal():
    assert net_security.validate_outbound_url("http://93.184.216.34/") == "93.184.216.34"


# ── validate_outbound_url: disguised numeric IP literals ────────────────────

def test_rejects_hex_ip_literal():
    with pytest.raises(UnsafeURLError):
        net_security.validate_outbound_url("http://0x7f000001/")


def test_rejects_decimal_ip_literal():
    with pytest.raises(UnsafeURLError):
        net_security.validate_outbound_url("http://2130706433/")


def test_rejects_octal_ip_literal():
    with pytest.raises(UnsafeURLError):
        net_security.validate_outbound_url("http://017700000001/")


def test_rejects_mixed_dotted_hex_octal_ip_literal():
    with pytest.raises(UnsafeURLError):
        net_security.validate_outbound_url("http://0x7f.0.0.1/")


def test_does_not_flag_ordinary_hostname_with_hex_looking_labels():
    # "beef" and "cafe" are valid hex digits character-for-character, but
    # neither is 0x-prefixed/octal/pure-decimal -- must not be treated as a
    # disguised IP literal.
    assert net_security.validate_outbound_url("https://beef.example/") == "beef.example"


# ── check_host_resolution: hostname DNS resolution ───────────────────────────

def test_check_host_resolution_rejects_hostname_resolving_to_loopback(monkeypatch):
    def fake_getaddrinfo(host, port, family=0, type=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(net_security.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeURLError):
        net_security.check_host_resolution("evil-rebind.example.com")


def test_check_host_resolution_rejects_if_any_resolved_address_is_banned(monkeypatch):
    # DNS rebinding / multi-answer defense: reject if ANY answer is banned,
    # not just the first -- the caller can't control which one the actual
    # HTTP client/browser will connect to.
    def fake_getaddrinfo(host, port, family=0, type=0):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
        ]

    monkeypatch.setattr(net_security.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeURLError):
        net_security.check_host_resolution("evil-rebind.example.com")


def test_check_host_resolution_allows_hostname_resolving_to_public_addresses(monkeypatch):
    def fake_getaddrinfo(host, port, family=0, type=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(net_security.socket, "getaddrinfo", fake_getaddrinfo)
    assert net_security.check_host_resolution("example.com") == ["93.184.216.34"]


def test_check_host_resolution_wraps_dns_failure():
    with pytest.raises(UnsafeURLError):
        net_security.check_host_resolution("this-domain-should-not-resolve.invalid")


# ── assert_safe_url: real (non-mocked) localhost resolution ─────────────────
# "localhost" and its hosts-file/DNS resolution to 127.0.0.1/::1 is universal
# enough to assert against directly, without mocking -- no network access
# required, matching test_middleware_order_session_available_before_auth_gate
# (test_auth.py) exercising real request dispatch rather than mocking it.

def test_assert_safe_url_rejects_localhost():
    with pytest.raises(UnsafeURLError):
        net_security.assert_safe_url("http://localhost:9/")


# ── main.py integration: redirect re-validation ──────────────────────────────
# (test_main_asset_security.py covers _safe_httpx_download's other
# responsibilities -- size cap, Content-Type/magic-byte classification.)

def test_safe_httpx_download_blocks_redirect_to_private_address(monkeypatch):
    """main._safe_httpx_download must re-validate every redirect hop, not
    just the URL it was given -- a scraped site's own (open) redirect could
    otherwise steer a favicon/logo download at an internal address after the
    initial check on the submitted URL already passed.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "http://93.184.216.34/":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/secret"})
        return httpx.Response(200, content=b"should never be reached")

    transport = httpx.MockTransport(handler)
    real_client_cls = httpx.Client
    monkeypatch.setattr(main.httpx, "Client", lambda *a, **k: real_client_cls(transport=transport))

    with pytest.raises(UnsafeURLError):
        main._safe_httpx_download("http://93.184.216.34/")


def test_safe_httpx_download_follows_safe_redirect(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "http://93.184.216.34/":
            return httpx.Response(302, headers={"Location": "http://93.184.216.35/final"})
        return httpx.Response(200, content=b"ok", headers={"content-type": "image/png"})

    transport = httpx.MockTransport(handler)
    real_client_cls = httpx.Client
    monkeypatch.setattr(main.httpx, "Client", lambda *a, **k: real_client_cls(transport=transport))

    content, content_type, final_url = main._safe_httpx_download("http://93.184.216.34/")
    assert content == b"ok"
    assert content_type == "image/png"
    assert final_url == "http://93.184.216.35/final"
