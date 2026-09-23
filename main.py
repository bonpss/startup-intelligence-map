import sys
import os
import re
import json
import asyncio
import threading
from urllib.parse import urlparse, urljoin
from html import unescape as _html_unescape
from playwright.async_api import async_playwright
import boto3
from botocore.exceptions import ClientError
import html2text
import httpx
import trafilatura
from embeddings import embed_one
from extractor import extract
from storage import save_startup, normalize_domain, INTERACTIVE_REQUEST, CURRENT_API_CALL_CONTEXT, _client as _db_client
from competitor import compare_jev, explore_transitive_jev, save_competitors_jev
import net_security
from net_security import UnsafeURLError


LOGO_EXTENSIONS = ("svg", "png", "jpg", "jpeg", "webp", "ico")

# Favicons/logos live in Cloudflare R2 by default (assets/logos/ is
# gitignored, so a locally-saved file never reaches the VPS -- this is what
# broke flaticon_url/logo_url for anything ingested outside the VPS). Set
# LOGO_STORAGE=local to write to local disk instead, for offline debugging
# only; production/VPS ingestion must never set this.
LOGO_STORAGE = os.environ.get("LOGO_STORAGE", "r2")

# Path-traversal defense-in-depth for the one place a file path is built out
# of app-controlled-but-externally-derived data (slugify(normalize_domain(url))):
# normalize_domain() shouldn't ever produce a slash/dot-dot, but this is
# checked explicitly at the point the filename is actually constructed
# rather than trusted implicitly from an upstream guarantee two functions
# away. Only lowercase alnum, dot, underscore, hyphen -- deliberately
# stricter than what a domain could theoretically contain, since this only
# ever needs to match slugify()'s own output.
_SAFE_ASSET_SLUG_RE = re.compile(r"^[a-z0-9._-]+$")


def _validate_asset_slug(slug: str) -> None:
    if not slug or ".." in slug or not _SAFE_ASSET_SLUG_RE.match(slug):
        raise ValueError(f"Unsafe asset filename slug: {slug!r}")

# One asyncio.Lock per normalized domain, serializing concurrent ingest() calls for
# the same company so they can't interleave their check-then-write DB sequences
# (storage.save_startup, storage.save_relationships/competitor.compare). Does NOT
# serialize two different companies racing on a shared competitor relationship --
# scoped to same-domain only, a DB-level constraint would be needed for the rest.
# setdefault() on a plain dict with no `await` in between is atomic on the
# single-threaded event loop, so no extra guard lock is needed here (unlike
# competitor.py's _mistral_lock, which guards against races from worker *threads*).
# Grows by one entry per distinct domain ever ingested, never evicted -- accepted
# tradeoff for a solo tool's data volumes.
_domain_locks: dict[str, asyncio.Lock] = {}


def slugify(name: str) -> str:
    return name.lower().replace(" ", "_").replace("/", "_")


# Shared full desktop-Chrome UA -- used by both _fetch_light (httpx) and
# _scrape_playwright (browser context) so a future version bump only needs one edit.
_FULL_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _attr(tag: str, name: str) -> str | None:
    """Extract an attribute's value from a single HTML tag string, e.g.
    _attr('<a href="x">', 'href') -> 'x'. Shared by every regex-based HTML
    scraper below so the attribute-value pattern lives in one place.

    The negative lookbehind requires a word/hyphen/colon boundary before the
    attribute name -- without it, _attr(tag, "src") would match inside
    "data-src=" too (a real risk now that _fetch_light scans raw, un-rendered
    HTML, where lazy-load attributes like data-src/data-original are common
    and haven't yet been swapped into the real src the way they are after a
    Playwright render), and _attr(tag, "href") would match inside a namespaced
    "xlink:href=" (e.g. an inline SVG sprite link) and return that instead of
    None.
    """
    match = re.search(rf'(?<![\w:-]){name}=["\']([^"\']*)["\']', tag, re.I)
    return match.group(1) if match else None


_LINKEDIN_COMPANY_RE = re.compile(r"^(https?://[^/]*linkedin\.com/company/[^/?#]+)", re.I)


def _linkedin_url_from_html(html: str, base_url: str) -> str | None:
    """Find a linkedin.com/company/ link declared as an <a href> in the raw HTML,
    truncated to the bare company page (drops a trailing /posts/, /jobs/, query
    string, etc. -- some sites link their "follow us" icon straight to a feed or
    subpage rather than the company root).

    Used for both scrape paths so extractor.py never has to find this in the
    markdown text -- it's deterministic, so there's no reason to spend Step 1
    prompt tokens asking the LLM to search for it (see extractor.py's
    linkedin_url parameter).
    """
    for tag in re.findall(r"<a[^>]+>", html, re.I):
        href = _attr(tag, "href")
        if href and "linkedin.com/company/" in href.lower():
            full = urljoin(base_url, href)
            m = _LINKEDIN_COMPANY_RE.match(full)
            return m.group(1) if m else full
    return None


