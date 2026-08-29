import sys
import os
import re
import json
import asyncio
from urllib.parse import urlparse, urljoin
from playwright.async_api import async_playwright
import html2text
import httpx
import trafilatura
from extractor import extract
from storage import save_startup, normalize_domain, COMPETITOR_THRESHOLD, INTERACTIVE_REQUEST, _client as _db_client
from competitor import compare, save_competitors, explore_transitive


LOGO_EXTENSIONS = ("svg", "png", "jpg", "jpeg", "webp", "ico")

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


def _linkedin_url_from_html(html: str, base_url: str) -> str | None:
    """Find a linkedin.com/company/ link declared as an <a href> in the raw HTML.

    Needed because _fetch_light's trafilatura extraction drops link URLs (and
    often the whole short "follow us" paragraph as boilerplate) -- the LinkedIn
    URL has to be recovered from raw HTML, not from the extracted text.
    """
    for tag in re.findall(r"<a[^>]+>", html, re.I):
        href = _attr(tag, "href")
        if href and "linkedin.com/company/" in href.lower():
            return urljoin(base_url, href)
    return None


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


def fetch_and_save_favicon(name: str, website: str) -> str | None:
    """Download the site favicon — used for graph circles (flaticon_url).

    Tries Google's favicon service (128px) first; if the domain is not in
    Google's index, falls back to the favicon declared in the site's own HTML.
    """
    if not website:
        return None

    domain = normalize_domain(website)
    slug   = slugify(name)
    os.makedirs("assets/logos", exist_ok=True)

    url = f"https://www.google.com/s2/favicons?domain={domain}&sz=128"
    try:
        r = httpx.get(url, timeout=5, follow_redirects=True)
        if r.status_code == 200 and len(r.content) > 68:
            path = f"assets/logos/{slug}.png"
            with open(path, "wb") as f:
                f.write(r.content)
            return f"/{path}"
    except Exception:
        pass

    # Fallback: favicon declared in the site's own HTML
    try:
        page = httpx.get(website, timeout=10, follow_redirects=True, headers={"User-Agent": _FULL_BROWSER_UA})
        if page.status_code != 200:
            return None
        icon_url = _favicon_url_from_html(page.text, str(page.url))
        if not icon_url:
            return None
        r = httpx.get(icon_url, timeout=10, follow_redirects=True, headers={"User-Agent": _FULL_BROWSER_UA})
        if r.status_code == 200 and len(r.content) > 68:
            ext = urlparse(icon_url).path.rsplit(".", 1)[-1].lower()
            if ext not in LOGO_EXTENSIONS:
                ext = "png"
            path = f"assets/logos/{slug}.{ext}"
            with open(path, "wb") as f:
                f.write(r.content)
            return f"/{path}"
    except Exception:
        pass

    return None


def fetch_and_save_real_logo(name: str, logo_url: str) -> str | None:
    """Download the actual logo found by the LLM — used for market maps (logo_url)."""
    if not logo_url:
        return None

    slug = slugify(name)
    os.makedirs("assets/logos", exist_ok=True)

    ext = urlparse(logo_url).path.rsplit(".", 1)[-1].lower()
    if ext not in LOGO_EXTENSIONS:
        ext = "png"

    try:
        r = httpx.get(logo_url, timeout=10, follow_redirects=True)
        if r.status_code == 200 and len(r.content) > 100:
            path = f"assets/logos/{slug}_logo.{ext}"
            with open(path, "wb") as f:
                f.write(r.content)
            return f"/{path}"
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


def _parse_light_fetch(html: str, base_url: str) -> tuple[str, list[dict]] | None:
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

    # trafilatura strips link URLs (and often the whole "follow us" paragraph) from
    # the extracted text, so extractor.py's linkedin_url prompt would never find one
    # via this fast path -- recover it from the raw HTML and surface it in the text.
    # Prepended, not appended: extractor.py truncates markdown to its first 30000
    # chars, which would silently drop a suffix on any longer page.
    linkedin_url = _linkedin_url_from_html(html, base_url)
    if linkedin_url:
        text = f"LinkedIn: {linkedin_url}\n\n{text}"

    return text, _logo_candidates_from_html(html, base_url)


