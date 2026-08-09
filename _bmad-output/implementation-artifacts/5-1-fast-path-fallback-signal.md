---
baseline_commit: ce6655d6a98e93d5359b79af0cb5c9a6389ec5af
---

# Story 5.1: Fast-path fallback signal (anti-bot detection + content completeness)

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a Julien,
I want `_fetch_light` to detect a blocked/interstitial page or JS-incomplete content and fall back to Playwright,
so that ingestion never silently saves interstitial/boilerplate text as a startup's real content, and never silently loses a JS-rendered logo or LinkedIn link the light fetch couldn't see.

## Acceptance Criteria

1. **Given** `_fetch_light`'s current signal is only "trafilatura-extracted text length ≥ 200 chars" (plus a template-placeholder check already added since this story was scoped — see Dev Notes "Current state" before starting), **when** the fetched page is actually a bot-block/consent-wall interstitial, or is JS-hydrated with real body text but a JS-injected header/logo/footer-LinkedIn-link, **then** `_fetch_light` must recognize this via a richer content-quality signal (too-short text, an abnormal text/boilerplate ratio, known interstitial keywords — the same category of heuristic `diagnose_scraping.py`'s `characterize()` already uses for `blocking_page`/`content_drowned_in_noise`, reused rather than reinvented) and return `None`, triggering the existing Playwright fallback in `scrape()`.
2. **And** a page that legitimately passes this richer check is returned as today — this story raises the bar for what counts as a successful light fetch, it does not change `_scrape_playwright`'s own already-correct anti-bot/cookie-banner handling.
3. **And** the interstitial-keyword vocabulary is shared with `diagnose_scraping.py`, not duplicated — one list, two importers.
4. **And** the noise-ratio check is adapted to `_fetch_light`'s actual output (`trafilatura.extract()` plain text), not copy-pasted from `characterize()`'s markdown-link-density clause, which Story 5.3 has already identified as unreachable against plain text.
5. **And** whether a genuinely blocked/JS-incomplete `_fetch_light` result should itself be logged to `quality_review_log` is decided by this story, not left open (see Dev Notes "Scope decision: no `quality_review_log` write from `scrape()`").

## Tasks / Subtasks

- [x] Task 0: **Read current state before starting** (blocks every other task)
  - [x] Read `main.py`'s `_parse_light_fetch` (L201-231) and `_fetch_light` (L234-249) in full. Two of the three problems this story's epics.md text described have already been partially addressed since 2026-08-12 (see Dev Notes "Current state") — do not re-implement what already exists; confirm the actual remaining gap before writing code. Confirmed: code matched the story's documented current state exactly.
