---
baseline_commit: ce6655d6a98e93d5359b79af0cb5c9a6389ec5af
---

# Story 5.3: Ingestion pipeline cleanup

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a Julien,
I want the retry-config duplication and the `quality_review_log` read-path duplication cleaned up,
so that the codebase doesn't keep drifting the way `competitor_validator.py`'s retry config already has, and every diagnostic heuristic that's supposed to run actually can.

## Acceptance Criteria

1. **Given** `storage.py`, `competitor.py`, `extractor.py`, and `competitor_validator.py` each hand-roll their own near-identical `tenacity` retry config for the same "is this a transient/retryable error" concept — and `competitor_validator.py`'s was already left un-widened (and even missing the `json.JSONDecodeError` branch) when the other two Mistral-backed retries were tuned together, proving the copy-paste approach drifts — **when** this story lands, **then** the retry-predicate/backoff construction is factored into one shared module all four call sites import, consistent with this project's existing "common logic factored, not duplicated" convention (e.g. `storage.normalize_domain()`, AD-8; `storage.RETRY_INTERACTIVE_WAIT`/`_STOP`, Story 5.2).
2. **And** `storage.get_quality_reviews()` no longer re-implements the `review_type`/`subject` validation branching `_validate_review()` already encodes as two independently-maintained copies of the same contract — the shared `review_type`+`subject` normalization is extracted into one helper both the write path (`save_quality_review()` → `_validate_review()`) and the read path (`get_quality_reviews()`) call, with only the write-only verdict check remaining separate.
3. **And** `diagnose_scraping.py`'s `content_drowned_in_noise` heuristic signals characteristics of trafilatura's actual plain-text output instead of markdown-link density, restoring FR-1's diagnostic coverage for this failure category — **already done, verify only** (see Dev Notes "Current state" — Story 5.1's own post-review fix already extracted this into `main._noise_ratio()`, dropping the markdown-link clause; no further code change expected here).

## Tasks / Subtasks

- [ ] Task 0: **Read current state before starting** (blocks every other task)
  - [ ] Read `storage.py`'s `_retry`/`_execute_retryable`/`_execute` (L53-80), `_validate_review`/`save_quality_review`/`get_quality_reviews` (L142-220ish), `competitor.py`'s `_is_retryable`/`_retry` (L29-44), `extractor.py`'s `_is_retryable`/inline `@retry(...)` (L18-40), and `competitor_validator.py`'s `_is_retryable`/`@retry(...)` (L18-30) in full.
  - [ ] Read `diagnose_scraping.py`'s `characterize()` (current `_noise_ratio()`-based version) and confirm AC #3 is already satisfied — do not re-implement it.