def _page_title_from_html(html: str) -> str | None:
    """Extract the page's <title> text (e.g. "Anemo Labs - Digitizing Smell").

    Minimalist landing pages (Framer/Webflow templates especially) often never
    spell out the company name anywhere in the visible body copy -- it only
    lives in the browser tab title and in the logo image, which is unreadable
    text. Without this, extractor.py's Step 1 has no textual signal at all for
    `name` and correctly returns null (confirmed on anemolabs.com: good body
    text, but zero occurrences of "Anemo Labs"). Prepended to the scraped text
    in both scrape paths so the LLM gets the same hint regardless of which one
    ran.
    """
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if not m:
        return None
    title = _html_unescape(m.group(1)).strip()
    return title or None


def _favicon_url_from_html(html: str, base_url: str) -> str | None:
    """Find the favicon URL declared in the page's <link rel> tags.

    Prefers apple-touch-icon (usually larger) over plain icons.
    """
    candidates = []
    for tag in re.findall(r"<link[^>]+>", html, re.I):
        rel, href = _attr(tag, "rel"), _attr(tag, "href")
        if rel and href and "icon" in rel.lower():
            candidates.append((rel.lower(), href))
    if not candidates:
        return None
    for rel, href in candidates:
        if "apple-touch-icon" in rel:
            return urljoin(base_url, href)
    return urljoin(base_url, candidates[0][1])


def _logo_candidates_from_html(html: str, base_url: str) -> list[dict]:
    """Collect likely logo URLs from raw HTML, in priority order.

    Returns a small list of {url, hint} dicts for the LLM to choose from —
    <img> tags mentioning "logo", then icon links, then og:image.
    """
    candidates: list[dict] = []
    seen: set[str] = set()

    def add(url: str, hint: str) -> None:
        full = urljoin(base_url, url)
        if full not in seen:
            seen.add(full)
            candidates.append({"url": full, "hint": hint})

    img_count = 0
    for tag in re.findall(r"<img[^>]+>", html, re.I):
        if img_count >= 5 or not re.search(r"logo", tag, re.I):
            continue
        src, alt = _attr(tag, "src"), _attr(tag, "alt")
        if src:
            add(src, f"img alt='{alt or ''}'")
            img_count += 1

    for tag in re.findall(r"<link[^>]+>", html, re.I):
        rel, href = _attr(tag, "rel"), _attr(tag, "href")
        if rel and href and "icon" in rel.lower():
            add(href, rel.lower())

    for tag in re.findall(r"<meta[^>]+og:image[^>]+>", html, re.I):
        content = _attr(tag, "content")
        if content:
            add(content, "og:image")

    return candidates[:8]


# Max redirect hops the safe-download helpers below will follow before
# giving up -- generous enough for a normal www-canonicalization/HTTPS-upgrade
# chain, but bounded so a malicious/misconfigured site can't wedge this in an
# infinite-redirect loop.
_MAX_REDIRECTS = 5

# Hard cap on any single favicon/logo download, enforced while streaming (not
# after the fact on a fully-buffered response) -- a malicious or
# misconfigured server can otherwise serve an arbitrarily large body at a
# URL that passed every other check, exhausting memory/disk for what's
# ultimately discarded as "too big to be a favicon" anyway.
_MAX_ASSET_BYTES = 512 * 1024  # 512 KiB

# Only these response Content-Types are ever trusted for a saved asset --
# the remote URL's own path/extension is NEVER consulted (a malicious site
# fully controls that string). image/gif is deliberately excluded even
# though browsers render it -- it's not in this project's LOGO_EXTENSIONS
# allowlist and animated GIFs have their own historical parser bugs.
_CONTENT_TYPE_TO_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/x-icon": "ico",
    "image/vnd.microsoft.icon": "ico",
    "image/svg+xml": "svg",
}

# Reverse of _CONTENT_TYPE_TO_EXT -- used to set the Content-Type an R2
# upload is served back with, since R2/S3 doesn't infer it from the key.
_EXT_TO_CONTENT_TYPE = {ext: mime for mime, ext in _CONTENT_TYPE_TO_EXT.items()}

# Regex byte-patterns that make a downloaded SVG unsafe to save/serve, even
# behind this project's /assets nosniff+sandboxed-CSP headers (defense in
# depth, not a substitute for those headers): inline scripts, on*= event
# handlers, <foreignObject> (can embed arbitrary HTML/JS inside an SVG),
# javascript: URIs, external DTD/entity declarations (XXE), any http(s)/
# protocol-relative href or xlink:href (a self-contained favicon never needs
# one -- only local #fragment references do), and @import (external
# stylesheet loading). Deliberately broad/allowlist-flavored: a legitimate
# favicon SVG is simple, self-contained vector art, so refusing anything
# that even looks like it might reach outside the file is the safe default,
# not an overreach.
_SVG_DANGEROUS_PATTERNS = (
    re.compile(rb"<\s*script", re.I),
    re.compile(rb"\bon[a-zA-Z]+\s*=", re.I),
    re.compile(rb"<\s*foreignObject", re.I),
    re.compile(rb"javascript\s*:", re.I),
    re.compile(rb"<!ENTITY", re.I),
    re.compile(rb"<!DOCTYPE[^>]*(SYSTEM|PUBLIC)", re.I),
    re.compile(rb"\bhref\s*=\s*[\"']\s*(https?:)?//", re.I),  # matches xlink:href too (word boundary after ':')
    re.compile(rb"@import", re.I),
)


