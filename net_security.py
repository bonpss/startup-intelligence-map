"""SSRF guard for every outbound URL this app fetches on a user's behalf --
/api/ingest's submitted URL, main.py's scrape (both the httpx fast path and
the Playwright fallback), and the favicon/logo httpx fetches. Isolated in its
own module (no imports from taxonomy.py/competitor.py, and nothing there
imports this) so it can be unit tested standalone and reused by every caller
without those modules needing to know about it.

Threat model: a logged-in (non-owner) user submits an arbitrary URL to
/api/ingest, which this app fetches server-side with a real browser
(Playwright) and via httpx (favicons/logos). Confirmed exploitable
2026-09-21: http://127.0.0.1:9/ reached Page.goto() and was only stopped by
Chromium's own ERR_UNSAFE_PORT list -- this app itself filtered nothing.
Without a guard, that same request path can reach the app's own bound ports,
any service on localhost or the private/CGNAT network the app's host sits
on (loopback, RFC1918, Tailscale's 100.64.0.0/10 CGNAT range), or the cloud
metadata endpoint (169.254.169.254) -- classic SSRF.

Two-step check, deliberately not merged into one function:
  1. validate_outbound_url() -- scheme/credentials/hostname-shape checks,
     no I/O. Rejects disallowed schemes, embedded credentials (user:pass@),
     and IP literals (both normal dotted-quad/IPv6 and disguised decimal/
     octal/hex forms) that resolve to a banned range.
  2. check_host_resolution() -- resolves a hostname that passed step 1 (i.e.
     wasn't itself an IP literal) via DNS and checks every returned address.
     Rejects if ANY resolved address is banned, not just the first -- the
     caller can't control which address the underlying HTTP client/browser
     ultimately connects to, and a rebinding attacker only needs one bad
     answer to win.
assert_safe_url() runs both and is what most callers should use.

This is a point-in-time check: DNS can change between this check and the
moment the actual connection happens (rebinding), and a redirect to a new
host bypasses it entirely unless the caller re-checks each hop. Callers that
follow redirects (main.py's httpx helpers) re-run assert_safe_url() on every
hop; Playwright's per-request context.route interception (main.py's
_scrape_playwright) re-runs it for every navigation/redirect/subresource the
browser itself issues, which is as close to connection-time as this app's
tooling allows.
"""

import ipaddress
import re
import socket
from urllib.parse import urlsplit


class UnsafeURLError(ValueError):
    """Raised when a URL fails the outbound-fetch SSRF guard. Subclasses
    ValueError so existing `except Exception` / `except ValueError` sites
    (e.g. enqueue_ingestion's malformed-URL handling, the favicon/logo
    fetchers' blanket `except Exception: pass`) keep working without being
    rewritten to know about this module.
    """


# RFC 6598 "Shared Address Space" (Carrier-Grade NAT) -- ipaddress.py does
# NOT classify this as is_private, but it's exactly the kind of address a
# self-hosted app can find itself reachable on behind a CGNAT/Tailscale
# network, so it's checked explicitly.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")

_ALLOWED_SCHEMES = ("http", "https")

# A single dot-separated label that WHATWG's URL "IPv4 number parsing"
# algorithm (implemented by every real browser, including the Chromium
# Playwright drives) treats as a numeric octet: 0x/0X-prefixed hex, a
# leading-zero octal run, or a plain decimal run. Python's ipaddress module
# is intentionally strict (rejects leading zeros, non-decimal digits, and
# non-4-part shorthand) and will raise ValueError on all of these -- which
# is correct for ipaddress, but means falling through to "must be a regular
# hostname, go resolve it via DNS" is wrong: a browser given the same string
# parses it as an IP literal and never does a DNS lookup at all. Any host
# whose labels are ALL numeric in one of these forms is therefore rejected
# outright here rather than treated as a hostname.
_NUMERIC_LABEL_RE = re.compile(r"^(0[xX][0-9a-fA-F]+|0[0-7]+|[0-9]+)$")


def _is_ip_literal_disguised_as_hostname(host: str) -> bool:
    """True if every dot-separated label of `host` is a decimal/octal/hex
    number -- i.e. a real browser's URL parser (and, per RFC 6943 s3.1's
    documented ambiguity, some socket libraries) would read this as an IPv4
    address, even though ipaddress.ip_address(host) itself raises. Covers
    whole-host decimal (2130706433), whole-host hex (0x7f000001), whole-host
    octal (017700000001), and mixed dotted forms (0x7f.0.0.1).
    """
    labels = host.split(".")
    if not labels or any(not label for label in labels):
        return False
    return all(_NUMERIC_LABEL_RE.match(label) for label in labels)


