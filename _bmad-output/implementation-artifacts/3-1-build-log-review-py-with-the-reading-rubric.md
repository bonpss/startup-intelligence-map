---
baseline_commit: ce6655d6a98e93d5359b79af0cb5c9a6389ec5af
---

# Story 3.1: Build `log_review.py` with the reading rubric

Status: done

## Story

As a Julien,
I want a script that guides me through FR-3's reading rubric and captures my verdict,
so that every taxonomy-fracture decision follows a consistent method instead of ad hoc judgment.

## Acceptance Criteria

1. **Given** a subsector name and its `graph_analysis_report.json` entry, **when** I run `log_review.py <subsector>`, **then** the script displays a sample of descriptions per Louvain-detected community and guides the reading per the rubric.
2. **And** the verdict contract accepts **all 4 values from this story onward** — `isolated mis-tag`, `structural gap`, `ambiguous`, `scraping artifact` — even though `scraping artifact` is normally only reached via Story 3.2's guardrail flow; the closed vocabulary must be complete from the start, or `storage.save_quality_review()` (AD-4) would reject the write once Story 3.2 tries to use it.
3. **And** the verdict is written to `quality_review_log` (`review_type='taxonomy_split'`, `subject`=exact subsector name), with `source_snapshot` capturing the relevant slice of `subsector_splits` before it's overwritten.
4. **And** the script never modifies `taxonomy.py`, `compspro`, or `competitors` — it captures a decision, it doesn't execute one.

## Tasks / Subtasks

