---
baseline_commit: ce6655d6a98e93d5359b79af0cb5c9a6389ec5af
---

# Story 1.2: Build `storage.save_quality_review()` with the `review_type`/`subject` contract

Status: done

## Story

As a Julien,
I want a single, validated write/read path to `quality_review_log`,
so that every future consumer (FR-1, FR-3) writes and queries it consistently, with no typo silently orphaning an entry.

## Acceptance Criteria

1. **Given** the `quality_review_log` table exists (Story 1.1, done), **when** `storage.save_quality_review()` is called with a `review_type` outside the known set (`taxonomy_split`, `scraping_diagnostic`), **then** the call is rejected with an explicit error (`ValueError`), not a silent write.
2. **And** for `review_type='taxonomy_split'`, `subject` must match an exact `TAXONOMY` key; for `scraping_diagnostic`, a site domain normalized via a new `storage.normalize_domain(url)` (AD-8: strip scheme, strip leading `www.`, strip trailing slash, lowercase) — `save_quality_review()` applies it internally so a caller can't forget to normalize before writing.
3. **And** `storage.normalize_domain()` is exported for other callers to reuse for lookups (not just writes) — Epic 2 (Story 2.1) and Epic 3 (Story 3.2) depend on this existing already; they must not reimplement it.
4. **And** a paired read function allows querying by `review_type` + `subject`.
5. **And** a unit test covers: rejection of an invalid `review_type`, acceptance of both known `review_type` values, and the expected `subject` format for each.

