---
baseline_commit: ce6655d6a98e93d5359b79af0cb5c9a6389ec5af
---

# Story 4.1: Start-trigger readiness check

Status: done

## Story

As a Julien,
I want a readiness check against `quality_review_log`,
so that I don't start recalibrating before the evidence base is stable enough to trust.

## Acceptance Criteria

1. **Given** `taxonomy_split` entries logged in `quality_review_log`, **when** I run the readiness check, **then** it reports whether ≥9 of the 11 queued subsectors are diagnosed and logged.
2. **And** it reports whether no new fracture type has appeared across the last 3 subsectors worked (chronological by `date_diagnosed`).
3. **And** it returns a clear "ready"/"not ready" verdict, not just raw counts to interpret myself.

## Tasks / Subtasks

- [x] Task 1: Encode the 11-subsector FR-3 queue as a module constant (AC: #1)
  - [x] New file `readiness_check.py` at repo root (flat, one script per concern — no existing file is a natural fit: `log_review.py` is the per-subsector reading tool, not a queue-wide report; `graph_analysis.py` stays out of scope, AD-5). This is the **first place this list exists in code** — until now it only lived in prose (PRD §4.2 FR-3, `epics.md` Epic 3). Define `QUEUED_SUBSECTORS` as a module-level tuple of the exact 11 strings: `"AI Agent Platforms And Automation"`, `"AI Data & Training Infrastructure"`, `"Threat Detection & Intelligence"`, `"Supply Chain & Logistics Automation"`, `"Payment And Fraud Solutions"`, `"Field & Industrial Operations"`, `"Financial Compliance Automation"`, `"Cybersecurity Risk Management"`, `"Embedded Financial Services"`, `"API Infrastructure"`, `"MLOps & Model Serving"` — all 11 confirmed to match `taxonomy.TAXONOMY` subsector names exactly (verified live, 2026-08-12; copy them verbatim, do not retype from memory).
- [x] Task 2: Fetch and resolve the current diagnosed state of the queue (AC: #1, #2)
  - [x] Call `storage.get_quality_reviews('taxonomy_split')` — **no** `subject` filter, one call fetches every `taxonomy_split` row that exists.
  - [x] Filter to rows whose `subject` is in `QUEUED_SUBSECTORS`. **Rows for subsectors outside the queue must be excluded from every part of this check** — Julien has already diagnosed several subsectors outside the official 11 (e.g. `CRM & Sales`, confirmed live in the current data), and PRD §4.3's start trigger is specifically about the FR-3 queue's own evidence stabilizing, not about incidental extra diagnoses. Both AC #1's count and AC #2's "last 3" must only ever be computed over this filtered, in-queue subset.
  - [x] A subsector can have more than one `taxonomy_split` row over time (re-diagnosis after a fix — AD-7, the same "repeated diagnoses are intentional history" rule Story 3.2 already applied to `scraping_diagnostic`, not something special to that review type). Keep only the **most recent** row per subsector (max `date_diagnosed`) — the readiness check must reflect each subsector's *current* verdict, not stale history.
  - [x] Result: a `{subsector: row}` dict, at most 11 entries, one per already-diagnosed queue subsector.
- [x] Task 3: Compute both trigger conditions (AC: #1, #2)
  - [x] **Count condition:** `count = len(diagnosed)`; ready iff `count >= 9` — check the literal AC number, not a recomputed percentage (9/11 ≈ 81.8%, but the AC says "≥9", not "≥80%" — use the integer).
  - [x] **Stability condition — get this right, it's the easiest part to implement wrong:** sort the diagnosed (in-queue only) rows chronologically by `date_diagnosed`, take the last 3 (fewer than 3 total diagnosed ⇒ stability condition is simply not met yet, not an error). Compute `seen_before = {verdicts of every diagnosed entry that comes BEFORE this last-3 window}` and `new_types = {verdicts in the last-3 window} - seen_before`. Stability holds iff `new_types` is empty. **Do not** compute this as "is the last-3 verdict set a subset of the verdict set over *all* diagnosed entries" — that set trivially always contains itself and would report "stable" unconditionally, defeating the whole point of the check.
  - [x] "Fracture type" for this check = the `verdict` field value (`isolated mis-tag` / `structural gap` / `ambiguous` / `scraping artifact`) — the only structured signal `quality_review_log`'s schema actually carries. (The PRD prose parenthetically says "verdict + cause" once — there is no separate `cause` field in the schema; `verdict` is what "fracture type" operationalizes to here. Don't invent a cause-extraction step that doesn't exist elsewhere in this codebase.)
- [x] Task 4: Report a single clear ready/not-ready verdict (AC: #3)
  - [x] Combine both conditions: `ready = count_ready and stability_ready` (PRD §4.3: the trigger "begins only once **both** hold").
  - [x] Print a report: the count (`X/11`) with the diagnosed subsectors and their current verdicts, the still-outstanding queue subsectors, the last-3 chronological window and whether it introduced a new type, and finish with an unambiguous `READY` or `NOT READY` line — if not ready, state which condition(s) failed (count, stability, or both), not just the raw numbers.
  - [x] No CLI arguments — `python readiness_check.py` reports on the whole queue's current state, matching `graph_analysis.py`'s own no-argument, read-only reporting convention.

## Dev Notes

- **This script is read-only** — same category as `graph_analysis.py` (AD-5's "stays strictly read-only" pattern extends naturally here, even though AD-5 itself only names `graph_analysis.py`/`diagnose_scraping.py`/`log_review.py` explicitly). It only calls `storage.get_quality_reviews()`; it never calls `save_quality_review()`, never touches `compspro`/`competitors`/`taxonomy.py`. There is nothing for this story to decide or execute — Story 4.2 is where an actual raised threshold gets applied, gated behind this story's own "ready" verdict.
- **Architecture gap, resolved here, not upstream:** `ARCHITECTURE-SPINE.md`'s Structural Seed and Capability→Architecture Map don't name a file for Story 4.1 — its own Capability row for FR-4 explicitly defers *when* FR-4 may start as "a process gate, not re-derived as an architectural decision" in the spine. `readiness_check.py` (flat script, repo root, no new dependency) is this story's own resolution of that gap, consistent with the existing convention every other Epic 2/3 story already followed (`diagnose_scraping.py`, `log_review.py`).
- Reuse `storage.get_quality_reviews()` exactly as-is (Story 1.2) — no new `storage.py` function needed; a plain `review_type='taxonomy_split'`, no-`subject` call already returns everything this story needs in one round trip.
- No new dependency, no `requirements.txt` change (stdlib `datetime`/string comparison on the ISO `date_diagnosed` field is sufficient for chronological sort — Postgres `timestamptz` values serialize as sortable ISO-8601 strings via `supabase-py`, no parsing needed to order them correctly).
- No test suite for this story — same as Stories 2.1/3.1/3.2, the no-test convention holds; Story 1.2's unit test remains the sole, explicitly scoped exception.
- No migration — no schema change in this story.

### Project Structure Notes

- New file: `readiness_check.py` at repo root — flat, no `src/` nesting.
- Do not modify: `storage.py`, `log_review.py`, `diagnose_scraping.py`, `graph_analysis.py`, `taxonomy.py`, `main.py`. Nothing in this story writes anywhere.

### Live state at story-creation time (2026-08-12 — a live snapshot to sanity-check the implementation against, not a fixture to hardcode)

Queried live via `storage.get_quality_reviews('taxonomy_split')`: **9 of the 11 queued subsectors are currently diagnosed** — `AI Agent Platforms And Automation` (ambiguous), `AI Data & Training Infrastructure` (structural gap), `Threat Detection & Intelligence` (isolated mis-tag), `Payment And Fraud Solutions` (isolated mis-tag), `Field & Industrial Operations` (structural gap), `Financial Compliance Automation` (isolated mis-tag), `Cybersecurity Risk Management` (ambiguous), `API Infrastructure` (structural gap), `MLOps & Model Serving` (isolated mis-tag). Not yet diagnosed: `Supply Chain & Logistics Automation`, `Embedded Financial Services`. The chronological last-3 (by `date_diagnosed`, in-queue only) are `Field & Industrial Operations` → structural gap, `Financial Compliance Automation` → isolated mis-tag, `API Infrastructure` → structural gap — all three verdict values already appeared earlier in the queue's history, so no new type in that window. **Both trigger conditions currently evaluate as met** (`count=9 ⇒ count_ready=True`, `new_types=[] ⇒ stability_ready=True` ⇒ `ready=True`) — a correct implementation should report `READY` when run against the live table today. Also present but correctly out of scope: `CRM & Sales`, `Sensors & IoT Devices`, `AI For Education And Creativity`, `Enterprise Project Management`, `AI Driven Developer Productivity` — none of these are in `QUEUED_SUBSECTORS`, and none should appear in this story's count or stability computation.

### References

- [Source: `_bmad-output/planning-artifacts/epics.md` Epic 4 / Story 4.1] — acceptance criteria this story implements.
- [Source: `_bmad-output/planning-artifacts/prds/prd-SM Project-2026-08-09/prd.md` §4.3 FR-4 "Start trigger"] — the two-condition trigger this story checks, and the exact 11-subsector list (§4.2 FR-3).
- [Source: `_bmad-output/planning-artifacts/architecture/architecture-SM Project-2026-08-10/ARCHITECTURE-SPINE.md` AD-5, Capability → Architecture Map (FR-4 row)] — confirms the start-trigger check itself was left as an unscoped process gate, not pre-assigned to a file; this story is the resolution.
- [Source: `storage.py`, read in full for prior stories] — `get_quality_reviews()` signature/behavior, unchanged, reused as-is.
- [Source: live `quality_review_log` query, 2026-08-12] — current real data confirming both trigger conditions are met today; see snapshot above.
- [Source: `_bmad-output/implementation-artifacts/3-2-...md`] — previous story; established the "most recent row wins" pattern for a review subject with multiple historical rows (there for `scraping_diagnostic`, reused here for `taxonomy_split`).

## Dev Agent Record

### Agent Model Used

claude-sonnet-5

### Debug Log References

- No pytest suite for this story (out of scope, matches Stories 2.1/3.1/3.2). Verified via: (1) clean import of `readiness_check.py` + all 11 other project modules, no regressions; (2) `pytest test_storage.py` — 29/29 still passing, untouched.
- **Live run against the real `quality_review_log` produced exactly the predicted outcome from the story's own "Live state at story-creation time" snapshot:** `9/11`, last-3 window = `Field & Industrial Operations -> structural gap, Financial Compliance Automation -> isolated mis-tag, API Infrastructure -> structural gap`, no new type, `READY`.
- Unit-verified with fake `get_quality_reviews` data across 6 cases: (1) count < 9 → `NOT READY`; (2) a genuinely new verdict type inside the last-3 window → correctly detected as `new_types`, `NOT READY` — confirms this is **not** the trivial always-stable bug the story explicitly warned against (first attempt at this test had an off-by-one in the fake data, not in `readiness_check.py` — caught and corrected before treating it as a pass); (3) a type introduced *before* the window and only reused *inside* it → correctly **not** flagged as new; (4) a re-diagnosed subsector → most-recent verdict wins, count unaffected by the extra historical row; (5) an out-of-queue subsector (`CRM & Sales`) with a fabricated very-recent date → correctly excluded from both count and the stability window; (6) fewer than 3 total diagnosed → handled gracefully (`stability_ready=False`, no crash).
- **Confirmed by Julien himself running the script for real** (`.venv/bin/python3 readiness_check.py`, not a mock or a code-check pass): output matched the predicted snapshot exactly — `9/11`, same last-3 window, `READY`. This is the trigger Epic 4 gates on; Story 4.2 can now proceed.

### Completion Notes List

- New file `readiness_check.py` implements all 4 tasks: `QUEUED_SUBSECTORS` constant (Task 1), `_diagnosed_queue_entries()` fetch+filter+dedup (Task 2), `check_readiness()` computing both trigger conditions (Task 3), `_print_report()` + `__main__` (Task 4).
- Stability check implemented as "verdicts in the last-3 window minus verdicts seen strictly before it" (`seen_before = {verdicts of chronological[:cutoff]}`), not "last-3 verdicts is a subset of all diagnosed verdicts" — the story's own Dev Notes flagged the latter as trivially always true; verified via the fake-data cases above that the implemented version actually distinguishes the two.
- No `requirements.txt`/migration change. No modification to `storage.py`, `log_review.py`, `diagnose_scraping.py`, `graph_analysis.py`, `taxonomy.py`, `main.py` — confirmed via `git status` (only `readiness_check.py` is new).

### File List

- `readiness_check.py` (new)

## Change Log

- 2026-08-12: Story implemented — new `readiness_check.py` at repo root. Encodes the 11-subsector FR-3 queue as `QUEUED_SUBSECTORS`, fetches all `taxonomy_split` rows in one call, filters to in-queue subjects only, dedups to the most-recent row per subsector, computes the two PRD Section 4.3 start-trigger conditions (≥9/11 diagnosed; no new fracture type across the chronological last 3), and prints a single unambiguous `READY`/`NOT READY` verdict with full supporting detail. Read-only throughout — no write path exists in this file. No test suite added (out of scope per story); verified via clean imports (no regressions across all 12 project modules), `test_storage.py` still 29/29, a live run against the real `quality_review_log` matching the story's own predicted snapshot exactly, and 6 fake-data cases covering both trigger conditions, the queue-scoping requirement, and the dedup rule. All 3 acceptance criteria met.
