---
baseline_commit: ce6655d6a98e93d5359b79af0cb5c9a6389ec5af
---

# Story 3.2: Wire the FR-1 guardrail into `log_review.py`

Status: done

## Story

As a Julien,
I want log_review.py to check for a known scraping gap before I conclude a structural taxonomy gap,
so that a scraping artifact from A is never misattributed to C.

## Acceptance Criteria

1. **Given** I'm about to record a `structural gap` verdict for a subsector, **when** the script normalizes the subsector's startup domains (via `storage.normalize_domain()`, built in Story 1.2 — AD-8, never a normalization logic local to this script) and checks `quality_review_log` for matching `scraping_diagnostic` entries, **then** if a match exists, the script surfaces the known diagnosis and offers `scraping artifact` as the alternative verdict.
2. **And** if no match exists, `structural gap` records normally.
3. **And** if FR-1 (Epic 2) hasn't run on this subsector yet, the script warns that the guardrail has nothing to check against, rather than silently implying a complete check.

## Tasks / Subtasks

- [x] Task 1: Fetch this subsector's startup domains (AC: #1)
  - [x] Add a new function `_fetch_subsector_websites(subsector) -> dict[str, str]` (name → `website`) in `log_review.py` — a **separate** query from Story 3.1's existing `_fetch_subsector_members()` (which only selects `name, description`), not a modification of it. Same shape: `_client().table("compspro").select("name, website").contains("subsectors", [subsector]).execute()`. Keeping it separate avoids touching/risking regression in `_fetch_subsector_members()`, which is already live-verified (Story 3.1); the extra lightweight query is a non-issue at this project's scale (a subsector has at most ~150 members).
