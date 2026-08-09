---
stepsCompleted: [step-01-validate-prerequisites, step-02-design-epics, step-03-create-stories, step-04-final-validation]
inputDocuments: ['_bmad-output/planning-artifacts/prds/prd-SM Project-2026-08-09/prd.md', '_bmad-output/planning-artifacts/architecture/architecture-SM Project-2026-08-10/ARCHITECTURE-SPINE.md']
---

# SM Project - Epic Breakdown

## Overview

This document provides the complete epic and story breakdown for SM Project, decomposing the requirements from the PRD and Architecture spine into implementable stories. Scope: the A/C/B/F pipeline-stabilization initiative (FR-1 through FR-5) — diagnosing scraping heterogeneity, building a persistent quality-review log, working the taxonomy fracture queue, and recalibrating competitor detection, on the existing brownfield Python/FastAPI/Supabase codebase.

## Requirements Inventory

### Functional Requirements

FR-1: Diagnose scraping heterogeneity — characterize the scraped input's quality/completeness issues across different site types before any fix is attempted. No dominant cause identified yet; a history of several distinct failure types has already been observed. Priority: v1, high (first link in the A → C → B chain). Consequences: a characterization pass exists (even informal) enumerating failure types across a sample of scraped sites; findings feed a decision on whether/how to fix extraction. Out of scope: the fix itself.

FR-2: Reduce LLM over-scraping cost — send a targeted subset of a page's content to the LLM rather than the full page markdown (currently up to 30,000 characters). Deferred technical debt, post-v1 priority — not blocking while usage stays solo.

FR-3: Work the fracture queue while logging fracture type — for each of ~11 large, never-diagnosed taxonomy subsectors, run the diagnose-then-decide chantier loop and record which fracture type was found.
  - **Hard precondition — reading rubric (not optional, blocks starting the queue):** a consistent per-subsector reading method must exist *before* the queue is worked, not improvised subsector-by-subsector (Louvain community sample → assess distinctness → verdict: isolated mis-tag / structural gap / ambiguous). Starting the queue without it risks contaminating `quality_review_log` from its very first entry — inconsistent judgment across the 11 diagnoses would invalidate the evidence this FR is meant to produce.
  - **Operational dependency on FR-1 (not just sequencing — a per-subsector check):** for every subsector diagnosed, FR-3 must be able to rule out "known scraping gap (A)" before concluding "taxonomy structural gap (C)." This requires FR-1's characterization work to be sufficiently advanced first, and its findings to be queryable (via `quality_review_log`, `review_type='scraping_diagnostic'`) at the moment each subsector is diagnosed.
  - Consequences: each subsector gets a recorded outcome; the accumulated log resolves the open Scenario A/B structural-risk question (finite catch-up vs. recurring problem rooted in extraction). Dependency: relies on FR-5's `quality_review_log` existing first.

FR-4: Recalibrate threshold and transitive exploration — once A (FR-1) and C (FR-3) have progressed enough to reduce upstream noise, revisit `COMPETITOR_THRESHOLD` (currently 0.85) and `explore_transitive()` for accuracy.
  - **(a) Start trigger (concrete, not "eventually" — both conditions required):** begins only once ≥9 of 11 queued subsectors (≥80%) are diagnosed and logged in `quality_review_log` AND no new fracture type has appeared across the last 3 subsectors worked. The 80% figure is revisable but not to be skipped as a gate.
  - **(b) Scope (concrete, not just future ingests):** recalibration re-scores retroactively across the entire existing `competitors` table (3,780 relationships), not only startups added after the recalibration. Verified zero-cost before committing to this scope: `score` is populated on 100% of existing rows (confirmed live against the production Supabase project).
  - "B reliable" is the exit milestone (see NFR1) — necessary but not sufficient for beta; multi-tenancy and accessibility are a separate future chantier, not this epic's concern. v1's goal stays: the product runs correctly for solo use.