def _is_safe_svg(content: bytes) -> bool:
    """True if `content` looks like a real, self-contained SVG document with
    none of _SVG_DANGEROUS_PATTERNS present. Not a full XML parse (this
    project has no XML dependency and a regex scan is sufficient for an
    allowlist-style "refuse anything suspicious" check) -- just requires an
    <svg ...> tag to appear near the start of the document, matching how a
    real favicon SVG looks (optionally preceded by an XML declaration/
    comments/whitespace), and none of the dangerous patterns anywhere.
    """
    head = content[:512].lstrip()
    if b"<svg" not in head[:256] and not head.startswith((b"<?xml", b"<!--")):
        return False
    if b"<svg" not in content:
        return False
    return not any(p.search(content) for p in _SVG_DANGEROUS_PATTERNS)


def _magic_bytes_match(ext: str, content: bytes) -> bool:
    if ext == "png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == "jpg":
        return content.startswith(b"\xff\xd8\xff")
    if ext == "webp":
        return content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    if ext == "ico":
        return content[:4] == b"\x00\x00\x01\x00"
    if ext == "svg":
        return _is_safe_svg(content)
    return False


def _classify_downloaded_image(content: bytes, content_type_header: str | None) -> str | None:
    """Returns a trusted extension (png/jpg/webp/ico/svg) for `content`, or
    None if it can't be trusted enough to save. Two independent signals must
    both agree: the response's own Content-Type header says what kind of
    file this claims to be (never the remote URL's path, which the old
    version of this code trusted and which a malicious/compromised site
    fully controls), AND the actual bytes' magic number -- or, for SVG, the
    strict content scan above -- confirm it. Either one alone isn't enough:
    a header with no matching bytes is a lie, and bytes with no recognized
    Content-Type are simply not saved as anything (this app never needs to
    guess a file's type from its content alone).
    """
    if not content_type_header:
        return None
    mime = content_type_header.split(";", 1)[0].strip().lower()
    ext = _CONTENT_TYPE_TO_EXT.get(mime)
    if ext is None:
        return None
    if not _magic_bytes_match(ext, content):
        return None
    return ext


def _safe_httpx_download(url: str, max_bytes: int = _MAX_ASSET_BYTES, **kwargs) -> tuple[bytes, str | None, str]:
    """Download `url`'s body for saving as a favicon/logo asset: per-hop SSRF
    re-validation (same as the old _safe_httpx_get, now removed since this
    replaced its only callers) plus a streaming size cap that aborts the
    download the moment it exceeds `max_bytes`, instead of buffering the
    full response via response.content the way httpx normally would.

    Returns (content, content_type_header, final_url) -- final_url is the
    post-redirect URL (needed by fetch_and_save_favicon to resolve a
    relative favicon href against the page's actual location, same as the
    old code used response.url for). content_type_header is returned raw;
    callers must run it through _classify_downloaded_image before trusting
    it for anything.

    Raises net_security.UnsafeURLError (unsafe/redirected-to-unsafe URL, or
    too many redirects), ValueError (body exceeds max_bytes), or
    httpx.HTTPStatusError (non-2xx final response) -- every caller already
    wraps this in a blanket `except Exception: pass`, matching the existing
    "logo/favicon not found" failure mode for any of these.
    """
    current = url
    with httpx.Client() as client:
        for _ in range(_MAX_REDIRECTS + 1):
            net_security.assert_safe_url(current)
            with client.stream("GET", current, follow_redirects=False, **kwargs) as resp:
                location = resp.headers.get("location") if resp.is_redirect else None
                if location:
                    current = urljoin(str(resp.url), location)
                    continue
                resp.raise_for_status()
                content_type = resp.headers.get("content-type")
                content = bytearray()
                for chunk in resp.iter_bytes():
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        raise ValueError(f"Response for {url!r} exceeds the {max_bytes}-byte limit.")
                return bytes(content), content_type, str(resp.url)
    raise UnsafeURLError(f"Too many redirects for {url!r}.")


_r2 = None
_r2_lock = threading.Lock()


def _r2_client():
    """Lazily create and cache a single R2 (S3-compatible) client. Same
    lazy-init-with-lock pattern as storage.py::_client() for the Supabase
    client -- one place this is constructed, imported everywhere, instead of
    inlined per caller.
    """
    global _r2
    if _r2 is None:
        with _r2_lock:
            if _r2 is None:
                _r2 = boto3.client(
                    "s3",
                    endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                    region_name="auto",
                )
    return _r2


def _r2_object_exists(key: str) -> bool:
    try:
        _r2_client().head_object(Bucket=os.environ["R2_BUCKET_NAME"], Key=key)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
            return False
        raise


def _r2_upload(key: str, content: bytes, content_type: str | None) -> str:
    extra = {"ContentType": content_type} if content_type else {}
    _r2_client().put_object(Bucket=os.environ["R2_BUCKET_NAME"], Key=key, Body=content, **extra)
    return f"{os.environ['R2_PUBLIC_URL']}/{key}"