*(Not explicitly listed in epics.md but required by Architecture AD-4 — see Dev Notes → Verdict validation: `taxonomy_split`'s `verdict` must also be validated against its closed 4-value set in this same function, since AD-4 names `save_quality_review()` as the one place that enforcement lives. Implementing AC #1-2 without this would leave the system not actually working end-to-end per AD-4.)*

## Tasks / Subtasks

- [x] Task 1: Implement `normalize_domain()` (AC: #2, #3)
  - [x] Added `normalize_domain(url: str) -> str` to `storage.py`, using `urlparse().netloc` (mirrors `main.py`'s existing favicon-lookup pattern) — strips scheme, path, query string, port, and leading `www.`; lowercases
  - [x] No I/O, pure string function
  - [x] **Post-implementation fix:** the first version only regex-stripped a trailing slash and left any path intact (`normalize_domain("https://example.com/about")` incorrectly returned `"example.com/about"`) — caught when Julien asked whether this function actually covers what Epic 2/Story 2.1 and Epic 3/Story 3.2 will need. Rewritten with `urlparse().netloc` to correctly extract just the domain regardless of path/query/port. Re-verified live (see below) and covered by new tests.
- [x] Task 2: Implement the validation contract (AC: #1, #2, + verdict validation per AD-4)
  - [x] Added module-level `_KNOWN_REVIEW_TYPES` and `_TAXONOMY_SPLIT_VERDICTS` to `storage.py`
  - [x] Added `_validate_review(review_type, subject, verdict) -> str`: rejects unknown `review_type`; `taxonomy_split` requires `subject in TAXONOMY` and `verdict in _TAXONOMY_SPLIT_VERDICTS`; `scraping_diagnostic` normalizes `subject` via `normalize_domain()`, no verdict constraint
  - [x] No I/O — verified callable and tested without a Supabase connection
- [x] Task 3: Implement `save_quality_review()` and the paired read function (AC: #1, #2, #4)
  - [x] `save_quality_review()`: calls `_validate_review()` first, then inserts via `_client()` (AD-1); `source_snapshot`/`resolution`/`notes` omitted from payload when `None`; `date_diagnosed` left to the DB's `default now()`
  - [x] `get_quality_reviews(review_type, subject=None)`: filters on `review_type` (+ normalized `subject` for `scraping_diagnostic`)
  - [x] No update/resolve path implemented — confirmed out of scope, no caller exists yet
- [x] Task 4: Unit tests (AC: #5)
  - [x] Created `test_storage.py` at repo root
  - [x] `_validate_review()` covered: invalid `review_type` raises; both known types accepted; `taxonomy_split` requires exact `TAXONOMY` key + valid verdict (all 4 parametrized); `scraping_diagnostic` normalizes subject and accepts free-text verdict
  - [x] `normalize_domain()` covered: scheme/`www.`/trailing-slash/case variants, path/query/port stripping, subdomain preservation, and no-scheme input all resolve correctly
  - [x] 15/15 tests pass (`pytest test_storage.py -v`)

### Review Findings

- [x] [Review][Patch] `_validate_review` checked `subject` against `TAXONOMY`'s top-level keys (sectors), not subsector names — fixed via `_TAXONOMY_SUBSECTORS`, the same flattened-set pattern `audit_taxonomy.py` already uses. Regression test added (`test_validate_review_rejects_sector_name_as_subject`). [storage.py, `_validate_review`]
- [x] [Review][Patch] `normalize_domain` returned `""` for bare `host:port` and mishandled userinfo/IPv6 — fixed via `"://" not in url` scheme detection + `urlparse(url).hostname`. 4 new tests (host:port, userinfo, IPv6, plus existing path/query/port coverage). [storage.py, `normalize_domain`]
- [x] [Review][Patch] `_validate_review` now rejects a `scraping_diagnostic` subject that normalizes to empty (test: `test_validate_review_rejects_scraping_diagnostic_subject_that_normalizes_empty`). [storage.py, `_validate_review`]
- [x] [Review][Patch] `get_quality_reviews` now validates `review_type` and `subject` as strictly as the write path, before any DB call. 3 new tests. [storage.py, `get_quality_reviews`]
- [x] [Review][Patch] Blank `verdict` now rejected for both review types (test: `test_validate_review_rejects_blank_verdict`). [storage.py, `_validate_review`]
- [x] [Review][Patch] Story file corrected: `storage.py` gained `from urllib.parse import urlparse`, not `import re`. [this story file, File List below]
- [x] [Review][Patch] Added a comment in `_validate_review` calling out that the `taxonomy_split`/`scraping_diagnostic` normalization asymmetry is deliberate. [storage.py, `_validate_review`]
- [x] [Review][Dismiss] Read-then-write race on `(review_type, subject)` with no unique constraint — contradicts Julien's explicit Story 1.1 decision that repeated diagnoses over time are intentional history, not duplicates to prevent.
- [x] [Review][Dismiss] No update path for `updated_at` — already clarified as deliberately out of this story's scope (see Completion Notes above).
- [x] [Review][Dismiss] `save_quality_review` returns `{}` when insert response has no data — matches (exceeds) existing project rigor; no other `storage.py` function checks insert response data at all.
- [x] [Review][Dismiss] Add `isinstance` type guards on inputs — inconsistent with this codebase's convention of zero runtime type-checking anywhere else.
- [x] [Review][Dismiss] Wrap Supabase calls in try/except — inconsistent with `storage.py`'s own established pattern of letting exceptions propagate to callers.
- [x] [Review][Dismiss] Architecture-decision (AD-N) references in comments aren't traceable from `storage.py` alone — low-value polish, folded into the patch pass instead of tracked separately.

### Reopened 2026-08-11 — `normalize_domain()` edge cases found while building Story 2.1

Story 2.1's `code-review` pass surfaced two `normalize_domain()` bugs while exercising it against real `compspro` data — reopened this story (rather than patching `storage.py` from within Story 2.1, which that story's own Dev Notes explicitly forbid) since `storage.py`/`normalize_domain()` are this story's deliverable.

- [x] [Review][Patch] `normalize_domain()` mishandled protocol-relative URLs (`//example.com/path`): `"://" not in url` was `True`, so `"https://"` was prepended in front of the existing `//`, producing `"https:////example.com/path"` — a malformed URL whose `.hostname` is `None`. Fixed by detecting `url.startswith("//")` first and prepending only `"https:"` in that case. [storage.py, `normalize_domain`]
- [x] [Review][Patch] `normalize_domain()` didn't strip a trailing `.` (valid absolute-FQDN DNS notation, e.g. `"example.com."`), so it normalized to a different subject than `"example.com"` — exactly the silent-miss failure mode AD-8 says this function exists to prevent. Fixed via `.rstrip(".")` on the final netloc, alongside the existing `www.` strip. [storage.py, `normalize_domain`]
- Both fixes reproduced live before patching and re-verified live after (via `get_quality_reviews`/direct calls against the real Supabase project), plus 2 new regression tests (`test_normalize_domain_handles_protocol_relative_url`, `test_normalize_domain_strips_trailing_dot_fqdn`) — 26/26 `test_storage.py` passing, all 9 project modules still import cleanly, all previously-covered `normalize_domain` cases (userinfo, IPv6, bare host:port, subdomains, path/query/port stripping) re-verified unaffected.
- [x] [Review][Dismiss] The `_TAXONOMY_SUBSECTORS` "Uncategorized" collision (26 of 28 sectors share an identically-named `"Uncategorized"` subsector, so a `taxonomy_split` review on it can't distinguish which sector) was also flagged by Story 2.1's code review. Julien's instruction was to include it in this reopening only if it proves to be a real data bug. Investigated: `audit_taxonomy.py` (lines 71-72, 81-84) already treats `"Uncategorized"` as one global bucket across all sectors, not per-sector — the exact same flattening `_TAXONOMY_SUBSECTORS` reproduces. This is a pre-existing project convention, not a bug this story introduced; "fixing" it here would create a *new* inconsistency with `audit_taxonomy.py` rather than resolve an existing one — so per Julien's own condition, not fixed.

### Resolved 2026-08-11 — concurrent `_execute()`/cached-client changes

Two changes landed directly in `storage.py`, outside any story (Julien, in the IDE, while Story 2.1 was in progress): a module-level cached Supabase client (`_supabase` singleton in `_client()`), and a `tenacity`-retried `_execute(query)` wrapper (`@_retry` on `httpx.TransportError`) now used by every `.execute()` call site in the file, including `save_quality_review()`/`get_quality_reviews()`. Julien asked for this to be reviewed before Epic 3, specifically for risk to dedup integrity around `save_quality_review()`/`get_quality_reviews()`. Findings below, followed by the fix Julien decided on and its verification.

**1. Cached `_client()` — no issue found.** Reuses one client/connection instead of constructing a new one per call; standard practice, no data-integrity concern identified.

**2. `_execute()` retry wrapper — confirmed real risk, write path only.** Verified directly against the installed `postgrest` library (2.30.1, source read in full at `postgrest/_sync/request_builder.py`):
- `query.execute()` already calls `send_with_retry()` internally — but postgrest-py's own built-in retry is deliberately scoped to `GET`/`HEAD` + Cloudflare 503/520 only (its own docstring: *"Retries idempotent requests"*). It does not retry `POST`/`PATCH`/`DELETE` — the standard reason being that those aren't safe to blindly resend.
- The new `_execute()` wrapper retries on **any** `httpx.TransportError`, regardless of HTTP method. `httpx.RemoteProtocolError` — confirmed a `TransportError` subclass, and the exact exception named in the wrapper's own comment as its motivating case ("dropped HTTP/2 stream") — is the textbook *in-doubt write*: it can fire after the server already committed the request but before the client parses the response.
- If that happens on an INSERT (`save_quality_review`, `save_relationships`, `save_startup`'s insert branch) or UPDATE (`save_startup`'s update branch), the retry calls `query.execute()` again — confirmed via `postgrest`'s own `execute()`/`send_with_retry()` source that this sends a **second, real HTTP request**, not a cached replay.
- For `quality_review_log` specifically: no unique constraint on `(review_type, subject)` (Story 1.1/1.2's own deliberate decision — repeated diagnoses over time are intentional history, not duplicates to prevent). A retry-induced duplicate insert would land as an indistinguishable extra row.
- `get_quality_reviews()` itself (a `SELECT`) is **not** at risk — retrying a read is safe. The risk is scoped to the write path: `save_quality_review()`, plus (outside this story's own surface but sharing the same `_execute()`) `save_relationships()`/`save_startup()`.

**Severity:** low-likelihood (needs a `TransportError` landing in the narrow post-commit/pre-ack window — most transport errors happen before the server ever sees the request, where a retry is safe) but silent, and not caught by any existing safeguard. It's a real gap relative to the safety line `postgrest-py`'s own author already drew (retry idempotent verbs only) — the new wrapper crosses that line one layer up. The *consequence* (a duplicate row) is low-severity given Julien already tolerates duplicates in this table by design (Story 1.1's dismissed race-condition finding) — but the *mechanism* (a network hiccup silently minting an extra row indistinguishable from a real diagnosis) differs from an intentional re-diagnosis, and Julien flagged it as worth an explicit decision rather than inheriting that old reasoning by default.

**Options presented to Julien:**
(a) Scope `_execute()`'s retry to read-only calls only, leaving writes un-retried — matches `postgrest-py`'s own line exactly.
(b) Keep retrying writes and accept the rare duplicate-row risk as consistent with the already-accepted "no unique constraint" position.
(c) Something narrower (e.g. retry writes only for exceptions provably raised before the request reached the server) — likely not worth the added complexity for a solo tool.

**Decision: (a).** A visible failure a caller can re-run manually is cheaper than a silent duplicate that nothing would ever detect, given `quality_review_log`'s no-unique-constraint design is deliberate (history, not dedup) rather than a gap to patch with a constraint. This also removes the inconsistency with `postgrest-py`'s own retry boundary instead of working around it.

**Fix applied:** `_execute(query)` now inspects `query.request.http_method` (confirmed live: `"GET"` for `.select()`, `"POST"` for `.insert()`, `"PATCH"` for `.update()`) and only routes through the `@_retry`-wrapped path (renamed `_execute_retryable`) when the method is `GET`/`HEAD`; every other method calls `query.execute()` directly, unretried. No unique constraint added — none needed, this is a request-dispatch change only, not a schema change.

**Verification:**
- 3 new regression tests (`test_execute_retries_get_on_transient_error`, `test_execute_does_not_retry_post_on_transient_error`, `test_execute_does_not_retry_patch_on_transient_error`) using a minimal fake query object — confirm a `GET` retries through `httpx.RemoteProtocolError` and succeeds, while `POST`/`PATCH` raise immediately on the first failure with zero retries. 29/29 `test_storage.py` passing.
- Live-verified against the real `postgrest` client: `.select()` → `http_method == "GET"`, `.insert()` → `"POST"`, `.update()` → `"PATCH"` — confirms the dispatch condition matches real query objects, not just the fakes.
- Live end-to-end: a real read (`get_quality_reviews`) and a real write (`save_quality_review`) both still succeed normally against the production Supabase project after the change.
- All 9 project modules still import cleanly.

## Dev Notes

### Current live schema (source of truth — read `migrations/002_harden_quality_review_log_schema.sql`, not just `001_...`)

Story 1.1 created the base table; its own code review then applied `migrations/002_harden_quality_review_log_schema.sql`, which changed things this story must account for:
- `id`: `generated always as identity` (not `by default`) — never pass an explicit `id`.
- `review_type`, `subject`: `NOT NULL` **and** `CHECK (length(trim(...)) > 0)` — the DB now rejects blank strings too; `_validate_review()` should still reject them at the Python level for a clearer error message, don't rely on the DB error surfacing usefully to the caller.
- `source_snapshot`: `CHECK (source_snapshot IS NULL OR jsonb_typeof(source_snapshot) = 'object')` — if you pass one, it must be a JSON object, not a bare array/scalar.
- `verdict`: still plain `text`, no DB constraint (AD-4) — the 4-value `taxonomy_split` constraint is Python-only, per this story's Task 2.
- `updated_at`: nullable `timestamptz`, added but **no story yet writes to it** — see Task 3's explicit note not to build an update path here.

### Verdict validation (why it's in scope despite not being an epics.md bullet)

Architecture AD-4: *"The 4-value constraint for `review_type = 'taxonomy_split'` ... is enforced in application code (`storage.save_quality_review()`), not in the database schema."* AD-4 names this function specifically as where that enforcement lives. `scraping_diagnostic`'s verdict vocabulary is explicitly **not** constrained (still being diagnosed, per AD-4/PRD FR-1) — do not validate it.

### Testability design (why `_validate_review` is a separate function)

This project has no existing mocking convention and this is the first test in the codebase. Rather than introduce one, `_validate_review()` is deliberately pure (no `_client()` call, no network) so the unit test in AC #5 can call it directly and assert `ValueError`/return value with zero I/O. `save_quality_review()` itself (which does call the DB) is not unit-tested here — only its pre-DB validation logic is, which is what AC #5 actually asks for ("rejection of an invalid `review_type`", "the expected `subject` format" — both are `_validate_review()`'s job, not the insert's).

### Project Structure Notes

- All changes land in the existing `storage.py` (UPDATE, not NEW — read the full current file, reproduced in References below; append new functions, do not reorder or touch `get_by_subsectors`/`get_known_competitors`/`get_company`/`relationship_exists`/`save_relationships`/`save_startup`/`COMPETITOR_THRESHOLD`/`_client()`).
- New file: `test_storage.py` at repo root (flat, matches every other script in this project — no `tests/` subdirectory).
- Import `TAXONOMY` from `taxonomy.py` (existing module, already imported the same way by `extractor.py`).

### References

- [Source: `_bmad-output/planning-artifacts/prds/prd-SM Project-2026-08-09/prd.md` §4.6 FR-5, §3 Glossary] — `review_type`/`subject` contract origin, verdict vocabulary.
- [Source: `_bmad-output/planning-artifacts/architecture/architecture-SM Project-2026-08-10/ARCHITECTURE-SPINE.md` AD-1, AD-2, AD-4, AD-7, AD-8] — single access point, contract enforcement location, domain normalization rule.
- [Source: `_bmad-output/planning-artifacts/epics.md` Epic 1 / Story 1.2] — acceptance criteria this story implements.
- [Source: `_bmad-output/implementation-artifacts/1-1-create-the-quality-review-log-table-via-migration.md`] — previous story; its Dev Agent Record and Review Findings are the reason the live schema now includes `CHECK` constraints and `updated_at` beyond the original `001_...` DDL.
- [Source: `storage.py`, read in full 2026-08-10] — existing patterns this story must match: `_client()` helper, docstring style, functions returning plain `list`/`dict`/`bool`, `ValueError` as the existing project's error-signaling convention (used in `save_startup`).
- [Source: live `.venv` check, 2026-08-10] — `pytest 7.4.0` already installed (not in `requirements.txt` — that gap is a known, separately-tracked issue, not this story's job to fix).

## Dev Agent Record

### Agent Model Used

claude-sonnet-5

### Debug Log References

None — RED confirmed first (`ImportError`, functions didn't exist), then GREEN on first implementation pass (12/12 tests). Verified no regressions: `storage.py`, `main.py`, `competitor.py`, `extractor.py`, `audit_taxonomy.py`, `graph_analysis.py`, `backfill_competitors.py`, `reprocess_list.py` all still import cleanly after the change (no circular import from `storage.py` → `taxonomy.py`).

### Completion Notes List

- Added `normalize_domain()`, `_validate_review()`, `save_quality_review()`, `get_quality_reviews()` to `storage.py` — additive only, no existing function touched.
- `_validate_review()` also enforces the `taxonomy_split` verdict vocabulary (AD-4), beyond what epics.md's AC bullets literally listed — required for the system to actually work end-to-end per the Architecture spine (see story's own note above the Tasks section).
- `updated_at` deliberately left unwired — no story yet calls for updating a row after initial write.
- `test_storage.py` created (first test file in this project); tests only `_validate_review()` and `normalize_domain()` (no I/O, no mocking needed) — `save_quality_review()`/`get_quality_reviews()`'s DB calls are not unit-tested, consistent with AC #5's actual scope (rejection/acceptance/subject-format, not persistence).
- `pytest` confirmed already installed in `.venv` (7.4.0); not added to `requirements.txt` — that gap is pre-existing and out of this story's scope.
- `normalize_domain()` fixed post-implementation to correctly strip path/query/port via `urlparse().netloc` instead of a regex that left paths intact — see Task 1 note. Re-verified live and via 4 new tests.

**Clarification on `updated_at` (asked before code review, answering here for the record):** the `updated_at` **column** was already added live via `migrations/002_harden_quality_review_log_schema.sql`, applied during **Story 1.1's** code review (Story 1.1 is `done`) — it exists in the DB before this story even started. What Story 1.2 deliberately does **not** do is write any Python code path that *sets* `updated_at` — no update/resolve function exists yet in `storage.py`, because no story in Epics 1-4 currently calls for updating a row after its initial write. The column is schema-ready; nothing in this story's scope wires it up, and nothing was silently dropped.

### File List

- `storage.py` (modified — added `normalize_domain`, `_validate_review`, `save_quality_review`, `get_quality_reviews`, `_KNOWN_REVIEW_TYPES`, `_TAXONOMY_SPLIT_VERDICTS`, `_TAXONOMY_SUBSECTORS`, and `from taxonomy import TAXONOMY` / `from urllib.parse import urlparse` at the top; reopened 2026-08-11 — `normalize_domain()` patched for protocol-relative URLs and trailing-dot FQDNs; `_execute()` split into a GET/HEAD-only retryable path (`_execute_retryable`) plus a direct, unretried path for writes. Note: `storage.py` also independently gained a `tenacity`-retried `_execute()` wrapper and a cached module-level Supabase client, made directly by Julien in the IDE, not part of Story 1.2's or Story 2.1's original scope — reviewed and the retry-scoping half fixed as part of this reopening.)
- `test_storage.py` (new; reopened 2026-08-11 — 2 tests for the `normalize_domain()` fixes, 3 tests for `_execute()`'s GET/HEAD-only retry scoping)

## Change Log

- 2026-08-10: Story implemented — `storage.py` extended with the `quality_review_log` read/write contract (`normalize_domain`, `_validate_review`, `save_quality_review`, `get_quality_reviews`); `taxonomy_split` verdict vocabulary enforced per AD-4 beyond the literal epics.md AC. First unit tests in the project (`test_storage.py`). No regressions in dependent modules. All acceptance criteria met.
- 2026-08-11: Reopened after Story 2.1's `code-review` surfaced two `normalize_domain()` bugs while exercising it against real `compspro` data (protocol-relative URLs resolving to an empty domain; trailing-dot FQDNs not normalizing the same as their non-trailing-dot form). Both fixed and reproduced live before/after. A third flagged issue (`_TAXONOMY_SUBSECTORS`'s cross-sector `"Uncategorized"` collision) was investigated and dismissed — confirmed consistent with `audit_taxonomy.py`'s own pre-existing global-flattening convention, not a bug this story introduced. 2 new regression tests added (26/26 `test_storage.py` passing). All 9 project modules re-verified importing cleanly.
- 2026-08-11: Julien requested a pre-Epic-3 review of two changes he made directly in `storage.py` outside any story (cached Supabase client, `tenacity`-retried `_execute()` wrapper). Cached client: no issue. `_execute()`: confirmed a real silent-duplicate-write risk — verified against the installed `postgrest` (2.30.1) source that the library's own retry is scoped to GET/HEAD only, while the new wrapper retried any `httpx.TransportError` regardless of HTTP method, including on INSERT/UPDATE where a `RemoteProtocolError` in-doubt write could duplicate a row `quality_review_log` has no unique constraint to catch. Julien decided: scope the retry to GET/HEAD only, matching `postgrest-py`'s own boundary, and leave writes unretried (a visible failure to re-run manually beats an undetectable duplicate). Fixed via `query.request.http_method` dispatch in `_execute()`. 3 new regression tests added (29/29 `test_storage.py` passing), live-verified against real `.select()`/`.insert()`/`.update()` query objects and a real read+write round-trip against production Supabase. All 9 project modules re-verified importing cleanly.
- 2026-08-10: Pre-code-review fix — `normalize_domain()` rewritten to use `urlparse().netloc` after Julien flagged that Epic 2/Epic 3 reuse of this function hadn't been confirmed to actually work; the original regex version left URL paths intact (`.../about` wasn't stripped), which would have broken dedup/cross-check lookups across those future stories. 4 tests added for path/query/port/subdomain/no-scheme cases. 15/15 tests passing.
- 2026-08-10: Code review (Blind Hunter + Edge Case Hunter + Acceptance Auditor) — 2 critical bugs confirmed live and fixed: `_validate_review` validated `subject` against `TAXONOMY`'s sector-level keys instead of subsector names (every real FR-3 write would have been rejected), and `normalize_domain` still returned `""` for bare `host:port` input and mishandled userinfo/IPv6 (found independently by all 3 layers). Rewrote `normalize_domain` to use `urlparse().hostname` instead of hand-rolled `.netloc` parsing. 5 more patches applied (blank-verdict guard, `get_quality_reviews` validation parity, empty-subject rejection, story-file correction, asymmetry comment). 6 findings dismissed — notably the read-then-write dedup race, which contradicts Julien's explicit Story 1.1 decision that repeated diagnoses are intentional history. 9 new tests added (24/24 passing). No regressions.