def _is_banned_ip(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """The actual range check, applied to every resolved/literal address.
    Recurses into IPv4-mapped (::ffff:a.b.c.d) and the deprecated
    IPv4-compatible (::a.b.c.d) IPv6 forms -- ipaddress's own is_loopback/
    is_private on an IPv6Address do NOT unwrap either of these, so
    ::ffff:127.0.0.1 would otherwise sail through as "not loopback".
    """
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return _is_banned_ip(mapped)
        if ip.packed[:12] == b"\x00" * 12 and int(ip) != 0 and int(ip) != 1:
            # Deprecated IPv4-compatible form (::a.b.c.d, RFC 4291 s2.5.5.1).
            # ::0 and ::1 are the unspecified/loopback IPv6 literals
            # themselves (already caught below by is_unspecified/
            # is_loopback) -- excluded here so they aren't double-unwrapped
            # into IPv4Address("0.0.0.1")-style nonsense.
            return _is_banned_ip(ipaddress.IPv4Address(ip.packed[12:]))

    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    ):
        return True
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NETWORK:
        return True
    return False


def validate_outbound_url(url: str) -> str:
    """Scheme/credentials/hostname-shape checks -- no I/O, safe to call from
    anywhere. Returns the lowercased hostname on success. Raises
    UnsafeURLError otherwise.

    Does NOT resolve a plain hostname (e.g. "example.com") -- a hostname
    that passes this still needs check_host_resolution() before it's safe to
    connect to, since it could resolve to a banned address. Use
    assert_safe_url() to run both in one call.
    """
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"Schema not allowed: {parsed.scheme!r}")

    # Checked against the raw netloc too, not just .username/.password:
    # urlsplit only populates those from a *parseable* userinfo section, and
    # a malformed one (e.g. stray '@' the underlying HTTP client's parser
    # tolerates differently) could otherwise slip past a percent-encoded or
    # multi-'@' netloc without either attribute getting set.
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise UnsafeURLError("Credentials in URL (user:pass@host) are not allowed.")

    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no hostname.")
    host = host.lower()

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if _is_ip_literal_disguised_as_hostname(host):
            raise UnsafeURLError(f"Numeric IP literal disguised as hostname rejected: {host!r}")
        return host  # A real hostname -- caller must still resolve + check it.

    if _is_banned_ip(ip):
        raise UnsafeURLError(f"IP address not allowed: {ip}")
    return host


def check_host_resolution(host: str) -> list[str]:
    """Resolve `host` (all A/AAAA records) and reject if ANY resolved
    address is banned. Synchronous/blocking (plain socket.getaddrinfo) --
    callers on an event loop must run this via asyncio.to_thread, same as
    every other blocking I/O call in this codebase (storage.py's Supabase
    calls, main.py's synchronous httpx.get in the favicon/logo fetchers).

    If `host` is itself an IP literal (validate_outbound_url already
    resolved that case, but callers may invoke this standalone), it's
    checked directly with no DNS lookup.
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if _is_banned_ip(ip):
            raise UnsafeURLError(f"IP address not allowed: {ip}")
        return [host]

    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise UnsafeURLError(f"DNS resolution failed for {host!r}: {e}")

    resolved: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in infos:
        addr = sockaddr[0]
        ip = ipaddress.ip_address(addr)
        if _is_banned_ip(ip):
            raise UnsafeURLError(f"{host!r} resolves to a disallowed address ({ip}).")
        resolved.append(addr)

    if not resolved:
        raise UnsafeURLError(f"No addresses resolved for {host!r}.")
    return resolved


def assert_safe_url(url: str) -> str:
    """validate_outbound_url() + check_host_resolution() -- the full,
    blocking check most callers should use before making one outbound
    request. Returns the hostname. Does NOT protect against a redirect to a
    different host discovered mid-request -- callers that follow redirects
    must call this again on each hop's URL (see main.py's _safe_httpx_get).
    """
    host = validate_outbound_url(url)
    check_host_resolution(host)
    return host