def _save_asset(slug: str, suffix: str, content: bytes, ext: str) -> str:
    """Persist a validated favicon/logo image under slug{suffix}.ext and
    return its servable URL. Writes to R2 by default; LOGO_STORAGE=local
    writes to local disk instead (offline debugging only -- see
    fetch_and_save_favicon's docstring for why R2 is the default).
    """
    if LOGO_STORAGE == "local":
        os.makedirs("assets/logos", exist_ok=True)
        path = f"assets/logos/{slug}{suffix}.{ext}"
        with open(path, "wb") as f:
            f.write(content)
        return f"/{path}"
    key = f"logos/{slug}{suffix}.{ext}"
    return _r2_upload(key, content, _EXT_TO_CONTENT_TYPE.get(ext))


def _existing_asset_url(slug: str, suffix: str = "") -> str | None:
    """Return the URL of an already-saved favicon/logo for slug{suffix}, if
    one exists -- checking each of LOGO_EXTENSIONS in turn, against R2 (or
    local disk under LOGO_STORAGE=local). Centralizes the "don't re-fetch if
    we already have it" check that main.ingest() and reprocess_list.process()
    each used to implement inline against os.path.exists(), so both callers
    share one implementation instead of drifting.
    """
    if LOGO_STORAGE == "local":
        for ext in LOGO_EXTENSIONS:
            path = f"assets/logos/{slug}{suffix}.{ext}"
            if os.path.exists(path):
                return f"/{path}"
        return None
    for ext in LOGO_EXTENSIONS:
        key = f"logos/{slug}{suffix}.{ext}"
        if _r2_object_exists(key):
            return f"{os.environ['R2_PUBLIC_URL']}/{key}"
    return None


def fetch_and_save_favicon(domain: str, website: str) -> str | None:
    """Download the site favicon — used for graph circles (flaticon_url).

    Tries Google's favicon service (128px) first; if the domain is not in
    Google's index, falls back to the favicon declared in the site's own HTML.

    Keyed by normalized domain, not the startup's display name -- two
    startups can share a name (e.g. "Corma" at corma.io and corma.ai), which
    used to make them silently overwrite each other's logo file on disk.

    The saved extension/filename is entirely app-controlled: extension comes
    from _classify_downloaded_image (Content-Type + magic bytes, never the
    remote URL), and slug is validated by _validate_asset_slug before it's
    ever used to build a path.
    """
    if not website:
        return None

    slug = slugify(domain)
    _validate_asset_slug(slug)

    url = f"https://www.google.com/s2/favicons?domain={domain}&sz=128"
    try:
        content, content_type, _final_url = _safe_httpx_download(url, timeout=5)
        ext = _classify_downloaded_image(content, content_type)
        if ext and len(content) > 68:
            return _save_asset(slug, "", content, ext)
    except Exception:
        pass

    # Fallback: favicon declared in the site's own HTML
    try:
        page_content, page_content_type, page_final_url = _safe_httpx_download(
            website, timeout=10, headers={"User-Agent": _FULL_BROWSER_UA}
        )
        page_mime = (page_content_type or "").split(";", 1)[0].strip().lower()
        if page_mime not in ("text/html", "application/xhtml+xml"):
            return None
        icon_url = _favicon_url_from_html(page_content.decode("utf-8", errors="replace"), page_final_url)
        if not icon_url:
            return None
        content, content_type, _final_url = _safe_httpx_download(icon_url, timeout=10, headers={"User-Agent": _FULL_BROWSER_UA})
        ext = _classify_downloaded_image(content, content_type)
        if ext and len(content) > 68:
            return _save_asset(slug, "", content, ext)
    except Exception:
        pass

    return None


def fetch_and_save_real_logo(domain: str, logo_url: str) -> str | None:
    """Download the actual logo found by the LLM — used for market maps (logo_url).

    Keyed by normalized domain -- see fetch_and_save_favicon's docstring.
    Extension/filename handling: see that function's docstring too.
    """
    if not logo_url:
        return None

    slug = slugify(domain)
    _validate_asset_slug(slug)

    try:
        content, content_type, _final_url = _safe_httpx_download(logo_url, timeout=10)
        ext = _classify_downloaded_image(content, content_type)
        if ext and len(content) > 100:
            return _save_asset(slug, "_logo", content, ext)
    except Exception:
        pass

    return None


# Below this, a light fetch is considered empty/blocked (anti-bot interstitial,
# JS-only content) and we fall back to Playwright instead of trusting it.
_LIGHT_FETCH_MIN_CHARS = 200

# Phrases indicating the page is a bot-block/consent-wall rather than real content.
# Shared with diagnose_scraping.py's characterize() (imported from here, not
# duplicated) so the two heuristics can't silently drift apart.
BLOCKING_MARKERS = (
    "enable javascript",
    "verify you are human",
    "checking your browser",
    "captcha",
    "access denied",
    "are you a robot",
    "unusual traffic",
)

# Unrendered Vue/Angular/Handlebars ({{ }}) or Jinja/Django ({% %}) template syntax
# surviving into the extracted text -- confirmed via alqem.ai: trafilatura can pull
# 900+ chars of surrounding static text past _LIGHT_FETCH_MIN_CHARS while the actual
# client-side-rendered content (including the company's own name) never scrapes,
# leaving the extractor working from real but incomplete/misleading text.
_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{\{[^}]+\}\}|\{%[^%]+%\}")

# Above this ratio of short (<40 char), boilerplate-looking lines, the extracted
# text is treated as nav/footer noise rather than substantive content.
_NOISE_RATIO_THRESHOLD = 0.85


