---
baseline_commit: ce6655d6a98e93d5359b79af0cb5c9a6389ec5af
---

# Story 5.2: Concurrent-ingestion write safety

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a Julien,
I want concurrent `/api/ingest` calls for the same company to not race on check-then-write DB sequences, and a single interactive ingest to never hang for minutes with no feedback,
so that two overlapping ingests can't create duplicate `compspro` rows or duplicate competitor relationships, and a slow Mistral response doesn't silently stall the browser client for up to ~16 minutes.

## Acceptance Criteria

1. **Given** `main.ingest()` now runs `_ingest_sync` (which calls `storage.save_startup` and `storage.save_relationships`/`competitor.compare`) via `asyncio.to_thread`, allowing two concurrent ingests to interleave their lookup-then-insert-or-update sequences, **when** two `/api/ingest` calls for the same startup domain run concurrently, **then** an `asyncio.Lock` keyed on `storage.normalize_domain()`-normalized domain serializes ingestion of that domain — without a DB migration/unique-constraint approach (explicitly out of scope, judged overkill for a solo, not-yet-multi-user tool per Julien 2026-08-12; revisit with a DB-level constraint if the project ever goes multi-user).
2. **And** the widened Mistral retry budget (7 attempts, up to ~124s backoff + up to 120s timeout per attempt — currently shared by every caller) no longer lets a single interactive `/api/ingest` request hang the browser client for up to ~16 minutes with no feedback per LLM call.
3. **And** the retry/timeout budget is split so the interactive path (`/api/ingest`, and the CLI's single-URL `python main.py <url>`) is bounded tighter than the batch/backfill path (`reprocess_list.py`, `backfill_competitors.py`) — the two currently share one hard-coded setting each in `competitor.py` and `extractor.py`; this story makes them independently tunable, not just re-tuned to a single new shared value.
4. **And** `reprocess_list.py` and `backfill_competitors.py` require no code changes to keep today's wide retry budget — they call `extract()`/`compare()`-family functions directly, never through `main.ingest()`, so the interactive/batch distinction must be based on which entry point is used, not on a flag every batch script has to remember to pass.

## Tasks / Subtasks

- [x] Task 0: **Read current state before starting** (blocks every other task)
  - [x] Read `main.py`'s `ingest()`/`_ingest_sync()` (L359-451), `competitor.py`'s `_retry`/`_client()`/`_chat_json()` (L24-112), and `extractor.py`'s `_is_retryable`/`_chat_complete`/`extract()`'s client construction (L11-23, L244-245) in full. Confirmed the exact current retry values matched the story's documented current state: `competitor.py` and `extractor.py` both used `wait_exponential(multiplier=2, min=4, max=60)` + `stop_after_attempt(7)` + a Mistral client constructed with `timeout_ms=120_000` — identical numbers in both files, both hit by a single interactive ingest.
- [x] Task 1: Add a shared `INTERACTIVE_REQUEST` context flag (AC #3, #4)
  - [x] In `storage.py`, added `import contextvars` and `INTERACTIVE_REQUEST: contextvars.ContextVar[bool] = contextvars.ContextVar("interactive_request", default=False)`, placed right after `COMPETITOR_THRESHOLD`. Confirmed via `grep -rn contextvars` before adding that this was a genuinely new pattern for the codebase.
  - [x] Confirmed `storage.py` is the correct home: no circular import (`competitor.py`/`extractor.py` already import from `storage.py`; `storage.py` imports only from `taxonomy.py`).
- [x] Task 2: Wire `INTERACTIVE_REQUEST` into `main.ingest()` (AC #1, #3)
  - [x] Imported `INTERACTIVE_REQUEST` alongside the existing `storage` import (`main.py:12`).
  - [x] `ingest()` sets the flag before the synchronous pipeline runs (`INTERACTIVE_REQUEST.set(True)`) and resets it in a `finally` block wrapping the lock+to_thread call, so a raised exception from `_ingest_sync` doesn't leave the flag stuck.
  - [x] Confirmed both the web UI's `/api/ingest` (`graph_app.py`'s `ingest_startup = main.ingest` alias) and the CLI's `python main.py <url>` use the interactive budget automatically, with zero changes to `graph_app.py`. Confirmed `reprocess_list.py`/`backfill_competitors.py` never call `ingest()` (grep'd their imports), so they never set the flag (AC #4).
- [x] Task 3: Add a per-domain `asyncio.Lock` registry to serialize concurrent ingests (AC #1)
  - [x] Added module-level `_domain_locks: dict[str, asyncio.Lock] = {}` in `main.py`, right after `LOGO_EXTENSIONS`.
  - [x] `ingest()` now does `domain = normalize_domain(url)`, `lock = _domain_locks.setdefault(domain, asyncio.Lock())` after `scrape(url)` returns, then wraps the `to_thread` call in `async with lock:`.
  - [x] Documented the same-domain-only scope boundary and the unbounded-dict-growth tradeoff as inline comments above `_domain_locks` in `main.py` (and in this story's Dev Notes, unchanged from story creation).
- [x] Task 4: Split `competitor.py`'s retry/timeout budget (AC #2, #3)
  - [x] Added `_RETRY_INTERACTIVE_WAIT`/`_RETRY_INTERACTIVE_STOP` next to `_retry`, and `_BATCH_TIMEOUT_MS`/`_INTERACTIVE_TIMEOUT_MS` next to `_client()`.
  - [x] Renamed `_chat_json` to `_chat_json_core(timeout_ms: int, **kwargs)`, kept `@_retry`, now passes `timeout_ms` explicitly to `_client().chat.complete(...)`.
  - [x] Added `_chat_json_core_interactive = _chat_json_core.retry_with(wait=_RETRY_INTERACTIVE_WAIT, stop=_RETRY_INTERACTIVE_STOP)` at module level.
  - [x] Added the new `_chat_json(**kwargs)` dispatcher reading `INTERACTIVE_REQUEST.get()`. `_score_chunk` (the sole existing call site) needed no change.
  - [x] Imported `INTERACTIVE_REQUEST` from `storage` in `competitor.py`'s existing import block.
- [x] Task 5: Split `extractor.py`'s retry/timeout budget the same way (AC #2, #3)
  - [x] Added `_RETRY_INTERACTIVE_WAIT`/`_RETRY_INTERACTIVE_STOP`/`_BATCH_TIMEOUT_MS`/`_INTERACTIVE_TIMEOUT_MS`, renamed `_chat_complete` to `_chat_complete_core(client, timeout_ms, **kwargs)` (kept its `@retry(...)` decorator unchanged), added `_chat_complete_core_interactive` via `retry_with()`, and a `_chat_complete(client, **kwargs)` dispatcher.
  - [x] Confirmed `_step2a_sectors`/`_step2b_subsectors`/`_step2c_sub_subsectors`/`extract()`'s Step 1 call all still call `_chat_complete(client, ...)` unchanged — no call-site edits needed.
  - [x] Added `from storage import INTERACTIVE_REQUEST` (new import for this file). Confirmed no cycle. Also pointed `extract()`'s own `Mistral(...)` construction at `_BATCH_TIMEOUT_MS` instead of a second hard-coded `120_000`, for consistency (construction-time default is now a fallback only, since every call overrides `timeout_ms` explicitly).
- [x] Task 6: Verify against synthetic/mocked scenarios (no live network or Mistral calls needed)
  - [x] Lock behavior: monkeypatched `main.scrape`/`main._ingest_sync` with fakes, fired two concurrent `main.ingest()` calls for the *same* domain (different paths, same host) — confirmed serialized (~0.6s elapsed for two 0.3s fake calls). Fired two concurrent calls for *different* domains — confirmed they ran concurrently (~0.3s elapsed, not ~0.6s).
  - [x] Retry/timeout split: inspected `competitor._chat_json_core.retry.stop`/`.retry.wait` vs `_chat_json_core_interactive`'s — confirmed `stop_after_attempt(7)` vs `(3)`, `wait_exponential(max=60)` vs `(max=20)`, `reraise=True` inherited on both. Same confirmed for `extractor._chat_complete_core`/`_chat_complete_core_interactive`.
  - [x] Confirmed `INTERACTIVE_REQUEST` propagates across `asyncio.to_thread`: a fake thread-target function read `False` unset and `True` when set beforehand in the calling coroutine — Python 3.11.5's documented `contextvars` propagation behavior holds as expected.
  - [x] Confirmed `reprocess_list.py`/`backfill_competitors.py` import and (by extension) still run their existing code paths with zero edits — `python -c "import reprocess_list; import backfill_competitors"` succeeds.

## Dev Notes

### Current state — read before writing any code

`main.ingest()` (`main.py:444-451`) already runs `_ingest_sync` via `asyncio.to_thread` (a prior story's change, not this one) — that's the concurrency-enabling change that makes AC #1's race possible; this story doesn't touch that `to_thread` wrapping itself, only adds a lock around it. `competitor.py` and `extractor.py` currently have **byte-for-byte identical** retry policy (`wait_exponential(multiplier=2, min=4, max=60)`, `stop_after_attempt(7)`) and Mistral client timeout (`timeout_ms=120_000`) — both were widened together in a prior round, which is exactly why a single interactive ingest (which calls both modules' chat functions in sequence) can accumulate the ~16-minute worst case the AC describes. `reprocess_list.py`/`backfill_competitors.py` never call `main.ingest()` — they call `extract()`/`compare()`-family functions directly (confirmed via `grep`), which is what makes the context-flag design in Task 1 work without touching either batch script.

### Design decision: `contextvars` over a threaded parameter

Threading an `interactive: bool` parameter through `ingest()` → `_ingest_sync()` → `extract()` → `_step2a_sectors`/`_step2b_subsectors`/`_step2c_sub_subsectors` → `_chat_complete()`, and separately through `compare()`/`explore_transitive()`/`score_candidates()`/`_score_chunk()` → `_chat_json()`, would touch ~10 function signatures across 2 modules for a value that's constant for the whole call tree of a single `ingest()` invocation. A `contextvars.ContextVar`, set once in `ingest()` and read only at the two leaf dispatch points (`competitor._chat_json`, `extractor._chat_complete`), achieves the same effect with a 2-line change at the call boundary and zero signature changes anywhere else. This relies on `asyncio.to_thread()`'s documented context-propagation behavior (the calling coroutine's `contextvars.Context` is copied into the worker thread) — Task 6 verifies this holds rather than assuming it.

### Design decision: `tenacity.retry_with()` over duplicated retry functions

Both `competitor.py` and `extractor.py` already decorate their chat-call function with a static `@retry(...)`/`@_retry` — a fixed policy baked in at import time. Rather than writing two near-duplicate function bodies with different decorators (one more instance of the exact duplication pattern Story 5.3 is separately scoped to clean up elsewhere), this story uses tenacity 9.1.4's own `retry_with(wait=..., stop=...)` method (confirmed present and functional on the installed version — verified live) to derive a second, independently-callable wrapped function from the same core implementation, overriding only `wait`/`stop` and inheriting the existing retry predicate and `reraise=True` unchanged.

### Design decision: per-call `timeout_ms`, not a second `Mistral` client

`competitor.py`'s `_client()` is a lazily-constructed, process-wide singleton (`competitor.py:91-106`); `extractor.py`'s `extract()` constructs a fresh client per call. Rather than constructing a second client with a different `timeout_ms` in either file, this story relies on the installed `mistralai` 2.4.9 SDK's `Chat.complete()` accepting a per-call `timeout_ms` keyword (verified directly against the installed package: `mistralai/client/chat.py:150-152`), which overrides the client's own construction-time default for that one call. This keeps `competitor.py`'s singleton pattern untouched and avoids `extractor.py` needing to know the interactive/batch distinction at client-construction time.

### Scope boundary: same-domain lock only, not cross-company

AC #1's lock is keyed on the *ingested URL's own* normalized domain. It does not, and per epics.md's own AC text is not meant to, serialize two *different* companies being ingested concurrently even if they turn out to be competitors of each other and both write to the same `competitors` table rows via `save_competitors`. A cross-company race is a real but separate and larger problem (effectively needs a lock over the pair, or the graph as a whole) — epics.md explicitly rejected the heavier DB-constraint alternative as overkill for this stage, and this story doesn't silently expand scope to cover it. Flagging this here so it isn't mistaken for solved.

### Architecture compliance

- This is Epic 5 (post-hoc hardening), not FR-1 through FR-5 — not bound by `ARCHITECTURE-SPINE.md`'s AD-1..AD-8 by name, but AD-1's spirit (single point of Supabase access via `storage.py`) is respected: the new `INTERACTIVE_REQUEST` context var lives in `storage.py` precisely because it's the one module every other touched file already imports, avoiding a new circular dependency.
- Consistent with the project's "common logic factored, not duplicated" convention (same principle Story 5.1 applied to `BLOCKING_MARKERS`/`_noise_ratio`): the interactive/batch retry split reuses one core chat function per module via `retry_with()`, not two hand-written copies.

### Library/framework requirements

No new dependency. `contextvars` is Python stdlib (3.7+, project pins 3.11.5). `tenacity` 9.1.4 already a pinned dependency; `retry_with()` has been available since early tenacity releases, confirmed present and working on the installed version. No `requirements.txt` change.

### Coding conventions to match

- Type hints everywhere, PEP 604 union syntax where applicable.
- Docstrings/comments citing the *why*, anchored to a story/AD when applicable (see `competitor.py:96-100`'s `_client()` docstring for the closest existing precedent of documenting a concurrency rationale).
- No `logging` module — plain `print(...)` if any new logging is warranted (none is strictly required by this story's ACs).
- Module-level constants immediately above/near the function that uses them, with an explanatory comment (established pattern from `_LIGHT_FETCH_MIN_CHARS`, `_NOISE_RATIO_THRESHOLD` in Story 5.1).

### Testing requirements

No pytest file for this story, per the same project-wide no-test-suite convention Story 5.1 followed (`storage.save_quality_review()`'s unit test remains the sole exception). Verify via Task 6's ad hoc synthetic/mocked checks — none require a live Mistral call or a real concurrent HTTP load test:
- The lock's non-overlap behavior can be verified entirely with monkeypatched fakes and `asyncio.sleep`-based timing, no network.
- The retry/timeout split can be verified by inspecting the static tenacity config objects (`.retry.stop`, `.retry.wait`) directly — no call needs to actually execute.
- The `contextvars` propagation across `asyncio.to_thread` can be verified with a trivial fake function, no Mistral involved.

### Project Structure Notes

- **UPDATE** (not new) files, read in full before editing: `storage.py` (add `INTERACTIVE_REQUEST`), `main.py` (add `_domain_locks`, wire the lock and context flag into `ingest()`), `competitor.py` (split `_chat_json` into core/interactive variants + dispatcher), `extractor.py` (same split for `_chat_complete`).
- No new files.
- Do **not** modify: `reprocess_list.py`, `backfill_competitors.py` (AC #4 — must keep working with zero changes), `graph_app.py` (the interactive flag is set inside `main.ingest()` itself, not at the API layer), `competitor_validator.py` (its own separate, already-known-to-be-unbounded retry config is out of scope here — Story 5.3's job), `storage.py`'s own `_execute()`/Supabase retry config (unrelated to Mistral, not touched).

### References

- [Source: `_bmad-output/planning-artifacts/epics.md` Epic 5 / Story 5.2] — original acceptance-criteria text this story implements.
- [Source: `main.py`, read in full 2026-08-16] — `ingest()`/`_ingest_sync()` L359-451, existing `storage` import L12, CLI entry point L454-463.
- [Source: `competitor.py`, read in full 2026-08-16] — `_is_retryable`/`_retry` L24-41, `_client()` L91-106, `_chat_json()` L110-112, `CHUNK_SIZE` L21.
- [Source: `extractor.py`, read in full 2026-08-16] — `_is_retryable` L11-16, `_chat_complete()` L20-25, `extract()`'s client construction L244-245.
- [Source: `graph_app.py`] — `ingest_startup = main.ingest` import (`from main import ingest as ingest_startup`), `/api/ingest` handler L63-76 — confirmed no timeout wrapper exists there and none is added by this story (out of AC scope; a request-level end-to-end timeout would be a separate, larger concern).
- [Source: installed `mistralai` 2.4.9 package, `mistralai/client/chat.py:101-154`] — confirmed `Chat.complete()` accepts a per-call `timeout_ms` override, verified live against the actual installed SDK, not assumed from documentation.
- [Source: installed `tenacity` 9.1.4] — confirmed `retry_with(wait=..., stop=...)` is present and functional, verified live: returns an independently-callable wrapped function inheriting the unspecified `retry`/`reraise` config from the original decorator.
- [Source: Python stdlib docs, `asyncio.to_thread`] — context propagation into the worker thread is documented behavior this design depends on; Task 6 verifies it directly against the installed Python 3.11.5 rather than trusting the docs alone.
- [Source: `_bmad-output/implementation-artifacts/5-1-fast-path-fallback-signal.md`] — precedent for ad hoc synthetic-fixture verification without a pytest file, for explicitly documenting scope-boundary decisions in Dev Notes rather than leaving them implicit, and for the "factor into one shared implementation, dispatch at the call boundary" pattern this story reuses for the retry split.

## Dev Agent Record

### Agent Model Used

claude-sonnet-5

### Debug Log References

- No pytest file for this story, per its own Testing Requirements. Verified via ad hoc synthetic/mocked scripts (no network, no Mistral, no Supabase calls):
  - Lock: two concurrent `main.ingest()` calls for the same domain (`same-domain.example.com/a` vs `/b`) with a monkeypatched `_ingest_sync` (0.3s fake sleep each) took ~0.61s total — serialized as expected (>=0.55s threshold). Two concurrent calls for different domains took ~0.30s — ran concurrently as expected (not serialized against each other).
  - Retry/timeout split: `competitor._chat_json_core.retry.stop.max_attempt_number == 7` vs `_chat_json_core_interactive.retry.stop.max_attempt_number == 3`; `.retry.wait.max` 60.0 vs 20.0; `.retry.reraise` True on both (inherited via `retry_with()`). Identical results for `extractor._chat_complete_core`/`_chat_complete_core_interactive`.
  - `contextvars` propagation: a fake `asyncio.to_thread`-wrapped function read `INTERACTIVE_REQUEST.get()` as `False` when unset and `True` when set in the calling coroutine beforehand — confirmed live on the installed Python 3.11.5, not assumed from documentation.
  - `python -c "import reprocess_list; import backfill_competitors"` — both succeed, zero edits needed to either file (AC #4).
- Full regression sweep: all 15 top-level project modules (`main`, `storage`, `taxonomy`, `extractor`, `competitor`, `graph_analysis`, `graph_app`, `audit_taxonomy`, `competitor_validator`, `diagnose_scraping`, `log_review`, `readiness_check`, `recalibrate_competitors`, `reprocess_list`, `backfill_competitors`) import cleanly with no errors. `pytest test_storage.py` — 29/29 still passing, untouched.
- **Post-review fixes** (`code-review` findings on this story's own diff):
  - `_attr(tag, "src")` correctly rejected `data-src=` before (regression-checked, still correct); `_attr(tag, "href")` incorrectly matched inside a namespaced `xlink:href=` (e.g. an inline SVG sprite link), returning that value instead of `None`. Fixed by adding `:` to the negative lookbehind's excluded-boundary class. Re-verified: `xlink:href` → `None`, `data-src`/`src` case still correct, plain `href` still matches normally.
  - The interactive retry/timeout constants (`RETRY_INTERACTIVE_WAIT`/`_STOP`, `BATCH_TIMEOUT_MS`, `INTERACTIVE_TIMEOUT_MS`) were duplicated verbatim in both `competitor.py` and `extractor.py`. Moved into `storage.py` next to `INTERACTIVE_REQUEST`; both modules now import the same objects (identity-checked: `competitor.RETRY_INTERACTIVE_WAIT is storage.RETRY_INTERACTIVE_WAIT` → `True`). The batch-side `_retry`/`@retry(...)` decorators stay local to each module — factoring those is Story 5.3's separately-scoped job, not touched here. Re-verified: both modules' `_core_interactive` variants still report `stop_after_attempt(3)`, and `main.ingest()` still runs end-to-end against a mocked pipeline.
  - The residual cumulative-chain-latency risk (a full ingest can still chain many retry-wrapped calls) and `storage.save_startup()`'s exact-string dedup were both raised again by this review round but are unchanged by design — explicitly out of this story's scope per Julien's fix instructions (disclosed risk / Story 5.3 backlog item, respectively).

### Completion Notes List

- Task 0 confirmed the story's "current state" analysis was accurate: `competitor.py`/`extractor.py` had byte-for-byte identical retry policy and timeout before this story.
- Tasks 1-2: `INTERACTIVE_REQUEST` context flag added to `storage.py`, wired into `main.ingest()` (`set`/`finally: reset`). No changes needed to `graph_app.py` — the flag is entirely internal to `main.ingest()`.
- Task 3: per-domain `asyncio.Lock` registry (`_domain_locks`) added to `main.py`, wrapping only the write-sequence part of `ingest()` (after `scrape()` returns, around the `to_thread(_ingest_sync, ...)` call) — scraping itself stays unserialized since it doesn't touch the DB.
- Tasks 4-5: both `competitor.py` and `extractor.py` split into a `_core`/`_core_interactive` pair via `tenacity.retry_with()` plus a small dispatcher reading `INTERACTIVE_REQUEST` — no duplicated function bodies, no caller-site changes at any existing call site in either file.
- No change to `reprocess_list.py`, `backfill_competitors.py`, `graph_app.py`, `competitor_validator.py`, or `storage.py`'s own `_execute()`/Supabase retry config — all confirmed out of scope per the story's Project Structure Notes.
- No `requirements.txt` change, no new dependency (`contextvars` is stdlib).
- All 4 acceptance criteria verified against synthetic/mocked scenarios; no live Mistral or concurrent-HTTP load test was needed or performed.

### File List

- `storage.py` (modified — added `import contextvars`, the `INTERACTIVE_REQUEST` context flag, and (post-review fix) the shared `RETRY_INTERACTIVE_WAIT`/`RETRY_INTERACTIVE_STOP`/`BATCH_TIMEOUT_MS`/`INTERACTIVE_TIMEOUT_MS` constants)
- `main.py` (modified — added `_domain_locks` registry and `INTERACTIVE_REQUEST` import; `ingest()` now acquires a per-domain lock around the write-sequence call and sets/resets the interactive flag around it; (post-review fix) `_attr()`'s negative lookbehind now also excludes `:` so namespaced attributes like `xlink:href` aren't mismatched as `href`)
- `competitor.py` (modified — added `INTERACTIVE_REQUEST` import; `_chat_json` split into `_chat_json_core` + `_chat_json_core_interactive` + a dispatcher; (post-review fix) the interactive retry/timeout constants are now imported from `storage.py` instead of locally defined)
- `extractor.py` (modified — added `INTERACTIVE_REQUEST` import (new dependency on `storage.py`); `_chat_complete` split the same way as `competitor.py`; `extract()`'s own client construction references `BATCH_TIMEOUT_MS`; (post-review fix) the interactive retry/timeout constants are now imported from `storage.py` instead of locally defined)

## Change Log

- 2026-08-16: Story implemented. Added `INTERACTIVE_REQUEST` (a `contextvars.ContextVar`) to `storage.py`, set by `main.ingest()` for the duration of a single interactive ingest and propagated across `asyncio.to_thread()` into `extract()`/`compare()`'s chat calls with zero signature changes elsewhere. Added a per-domain `asyncio.Lock` registry to `main.ingest()`, serializing concurrent ingests of the same normalized domain around the write-sequence part only (not the network scrape). Split `competitor.py`'s and `extractor.py`'s Mistral retry/timeout budgets into interactive (3 attempts, 20s max backoff, 30s per-call timeout) vs. batch (7 attempts, 60s max backoff, 120s per-call timeout) via `tenacity.retry_with()` — one core implementation per module, no duplicated function bodies, no call-site changes. `reprocess_list.py`/`backfill_competitors.py` needed zero edits since they never call `main.ingest()`. Verified via ad hoc synthetic/mocked scripts: lock serializes same-domain concurrent calls and does not serialize different-domain calls; retry/timeout configs confirmed to differ as designed; `contextvars` propagation across `asyncio.to_thread` confirmed live. No regressions: all 15 project modules import cleanly, `test_storage.py` 29/29 still passing. All 4 acceptance criteria met.
- 2026-08-16: Code review (`code-review` skill) fixes, per Julien's explicit scope: (1) the interactive retry/timeout constants, duplicated verbatim in `competitor.py` and `extractor.py`, were moved into `storage.py` next to `INTERACTIVE_REQUEST` — both modules now import the same objects, confirmed via identity check. (2) `main.py`'s `_attr()` negative lookbehind now also excludes `:`, fixing a mismatch where `xlink:href` (e.g. an inline SVG sprite link) was returned as if it were a real `href`. Explicitly left unchanged per Julien's instruction: the per-call retry/timeout logic itself (already correct), `storage.py`'s `save_startup()` dedup (Story 5.3 backlog item), and any global end-to-end timeout or parallelization change to `main.ingest()` (cumulative latency remains a disclosed, not fixed, residual risk). Re-verified: no regressions (15 modules import cleanly, `test_storage.py` 29/29), `xlink:href`/`data-src` edge cases both correct, shared constants confirmed identical by object identity, `main.ingest()` still runs end-to-end against a mocked pipeline.
