"""Unit tests for main.py's asset-download hardening (BUG 2, option a):
Content-Type + magic-byte classification, the SVG content scan, the
streaming size cap, and the filename-slug validator. No real network calls
-- httpx.MockTransport stands in for the remote server, same convention as
test_net_security.py's redirect tests.
"""

import httpx
import pytest

import main
from net_security import UnsafeURLError


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
_JPEG_MAGIC = b"\xff\xd8\xff" + b"\x00" * 100
_WEBP_MAGIC = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 100
_ICO_MAGIC = b"\x00\x00\x01\x00" + b"\x00" * 100
_SAFE_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle cx="5" cy="5" r="4" fill="red"/></svg>'


# ── _classify_downloaded_image: Content-Type + magic bytes must both agree ──

def test_classifies_real_png():
    assert main._classify_downloaded_image(_PNG_MAGIC, "image/png") == "png"


def test_classifies_real_png_with_charset_param():
    assert main._classify_downloaded_image(_PNG_MAGIC, "image/png; charset=binary") == "png"


def test_classifies_real_jpeg():
    assert main._classify_downloaded_image(_JPEG_MAGIC, "image/jpeg") == "jpg"


def test_classifies_real_webp():
    assert main._classify_downloaded_image(_WEBP_MAGIC, "image/webp") == "webp"


def test_classifies_real_ico():
    assert main._classify_downloaded_image(_ICO_MAGIC, "image/x-icon") == "ico"


def test_classifies_safe_svg():
    assert main._classify_downloaded_image(_SAFE_SVG, "image/svg+xml") == "svg"


def test_rejects_fake_png_with_wrong_magic_bytes():
    """Content-Type claims PNG, but the bytes are plain text -- classic
    'inconsistent Content-Type' / spoofed-extension attack. Must be
    rejected outright, not saved as an untrusted-but-plausible png.
    """
    assert main._classify_downloaded_image(b"just some text, not a real image", "image/png") is None


def test_rejects_html_disguised_as_image_content_type():
    html = b"<html><body><script>alert(1)</script></body></html>"
    assert main._classify_downloaded_image(html, "image/png") is None
    assert main._classify_downloaded_image(html, "text/html") is None  # not an allowed image type at all


def test_rejects_real_png_bytes_with_mismatched_content_type():
    # Bytes are genuinely PNG, but the server claims something else entirely
    # -- still rejected, since the Content-Type is what decides the
    # extension in the first place; a real image under the wrong claimed
    # type is exactly the "inconsistent Content-Type" case being guarded
    # against, not a false positive to special-case around.
    assert main._classify_downloaded_image(_PNG_MAGIC, "image/webp") is None


def test_rejects_unrecognized_content_type():
    assert main._classify_downloaded_image(_PNG_MAGIC, "application/octet-stream") is None


def test_rejects_missing_content_type():
    assert main._classify_downloaded_image(_PNG_MAGIC, None) is None


# ── _is_safe_svg / SVG trap patterns ─────────────────────────────────────────

@pytest.mark.parametrize("payload", [
    b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><circle r="1"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="1" onmouseover="alert(1)"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><script>alert(1)</script></foreignObject></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><a xlink:href="javascript:alert(1)"><circle r="1"/></a></svg>',
    b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><svg>&xxe;</svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><image href="http://evil.example/track.png"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><image xlink:href="//evil.example/track.png"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import url(http://evil.example/x.css);</style></svg>',
])
def test_rejects_trapped_svgs(payload):
    assert main._is_safe_svg(payload) is False
    assert main._classify_downloaded_image(payload, "image/svg+xml") is None


def test_accepts_self_contained_svg_with_local_fragment_href():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><use href="#icon"/><defs><g id="icon"><circle r="1"/></g></defs></svg>'
    assert main._is_safe_svg(svg) is True


def test_rejects_non_svg_content_claiming_to_be_svg():
    assert main._is_safe_svg(b"not xml or svg at all") is False


# ── _safe_httpx_download: streaming size cap ─────────────────────────────────

def test_download_rejects_body_exceeding_size_cap(monkeypatch):
    big_body = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big_body, headers={"content-type": "image/png"})

    transport = httpx.MockTransport(handler)
    real_client_cls = httpx.Client
    monkeypatch.setattr(main.httpx, "Client", lambda *a, **k: real_client_cls(transport=transport))

    with pytest.raises(ValueError):
        main._safe_httpx_download("http://93.184.216.34/big.png", max_bytes=100)


def test_download_allows_body_within_size_cap(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_PNG_MAGIC, headers={"content-type": "image/png"})

    transport = httpx.MockTransport(handler)
    real_client_cls = httpx.Client
    monkeypatch.setattr(main.httpx, "Client", lambda *a, **k: real_client_cls(transport=transport))

    content, content_type, _url = main._safe_httpx_download("http://93.184.216.34/ok.png", max_bytes=10_000)
    assert content == _PNG_MAGIC
    assert content_type == "image/png"


# ── _validate_asset_slug: malicious filename ─────────────────────────────────

@pytest.mark.parametrize("slug", [
    "../../etc/passwd",
    "..",
    "a/b",
    "a\\b",
    "",
    "a/../../b",
    "logo;rm -rf",
    "logo\x00.png",
])
def test_rejects_malicious_slug(slug):
    with pytest.raises(ValueError):
        main._validate_asset_slug(slug)


@pytest.mark.parametrize("slug", ["acme.com", "sub.acme-startup.io", "a1b2c3"])
def test_accepts_ordinary_domain_slug(slug):
    main._validate_asset_slug(slug)  # must not raise