def _noise_ratio(text: str) -> float:
    """Ratio of short (<40 char) non-blank lines in text -- shared by
    _parse_light_fetch's fallback check and diagnose_scraping.py's characterize(),
    so the two heuristics can't silently drift apart (same sharing pattern as
    BLOCKING_MARKERS above). Deliberately doesn't check for markdown-link-bracket
    lines (characterize()'s old inline version did) -- trafilatura.extract() never
    produces markdown link syntax, so that clause never fires against this fast
    path's output (same reason Story 5.3 flags it as unreachable there).

    Returns 0.0 for text with no non-blank lines (avoids a division by zero).
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return 0.0
    noisy = sum(1 for line in lines if len(line) < 40)
    return noisy / len(lines)


# ── Generic post-scrape text cleanup ────────────────────────────────────────
# Applied to the output of BOTH scrape paths (_parse_light_fetch's trafilatura
# text and _scrape_playwright's html2text output) so every site benefits
# regardless of which one ran -- these are markup/boilerplate patterns common
# to nearly all sites, not anything specific to one page. Purely about shrinking
# extractor.py's Step 1 input token count; none of this content ever helped
# name/country/description/sector extraction.

_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _is_nav_dense_block(block: str) -> bool:
    """A paragraph block made up mostly of consecutive markdown links with
    little or no other text is a nav bar, footer link row, or social-icon row
    -- e.g. "[Home](/)[Products](/products)[Services](/services)" or
    "[](https://x.com/x)[](https://linkedin.com/company/x)". Requires >=3 link
    segments AND almost no leftover text, so a real sentence that happens to
    contain a couple of inline links is never caught by this.

    Checked per BLOCK (text between blank lines), not per physical line --
    html2text word-wraps long link rows (e.g. a 3-item legal-links footer row)
    across two physical lines, which would silently undercount a line-scoped
    link tally and let the row through.
    """
    links = _MD_LINK_RE.findall(block)
    if len(links) < 3:
        return False
    leftover = _MD_LINK_RE.sub("", block).strip()
    return len(leftover) < 10


# Exact-phrase boilerplate footer/legal lines seen on nearly every site. Matched
# only against a line's FULL (trimmed) content, never a substring -- so a
# genuine sentence that happens to mention "cookie" (e.g. a food-tech startup)
# is never dropped, only a standalone footer item that IS just that phrase.
_BOILERPLATE_LINE_PHRASES = {
    "privacy policy", "terms of service", "terms of use", "terms & conditions",
    "terms and conditions", "cookie policy", "cookie settings", "cookie preferences",
    "manage cookies", "accept cookies", "use of cookies", "all rights reserved",
}
_COPYRIGHT_LINE_RE = re.compile(r"^©\s?\d{0,4}.{0,60}all rights reserved\.?$", re.I)


def _is_boilerplate_line(line: str) -> bool:
    stripped = line.strip(" \t-|•.")
    if not stripped:
        return False
    if stripped.lower() in _BOILERPLATE_LINE_PHRASES:
        return True
    return bool(_COPYRIGHT_LINE_RE.match(stripped))


def _dedup_paragraphs(text: str) -> str:
    """Drop repeat occurrences of a paragraph/block that appears again later,
    byte-for-byte (modulo whitespace/case) -- e.g. the same mission statement or
    CTA block repeated in a hero section and again in the footer. Only applied
    above a length floor so short structural fragments that legitimately repeat
    (a lone "Learn More", a repeated price) are left alone; this only fires on
    a genuinely duplicated sentence-or-longer block.
    """
    blocks = re.split(r"\n\s*\n", text)
    seen: set[str] = set()
    kept: list[str] = []
    for block in blocks:
        key = re.sub(r"\s+", " ", block).strip().lower()
        if len(key) > 40:
            if key in seen:
                continue
            seen.add(key)
        kept.append(block)
    return "\n\n".join(kept)


def _clean_scraped_text(text: str) -> str:
    """Shrink scraped markdown before it reaches extractor.py's Step 1 prompt,
    without touching anything that could carry extraction signal. Order matters:
    nav-density is measured on the original `[text](url)` syntax at block
    granularity (a stripped block loses the density signal), link stripping
    must happen before the boilerplate/dedup passes (a boilerplate phrase can
    be wrapped in a link), and blank-line collapsing runs last since every
    prior pass can leave gaps.
    """
    blocks = [b for b in re.split(r"\n\s*\n", text) if not _is_nav_dense_block(b)]
    text = "\n\n".join(blocks)
    text = _MD_LINK_RE.sub(lambda m: m.group(1), text)
    lines = [line for line in text.split("\n") if not _is_boilerplate_line(line)]
    text = "\n".join(lines)
    text = _dedup_paragraphs(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _parse_light_fetch(html: str, base_url: str) -> tuple[str, list[dict], str | None] | None:
    """CPU-bound parsing for _fetch_light: trafilatura extraction + the regex-based
    HTML scans. Run via asyncio.to_thread() -- trafilatura's lxml parse is
    synchronous and would otherwise block the event loop for its duration, same
    concern _ingest_sync's asyncio.to_thread wrapping already addresses one call
    later in the pipeline.

    Returns None (triggering the Playwright fallback in scrape()) if extraction
    raises, or the result is too short to be useful -- matches _fetch_light's own
    documented contract, which didn't previously catch a trafilatura exception.
    """
    try:
        text = trafilatura.extract(html, include_comments=False, include_tables=True) or ""
    except Exception:
        return None
    if len(text) < _LIGHT_FETCH_MIN_CHARS:
        return None
    if _TEMPLATE_PLACEHOLDER_RE.search(text):
        print("[_fetch_light] Unresolved template syntax detected, falling back to Playwright")
        return None

    lowered = text.lower()
    if any(marker in lowered for marker in BLOCKING_MARKERS):
        print("[_fetch_light] Blocking-page marker detected, falling back to Playwright")
        return None

    if _noise_ratio(text) > _NOISE_RATIO_THRESHOLD:
        print("[_fetch_light] High boilerplate/noise ratio detected, falling back to Playwright")
        return None

    # trafilatura strips link URLs, so the LinkedIn link has to be recovered from
    # the raw HTML rather than the extracted text -- returned separately (not
    # embedded in the text) since extractor.py takes it as a plain parameter
    # instead of asking the LLM to find it, now that every link is gone below.
    linkedin_url = _linkedin_url_from_html(html, base_url)
    text = _clean_scraped_text(text)
    title = _page_title_from_html(html)
    if title:
        text = f"PAGE TITLE: {title}\n\n{text}"

    return text, _logo_candidates_from_html(html, base_url), linkedin_url


async def _safe_httpx_get_async(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """Async counterpart to _safe_httpx_get -- same manual, per-hop
    SSRF re-validated redirect handling, for _fetch_light's async client.
    """
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        await asyncio.to_thread(net_security.assert_safe_url, current)
        resp = await client.get(current, follow_redirects=False, **kwargs)
        if not resp.is_redirect:
            return resp
        location = resp.headers.get("location")
        if not location:
            return resp
        current = urljoin(str(resp.url), location)
    raise UnsafeURLError(f"Too many redirects for {url!r}.")


async def _fetch_light(url: str) -> tuple[str, list[dict], str | None] | None:
    """Fast path for server-rendered pages: plain HTTP GET + trafilatura extraction,
    no browser. Returns None (triggering the Playwright fallback) on any HTTP error,
    extraction failure, or if the extracted text is too short to be useful.

    A redirect to an unsafe address (net_security.UnsafeURLError) is NOT
    caught here -- it propagates out of scrape()/ingest() as a hard failure
    instead of silently falling back to Playwright, which would just repeat
    the same navigation (safely, thanks to _scrape_playwright's context.route
    guard, but pointlessly).
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await _safe_httpx_get_async(client, url, headers={
                "User-Agent": _FULL_BROWSER_UA,
                "Accept-Language": "en-US,en;q=0.9",
            })
        resp.raise_for_status()
    except httpx.HTTPError:
        return None

    return await asyncio.to_thread(_parse_light_fetch, resp.text, str(resp.url))