- [ ] Task 1: Create the shared `retry.py` module (AC #1)
  - [ ] New flat module at repo root: `retry.py` — matches this project's "one flat script/module per concern, no `src/` nesting" convention (same tier as `taxonomy.py`, a leaf module with no dependency on any of the four callers, importable by all of them without creating a cycle).
  - [ ] `storage.py` is deliberately **not** the new home this time (unlike `INTERACTIVE_REQUEST`/`RETRY_INTERACTIVE_WAIT`/`_STOP` in Story 5.2): `storage.py` is itself one of the four call sites that needs this module, so it can't also *be* the shared module without importing from itself. This is the concrete signal that a genuinely new module is warranted here, not a repeat of Story 5.2's "extend storage.py" pattern.
  - [ ] Add `is_mistral_retryable(exc: BaseException) -> bool`: the shared predicate for every Mistral-backed retry (`competitor.py`, `extractor.py`, `competitor_validator.py`) — `isinstance(exc, httpx.TransportError)`, `isinstance(exc, json.JSONDecodeError)`, or `isinstance(exc, SDKError) and any(code in str(exc) for code in ("429", "503", "529"))`. This is `competitor.py`'s/`extractor.py`'s existing (already-identical) predicate, moved here — `competitor_validator.py`'s own predicate is currently missing the `json.JSONDecodeError` branch (verify this live before writing the module: read `competitor_validator.py:18-21`); using the shared predicate closes that gap for `competitor_validator.py` rather than preserving a third, slightly different variant. Document this as a deliberate, disclosed behavior change (see Dev Notes "Scope decision" below) — small and in the direction the AC itself is pointing at (stop the drift), not something to hide.
  - [ ] Add `build_retry(predicate, *, wait_multiplier, wait_min, wait_max, stop_attempts, reraise=True)`: constructs and returns a `tenacity` retry decorator (`retry(retry=retry_if_exception(predicate), wait=wait_exponential(multiplier=wait_multiplier, min=wait_min, max=wait_max), stop=stop_after_attempt(stop_attempts) if stop_attempts is not None else omitted, reraise=reraise)`). `stop_attempts=None` means unbounded — tenacity's own behavior when no `stop=` is passed — making an unbounded retry an explicit, visible parameter at the call site instead of a silent omission (exactly `competitor_validator.py`'s current, easy-to-miss state).
  - [ ] This factors the **construction pattern**, not a single shared policy value — `storage.py`'s postgrest/Supabase failures and the three Mistral-backed retries are genuinely different failure classes with genuinely different backoff numbers; each call site still supplies its own predicate and numbers to `build_retry()`.