async def _fetch_light(url: str) -> tuple[str, list[dict]] | None:
    """Fast path for server-rendered pages: plain HTTP GET + trafilatura extraction,
    no browser. Returns None (triggering the Playwright fallback) on any HTTP error,
    extraction failure, or if the extracted text is too short to be useful.
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(url, headers={
                "User-Agent": _FULL_BROWSER_UA,
                "Accept-Language": "en-US,en;q=0.9",
            })
        resp.raise_for_status()
    except httpx.HTTPError:
        return None

    return await asyncio.to_thread(_parse_light_fetch, resp.text, str(resp.url))


async def scrape(url: str) -> tuple[str, list[dict]]:
    """Scrape a page. Tries a lightweight HTTP fetch first (fast, no browser) —
    works for server-rendered sites, which covers most cases. Falls back to
    Playwright only when the light fetch fails or comes back too short (JS-rendered
    content, anti-bot interstitial). Returns (markdown, logo_candidates).
    """
    light = await _fetch_light(url)
    if light is not None:
        return light
    return await _scrape_playwright(url)


async def _scrape_playwright(url: str) -> tuple[str, list[dict]]:
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
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            if url.startswith("https://") and "ERR_SSL_" in str(e):
                url = "http://" + url[len("https://"):]
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            else:
                raise
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
    return converter.handle(html), _logo_candidates_from_html(html, final_url)


def _ingest_sync(markdown: str, logo_candidates: list[dict], url: str) -> dict:
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
    data = extract(markdown, website=url, logo_candidates=logo_candidates)
    print(json.dumps(data, ensure_ascii=False, indent=2))

    if not data.get("name"):
        raise ValueError("Could not extract startup info from this page.")

    data.setdefault("sub_subsectors", [])

    data.pop("sector_confidences", None)
    data.pop("subsector_confidences", None)

    extracted_logo_url = data.pop("logo_url", None)

    action  = save_startup(data)
    name    = data.get("name", "unknown")
    website = data.get("website", "")
    slug    = slugify(name)
    print(f"Startup {action}: {name}")

    # Favicon — displayed in graph circles
    flaticon_url = None
    for ext in LOGO_EXTENSIONS:
        if os.path.exists(f"assets/logos/{slug}.{ext}"):
            flaticon_url = f"/assets/logos/{slug}.{ext}"
            break
    if not flaticon_url:
        flaticon_url = fetch_and_save_favicon(name, website)

    # Real logo — for market maps
    logo_url = None
    for ext in LOGO_EXTENSIONS:
        if os.path.exists(f"assets/logos/{slug}_logo.{ext}"):
            logo_url = f"/assets/logos/{slug}_logo.{ext}"
            break
    if not logo_url:
        logo_url = fetch_and_save_real_logo(name, extracted_logo_url)

    updates = {}
    if flaticon_url:
        updates["flaticon_url"] = flaticon_url
    if logo_url:
        updates["logo_url"] = logo_url
    if updates:
        _db_client().table("compspro").update(updates).eq("name", name).execute()

    print(f"Favicon: {flaticon_url or 'not found'}")
    print(f"Logo:    {logo_url or 'not found'}")

    saved_relationships = []
    results = compare(data)
    if results:
        print(f"\nCompetitor analysis ({len(results)} candidates):")
        for r in results:
            mark = "✓ competitor" if r["score"] >= COMPETITOR_THRESHOLD else "✗ not competitor"
            print(f"  {r['name']} → score: {r['score']:.2f} {mark}")

        saved = save_competitors(data, results)
        saved_relationships.extend(saved)
        if saved:
            print()
            for rel in saved:
                print(f"  Relationship saved: {rel['company_a']} ↔ {rel['company_b']} (score: {rel['score']:.2f})")

        direct_names = [rel["company_b"] for rel in saved]
        transitive_saved = explore_transitive(data, direct_names)
        saved_relationships.extend(transitive_saved)
        if transitive_saved:
            print()
            for rel in transitive_saved:
                print(f"  Relationship saved (transitive): {rel['company_a']} ↔ {rel['company_b']} (score: {rel['score']:.2f})")
    else:
        print("No candidates found in same subsectors.")

    return {"name": name, "action": action, "competitors_found": len(saved_relationships)}


async def ingest(url: str, interactive: bool = True) -> dict:
    """Scrape, classify, save, fetch logos, and score competitors for one startup URL.

    Reused by both the CLI entrypoint below and the Story 6.1 background worker
    (graph_app.py's _ingestion_worker, itself triggered by the web search bar's
    "Add" action via /api/ingest -- no caller awaits ingest() directly anymore).
    Raises ValueError if no startup info could be extracted from the page.

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
    markdown, logo_candidates = await scrape(url)
    domain = normalize_domain(url)
    lock = _domain_locks.setdefault(domain, asyncio.Lock())
    token = INTERACTIVE_REQUEST.set(interactive)
    try:
        async with lock:
            return await asyncio.to_thread(_ingest_sync, markdown, logo_candidates, url)
    finally:
        INTERACTIVE_REQUEST.reset(token)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python main.py <url>", file=sys.stderr)
        sys.exit(1)

    try:
        asyncio.run(ingest(sys.argv[1], interactive=False))
    except ValueError as e:
        print(f"{e} Skipping.", file=sys.stderr)
        sys.exit(0)