- [x] Task 1: Share the interstitial-keyword vocabulary between `main.py` and `diagnose_scraping.py` (AC #1, #3)
  - [x] Move `_BLOCKING_MARKERS` (currently `diagnose_scraping.py:23-31`) into `main.py` as a module-level constant named `BLOCKING_MARKERS` (no leading underscore — it's now imported by another module, matching this file's existing public/private naming split).
  - [x] Update `diagnose_scraping.py` to `from main import BLOCKING_MARKERS` (it already imports `scrape` from `main`, so this adds no new import edge) and drop its own local tuple. Do not change `characterize()`'s behavior or return values.
- [x] Task 2: Add interstitial-keyword detection to `_parse_light_fetch` (AC #1)
  - [x] After the existing template-placeholder check (`main.py:218-220`) and before the `return text, _logo_candidates_from_html(...)` line, add: if `text.lower()` contains any marker from `BLOCKING_MARKERS`, `print("[_fetch_light] Blocking-page marker detected, falling back to Playwright")` and `return None` — same style as the existing template-placeholder branch immediately above it.
- [x] Task 3: Add a noise-ratio check to `_parse_light_fetch` (AC #1, #4)
  - [x] Add a new check, adapted from `characterize()`'s noise-ratio logic (`diagnose_scraping.py:69-76`) but **using only the short-line-density component** (`len(line) < 40`), deliberately dropping the markdown-link-bracket clause (`line.startswith("[") and "](" in line") — `_parse_light_fetch` calls `trafilatura.extract()` directly and never produces markdown link syntax, the same reason Story 5.3 flags that clause as already unreachable in `diagnose_scraping.py` itself. Reusing the dead half here would just plant the identical bug a second place.
  - [x] Threshold: reuse `0.85` (the ratio) as-is — same bar as `characterize()`'s `content_drowned_in_noise`, no new constant to justify. Skip the check entirely if `text` has zero non-blank lines (avoid a division by zero on pathological input).
  - [x] On trip, `print("[_fetch_light] High boilerplate/noise ratio detected, falling back to Playwright")` and `return None`.
- [x] Task 4: Verify against synthetic fixtures (no live network calls needed — `_parse_light_fetch` is a pure function of `(html, base_url)`)
  - [x] Confirm a synthetic HTML string whose `trafilatura.extract()` output contains a `BLOCKING_MARKERS` phrase (e.g. `"Please verify you are human before continuing."`, ≥200 chars of padding so the length check alone doesn't already catch it) returns `None`.
  - [x] Confirm a synthetic HTML string with ≥200 chars of short, boilerplate-style lines (nav/footer-like, each <40 chars, >85% of lines) and no blocking marker and no template syntax returns `None`.
  - [x] Confirm a known-good synthetic fixture (≥200 chars of normal prose, no marker, no template syntax, noise ratio well under 0.85) still returns `(text, logo_candidates)` as today — this is AC #2's regression guard.
  - [x] Confirm `diagnose_scraping.py` still imports and runs cleanly after the `BLOCKING_MARKERS` move (`python -c "import diagnose_scraping"` at minimum; `characterize()`'s existing behavior on its own known blocking-phrase input must be unchanged).

## Dev Notes

### Current state — read before writing any code

The epics.md AC text for this story was written 2026-08-12 and describes `_fetch_light` as having only a `< 200 chars` signal. **That is no longer accurate.** Since then, `_parse_light_fetch` (`main.py:201-231`) has already gained a second check not mentioned in the original story text:

- `main.py:198,218-220` — `_TEMPLATE_PLACEHOLDER_RE` (`\{\{[^}]+\}\}|\{%[^%]+%\}`) catches unrendered Vue/Angular/Handlebars/Jinja/Django template syntax surviving into the extracted text — this already closes part of the "JS-hydrated with real body text but incomplete/misleading content" gap the original AC described (confirmed live against a real site, per the comment at `main.py:193-197`).
- `main.py:227-229` and `main.py:80-114` — LinkedIn URL and logo candidates are already recovered from **raw HTML** (`_linkedin_url_from_html`, `_logo_candidates_from_html`) rather than from trafilatura's extracted text, and prepended/attached before the fallback decision. This already covers the case where a link/logo exists as a plain `<a href>`/`<img>`/`<link>` tag in the raw HTML but gets stripped by trafilatura's boilerplate removal.

**What is genuinely still missing** (the real, narrower scope of this story): raw-HTML-tag recovery only works if the link/logo tag is present in the *unrendered* HTML at all. A page whose header/footer/logo is injected entirely by client-side JS after hydration has no such tag in the raw HTML — `_linkedin_url_from_html`/`_logo_candidates_from_html` will find nothing, `trafilatura.extract()` may still return ≥200 chars of real surrounding static content with no template placeholders, and today's `_parse_light_fetch` returns that content as a *successful* light fetch even though the logo/LinkedIn link were silently lost. Symmetrically, a bot-block/consent-wall interstitial with ≥200 chars of generic "please verify you are human" boilerplate (no template syntax) also passes unfiltered today and gets treated as real page content.

This story closes exactly that remaining gap: interstitial-keyword detection (Task 2) and a noise-ratio check (Task 3), both operating on the already-extracted `text` inside `_parse_light_fetch`, in the same place the template check already lives. Do not re-touch the template-placeholder logic, the LinkedIn/logo recovery, or the `< 200 chars` check — they are correct and out of this story's scope.

### Scope decision: no `quality_review_log` write from `scrape()`

Epics.md left open whether a genuinely blocked/JS-incomplete `_fetch_light` result should itself be logged via `storage.save_quality_review(review_type='scraping_diagnostic', ...)`. Resolved here: **no.** `scrape()`/`_fetch_light` stays a pure operational fallback path with no `quality_review_log` write, for two reasons:
1. AD-5 (`ARCHITECTURE-SPINE.md`) deliberately scopes `scraping_diagnostic` writes to `diagnose_scraping.py` sampling already-known `compspro` sites — not every ad hoc `scrape()` call across the whole ingestion pipeline (which runs on arbitrary new URLs during `/api/ingest`, not just the diagnostic sample).
2. The existing `< 200 chars` and (now) template-placeholder fallback triggers already behave this way — silently triggering Playwright, no log write — and this story is extending that same signal category, not introducing a new one with different semantics.

If a website's `_fetch_light` result keeps tripping these new checks, `diagnose_scraping.py`'s own sampling run will independently characterize and log it the next time it's sampled — no gap in the eventual diagnostic corpus, just not written from this code path.

### Architecture compliance

- AD-1 (`storage.py` is the sole Supabase access point): not implicated — this story touches no Supabase calls at all, pure HTML/text parsing.
- This story is Epic 5 (post-hoc hardening from the 2026-08-12 code review), not one of FR-1 through FR-5 — it isn't bound by the architecture spine's AD-1..AD-8 by name, but the "reuse or align with `diagnose_scraping.py`'s vocabulary" instruction in the AC is this story's own equivalent constraint, and Task 1 satisfies it directly (shared constant, not a parallel copy) — consistent with the project's general "common logic factored, not duplicated" convention (`storage.normalize_domain`/AD-8 is the existing precedent for this pattern).

### Library/framework requirements

No new dependency. Uses only what `main.py` already imports (`re`, `trafilatura` indirectly via existing extraction). No `requirements.txt` change.

### Coding conventions to match (from `main.py`/`diagnose_scraping.py`)

- Type hints everywhere, PEP 604 union syntax (`str | None`, not `Optional[str]`).
- No `logging` module in this file — plain `print(...)`, bracket-tagged by function, e.g. `print("[_fetch_light] ...")` (see `main.py:219` for the exact existing style to match).
- Module-level constants immediately above the function that uses them, with an explanatory comment (see `_LIGHT_FETCH_MIN_CHARS` at `main.py:189-191`, `_TEMPLATE_PLACEHOLDER_RE` at `main.py:193-198`).
- Narrow `try/except` around exactly the call that can fail — don't wrap the new checks in exception handling, they operate on a plain `str` and can't raise.

### Testing requirements

No pytest file for this story. Project-wide convention (confirmed via `ARCHITECTURE-SPINE.md` Deferred section and every Epic 1-4 story's Dev Notes): no test suite exists for this codebase; `storage.save_quality_review()`'s unit test (Story 1.2) is the sole, explicitly scoped exception, because it's the single validated write gate every other story writes through. `_parse_light_fetch`'s new checks don't meet that bar — verify via Task 4's synthetic-fixture checks (ad hoc script or REPL, matching how Story 4.2 verified `recalibrate_competitors.py`'s core logic against fake data without adding a pytest file for it), not a new `test_main.py`.

`_parse_light_fetch(html, base_url)` takes plain strings and does no I/O, so all of Task 4 can run with zero network calls or Playwright launches — no live site needed to verify this story.

### Project Structure Notes

- **UPDATE** (not new) files, read in full before editing, only the specific lines named in Tasks 1-3 touched: `main.py` (add `BLOCKING_MARKERS` constant, add two new early-return checks inside `_parse_light_fetch`), `diagnose_scraping.py` (replace local `_BLOCKING_MARKERS` definition with an import from `main`).
- No new files.
- Do not modify: `_scrape_playwright`, `_fetch_light`'s own signature/HTTP-error handling, `_linkedin_url_from_html`, `_logo_candidates_from_html`, `_favicon_url_from_html`, `characterize()`'s own logic in `diagnose_scraping.py` (only its marker-list *source* changes, not its behavior), `storage.py`.

### References

- [Source: `_bmad-output/planning-artifacts/epics.md` Epic 5 / Story 5.1] — original acceptance-criteria text this story implements; scope narrowed per "Current state" above based on code that changed after that text was written.
- [Source: `_bmad-output/planning-artifacts/architecture/architecture-SM Project-2026-08-10/ARCHITECTURE-SPINE.md` AD-5] — basis for the "no `quality_review_log` write from `scrape()`" scope decision.
- [Source: `main.py`, read in full 2026-08-16] — `_attr` L32-45, `_linkedin_url_from_html` L48-59, `_logo_candidates_from_html` L80-114, `_LIGHT_FETCH_MIN_CHARS`/`_TEMPLATE_PLACEHOLDER_RE` L189-198, `_parse_light_fetch` L201-231, `_fetch_light` L234-249, `scrape` L252-261, `_scrape_playwright` L264-312.
- [Source: `diagnose_scraping.py`, read in full 2026-08-16] — `_BLOCKING_MARKERS` L23-31, `_MIN_CONTENT_LENGTH` L34, `characterize()` L51-78 (marker-loop L62-64, length check L66-67, noise-ratio check L69-76).
- [Source: `storage.py`] — `save_quality_review()` L152-176, `_KNOWN_REVIEW_TYPES` L61 (`scraping_diagnostic` already present, confirming no new review-type would be needed even if the scope decision above had gone the other way).
- [Source: `test_storage.py`] — sole existing test file, pytest/plain-assert/section-comment style referenced for Task 4's ad hoc verification approach, though no new test file is being added by this story.
- [Source: `_bmad-output/implementation-artifacts/4-2-...md`] — precedent for verifying new logic against fake/synthetic fixtures rather than live data when a pytest file isn't warranted, and for a story explicitly resolving an epics.md-flagged open question in its own Dev Notes rather than leaving it for the dev agent to guess.
- [Source: `_bmad-output/planning-artifacts/epics.md` Epic 5 / Story 5.3] — origin of the "markdown-link noise clause is unreachable against trafilatura's plain-text output" finding this story's Task 3 explicitly avoids re-introducing.

## Dev Agent Record

### Agent Model Used

claude-sonnet-5

### Debug Log References

- No pytest file for this story, per its own Testing Requirements (no test suite convention, `save_quality_review()`'s unit test remains the sole exception). Verified via ad hoc synthetic fixtures against `_parse_light_fetch(html, base_url)` directly (pure function, no network/Playwright needed):
  - Blocking-marker fixture (≥200 chars, contains `"verify you are human"`, no template syntax) → `None`, printed `"[_fetch_light] Blocking-page marker detected, falling back to Playwright"`.
  - High-noise fixture (60 short `<p>` block lines, each <40 chars) → `None`, printed `"[_fetch_light] High boilerplate/noise ratio detected, falling back to Playwright"`.
  - Known-good fixture (≥200 chars of normal prose, no marker, no template syntax, low noise ratio) → returned `(text, logo_candidates)` unchanged, confirming AC #2's regression guard.
- Confirmed `diagnose_scraping.py` imports cleanly after the `BLOCKING_MARKERS` move, `diagnose_scraping.BLOCKING_MARKERS is main.BLOCKING_MARKERS` (same object, not a copy), and `characterize()`'s own behavior on a known blocking phrase and a known short-text case is unchanged (`blocking_page` / `incomplete_content` verdicts, same as before the move).
- Regression sweep: all 14 top-level project modules (`main`, `storage`, `taxonomy`, `extractor`, `competitor`, `graph_analysis`, `graph_app`, `audit_taxonomy`, `competitor_validator`, `diagnose_scraping`, `log_review`, `readiness_check`, `recalibrate_competitors`, `reprocess_list`) import cleanly with no errors. `pytest test_storage.py` — 29/29 still passing, untouched (this story doesn't touch `storage.py`).
- **Post-review fix** (`code-review` finding, `main.py:250`): re-ran the same 3 synthetic fixtures (blocking-marker, high-noise, known-good) against `_parse_light_fetch` after the noise-ratio logic was refactored into a shared `_noise_ratio()` — all 3 still pass, same printed messages. Added a 4th check confirming `_noise_ratio("") == 0.0` (zero-lines guard preserved). Confirmed `characterize()`'s `content_drowned_in_noise` verdict still fires with the identical message format (`noise_ratio={:.2f} over {n} lines (length={length})`) on a fresh short-line fixture, and its `blocking_page`/`incomplete_content`/`ok` verdicts are unchanged. Full regression sweep (14 modules + `test_storage.py`) re-run clean after the fix.

### Completion Notes List

- Task 0 confirmed the story's "Current state" section was accurate: `_parse_light_fetch` had exactly the `< 200 chars` length check and the template-placeholder check, nothing else — the interstitial-keyword and noise-ratio gaps were genuinely open before this story.
- Task 1: `_BLOCKING_MARKERS` moved from `diagnose_scraping.py` to `main.py` as public `BLOCKING_MARKERS`, placed next to `_LIGHT_FETCH_MIN_CHARS`. `diagnose_scraping.py` now imports it (`from main import BLOCKING_MARKERS, scrape`) instead of defining its own copy; `characterize()`'s marker loop updated to reference the imported name, no behavior change.
- Tasks 2-3: two new early-return checks added inside `_parse_light_fetch`, directly after the existing template-placeholder check and before the LinkedIn/logo recovery block — interstitial-keyword match (reusing the now-shared `BLOCKING_MARKERS`) and a short-line noise-ratio check (`_NOISE_RATIO_THRESHOLD = 0.85`, short-line-density only, no markdown-link clause, per AC #4). Both follow the existing `print("[_fetch_light] ...")` + `return None` style of the check immediately above them.
- No change to `_scrape_playwright`, `_fetch_light`'s own signature/HTTP-error handling, `_linkedin_url_from_html`, `_logo_candidates_from_html`, `_favicon_url_from_html`, or `storage.py` — all out of this story's scope per its Project Structure Notes.
- No `requirements.txt` change, no new dependency.
- The "no `quality_review_log` write from `scrape()`" scope decision (Dev Notes) required no code — it's a decision not to add a write path, confirmed by not adding one.
- **Post-review fix**: a `code-review` finding on this story's own diff noted that the noise-ratio check added to `_parse_light_fetch` (Task 3) duplicated `characterize()`'s noise-ratio shape/threshold as a second hand-written copy, inconsistent with `BLOCKING_MARKERS` (which this story correctly shared). Fixed by extracting the short-line-density computation into a new shared `main._noise_ratio(text) -> float` (same no-markdown-link-clause scope as before, same 0.85 threshold, same zero-lines guard) that both `_parse_light_fetch` and `diagnose_scraping.characterize()` now call — `diagnose_scraping.py` imports it alongside `BLOCKING_MARKERS`. `characterize()`'s line count for its notes message is still computed locally (a one-line count, not classification logic) so its `content_drowned_in_noise` message format is unchanged.

### File List

- `main.py` (modified — added `BLOCKING_MARKERS` constant, `_NOISE_RATIO_THRESHOLD` constant, and shared `_noise_ratio()` helper; added interstitial-keyword and noise-ratio checks inside `_parse_light_fetch`)
- `diagnose_scraping.py` (modified — `_BLOCKING_MARKERS` replaced with `from main import BLOCKING_MARKERS`; `characterize()`'s marker loop updated to use the imported name; `characterize()`'s noise-ratio computation now calls `main._noise_ratio()` instead of its own inline calculation)

## Change Log

- 2026-08-16: Story implemented. Confirmed the story's "current state" analysis was accurate before starting (Task 0). Moved the bot-block/consent-wall keyword list (`BLOCKING_MARKERS`) from `diagnose_scraping.py` into `main.py` as a shared constant, with `diagnose_scraping.py` now importing it instead of duplicating it. Added two new early-return checks to `_parse_light_fetch` — an interstitial-keyword match and a short-line noise-ratio check (adapted from `characterize()`'s `content_drowned_in_noise` heuristic, deliberately dropping its markdown-link clause since trafilatura's plain-text output never produces markdown link syntax, per Story 5.3's own finding) — both triggering the existing Playwright fallback in `scrape()`. Resolved the epics.md open question on logging to `quality_review_log` from this path: decided against it (Dev Notes), no code added. Verified via synthetic HTML fixtures against the pure `_parse_light_fetch(html, base_url)` function (no pytest file, per this story's explicit no-test-suite convention) — blocking-marker page, high-noise page, and a known-good regression page all behaved as specified. No regressions: all 14 project modules import cleanly, `test_storage.py` 29/29 still passing. All 5 acceptance criteria met.
- 2026-08-16: Code review (`code-review` skill) fix. Finding: the noise-ratio check added above duplicated `characterize()`'s heuristic instead of being factored into one shared function, inconsistent with `BLOCKING_MARKERS`. Fixed by extracting the short-line-density computation into a new shared `main._noise_ratio(text) -> float`; `_parse_light_fetch` and `diagnose_scraping.characterize()` both now call it instead of each having their own inline copy. `characterize()`'s notes-message line count is still computed locally (a plain count, not classification logic), so its `content_drowned_in_noise` output format is unchanged. Re-verified all 3 original synthetic fixtures plus a new zero-lines guard check and a `characterize()` regression check on its own `content_drowned_in_noise` case — all pass, no regressions (14 modules import cleanly, `test_storage.py` 29/29).