- [x] Task 2: Compute the guardrail check against `quality_review_log` (AC: #1, #2, #3)
  - [x] Add `_scraping_guardrail_check(websites: dict[str, str]) -> tuple[dict[str, dict], dict[str, dict]]` (or equivalent — exact return shape is a dev-agent implementation choice, the two outcomes below are not).
  - [x] Fetch **all** `scraping_diagnostic` rows in **one** call: `get_quality_reviews("scraping_diagnostic")` (no `subject` filter). Do not loop and query per-domain — with up to ~150 members per subsector that would be up to 150 network round-trips for one guardrail check; one bulk fetch plus a local set/dict lookup is the only sane shape here.
  - [x] Build `latest_by_domain: dict[str, dict]` — for each row, keep only the **most recent** one per (already-normalized) `subject`, comparing `date_diagnosed`. Diagnoses are intentional history, not deduplicated at write time (Story 1.1/1.2's explicit decision — AD-7) — a domain can have multiple rows over time, and the guardrail must reflect the *current* state of that site (e.g. a `blocking_page` finding superseded by a later `ok` re-diagnosis must not still read as a known problem).
  - [x] For each `(name, website)` in Task 1's result, skip if `website` is falsy; otherwise normalize via `storage.normalize_domain(website)` (already imported/used elsewhere in this file — do not reimplement, AD-8) and look it up in `latest_by_domain`.
  - [x] Split the results into two dicts: **`covered`** = every member whose normalized domain has *any* row in `latest_by_domain` (regardless of verdict) — this is what AC #3's "has FR-1 run on this subsector" actually means, since `diagnose_scraping.py` (Story 2.1) writes a row for *every* site it visits, including ones it found nothing wrong with (`verdict="ok"`). **`problems`** = the subset of `covered` whose most-recent row's `verdict != "ok"` — this is AC #1's "match". **`"ok"` is not a match** — a site FR-1 already confirmed clean is not a known scraping gap, and must not trigger the switch-to-`scraping artifact` offer.
- [x] Task 3: Wire the check into the verdict flow, scoped to `structural gap` only (AC: #1, #2, #3)
  - [x] In `main()`, immediately after `_prompt_verdict()` returns (before `_prompt_notes()`), check: **only if the returned verdict is exactly `"structural gap"`**, run Task 1 + Task 2 and branch on the result. For the other 3 verdicts (`isolated mis-tag`, `ambiguous`, `scraping artifact`), skip this entirely and proceed exactly as Story 3.1 already does — the Given clause scopes this guardrail specifically to a `structural gap` conclusion (matches AD-2's own wording: "before recording a `taxonomy_split` verdict... checks the log... before concluding 'structural gap' rather than 'scraping artifact'"). Do not run it for `isolated mis-tag` even though AC #2's prose also names that verdict — that mention is reassurance that other verdicts are unaffected by this guardrail, not a second trigger condition; the Given clause is the authoritative scope.
  - [x] If `covered` is empty (Task 2): print a clear warning that FR-1 hasn't diagnosed any of this subsector's current startup domains yet, so the guardrail has nothing to check against (AC #3) — do not silently proceed as if a complete check happened. Keep the verdict as `structural gap`.
  - [x] Elif `problems` is non-empty (Task 2): print, per matched company, its name, normalized domain, the known `verdict`, `notes`, and `date_diagnosed` from `latest_by_domain` (AC #1's "surfaces the known diagnosis" — not just the company name). Then prompt (`input()`, default No on empty/anything but `y`/`yes`): switch the verdict to `"scraping artifact"`? If yes, reassign the local `verdict` variable; if no, keep `structural gap`. This decision stays a single subsector-level choice — `quality_review_log`'s `taxonomy_split` row is per-subsector, not per-company, so a per-company partial switch isn't a coherent outcome here.
  - [x] Else (`covered` non-empty, `problems` empty): print a short confirmation that FR-1 has already checked N of this subsector's sites and found no scraping issue, and that `structural gap` records normally (AC #2). No prompt needed — nothing to reconsider.

## Dev Notes

- **AD-8 (single domain-normalization function):** import and reuse `storage.normalize_domain()` — already imported in this file's Story 1.2/2.1 lineage (well, actually not yet imported in `log_review.py` as of Story 3.1 — add it to the existing `from storage import _client, save_quality_review` line). Never write a local normalization routine; two independently-normalized forms of the same domain is exactly the failure mode AD-8 exists to prevent, and it would silently break this guardrail specifically (a mismatch here reports false confidence — "no known scraping gap" — instead of correctly finding one, which is the scenario ARCHITECTURE-SPINE.md's own AD-8 rationale calls out by name for this exact story).
- **AD-7 (repeated diagnoses are intentional history):** `scraping_diagnostic` rows are never deduplicated or overwritten at write time — a domain can accumulate several rows over time, each a snapshot at that moment. The guardrail must read the *most recent* one per domain, not just "any row" or "the first row found" — an old problem finding superseded by a newer clean one must not still block/flag as a known gap.
- **Story 2.1's `"ok"` verdict is the load-bearing signal for "covered but no problem":** `diagnose_scraping.py`'s `characterize()` returns `"ok"` for a site with nothing wrong (`diagnose_scraping.py`, `characterize()`) — every other label it currently produces (`incomplete_content`, `blocking_page`, `content_drowned_in_noise`, `scrape_exception`) signals an actual problem, and any *future* label a future run of that script's still-open vocabulary might produce (AD-4 — `scraping_diagnostic`'s verdict has no closed vocabulary) should be treated the same way: **anything other than the literal string `"ok"` counts as a match.** Do not hardcode a positive list of "known problem" labels to check against — that would silently stop working the moment `diagnose_scraping.py` starts producing a label nobody anticipated when this story was written. Check the negative condition (`verdict != "ok"`) instead.
- **AD-5 (human deliberation, not automated override):** the guardrail *offers*, it never silently rewrites the verdict Julien already chose. A `structural gap` verdict that survives the offer (Julien says no) is recorded exactly as selected — the guardrail's job is to surface evidence, not to make the call.
- No new dependency, no `requirements.txt` change. No migration — no schema change in this story.
- No test suite for this story — same as Stories 2.1 and 3.1, the no-test convention holds; Story 1.2's unit test remains the sole, explicitly scoped exception.

### Project Structure Notes

- `log_review.py` is an **UPDATE** this story, not a new file — Story 3.1 already built and live-verified it (first real `taxonomy_split` row written 2026-08-11: `MLOps & Model Serving` → `isolated mis-tag`). Read the current file in full before editing (reproduced in full below) — do not reorder or touch `_load_report`, `_find_split`, `_find_community`, `_fetch_subsector_members`, `_display_samples`, `_prompt_notes`, `_save_verdict`, `_resume`, `PENDING_FILE`/`--resume` machinery, or `SAMPLE_SIZE_PER_COMMUNITY`. This story only adds `_fetch_subsector_websites`, `_scraping_guardrail_check` (or equivalently-named helpers), and inserts new logic into `main()` between the existing `_prompt_verdict()` and `_prompt_notes()` calls.
- Do not modify: `storage.py`, `main.py`, `taxonomy.py`, `graph_analysis.py`, `diagnose_scraping.py`. Nothing in this story writes to `compspro`, `competitors`, or `taxonomy.py` (this story only adds a *read* check before the existing `taxonomy_split` write path — the write path itself, `_save_verdict`, is unchanged).

### Current `log_review.py` (post Story 3.1 + its code-review patch — read in full, 2026-08-12)

Full file is 238 lines. Key structure for this story's purposes:
- Imports (lines 11-17): `json, os, random, sys` (stdlib); `from storage import _client, save_quality_review` — **add `normalize_domain, get_quality_reviews` to this import line**; `from taxonomy import TAXONOMY`.
- `_fetch_subsector_members()` (lines 84-94): `{name: description}` via `.select("name, description").contains("subsectors", [subsector])` — the pattern Task 1's new `_fetch_subsector_websites()` mirrors, swapping `description` for `website`.
- `main()` (lines 190-233): the insertion point is between `verdict = _prompt_verdict()` (line 215) and `notes = _prompt_notes()` (line 216) — everything after `notes = _prompt_notes()` (payload construction, `_save_verdict` call, resume-on-failure messaging) stays exactly as Story 3.1 left it; the guardrail only decides what `verdict` holds by the time execution reaches the existing `payload = {...}` block.

### References

- [Source: `_bmad-output/planning-artifacts/epics.md` Epic 3 / Story 3.2] — acceptance criteria this story implements.
- [Source: `_bmad-output/planning-artifacts/prds/prd-SM Project-2026-08-09/prd.md` §4.2 FR-3 Precondition 1, §4.6 FR-5 schema (`verdict` includes `scraping artifact`)] — the sequencing guardrail this story implements, and why `scraping artifact` exists as a distinct verdict value.
- [Source: `_bmad-output/planning-artifacts/architecture/architecture-SM Project-2026-08-10/ARCHITECTURE-SPINE.md` AD-2, AD-4, AD-5, AD-7, AD-8] — AD-2 names the exact guardrail mechanism (query by `review_type`+`subject`); AD-8 explicitly calls out this story (Epic 3/Story 3.2) as a binder and names the exact failure mode (false "no known gap" confidence) a normalization mismatch would cause here.
- [Source: `log_review.py`, read in full 2026-08-12] — current implementation this story updates; exact insertion point in `main()`.
- [Source: `diagnose_scraping.py`, read in full 2026-08-11 for Story 2.1] — `characterize()`'s verdict vocabulary (`ok`/`incomplete_content`/`blocking_page`/`content_drowned_in_noise`/`scrape_exception`), source of the `verdict != "ok"` match rule; confirms the vocabulary is open-ended (AD-4), motivating the negative-condition check over a positive list.
- [Source: `storage.py`, read in full 2026-08-11] — `get_quality_reviews()`/`normalize_domain()` signatures and internal-normalization behavior (both already used correctly elsewhere in this project — no new usage pattern needed here).
- [Source: `_bmad-output/implementation-artifacts/3-1-...md`] — previous story; confirms `log_review.py`'s current live-verified state and the exact real verdict already recorded (`MLOps & Model Serving` → `isolated mis-tag`, which this story's guardrail would **not** have triggered for, since that verdict isn't `structural gap` — consistent with Task 3's scoping).

## Dev Agent Record

### Agent Model Used

claude-sonnet-5

### Debug Log References

- No pytest suite for this story (out of scope, matches Stories 2.1/3.1). Verified via: (1) clean import of `log_review.py` + all 10 project modules, no regressions; (2) `pytest test_storage.py` — 29/29 still passing, untouched.
- Unit-verified `_scraping_guardrail_check()` with fake `get_quality_reviews` data: most-recent-per-domain resolution confirmed (an older `blocking_page` superseded by a newer `ok` correctly drops out of `problems`); `"ok"` correctly excluded from `problems` while still counting toward `covered`; a website-less member and a never-diagnosed domain both correctly excluded from `covered`.
- Unit-verified all 3 `_apply_scraping_guardrail()` branches: empty `covered` (AC #3 warning), non-empty `covered`/empty `problems` (AC #2, silent pass-through), and non-empty `problems` with both a decline and an accept response (including bare Enter defaulting to decline).
- Verified via `main()` with mocked input across all 4 verdict choices that the guardrail fires exactly once for `structural gap` and zero times for `isolated mis-tag`/`ambiguous`/`scraping artifact` — confirms Task 3's scoping is correct, not just documented.
- Live-verified `_fetch_subsector_websites()` and `_scraping_guardrail_check()` against real `compspro`/`quality_review_log` data for `"MLOps & Model Serving"` (42 real members): today `covered=0` (FR-1's 5 real diagnoses so far don't overlap this subsector) — a real instance of AC #3's warning path.
- Full end-to-end live test: real subsector members + a simulated `scraping_diagnostic` row on a **real** member's real domain (`Corvic AI` / `corvic.ai`) correctly triggered the guardrail, displayed the finding, and applied the accepted switch to `scraping artifact` — verified via a faked (not real) `save_quality_review` call, for the same reason Story 3.1 avoided a real write during verification: a code-check pass choosing/confirming a verdict isn't Julien's own deliberation (AD-5).
- **Full nominal path confirmed by Julien himself in real subsequent use of `log_review.py`** (not a mock, not this session's code-check): 3 further real `taxonomy_split` rows landed in `quality_review_log` after this story shipped — `Cybersecurity Risk Management -> ambiguous`, `CRM & Sales -> structural gap`, `AI Data & Training Infrastructure -> structural gap`. The latter two are `structural gap` verdicts, meaning the FR-1 guardrail this story adds actually ran for both and let them through without error — real-world confirmation the guardrail integrates correctly into the live flow, on top of this story's own mocked/simulated verification above.

### Completion Notes List

- `log_review.py` updated (not replaced) per the story's Project Structure Notes: added `_fetch_subsector_websites`, `_scraping_guardrail_check`, `_apply_scraping_guardrail`; inserted a 3-line dispatch in `main()` between the existing `_prompt_verdict()` and `_prompt_notes()` calls. `_load_report`, `_find_split`, `_find_community`, `_fetch_subsector_members`, `_display_samples`, `_prompt_notes`, `_save_verdict`, `_resume`, `PENDING_FILE`/`--resume` machinery all untouched.
- Guardrail match rule implemented as the negative condition (`verdict != "ok"`), not a positive list of known-problem labels, per the story's explicit anti-pattern warning — future `diagnose_scraping.py` labels will be caught automatically.
- Guardrail decision stays subsector-level (one prompt, one possible switch) even though the underlying `problems` dict can contain multiple matched companies — matches `quality_review_log`'s per-subsector `taxonomy_split` grain.
- No `requirements.txt`/migration change. `storage.py`, `main.py`, `taxonomy.py`, `graph_analysis.py`, `diagnose_scraping.py` untouched by this story (confirmed via `git status` — those files show pre-existing, out-of-band modifications from Julien's own concurrent IDE work, unrelated to and undisturbed by this story).

### File List

- `log_review.py` (modified — added `_fetch_subsector_websites`, `_scraping_guardrail_check`, `_apply_scraping_guardrail`; `main()` updated to call the guardrail when `verdict == "structural gap"`; import line extended with `get_quality_reviews`, `normalize_domain`)

## Change Log

- 2026-08-12: Story implemented — FR-1 guardrail wired into `log_review.py`'s verdict flow. Fetches the subsector's startup domains, bulk-fetches all `scraping_diagnostic` rows (one query, not N+1), resolves the most-recent finding per domain, and — only when `structural gap` is selected — either warns that FR-1 hasn't covered this subsector yet (AC #3), confirms silently that covered sites show no issue (AC #2), or surfaces the known gap and offers to switch to `scraping artifact` (AC #1). Verified via unit-level checks with fake data, a scoping check across all 4 verdicts, and a full live end-to-end run against real subsector member data with a simulated match. No regressions (29/29 `test_storage.py`, all 10 modules import cleanly). All 3 acceptance criteria met.