- [ ] Task 2: Wire `storage.py` onto the shared module (AC #1)
  - [ ] Import `build_retry` from `retry` in `storage.py`.
  - [ ] Replace `storage.py`'s own `_retry = retry(retry=retry_if_exception(lambda exc: isinstance(exc, httpx.TransportError)), wait=wait_exponential(multiplier=1, min=2, max=20), stop=stop_after_attempt(5), reraise=True)` (`storage.py:53-58`) with `_retry = build_retry(lambda exc: isinstance(exc, httpx.TransportError), wait_multiplier=1, wait_min=2, wait_max=20, stop_attempts=5)`. Keep the existing large explanatory comment above it (`storage.py:43-52`) — the *why* doesn't change, only the construction call.
  - [ ] Remove `storage.py`'s now-unused direct imports of `retry`/`retry_if_exception` from `tenacity` (`storage.py:9`) — `wait_exponential`/`stop_after_attempt` stay imported (still used directly for `RETRY_INTERACTIVE_WAIT`/`RETRY_INTERACTIVE_STOP`, Story 5.2). Confirm via grep that neither `retry` nor `retry_if_exception` is referenced anywhere else in the file before removing.
- [ ] Task 3: Wire `competitor.py` onto the shared module (AC #1)
  - [ ] Import `build_retry, is_mistral_retryable` from `retry`.
  - [ ] Remove the local `_is_retryable` function (`competitor.py:29-33ish`) — replaced by the shared `is_mistral_retryable`.
  - [ ] Replace `_retry = retry(retry=retry_if_exception(_is_retryable), wait=wait_exponential(multiplier=2, min=4, max=60), stop=stop_after_attempt(7), reraise=True)` with `_retry = build_retry(is_mistral_retryable, wait_multiplier=2, wait_min=4, wait_max=60, stop_attempts=7)`. Numeric values unchanged.
  - [ ] Remove now-unused direct `tenacity` imports (`retry`, `stop_after_attempt`, `wait_exponential`, `retry_if_exception`) — confirm via grep neither is referenced elsewhere in the file (the interactive variant already only references pre-built `storage.RETRY_INTERACTIVE_WAIT`/`_STOP` objects, no raw tenacity construction needed here after this change).
- [ ] Task 4: Wire `extractor.py` onto the shared module (AC #1)
  - [ ] Same pattern as Task 3: import `build_retry, is_mistral_retryable` from `retry`; remove the local `_is_retryable`; replace the inline `@retry(retry=retry_if_exception(_is_retryable), wait=wait_exponential(multiplier=2, min=4, max=60), stop=stop_after_attempt(7), reraise=True)` decorator on `_chat_complete_core` with a named `_retry = build_retry(is_mistral_retryable, wait_multiplier=2, wait_min=4, wait_max=60, stop_attempts=7)` applied as `@_retry`. Numeric values unchanged.
  - [ ] Remove now-unused direct `tenacity` imports, same check as Task 3.
- [ ] Task 5: Wire `competitor_validator.py` onto the shared module (AC #1)
  - [ ] Import `build_retry, is_mistral_retryable` from `retry`.
  - [ ] Remove the local `_is_retryable` function (`competitor_validator.py:18-21`).
  - [ ] Replace `@retry(retry=retry_if_exception(_is_retryable), wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)` on `_chat_complete` (`competitor_validator.py:24-30`) with a named `_retry = build_retry(is_mistral_retryable, wait_multiplier=1, wait_min=2, wait_max=30, stop_attempts=None)` applied as `@_retry`. **Numeric values (1/2/30) and the unbounded retry ceiling are deliberately preserved as-is** — this story fixes the *duplication risk*, not the *policy* (whether `competitor_validator.py`'s retries should be bounded like the other two is a separate decision for Julien to make explicitly, same pattern as Story 4.2 leaving `COMPETITOR_THRESHOLD`'s exact value to him). The only behavior change here is the predicate gaining the `json.JSONDecodeError` branch (Task 1's disclosed fix).
  - [ ] Remove now-unused direct `tenacity` imports, same check as Task 3.
- [ ] Task 6: Extract shared `review_type`/`subject` normalization in `storage.py` (AC #2)
  - [ ] Add `_normalize_subject(review_type: str, subject: str) -> str`, containing exactly the `review_type`/`subject` branching logic currently duplicated between `_validate_review()` (`storage.py:148-149,154-159,164-168,170-174`) and `get_quality_reviews()` (`storage.py:209-...`): unknown-`review_type` check, `taxonomy_split` → exact-subsector check (no normalization), `redundant_uncategorized_cleanup`/`empty_subsectors_backfill` → non-blank + `.strip()` (no normalization), else (`scraping_diagnostic`) → `normalize_domain()` + non-empty check. Returns the normalized subject; raises `ValueError` on any violation — identical error conditions and messages to today's `_validate_review()` (byte-for-byte, since that's the copy being kept as the canonical one).
  - [ ] Rewrite `_validate_review(review_type, subject, verdict)` to: check `verdict` non-blank first (unchanged), call `subject = _normalize_subject(review_type, subject)`, then (only for `taxonomy_split`) check `verdict in _TAXONOMY_SPLIT_VERDICTS`, return `subject`. This is the "verdict check stays separate, write-only" part of AC #2.
  - [ ] Rewrite `get_quality_reviews(review_type, subject=None)` to: keep its own top-level `if review_type not in _KNOWN_REVIEW_TYPES: raise` (still needed for the `subject is None` case, where `_normalize_subject` is never called), then `if subject is not None: subject = _normalize_subject(review_type, subject)` — replacing the current `if review_type == "taxonomy_split": ... else: subject = normalize_domain(subject) ...` branch entirely. **This is also the fix for the previously-flagged bug**: today, `get_quality_reviews("redundant_uncategorized_cleanup", subject="Maisa")` incorrectly runs `"Maisa"` through `normalize_domain()` (the `else` branch doesn't special-case the two newer review types the way `_validate_review()`'s write path does) and returns `""` → raises, or silently queries the wrong subject — after this change it correctly goes through `_normalize_subject`'s matching branch and stays `"Maisa"`.
  - [ ] Do not change `save_quality_review()`'s or `get_quality_reviews()`'s own signatures, return shapes, or any other behavior beyond the subject-normalization fix.
- [ ] Task 7: Verify AC #3 is already satisfied (no code change expected)
  - [ ] Confirm `diagnose_scraping.py`'s `characterize()` calls `main._noise_ratio(markdown)` for its `content_drowned_in_noise` check (not a local, markdown-link-aware calculation) — this was landed as part of Story 5.1's own post-review fix (2026-08-16), before this story existed. Re-read `diagnose_scraping.py` and `main.py`'s `_noise_ratio()` to confirm no drift since then.
  - [ ] If (and only if) the code has since regressed back to a local/markdown-aware calculation, restore the shared `main._noise_ratio()` call — but this is not expected to be needed.
- [ ] Task 8: Add regression tests for the `get_quality_reviews()` fix (extends `test_storage.py`, Story 1.2's established exception to the no-test-suite convention)
  - [ ] Add `from storage import _normalize_subject` to `test_storage.py`'s existing `storage` import line.
  - [ ] Add `test_normalize_subject_leaves_redundant_uncategorized_cleanup_subject_unmangled()`: `_normalize_subject("redundant_uncategorized_cleanup", "Maisa") == "Maisa"` (previously, going through the old `get_quality_reviews()` path, this would have been wrongly run through `normalize_domain()`).
  - [ ] Add `test_normalize_subject_leaves_empty_subsectors_backfill_subject_unmangled()`: same shape for `"empty_subsectors_backfill"`.
  - [ ] Add `test_get_quality_reviews_accepts_redundant_uncategorized_cleanup_subject()`: confirms `get_quality_reviews("redundant_uncategorized_cleanup", subject="Maisa")` no longer raises at the validation stage — this only tests the validation path (it will still attempt a real Supabase call after passing validation; if the test environment lacks live credentials, assert via `_normalize_subject` directly instead, matching the "no I/O" testability note already on `_validate_review`'s own docstring, or catch-and-inspect that any raised error is a Supabase/connection error, not the `ValueError` this story fixes — confirm which approach `test_storage.py`'s existing DB-touching tests, if any, already use and match that pattern rather than inventing a new one).
- [ ] Task 9: Regression sweep
  - [ ] All project modules import cleanly (`main`, `storage`, `taxonomy`, `extractor`, `competitor`, `graph_analysis`, `graph_app`, `audit_taxonomy`, `competitor_validator`, `diagnose_scraping`, `log_review`, `readiness_check`, `recalibrate_competitors`, `reprocess_list`, `backfill_competitors`, and the new `retry`).
  - [ ] `pytest test_storage.py` — all existing tests plus Task 8's new ones pass. Confirm none of the existing `_validate_review`/`get_quality_reviews` tests needed their assertions changed (they use bare `pytest.raises(ValueError)`, not message-text matching, so the internal restructuring in Task 6 should not require touching any existing test).
  - [ ] Confirm `competitor._chat_json_core.retry.stop.max_attempt_number == 7`, `extractor._chat_complete_core.retry.stop.max_attempt_number == 7`, and `competitor_validator._chat_complete.retry.stop` is `None`-equivalent (unbounded) — i.e. the refactor preserved each site's exact retry ceiling, not just "some ceiling."

## Dev Notes

### Current state — read before writing any code

This story's epics.md text was written 2026-08-12, before Stories 5.1 and 5.2 landed. Two things have changed since:

- **AC #3 is already done.** Story 5.1's own post-review fix (2026-08-16, same day as this story's siblings) extracted `diagnose_scraping.py`'s noise-ratio logic into `main._noise_ratio()` — dropping the markdown-link-bracket clause exactly as this AC describes, and sharing it with `_fetch_light`'s own noise check. `characterize()`'s `content_drowned_in_noise` branch (`diagnose_scraping.py`, current version) already reads `noise_ratio = _noise_ratio(markdown)`, not a local markdown-aware calculation. Task 7 verifies this holds; no new code is expected for AC #3.
- **Part of AC #1 is already done.** Story 5.2 factored the *interactive* Mistral retry/timeout constants (`RETRY_INTERACTIVE_WAIT`, `RETRY_INTERACTIVE_STOP`, `BATCH_TIMEOUT_MS`, `INTERACTIVE_TIMEOUT_MS`) into `storage.py`, shared by `competitor.py`/`extractor.py`. What Story 5.2 explicitly did **not** touch (per its own Project Structure Notes): the *batch*-side `_retry`/`@retry(...)` decorators in `competitor.py`/`extractor.py`, `storage.py`'s own postgrest `_retry`, and `competitor_validator.py`'s retry entirely — all four are still hand-rolled today, which is exactly this story's remaining scope.

`competitor_validator.py`'s retry (`competitor_validator.py:18-30`) is the clearest evidence of drift the AC cites: `wait_exponential(multiplier=1, min=2, max=30)`, **no `stop=`** (unbounded), and a predicate missing the `json.JSONDecodeError` branch competitor.py's/extractor.py's identical predicates already have — confirm these exact values live before writing `retry.py`, since the module's `is_mistral_retryable`/`build_retry(..., stop_attempts=None)` design depends on this being accurate.

### Scope decision: fix the predicate gap, preserve the backoff numbers

Task 5 gives `competitor_validator.py` the shared `is_mistral_retryable` predicate (closing its `json.JSONDecodeError` gap) but keeps its exact backoff numbers (`multiplier=1, min=2, max=30`) and its unbounded retry ceiling unchanged. This is a deliberate split: the *predicate* gap is a straightforward correctness fix directly inside this story's "stop the drift" mandate (AD-8-style: one canonical definition of "is this retryable," not three slowly-diverging copies). Whether `competitor_validator.py`'s retry ceiling should also be *bounded* to match the other two (or given its own considered value) is a policy decision about a manually-run QA script's behavior under sustained failure — not something this story's AC asks for, and not something a dev agent should decide unilaterally. If Julien wants that changed, it's a one-line follow-up (`stop_attempts=None` → a number) once `retry.py` exists, precisely because the factoring this story does makes that follow-up trivial rather than another copy-paste.

### Architecture compliance

- AD-1 (`storage.py` is the sole Supabase access point): not implicated — `retry.py` has no Supabase awareness at all, and `storage.py` remains the only module instantiating a Supabase client.
- This is Epic 5 (post-hoc hardening), not FR-1 through FR-5 — not bound by `ARCHITECTURE-SPINE.md`'s AD-1..AD-8 by name, but directly continues the "common logic factored, not duplicated" convention those ADs (especially AD-8, `normalize_domain`) established, applied here to retry-policy construction and to `quality_review_log`'s subject-normalization contract.
- `retry.py` is a new leaf module (no dependency on any of its four callers) — placed alongside `taxonomy.py` in the existing flat, no-`src/`-nesting structure, not nested or namespaced.

### Library/framework requirements

No new dependency. `tenacity` 9.1.4 and `mistralai` 2.4.9 are both already pinned; `retry.py` imports `SDKError` from the same `mistralai.client.errors.sdkerror` path every other module already uses. No `requirements.txt` change.

### Coding conventions to match

- Type hints everywhere, PEP 604 union syntax (`int | None` for `stop_attempts`).
- Docstrings citing the *why* and the story/AD, matching `storage.normalize_domain()`'s AD-8 citation as the closest precedent for "one shared function instead of N reimplementations."
- No `logging` module — this story adds no new runtime logging.
- Module-level constants/functions grouped with an explanatory comment, matching the established pattern from `_LIGHT_FETCH_MIN_CHARS` (Story 5.1) and `RETRY_INTERACTIVE_WAIT`/`_STOP` (Story 5.2).

### Testing requirements

Unlike Stories 5.1/5.2, this story **does** extend `test_storage.py` (Task 8) — not a new exception to the no-test-suite convention, but a direct continuation of Story 1.2's already-established one: `_validate_review`/`get_quality_reviews`'s contract already has extensive coverage in `test_storage.py`, and this story refactors that exact contract (extracting `_normalize_subject`) while also fixing a bug in it. Locking the fix in with a test is squarely inside that existing exception's scope, not a new one. No pytest file is expected for `retry.py`, `competitor.py`, `extractor.py`, or `competitor_validator.py`'s changes — those are pure construction-call substitutions verifiable by inspecting the resulting `.retry.stop`/`.retry.wait`/`.retry.retry` objects directly (Task 9), the same ad hoc approach Story 5.2 used for its own retry-split verification.

### Project Structure Notes

- **NEW file**: `retry.py` at repo root (flat, no `src/` nesting).
- **UPDATE** (not new) files, read in full before editing: `storage.py` (swap `_retry` construction, extract `_normalize_subject`, drop unused `tenacity` imports), `competitor.py` (swap `_retry` construction, drop local `_is_retryable`, drop unused `tenacity` imports), `extractor.py` (same), `competitor_validator.py` (same), `test_storage.py` (add Task 8's new tests).
- Do **not** modify: `diagnose_scraping.py`/`main.py`'s `_noise_ratio()` (AC #3 is already correct — Task 7 is verification only), `graph_app.py`, `reprocess_list.py`, `backfill_competitors.py`, `main.py`'s `ingest()`/lock/`INTERACTIVE_REQUEST` wiring (Story 5.2's own scope, unaffected by this story), `storage.py`'s `RETRY_INTERACTIVE_WAIT`/`RETRY_INTERACTIVE_STOP`/`BATCH_TIMEOUT_MS`/`INTERACTIVE_TIMEOUT_MS` (Story 5.2's constants — unrelated to the batch-side factoring this story does, and not to be merged into `retry.py`).

### References

- [Source: `_bmad-output/planning-artifacts/epics.md` Epic 5 / Story 5.3] — original acceptance-criteria text this story implements; AC #3's scope narrowed per "Current state" above (already delivered by Story 5.1).
- [Source: `storage.py`, read in full 2026-08-16] — `_retry`/`_execute_retryable`/`_execute` L53-80, `_KNOWN_REVIEW_TYPES`/`_TAXONOMY_SPLIT_VERDICTS`/`_TAXONOMY_SUBSECTORS` L86-88, `_validate_review` L142-174, `save_quality_review` L177-201, `get_quality_reviews` L204-220ish.
- [Source: `competitor.py`, read in full 2026-08-16] — `_is_retryable`/`_retry` L29-44 (post-Story-5.2 line numbers).
- [Source: `extractor.py`, read in full 2026-08-16] — `_is_retryable`/inline `@retry(...)` L18-40 (post-Story-5.2 line numbers).
- [Source: `competitor_validator.py`, read in full 2026-08-16] — `_is_retryable`/`@retry(...)`/`_chat_complete` L18-30.
- [Source: `test_storage.py`] — existing `_validate_review`/`get_quality_reviews` test coverage (L58-136), confirmed to use bare `pytest.raises(ValueError)` with no message-text matching, so Task 6's internal restructuring needs no existing-test changes.
- [Source: `_bmad-output/implementation-artifacts/5-1-fast-path-fallback-signal.md`] — origin of the `main._noise_ratio()` extraction that already satisfies this story's AC #3; precedent for a story explicitly documenting "already done, verify only" scope narrowing rather than re-implementing.
- [Source: `_bmad-output/implementation-artifacts/5-2-concurrent-ingestion-write-safety.md`] — precedent for the "new shared module vs. extend storage.py" decision (Story 5.2 chose storage.py because storage.py wasn't itself a caller; this story's callers include storage.py itself, so the same choice isn't available here) and for the `tenacity.retry_with()`/shared-constant sharing pattern this story extends to a full `build_retry()` factory.

## Dev Agent Record

### Agent Model Used

{{agent_model_name_version}}

### Debug Log References

### Completion Notes List

### File List