async def scrape(url: str) -> tuple[str, list[dict], str | None]:
    """Scrape a page. Tries a lightweight HTTP fetch first (fast, no browser) —
    works for server-rendered sites, which covers most cases. Falls back to
    Playwright only when the light fetch fails or comes back too short (JS-rendered
    content, anti-bot interstitial). Returns (markdown, logo_candidates, linkedin_url).

    Validated against net_security's SSRF guard up front (scheme, credentials,
    IP-literal/private-range checks, and a DNS resolution of the hostname) --
    both branches below still re-check every request they actually make
    (_fetch_light's redirects, _scrape_playwright's context.route guard for
    every navigation/redirect/subresource), since this initial check can't
    account for a redirect discovered mid-fetch or a DNS answer that changes
    between now and the moment of connection.
    """
    await asyncio.to_thread(net_security.assert_safe_url, url)
    light = await _fetch_light(url)
    if light is not None:
        return light
    return await _scrape_playwright(url)


async def _guard_playwright_route(route) -> None:
    """context.route handler applied to every request Playwright's browser
    context issues -- the top-level navigation, every redirect the browser
    follows internally, and every subresource (script/image/xhr/fetch) the
    rendered page loads. This is the layer that actually stops a page from
    steering the browser at an internal address after the initial scrape()
    check already passed: a malicious/compromised page can redirect its own
    navigation, or load an <img>/fetch() pointed at a private/loopback/
    link-local/CGNAT address, and neither is visible to scrape()'s one-time
    pre-check.

    Only http(s) requests are validated -- data:/blob:/about: etc. never hit
    the network and would otherwise be wrongly aborted (breaking inline
    images and other same-document resources). Any request whose host fails
    net_security.assert_safe_url() is aborted rather than allowed to
    continue.
    """
    request_url = route.request.url
    scheme = urlparse(request_url).scheme
    if scheme not in ("http", "https"):
        await route.continue_()
        return
    try:
        await asyncio.to_thread(net_security.assert_safe_url, request_url)
    except UnsafeURLError as e:
        print(f"[scrape] Blocked unsafe request during Playwright render: {request_url} ({e})")
        await route.abort()
        return
    await route.continue_()