FR-5: Persistent, queryable quality-review log — build a durable `quality_review_log` Supabase table (generic schema: review_type, subject, date_diagnosed, source_snapshot, verdict, resolution, notes) to capture the diagnose-then-decide outcome of every chantier, since `graph_analysis_report.json` is overwritten on every run and cannot serve as a historical log.
  - **Schema is deliberately generic, not taxonomy-specific — implementation must not narrow it.** `review_type` already covers at least two distinct uses in this epic set alone (`taxonomy_split` for FR-3, `scraping_diagnostic` for FR-1), and is designed to extend to future quality loops beyond both (e.g. auditing FR-4's recalibrated competitor scores). Do not hardcode taxonomy-only assumptions (e.g. a closed enum, a taxonomy-only table name, or fields that only make sense for subsectors) into the table or its access function.
  - Consequences: table created via a minimal migration mechanism (not ad hoc SQL, see Architecture AD-3); write mechanism instrumented at the point a diagnosis is read and a decision is made; FR-3's queue processing writes one row per subsector worked; FR-1's `diagnose_scraping.py` writes its own findings directly (see Architecture AD-5, AD-7).

### NonFunctional Requirements

No dedicated NFR section exists in the PRD (confirmed appropriate for this project's shape — solo tool, no scale/security stakes yet — during PRD review). The closest quality bars are captured as Success Metrics rather than NFRs:

NFR1 (from PRD Success Metrics SM-1): "B reliable" — precision (not recall) on a stratified sample validated via `competitor_validator.py`, target 90-95% (exact threshold still open), measured after FR-4's recalibration.

### Additional Requirements

From the Architecture spine (`ARCHITECTURE-SPINE.md`), binding AD-1 through AD-7:

- AD-1: All Supabase access goes through `storage.py` — no other module imports `supabase-py` directly. `[ADOPTED]` existing convention, extended to all new work.
- AD-2: `quality_review_log` access via a new `storage.save_quality_review()` (+ paired getter), queryable by `review_type` and `subject` — no separate module for quality-loop data.
- AD-3: Schema changes applied via the Supabase MCP `apply_migration` tool AND saved as a versioned file under `migrations/` (sequential zero-padded prefix, e.g. `001_create_quality_review_log.sql`, never reused). New tables get Row Level Security enabled at creation, matching all 7 existing tables.
- AD-4: `quality_review_log.verdict` is a plain `text` column, not a DB enum — the 4-value constraint for `review_type='taxonomy_split'` is enforced in application code (`storage.save_quality_review()`), not the schema.
- AD-5: `graph_analysis.py` stays strictly read-only, no writes anywhere. `diagnose_scraping.py` (new) writes its own `scraping_diagnostic` findings to `quality_review_log` directly as it runs. A `taxonomy_split` verdict may only be written after a human applies FR-3's reading rubric (today via `log_review.py`, new).
- AD-6: When `COMPETITOR_THRESHOLD` changes, all existing `competitors` rows are re-evaluated against the new threshold, not just future ingests.
- AD-7: `review_type` is a small, fixed, application-validated vocabulary (not a DB enum). `subject` format is defined per `review_type`: `taxonomy_split` → exact subsector name; `scraping_diagnostic` → site domain. `diagnose_scraping.py` derives its own progress by querying `quality_review_log`, not a separate local state file.
- Deployment & environments: none exist and none are introduced by this work (no Dockerfile/CI/distinct environments) — explicitly deferred, not silently omitted.
- No starter template applies — this is brownfield work on an existing codebase, not a greenfield setup.
- No test suite exists for this codebase and this work does not introduce one (existing project convention, explicitly deferred).
- Dependency version risks flagged for implementers (not blocking): `networkx` 3.6.x has had deprecation/rename churn worth checking against `graph_analysis.py`'s calls; `playwright-stealth` 2.x replaced 1.x's API (relevant if FR-1 touches the scrape path); `mistralai`'s retry-pattern behavior at the pinned 2.4.9 wasn't independently confirmed.

### UX Design Requirements

No UX design contract exists for this project — none found, none requested by the user, not applicable to this backend/pipeline work.

### FR Coverage Map

FR-5: Epic 1 - Quality decision infrastructure (`quality_review_log` table + migration + `storage.save_quality_review()`)
FR-1: Epic 2 - Scraping-heterogeneity diagnosis, persisted via Epic 1's infrastructure
FR-3: Epic 3 - Taxonomy fracture queue, gated on Epic 1 and informed by Epic 2's progress
FR-4: Epic 4 - Competitor detection recalibration, gated on Epic 3's concrete start trigger
NFR1: Epic 4 - "B reliable" precision target, validated after FR-4's recalibration
FR-2: Deferred — not scheduled this cycle (LLM over-scraping cost, post-v1)

## Epic List

### Epic 1: Persistent Quality Decision Infrastructure
Julien has a durable, queryable record (`quality_review_log`) for every quality-loop decision — ending the pattern where each past taxonomy investigation's reasoning evaporated into an overwritten report.
**FRs covered:** FR-5

### Epic 2: Scraping-Heterogeneity Diagnosis
Julien gets a persisted characterization of the scraping failure types actually observed across different site types — the evidence base for any future fix to A.
**FRs covered:** FR-1

### Epic 3: Taxonomy Fracture Queue
Julien works the ~11 pending subsectors with a consistent reading rubric, a tracked verdict per subsector, and a hard check that rules out known scraping artifacts before concluding a real taxonomy gap.
**FRs covered:** FR-3

### Epic 4: Competitor Detection Recalibration
Julien gets a recalibrated, retroactively-rescored competitor graph — precision-first, not just more links — reaching the "B reliable" milestone.
**FRs covered:** FR-4, NFR1

### Epic 5: Ingestion Pipeline Hardening
Surfaced by the 2026-08-12 `code-review` of the fast-path scraping (`_fetch_light`/trafilatura) and concurrent-ingestion (`asyncio.to_thread`) changes Julien made directly while Epics 1-4 were in flight — not part of the original A/C/B/F PRD scope, but the review findings warranted tracked stories rather than ad hoc patches. Julien triaged and grouped the findings himself (2026-08-12).
**FRs covered:** none (post-hoc hardening, outside the FR-1..FR-5 scope)

## Epic 1: Persistent Quality Decision Infrastructure

Julien has a durable, queryable record (`quality_review_log`) for every quality-loop decision — ending the pattern where each past taxonomy investigation's reasoning evaporated into an overwritten report. **FRs covered:** FR-5.

### Story 1.1: Create the `quality_review_log` table via migration

As a Julien,
I want the `quality_review_log` table created via a tracked, reproducible migration,
So that schema changes for this table never repeat the untracked-drift pattern the table itself exists to prevent.

**Acceptance Criteria:**

**Given** no `quality_review_log` table exists yet
**When** migration `migrations/001_create_quality_review_log.sql` is applied via the Supabase MCP (`apply_migration`)
**Then** the table exists with the schema (`review_type`, `subject`, `date_diagnosed`, `source_snapshot`, `verdict`, `resolution`, `notes`), `verdict` as a plain `text` column (no DB enum, AD-4), and Row Level Security enabled (AD-3)
**And** the SQL actually executed matches the file versioned at `migrations/001_create_quality_review_log.sql`, following the naming convention (zero-padded sequential prefix, never reused)

### Story 1.2: Build `storage.save_quality_review()` with the `review_type`/`subject` contract

As a Julien,
I want a single, validated write/read path to `quality_review_log`,
So that every future consumer (FR-1, FR-3) writes and queries it consistently, with no typo silently orphaning an entry.

**Acceptance Criteria:**

**Given** the `quality_review_log` table exists (Story 1.1)
**When** `storage.save_quality_review()` is called with a `review_type` outside the known set (`taxonomy_split`, `scraping_diagnostic`)
**Then** the call is rejected with an explicit error, not a silent write
**And** for `review_type='taxonomy_split'`, `subject` must match an exact `TAXONOMY` key; for `scraping_diagnostic`, a site domain normalized via a new `storage.normalize_domain(url)` (AD-8: strip scheme, strip leading `www.`, strip trailing slash, lowercase) — `save_quality_review()` applies it internally so a caller can't forget to normalize before writing
**And** `storage.normalize_domain()` is exported for other callers to reuse for lookups (not just writes) — Epic 2 and Epic 3 depend on this existing already, they do not reimplement it
**And** a paired read function allows querying by `review_type` + `subject`
**And** a unit test covers: rejection of an invalid `review_type`, acceptance of both known `review_type` values, and the expected `subject` format for each *(requirement surfaced by Murat during party-mode review)*

> **Note — deliberate, scoped exception to "no test suite" (not a convention change):** this is the one unit test in this initiative, justified specifically because `save_quality_review()` is the single access point (AD-1/AD-2) every other story writes through — a silently-failing validation here contaminates FR-1 and FR-3 downstream without anyone noticing. Nothing else in Epics 1-4 is expected to carry a test on this basis alone.

## Epic 2: Scraping-Heterogeneity Diagnosis

*Depends on Story 1.2 delivered: `diagnose_scraping.py` calls `storage.save_quality_review()`.*

Julien gets a persisted characterization of the scraping failure types actually observed across different site types — the evidence base for any future fix to A. **FRs covered:** FR-1.

### Story 2.1: Build `diagnose_scraping.py`

As a Julien,
I want a read-only script that samples scraped sites and characterizes the failure types it finds,
So that I have persisted evidence of scraping heterogeneity before deciding whether/how to fix extraction.

**Acceptance Criteria:**

**Given** URLs for sites already present in `compspro`
**When** `diagnose_scraping.py` runs
**Then** it keeps sampling until no new failure type has appeared across N consecutive sites (N revisable — same pattern as Story 4.1's "no new fracture type in the last 3" stabilization signal), rather than a fixed sample size decided blind
**And** it re-runs `scrape()` (`main.py`) on each sampled site and characterizes the result (incomplete content, blocking page, content drowned in noise, etc. — the failure-type vocabulary itself stays intentionally open, free text; only the script's stopping condition is being pinned down here)
**And** each characterization is written directly to `quality_review_log` via `storage.save_quality_review()` (`review_type='scraping_diagnostic'`, `subject`=site domain — normalization already enforced internally by `save_quality_review()`, built in Story 1.2), not to a local ephemeral report
**And** before re-characterizing a domain, the script queries `quality_review_log` (using the same normalized form) to avoid duplicating an already-known diagnosis (AD-7 — no separate local state file); two URL variants of the same site (e.g. `https://Example.com/` and `example.com`) must resolve to one `subject`, not two
**And** the script never writes anywhere other than `quality_review_log` — no modification to `compspro` or `competitors` (AD-5, diagnosis-only)

## Epic 3: Taxonomy Fracture Queue

Julien works the ~11 pending subsectors with a consistent reading rubric, a tracked verdict per subsector, and a hard check that rules out known scraping artifacts before concluding a real taxonomy gap. **FRs covered:** FR-3.

### Story 3.1: Build `log_review.py` with the reading rubric

As a Julien,
I want a script that guides me through FR-3's reading rubric and captures my verdict,
So that every taxonomy-fracture decision follows a consistent method instead of ad hoc judgment.

**Acceptance Criteria:**

**Given** a subsector name and its `graph_analysis_report.json` entry
**When** I run `log_review.py <subsector>`
**Then** the script displays a sample of descriptions per Louvain-detected community and guides the reading per the rubric
**And** the verdict contract accepts **all 4 values from this story onward** — `isolated mis-tag`, `structural gap`, `ambiguous`, `scraping artifact` — even though `scraping artifact` is normally only reached via Story 3.2's guardrail flow; the closed vocabulary must be complete from the start, or `storage.save_quality_review()` (AD-4) would reject the write once Story 3.2 tries to use it
**And** the verdict is written to `quality_review_log` (`review_type='taxonomy_split'`, `subject`=exact subsector name), with `source_snapshot` capturing the relevant slice of `subsector_splits` before it's overwritten
**And** the script never modifies `taxonomy.py`, `compspro`, or `competitors` — it captures a decision, it doesn't execute one

### Story 3.2: Wire the FR-1 guardrail into `log_review.py`

As a Julien,
I want log_review.py to check for a known scraping gap before I conclude a structural taxonomy gap,
So that a scraping artifact from A is never misattributed to C.

**Acceptance Criteria:**

**Given** I'm about to record a `structural gap` verdict for a subsector
**When** the script normalizes the subsector's startup domains (via `storage.normalize_domain()`, built in Story 1.2 — AD-8, never a normalization logic local to this script) and checks `quality_review_log` for matching `scraping_diagnostic` entries
**Then** if a match exists, the script surfaces the known diagnosis and offers `scraping artifact` as the alternative verdict
**And** if no match exists, `structural gap` or `isolated mis-tag` records normally
**And** if FR-1 (Epic 2) hasn't run on this subsector yet, the script warns that the guardrail has nothing to check against, rather than silently implying a complete check

## Epic 4: Competitor Detection Recalibration

Julien gets a recalibrated, retroactively-rescored competitor graph — precision-first, not just more links — reaching the "B reliable" milestone. **FRs covered:** FR-4, NFR1.

### Story 4.1: Start-trigger readiness check

As a Julien,
I want a readiness check against `quality_review_log`,
So that I don't start recalibrating before the evidence base is stable enough to trust.

**Acceptance Criteria:**

**Given** `taxonomy_split` entries logged in `quality_review_log`
**When** I run the readiness check
**Then** it reports whether ≥9 of the 11 queued subsectors are diagnosed and logged
**And** it reports whether no new fracture type has appeared across the last 3 subsectors worked (chronological by `date_diagnosed`)
**And** it returns a clear "ready"/"not ready" verdict, not just raw counts to interpret myself

### Story 4.2: Retroactive recalibration with marking (not deletion)

As a Julien,
I want to raise `COMPETITOR_THRESHOLD` and mark existing relationships that no longer qualify,
So that the graph reflects one consistent, stricter precision bar without losing the rejected relationships as future calibration data.

**Acceptance Criteria:**

**Given** a new, higher `COMPETITOR_THRESHOLD` value chosen after reviewing FR-1/FR-3 progress — **this story assumes a raised threshold; a lowered threshold would require a full LLM rescoring of previously-rejected candidate pairs (never persisted) and is explicitly out of scope**
**When** the constant is updated in `storage.py` (code change) and the recalibration script runs
**Then** a migration (per AD-3: MCP `apply_migration` + versioned file, `migrations/002_add_active_to_competitors.sql`) adds an `active` boolean column to `competitors`, default `true`
**And** all existing rows are re-evaluated against the new threshold using their already-stored `score` — no new LLM calls needed (100% of rows have `score` populated)
**And** rows whose score falls below the new threshold are marked `active=false`, never deleted — preserved as potential future input to a `review_type='competitor_score_audit'` entry in `quality_review_log`
**And** rows still meeting the new threshold remain `active=true`, unchanged
**And** every consumer that surfaces relationships as *currently valid* filters on `active=true` — confirmed by full code audit (grep), not assumed: `graph_app.py:104` (`/api/graph/all`), `graph_app.py:141-142` (`/api/graph/{name}`), `graph_analysis.py:49` (`fetch_edges()`, otherwise inactive links would also skew community/betweenness detection), `competitor_validator.py:68` (**critical** — this is literally the SM-1 sampling path; unfiltered, it would validate a mix of active/inactive relationships and corrupt the precision metric FR-4 exists to produce), `competitor_validator.py:164` (the "remaining unchecked" counter, for consistency)
**And** `storage.py`'s `get_known_competitors()` (lines 53-54) and `relationship_exists()` (line 78) deliberately stay **unfiltered** — their semantics are "already evaluated" (dedup), not "currently valid for display," and filtering them would cause a redundant LLM rescore of an already-known pair on a future ingest

> **Deferred dependency, not a Story 4.2 concern:** if lowering the threshold is ever considered (out of scope today), `get_known_competitors()`/`relationship_exists()` staying unfiltered would then also block a legitimate rescoring of `active=false` pairs. Noted here so it surfaces as a known implicit dependency when that scenario is actually proposed, not rediscovered as a surprise.

## Epic 5: Ingestion Pipeline Hardening

Surfaced by the 2026-08-12 `code-review` of the fast-path scraping (`_fetch_light`/trafilatura) and concurrent-ingestion (`asyncio.to_thread`) changes Julien made directly while Epics 1-4 were in flight. **FRs covered:** none — post-hoc hardening outside the FR-1..FR-5 scope, tracked as stories rather than patched ad hoc so the reasoning stays traceable.

### Story 5.1: Fast-path fallback signal (anti-bot detection + content completeness)

As a Julien,
I want `_fetch_light` to detect a blocked/interstitial page or JS-incomplete content and fall back to Playwright,
so that ingestion never silently saves interstitial/boilerplate text as a startup's real content, and never silently loses a JS-rendered logo or LinkedIn link the light fetch couldn't see.

**Acceptance Criteria:**

**Given** `_fetch_light`'s current signal is only "trafilatura-extracted text length ≥ 200 chars"
**When** the fetched page is actually a bot-block/consent-wall interstitial, or is JS-hydrated with real body text but a JS-injected header/logo/footer-LinkedIn-link
**Then** `_fetch_light` must recognize this via a richer content-quality signal (too-short text, an abnormal text/boilerplate ratio, known interstitial keywords — same category of heuristic `diagnose_scraping.py`'s `characterize()` already uses for `blocking_page`/`content_drowned_in_noise`, reuse or align with it rather than inventing a third vocabulary) and return `None`, triggering the existing Playwright fallback in `scrape()`
**And** a page that legitimately passes this richer check is returned as today — this story raises the bar for what counts as a successful light fetch, it does not change `_scrape_playwright`'s own already-correct anti-bot/cookie-banner handling
**And** the `linkedin_url`/logo extraction regression this story closes is the same class of gap Story 2.1's `diagnose_scraping.py` was built to characterize for scraped content generally — consider whether a genuinely blocked/JS-incomplete `_fetch_light` result should itself be logged via `storage.save_quality_review(review_type='scraping_diagnostic', ...)` when `diagnose_scraping.py` next samples that domain, or whether that's already covered by the existing characterization path once Playwright takes over (open question for story context-engineering, not decided here)

### Story 5.2: Concurrent-ingestion write safety

As a Julien,
I want concurrent `/api/ingest` calls for the same (or related) company to not race on check-then-write DB sequences,
so that two overlapping ingests can't create duplicate `compspro` rows or duplicate competitor relationships now that `asyncio.to_thread` genuinely parallelizes the ingestion pipeline.

**Acceptance Criteria:**

**Given** `main.ingest()` now runs `_ingest_sync` (which calls `storage.save_startup` and `storage.save_relationships`/`competitor.compare`) via `asyncio.to_thread`, allowing two concurrent ingests to interleave their lookup-then-insert-or-update sequences
**When** two `/api/ingest` calls for the same or a related startup domain run concurrently
**Then** an `asyncio.Lock` keyed on `storage.normalize_domain()`-normalized domain serializes ingestion of the same company, without a DB migration/unique-constraint approach (judged overkill for a solo, not-yet-multi-user tool per Julien 2026-08-12 — revisit with a DB-level constraint if the project ever goes multi-user)
**And** the widened Mistral retry budget (7 attempts, up to ~124s backoff + up to 120s timeout per attempt) no longer lets a single interactive `/api/ingest` request hang the browser client for up to ~16 minutes with no feedback — split the retry/timeout budget so the interactive path (`/api/ingest`) is bounded tighter than the batch/backfill path (`reprocess_list.py`, `backfill_competitors.py`), rather than the one shared setting both paths currently use

### Story 5.3: Ingestion pipeline cleanup

As a Julien,
I want the retry-config duplication, the `quality_review_log` read-path duplication, and `diagnose_scraping.py`'s now-unreachable noise heuristic cleaned up,
so that the codebase doesn't keep drifting the way `competitor_validator.py`'s retry config already has, and every diagnostic heuristic that's supposed to run actually can.

**Acceptance Criteria:**

**Given** `storage.py`, `competitor.py`, `extractor.py`, and `competitor_validator.py` each hand-roll their own near-identical `tenacity` retry config for the same "is this a transient/retryable error" concept — and `competitor_validator.py`'s was already left un-widened when the other three were tuned together, proving the copy-paste approach drifts
**When** this story lands
**Then** the retry-predicate/backoff configuration is factored into one shared module all four call sites import, consistent with this project's existing "common logic factored, not duplicated" convention (e.g. `storage.normalize_domain()`, AD-8)

**Given** `storage.get_quality_reviews()` re-implements the `review_type`/`subject` validation branching `_validate_review()` already encodes, as two independently-maintained copies of the same contract
**When** this story lands
**Then** the shared `review_type`+`subject` validation is extracted into one helper both `save_quality_review()`'s write path and `get_quality_reviews()`'s read path call, with only the write-only verdict check remaining separate — landed as its own dedicated, traceable change (Story 1.2's delivered code, touch it deliberately)

**Given** `diagnose_scraping.py`'s `content_drowned_in_noise` heuristic checks for markdown-link syntax (`[text](url)`) that `main.py`'s trafilatura fast-path no longer produces (plain text, not markdown), making that verdict effectively unreachable for most sites sampled through the fast path
**When** this story lands
**Then** the heuristic is adapted to signal characteristics of trafilatura's actual plain-text output (e.g. repetition ratio, boilerplate-phrase density) instead of markdown link density, restoring FR-1's diagnostic coverage for this failure category