- [x] Task 1: Parse the CLI arg, validate the subsector, load the report (AC: #1)
  - [x] `sys.argv` usage matching `main.py`'s own pattern: `if len(sys.argv) != 2: print("Usage: python log_review.py <subsector>", file=sys.stderr); sys.exit(1)`. Multi-word subsector names are passed shell-quoted by the caller (e.g. `python log_review.py "AI Agent Platforms And Automation"`) — no special parsing needed on this script's side.
  - [x] Validate the subsector name against `TAXONOMY` (import from `taxonomy.py`) **before** any other work — build a local flattened set the same way `audit_taxonomy.py:71-72` already does (`{sub for subs in TAXONOMY.values() for sub in subs}`), not by importing `storage.py`'s private `_TAXONOMY_SUBSECTORS`. This is a read-only, friendly early check (clear CLI error instead of only failing much later inside `save_quality_review()` after the human has already done the reading work) — the real write-time enforcement stays centralized in `storage.py` per AD-1/AD-2/AD-4; this doesn't duplicate that authority, it duplicates the same harmless local-flattening pattern `audit_taxonomy.py` already established.
  - [x] Load `graph_analysis_report.json` from the repo root. If it doesn't exist, print a clear message telling Julien to run `python graph_analysis.py` first and exit — do not attempt to generate it from this script (that's `graph_analysis.py`'s job, and it must stay strictly read-only per AD-5; this script only reads the file it produces).
  - [x] Find the entry in `report["subsector_splits"]` whose `"subsector"` field exact-matches the CLI arg. If none exists, print a clear "this subsector isn't currently flagged as split — nothing to review" message and exit without writing anything to `quality_review_log`. Note: the file on disk today is a snapshot from a previous `graph_analysis.py` run — expect it to need a fresh run before real queue work, this script doesn't and shouldn't force that itself.
- [x] Task 2: Fetch this subsector's own `compspro` members (AC: #1)
  - [x] Query `compspro` via `_client()` imported from `storage.py` — matches the existing project convention for ad hoc reads (`reprocess_list.py:33-43`'s `urls_for_subsector`, `diagnose_scraping.py`'s `_candidate_websites`) rather than adding a new `storage.py` wrapper for a one-off read (AD-1 requires `storage.py` be the sole place the Supabase *client* is instantiated, not the sole place every query is written).
  - [x] `.select("name, description").contains("subsectors", [subsector])` — one query, both fields needed later, no second round-trip.
  - [x] Build a `{name: description}` dict from the result for local lookups in Task 3.
- [x] Task 3: Build the per-community sample and display it, guided by the rubric (AC: #1)
  - [x] For each `{community_id, members}` entry in the split's `"split_across"` list, find the matching community by **searching `report["communities"]` for `c["id"] == community_id`** — do not assume list-index alignment with `community_id` even though it happens to coincide today (`graph_analysis.py` assigns `id` via `enumerate()` over an already-filtered, already-sorted list; a future change to that filtering/sorting would silently break an index-based lookup without changing the `id` field itself).
  - [x] Intersect that community's `"members"` (full list, spans every subsector present in the community) with this subsector's own member-name set (Task 2's dict keys) — this gives "this subsector's members that landed in this community," which is what the rubric is actually about (not the community's other, unrelated members).
  - [x] Take a random sample (`random.sample`, module-level constant `SAMPLE_SIZE_PER_COMMUNITY = 8`, revisable — same "small, named, revisable constant" pattern as Story 2.1's `STABILITY_WINDOW`) of the intersected names per community — `report["communities"][*]["members"]` is alphabetically sorted, so a plain slice would bias toward early-alphabet names instead of a representative sample.
  - [x] Print, per community: its `id`, its `"dominant_subsectors"` (already computed by `graph_analysis.py`, a list of `[subsector, count]` pairs — reuse it, don't recompute), how many of *this subsector's* members fall in it, and the sampled descriptions (looked up from Task 2's dict).
  - [x] After the samples, print the reading rubric itself (PRD §4.2) as on-screen guidance: (1) read the sample per community; (2) assess whether communities reflect substantially different activities, or just wording variation around the same positioning; (3) verdict *isolated mis-tag* if the majority are correctly classified and only 1-2 are mistagged; (4) verdict *structural gap* if communities correspond to genuinely distinct activities; (5) if genuinely unclear, verdict *ambiguous* rather than forcing a binary call.
- [x] Task 4: Prompt for and validate the verdict (AC: #2)
  - [x] Present a numbered menu of exactly the 4 closed values, byte-for-byte matching `storage.py`'s `_TAXONOMY_SPLIT_VERDICTS` (`"isolated mis-tag"`, `"structural gap"`, `"ambiguous"`, `"scraping artifact"`) — a reworded or paraphrased string would be silently rejected by `save_quality_review()`'s AD-4 enforcement. `"scraping artifact"` is offered here as a plain menu option even though no automated guardrail check exists yet in this story (that's Story 3.2) — Julien can still choose it manually if he already knows a scraping cause, per AC #2.
  - [x] Loop on `input()` until a valid selection (1-4) is entered.
  - [x] Prompt for optional free-text notes (`input()`, blank = skip → pass `None`, not `""`, to `save_quality_review`'s `notes` param).
- [x] Task 5: Persist the verdict (AC: #3, #4)
  - [x] Call `storage.save_quality_review(review_type='taxonomy_split', subject=<the exact subsector arg>, verdict=<chosen>, source_snapshot=<the subsector_splits entry dict found in Task 1>, notes=<optional>)`. The `subsector_splits` entry (`{"subsector": ..., "total_members": ..., "split_across": [...]}`) is already a JSON object/dict — satisfies the DB's `source_snapshot` `CHECK (jsonb_typeof(...) = 'object')` constraint (Story 1.2) without any reshaping.
  - [x] No `resolution` value is passed (leave the parameter unset/`None`) — this story diagnoses and records a verdict, it does not execute a fix (AC #4); `resolution` (per PRD: "cleanup rule added / new subsector created / other") describes work this story explicitly doesn't do.
  - [x] Print a confirmation showing the saved row (verdict, subject, and the row `id`/`date_diagnosed` `save_quality_review()` returns).
  - [x] Structural guardrail for AC #4: this file imports nothing from `main.py`, `extractor.py`, or any `save_startup`/`save_relationships`/taxonomy-mutation path — there is no code path in this script capable of touching `compspro`, `competitors`, or `taxonomy.py`, by construction, not just by convention.

## Dev Notes

- **AD-5 (asymmetric diagnosis paths):** this is the human-in-the-loop half of that asymmetry — `graph_analysis.py` stays strictly read-only, and a `taxonomy_split` verdict may only be written after a human (Julien, via this script) has actually applied the reading rubric. Do not add any code path that could write a `taxonomy_split` row without the human prompt in Task 4 actually running — e.g. no `--yes`/non-interactive flag, no default verdict. The deliberation step is the point.
- **AD-2 / AD-7:** all `quality_review_log` access goes through `storage.save_quality_review()` — never `.table("quality_review_log")` directly from this script. `subject` for `taxonomy_split` is the exact subsector name, no normalization (unlike `scraping_diagnostic`'s domain normalization) — this is already how `_validate_review()` treats it, nothing new to implement here.
- **AD-4:** the verdict's closed 4-value vocabulary is enforced by `storage.save_quality_review()` itself — this script's menu (Task 4) exists for good UX (fail before the DB call, not after), not because the enforcement lives here. Get the 4 strings exactly right; a typo produces a confusing `ValueError` at the very last step after a human has already done the reading work.
- **Forward note for Story 3.2 (not this story's job):** Story 3.2 adds a guardrail check before a `structural gap` verdict is recorded (query `quality_review_log` for a matching `scraping_diagnostic` entry via `storage.normalize_domain()`-normalized startup domains, and offer `scraping artifact` as an alternative if one is found). Nothing in this story needs to anticipate that logic — just don't structure Task 4's verdict-selection code in a way that would make inserting a pre-check before the `structural gap` branch awkward later (e.g., keep it a simple menu dispatch, not deeply nested control flow).
- No new dependency, no `requirements.txt` change — `json`, `random`, `sys` are stdlib; `_client`, `save_quality_review` from `storage.py`; `TAXONOMY` from `taxonomy.py` (same import every other script already uses).
- No test suite for this story — same as Story 2.1, the no-test convention holds; Story 1.2's unit test remains the sole, explicitly scoped exception.
- No migration — no schema change in this story.

### Project Structure Notes

- New file: `log_review.py` at repo root — flat, one script per concern, no `src/` nesting (matches `ARCHITECTURE-SPINE.md`'s Structural Seed, which names this exact file as `NEW (F/FR-5)`).
- Do not modify: `graph_analysis.py` (stays read-only, AD-5), `storage.py`, `taxonomy.py`, `main.py`. Nothing in this story writes to `compspro`, `competitors`, or `taxonomy.py` (AC #4).

### `graph_analysis_report.json` shape (read in full, 2026-08-11 — current file dated 2026-08-02, will be stale by the time this runs for real)

```json
{
  "summary": {"nodes": ..., "edges": ..., "isolates": ..., "communities": ...},
  "communities": [
    {"id": 0, "size": 14, "members": ["Alpha", "Beta", ...], "dominant_subsectors": [["Productivity Tools", 46], ["CRM & Sales", 33]]}
  ],
  "subsector_splits": [
    {"subsector": "MLOps & Model Serving", "total_members": 43, "split_across": [{"community_id": 1, "members": 14}, {"community_id": 7, "members": 7}]}
  ]
}
```
- `communities[*]["members"]` is the **full** community — spans every subsector present in it, not just the one being reviewed; Task 3's intersection step is what narrows it to the relevant subset.
- `communities[*]["dominant_subsectors"]` entries are `[name, count]` pairs (JSON-serialized tuples) — unpack as `for sub, cnt in c["dominant_subsectors"]`, not as dict items.
- Live-checked 2026-08-11 against the current (dated 2026-08-02) file: 9 of the PRD's 11 queued subsectors are present in `subsector_splits` today (`Supply Chain & Logistics Automation` and `Embedded Financial Services` are not, in this snapshot) — confirms the design works against real data, but also confirms the file is a point-in-time snapshot Julien will want to refresh (`python graph_analysis.py`) before working the queue for real; this story doesn't need to force that.

### References

- [Source: `_bmad-output/planning-artifacts/epics.md` Epic 3 / Story 3.1] — acceptance criteria this story implements.
- [Source: `_bmad-output/planning-artifacts/prds/prd-SM Project-2026-08-09/prd.md` §4.2 FR-3 (reading rubric steps 1-5), §4.6 FR-5 schema, §3 Glossary "Chantier"/"Fracture"] — rubric text and `quality_review_log` schema this script writes into.
- [Source: `_bmad-output/planning-artifacts/architecture/architecture-SM Project-2026-08-10/ARCHITECTURE-SPINE.md` AD-1, AD-2, AD-4, AD-5, AD-7, Structural Seed, Capability → Architecture Map] — single Supabase access point, verdict/subject contract, asymmetric diagnosis-path rule (this script is the human-in-the-loop writer AD-5 names).
- [Source: `graph_analysis.py`, read in full 2026-08-11] — `subsector_splits`/`communities` report shape this script consumes; confirms it never writes anywhere (AD-5 precedent to match).
- [Source: `graph_analysis_report.json`, read in full 2026-08-11] — live-verified shape and current content against 9 of the 11 real queued subsectors.
- [Source: `storage.py`, read in full 2026-08-11] — `save_quality_review()`'s `subject`/`verdict`/`source_snapshot` contract for `review_type='taxonomy_split'`; `_TAXONOMY_SPLIT_VERDICTS`'s exact 4 strings to match.
- [Source: `reprocess_list.py:33-43`, `diagnose_scraping.py`] — precedent for querying `compspro` directly via `_client()` from a calling script rather than a new `storage.py` wrapper.
- [Source: `_bmad-output/implementation-artifacts/1-2-...md`, `2-1-...md`] — previous stories; confirm `save_quality_review()`/`get_quality_reviews()` are live and tested, and the no-test-suite convention holds for this story too.

## Dev Agent Record

### Agent Model Used

claude-sonnet-5

### Debug Log References

- No pytest suite for this story (explicitly out of scope, see Dev Notes). Verified instead via: (1) clean import of `log_review.py` alongside all other project modules (10 total: `storage`, `main`, `competitor`, `extractor`, `audit_taxonomy`, `graph_analysis`, `backfill_competitors`, `reprocess_list`, `diagnose_scraping`, `log_review`) — no regressions; (2) `pytest test_storage.py` — 29/29 still passing, untouched by this story.
- Live verification against the real `graph_analysis_report.json` and real `compspro` data for subsector `"MLOps & Model Serving"` (genuinely flagged as split, 2 communities): `_load_report`/`_find_split`/`_fetch_subsector_members`/`_display_samples` all run correctly end-to-end — output showed a real, plausible signal (Community #0 dominated by general AI agent/dev tooling, Community #2 dominated by AI security/governance/compliance startups), confirming the intersection + sampling logic surfaces genuinely relevant descriptions, not noise from the community's other subsectors.
- `main()`'s full control flow verified end-to-end with mocked `input()` and a **faked** `save_quality_review()` (captures call args instead of writing) — deliberately not a real write: `quality_review_log`'s `taxonomy_split` rows are meant to hold Julien's own genuine human judgment (AD-5), and a code-verification pass choosing a verdict itself would be exactly the kind of non-deliberated write AD-5 exists to prevent, and would pollute the real evidence base Story 4.1's readiness check later counts against. Confirmed the captured call had `review_type='taxonomy_split'`, exact `subject`, exact chosen `verdict`, `source_snapshot` matching the real `subsector_splits` entry, and `notes` passed through correctly (including blank input correctly becoming `None`, not `""`).
- Invalid-menu-choice retry loop verified (`"9"`, `"garbage"` rejected before a valid `"3"` is accepted).
- **Full nominal path verified end-to-end by Julien himself, with a real human verdict** (not a mock): ran via `.venv/bin/python3 log_review.py "MLOps & Model Serving"` (after diagnosing and fixing a separate environment issue — `python graph_analysis.py` had been run under the conda `base` env's networkx 2.6.3 instead of the project `.venv`'s 3.6.1, unrelated to this story's code), read the samples, applied the rubric, and recorded a genuine verdict. Confirmed live in `quality_review_log`: `MLOps & Model Serving -> isolated mis-tag`, `date_diagnosed=2026-08-11T21:04:27Z`. This is the first real `taxonomy_split` row this system has ever produced.
- All 3 early-exit paths verified via subprocess: unknown subsector → exit code 1, clear stderr, no report load attempted; wrong arg count → exit code 1, usage message; valid subsector not currently flagged as split (`"AI Driven Bioproduction"`, confirmed absent from `subsector_splits`) → exit code **0** (not an error), clear stdout message, no `save_quality_review` call.
- AC #4 structural guardrail confirmed by direct inspection of the file's imports: only `json`/`random`/`sys` (stdlib) and `_client`/`save_quality_review` from `storage.py`, `TAXONOMY` from `taxonomy.py` — no `main.py`/`extractor.py`/`save_startup`/`save_relationships` import exists anywhere in the file, so no code path can touch `compspro`/`competitors`/`taxonomy.py`, not just by convention.

### Review Findings

- [x] [Review][Patch] `save_quality_review()`'s single call at the end of `main()` had no failure recovery: if it raised (e.g. a `RemoteProtocolError` on the now-unretried write path, per Story 1.2's `_execute()` scoping), Julien's already-completed reading + verdict selection + notes were lost with no way to retry short of rerunning the whole script from scratch. Fixed via a local recovery mechanism: the full verdict payload (`subject`, `verdict`, `resolution`, `source_snapshot`, `notes`) is written to `.log_review_pending.json` immediately before the save attempt, deleted immediately after it succeeds, and a new `python log_review.py --resume` mode retries only the write from that file — no re-reading, no re-prompting. `source_snapshot` specifically motivated this over a "just re-run it" fallback: it's a precise slice of `subsector_splits` captured at diagnosis time, not something reconstructable from memory. Verified live: a forced write failure leaves a pending file with the exact correct payload; `--resume` retries with that exact payload and deletes the file on success; `--resume` with nothing pending exits cleanly with a clear message; starting a new review while an unresolved pending file exists for a *different* subsector prints a one-line warning before overwriting it (matches the "no queue, plain file" design — one pending item, not a backlog). `.log_review_pending.json` added to `.gitignore` (ephemeral local recovery state, not repo content). [log_review.py, `_save_verdict`/`_resume`/`main`]
- [x] [Review][Dismiss] `storage.normalize_domain()` unconditionally strips a leading `"www."`, which would incorrectly collapse a domain that is genuinely `www.<short-tld>` (e.g. `www.ai` → `ai`) rather than a `www.` prefix on a longer domain — confirmed real (`normalize_domain("https://www.ai")` returns `"ai"`). Not fixed: low-probability edge case for this dataset, `storage.py` is outside this story's scope, and Julien judged it not worth the churn right now.
- [x] [Review][Dismiss] `_execute()` (Story 1.2) branches on `query.request.http_method`, an attribute of `postgrest-py`'s internal `RequestConfig` rather than a documented public API, while `requirements.txt` pins no version for any dependency in the project. Confirmed real, but not fixed: `http_method` is the same attribute `postgrest-py`'s own internal retry logic reads, so it's not meaningfully less stable than the library itself; and the missing version pins are a pre-existing, project-wide condition (every dependency, not something this session introduced), better addressed as its own separate piece of work if it's ever prioritized rather than folded into this story.

### Completion Notes List

- New file `log_review.py` at repo root implements all 5 tasks: CLI arg + early validation + report loading (Task 1), `compspro` member fetch (Task 2), per-community sampling + rubric display (Task 3), verdict menu + notes prompt (Task 4), and persistence via `storage.save_quality_review()` (Task 5).
- Community lookup deliberately searches `report["communities"]` for `c["id"] == community_id` rather than indexing by position — a direct guard against the exact kind of assumption bug the story's own Dev Notes called out (today's `graph_analysis.py` output happens to keep `id` aligned with list position, but the `id` field is the actual contract, not the position).
- Added one small UX addition beyond the story's literal text, still within its spirit: if a community's intersection with the target subsector's *current* live members comes up empty (data changed since the report snapshot was taken), the script prints a one-line note about likely staleness instead of silently showing an empty section — cheap, and directly serves the story's own documented staleness caveat.
- `source_snapshot` is passed the `subsector_splits` entry dict exactly as found in the report, unmodified — already a JSON object, satisfies the DB `CHECK` constraint from Story 1.2 with no reshaping.
- `resolution` is never passed (stays unset/`None`) — consistent with AC #4, this story records a verdict, it doesn't execute a fix.
- No `requirements.txt` change, no migration, no modification to `graph_analysis.py`/`storage.py`/`taxonomy.py`/`main.py` — confirmed by `git status` before finalizing (only `log_review.py` is new).

### File List

- `log_review.py` (new; patched post-review — see Review Findings)
- `.gitignore` (modified — added `.log_review_pending.json`)

## Change Log

- 2026-08-11: Story implemented — new `log_review.py` at repo root. Validates the given subsector against `TAXONOMY`, loads `graph_analysis_report.json`, finds the subsector's split entry (or exits cleanly if not flagged as split), fetches the subsector's current `compspro` members, samples descriptions per community (intersected against the subsector's own members, not the community's unrelated members), displays them alongside the PRD §4.2 reading rubric, prompts for one of the 4 closed verdict values plus optional notes, and persists via `storage.save_quality_review(review_type='taxonomy_split', ...)` with `source_snapshot` set to the exact `subsector_splits` slice used. No test suite added (out of scope per story); verified via clean imports (no regressions across all 10 project modules), `test_storage.py` still 29/29, a live read-only run against real report/DB data for `"MLOps & Model Serving"`, and a mocked end-to-end `main()` run (fake `save_quality_review` — deliberately not a real write, to avoid inserting a non-human-deliberated verdict into `quality_review_log`). All 4 acceptance criteria met.
- 2026-08-11: Code review (`code-review` skill) — 1 confirmed finding fixed: an unhandled `save_quality_review()` failure at the very end of the flow could lose Julien's already-completed reading + verdict + notes with no recovery path. Fixed via a local `.log_review_pending.json` recovery file (written before the save attempt, deleted after success) and a new `python log_review.py --resume` mode that retries only the write. Verified live: forced-failure → correct pending file → `--resume` succeeds with the exact preserved payload → file deleted; `--resume` with nothing pending exits cleanly; starting a new review over an unresolved pending one warns before overwriting. 2 findings confirmed real but dismissed per Julien's explicit decision: `normalize_domain()`'s `www.<short-tld>` edge case (low-probability for this dataset, `storage.py` out of this story's scope) and `_execute()`'s reliance on `postgrest-py`'s internal `http_method` attribute (no less stable than the library's own internal retry logic; the broader missing-version-pins issue is pre-existing and project-wide, not specific to this change). Full regression re-confirmed after the patch (29/29 `test_storage.py`, all 10 modules import cleanly).