async def _scrape_playwright(url: str) -> tuple[str, list[dict], str | None]:
    """Full browser render — fallback when _fetch_light isn't enough."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=_FULL_BROWSER_UA,
            locale="en-US",
            viewport={"width": 1280, "height": 800},
            ignore_https_errors=True,
        )
        await ctx.route("**/*", _guard_playwright_route)
        page = await ctx.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            if url.startswith("https://") and "ERR_SSL_" in str(e):
                url = "http://" + url[len("https://"):]
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            else:
                raise
        if resp is not None and resp.status in (401, 403, 429):
            # Unlike _fetch_light (resp.raise_for_status()), page.goto() doesn't
            # raise on a non-2xx status -- it happily renders the WAF/anti-bot
            # block page's HTML (e.g. Cloudflare's generic "403 - Forbidden"),
            # which then sails through extraction as ordinary page content with
            # no company name in it, surfacing as the misleading "Could not
            # extract startup info from this page" (main.py's _ingest_sync)
            # instead of naming the real cause: the site blocked the scraper,
            # there was never anything to extract (confirmed on
            # impossible-objects.com, 2026-09-16 bug report). Bailing here
            # also skips the ~45s of wait_for_function/networkidle timeouts
            # below, which can't help since a block page never grows real
            # content. Limited to 401/403/429 (auth/block/rate-limit) rather
            # than every 4xx/5xx, since e.g. a transient 500 or a client-side
            # SPA that free-renders over a non-200 document response
            # shouldn't be misread the same way.
            await browser.close()
            raise ValueError(f"Ce site a bloqué le scraping (HTTP {resp.status}) au lieu de servir la page.")
        try:
            await page.wait_for_function(
                "() => document.body && document.body.innerText.length > 200 && !document.body.innerText.includes('Checking your browser')",
                timeout=30000,
            )
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await page.evaluate("""
            [
                '[id*="cookiebot" i]', '[class*="cookiebot" i]',
                '#onetrust-consent-sdk', '#onetrust-banner-sdk',
                '#sp-cc', '.cc-window', '#cookie-law-info-bar',
                '[id*="cookie-consent" i]', '[id*="gdpr" i]',
            ].forEach(sel => document.querySelectorAll(sel).forEach(el => el.remove()));
        """)
        html = await page.content()
        final_url = page.url
        await browser.close()

    converter = html2text.HTML2Text()
    converter.ignore_links = False
    converter.ignore_images = True
    text = _clean_scraped_text(converter.handle(html))
    linkedin_url = _linkedin_url_from_html(html, final_url)
    title = _page_title_from_html(html)
    if title:
        text = f"PAGE TITLE: {title}\n\n{text}"
    return text, _logo_candidates_from_html(html, final_url), linkedin_url


def _ingest_sync(markdown: str, logo_candidates: list[dict], linkedin_url: str | None, url: str, added_by_user_id: int | None = None) -> dict:
    """Classify, save, fetch logos, and score competitors -- the fully synchronous
    part of ingest() (LLM calls, Supabase calls, file I/O). Run via
    asyncio.to_thread() from ingest() so it doesn't block the event loop: since
    Story 6.1, graph_app.py's background worker awaits ingest() (not /api/ingest
    itself, which only enqueues and returns immediately), and this pipeline can
    run for minutes under Mistral rate limiting (score_candidates' chunk pacing +
    widened retry backoff) -- without offloading to a thread, every other
    concurrent request (including other API routes, not just ingestion) would
    freeze for the whole duration.
    """
    data = extract(markdown, website=url, logo_candidates=logo_candidates, linkedin_url=linkedin_url)
    print(json.dumps(data, ensure_ascii=False, indent=2))

    if not data.get("name"):
        raise ValueError("Could not extract startup info from this page.")

    data.setdefault("sub_subsectors", [])

    data.pop("sector_confidences", None)
    data.pop("subsector_confidences", None)

    extracted_logo_url = data.pop("logo_url", None)

    # Embedding pre-filter chantier: computed once here, at ingestion time, so
    # competitor.py never has to re-embed an existing candidate -- only the new
    # company's own description is ever embedded.
    if data.get("description"):
        data["embedding"] = embed_one(data["description"])

    action, row_id, domain = save_startup(data, added_by_user_id=added_by_user_id)
    data["id"] = row_id  # threaded through compare_jev()/save_competitors_jev() below
    name    = data.get("name", "unknown")
    website = data.get("website", "")
    slug    = slugify(domain)  # domain, not name -- two same-named startups must not collide on disk
    _validate_asset_slug(slug)  # checked once here since it's reused below for the on-disk existence check too, not just inside the fetchers
    print(f"Startup {action}: {name}")

    # Favicon — displayed in graph circles
    flaticon_url = _existing_asset_url(slug)
    if not flaticon_url:
        flaticon_url = fetch_and_save_favicon(domain, website)

    # Real logo — for market maps
    logo_url = _existing_asset_url(slug, "_logo")
    if not logo_url:
        logo_url = fetch_and_save_real_logo(domain, extracted_logo_url)

    updates = {}
    if flaticon_url:
        updates["flaticon_url"] = flaticon_url
    if logo_url:
        updates["logo_url"] = logo_url
    if updates:
        _db_client().table("compspro").update(updates).eq("id", row_id).execute()

    print(f"Favicon: {flaticon_url or 'not found'}")
    print(f"Logo:    {logo_url or 'not found'}")

    # Jev scoring path (default since 2026-09-17 -- see competitor.py's Jev
    # section docstring for the decision trail). The Mistral path
    # (compare/save_competitors/explore_transitive) stays in competitor.py,
    # importable directly, but is no longer called from here.
    #
    # explore_transitive_jev() (2026-09-17) is the Jev port of
    # explore_transitive() -- same 2nd-degree discovery via each direct
    # competitor's own known links, scored/saved through the Jev zone split.
    # Wired here so every future ingest gets transitive discovery, not just
    # one-off backfills (see reprocess_list.py for backfilling startups
    # already in compspro before this was wired in).
    saved_relationships = []
    pending_review = []
    results = compare_jev(data)
    if results:
        print(f"\nCompetitor analysis via Jev ({len(results)} candidates):")
        for r in results:
            print(f"  {r['name']} → score: {r['score']:.2f} [{r['zone']}]")

        outcome = save_competitors_jev(data, results)
        saved_relationships.extend(outcome["saved"])
        pending_review.extend(outcome["review_queued"])
        if outcome["saved"]:
            print()
            for rel in outcome["saved"]:
                print(f"  Relationship saved: {rel['company_a']} ↔ {rel['company_b']} (score: {rel['score']:.2f})")
        if outcome["review_queued"]:
            print()
            for rel in outcome["review_queued"]:
                print(f"  Queued for review: {rel['company_a']} ↔ {rel['company_b']} (score: {rel['score']:.2f})")

        if outcome["saved"]:
            transitive_outcome = explore_transitive_jev(data, outcome["saved"])
            saved_relationships.extend(transitive_outcome["saved"])
            pending_review.extend(transitive_outcome["review_queued"])
            if transitive_outcome["saved"]:
                print()
                for rel in transitive_outcome["saved"]:
                    print(f"  Transitive relationship saved: {rel['company_a']} ↔ {rel['company_b']} (score: {rel['score']:.2f})")
            if transitive_outcome["review_queued"]:
                print()
                for rel in transitive_outcome["review_queued"]:
                    print(f"  Transitive relationship queued for review: {rel['company_a']} ↔ {rel['company_b']} (score: {rel['score']:.2f})")
    else:
        print("No candidates found in same subsectors.")

    return {
        "name": name, "domain": domain, "id": row_id, "action": action,
        "competitors_found": len(saved_relationships),
        "competitors_pending_review": len(pending_review),
    }


async def ingest(url: str, interactive: bool = True, added_by_user_id: int | None = None, ingestion_queue_id: int | None = None) -> dict:
    """Scrape, classify, save, fetch logos, and score competitors for one startup URL.

    Reused by both the CLI entrypoint below and the Story 6.1 background worker
    (graph_app.py's _ingestion_worker, itself triggered by the web search bar's
    "Add" action via /api/ingest -- no caller awaits ingest() directly anymore).
    Raises ValueError if no startup info could be extracted from the page.

    added_by_user_id is the users.id of whoever submitted this URL (threaded
    through from ingestion_queue.requested_by_user_id by the background
    worker) and is only ever written to compspro.added_by_user_id on a fresh
    insert -- see storage.save_startup. None for the CLI entrypoint below,
    which has no logged-in user.

    ingestion_queue_id (also threaded through from the background worker, None
    for the CLI entrypoint) sets CURRENT_API_CALL_CONTEXT for the duration of
    this ingest so every Mistral call underneath (extract/compare/embed_one)
    logs its usage/cost against the right ingestion_queue row -- see
    storage.CURRENT_API_CALL_CONTEXT and storage.log_api_call.

    Sets INTERACTIVE_REQUEST for the duration of the write-sequence part so
    competitor.py/extractor.py use the tighter interactive retry/timeout budget,
    and serializes concurrent calls for the same domain via a per-domain lock --
    scrape() itself isn't locked, since it never touches the DB.

    interactive=True is this function's default, but no live caller uses it:
    the background worker passes interactive=False (no browser is waiting on
    it, so it can afford the more patient batch budget) and so does the CLI
    entrypoint below (one-off runs would rather retry longer against a slow
    Mistral response than give up after 3 attempts). The parameter/tight
    budget stay available for a future synchronous caller.
    """
    markdown, logo_candidates, linkedin_url = await scrape(url)
    domain = normalize_domain(url)
    lock = _domain_locks.setdefault(domain, asyncio.Lock())
    token = INTERACTIVE_REQUEST.set(interactive)
    cost_ctx_token = CURRENT_API_CALL_CONTEXT.set({"ingestion_queue_id": ingestion_queue_id, "label": url})
    try:
        async with lock:
            return await asyncio.to_thread(_ingest_sync, markdown, logo_candidates, linkedin_url, url, added_by_user_id)
    finally:
        INTERACTIVE_REQUEST.reset(token)
        CURRENT_API_CALL_CONTEXT.reset(cost_ctx_token)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python main.py <url>", file=sys.stderr)
        sys.exit(1)

    try:
        asyncio.run(ingest(sys.argv[1], interactive=False))
    except ValueError as e:
        print(f"{e} Skipping.", file=sys.stderr)
        sys.exit(0)
