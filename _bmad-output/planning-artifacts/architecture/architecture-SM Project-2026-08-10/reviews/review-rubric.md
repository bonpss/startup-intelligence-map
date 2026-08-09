---
title: Reviewer Gate — Good-Spine Checklist
target: ARCHITECTURE-SPINE.md (architecture-SM Project-2026-08-10)
reviewed: 2026-08-10
verdict: CONDITIONAL PASS — one internal contradiction must be fixed before this spine gates story creation
---

# Review

## Overall verdict

**Conditional pass.** The spine is unusually well-grounded — nearly every claim about the brownfield codebase and the installed stack checks out against the live system, not just the PRD. But there is one genuine internal contradiction between an AD's Rule and the Structural Seed that must be resolved before this can gate downstream work, plus a couple of real gaps.

## Findings

### 1. [HIGH] AD-5 and the Structural Seed directly contradict each other on `diagnose_scraping.py`

AD-5's Rule (lines 98-103) is explicit: "`graph_analysis.py` and `diagnose_scraping.py` never write to `quality_review_log` or any table — they only read and report. Writing a verdict is `log_review.py`'s job alone."

The Structural Seed (line 152) annotates the same file: `diagnose_scraping.py # NEW (FR-1) — read-only scraping-heterogeneity characterization; writes via storage.py`

`storage.py`'s entire raison d'être per AD-1 is Supabase access — "writes via storage.py" can only mean a DB write. That directly contradicts AD-5's "never write to... any table." This is exactly the kind of self-contradiction the checklist flags: an implementer building from the Seed table would wire a write path AD-5 forbids. One of the two must be corrected — most likely the Seed annotation is a copy-paste leftover from the `log_review.py` line directly below it, which legitimately does write via `storage.py`.

### 2. [MEDIUM] New table's RLS posture is undecided, despite every existing table having it on

Checked live via Supabase MCP (`list_tables`, project `umxlmpyxiujyzpkxxkmr`): all 7 existing tables (`startups`, `sources`, `jobs`, `tags`, `job_tags`, `scrape_logs`, `compspro`, `competitors`) have `rls_enabled: true`. AD-2/AD-3 spec `quality_review_log`'s access pattern and migration mechanism in detail but say nothing about RLS. Given every precedent table in this DB has RLS on, silently leaving it undecided for the one new table is a real divergence point at story/migration-write time — flip a coin on RLS and you either break the service-role scripts or leave the row-level policy inconsistent with the rest of the schema. Should be a one-line addition to AD-2 or AD-3 ("RLS enabled, consistent with existing tables, [permissive/service-role-only] policy") rather than left to be improvised when `001_create_quality_review_log.sql` is written.

### 3. [LOW] Testing strategy is silently blank, not deferred

Deployment/environments gets explicit treatment (Structural Seed note + a Deferred bullet). Testing does not appear anywhere — not decided, not deferred, not an open question — despite the spine introducing two new scripts and a migration, and despite `docs/index.md` flagging "no test suite anywhere in the project" as a known gap. This isn't necessarily wrong (matching existing convention of no tests may be the right v1 call), but per the checklist's "every dimension the altitude owns is decided, deferred, or an open question," it should say so explicitly rather than being absent.

### 4. [INFO] AD-3's framing slightly undersells its own precedent

Live `list_migrations` shows 7 prior Supabase-side migrations already applied via what is presumably the same MCP `apply_migration` path (`create_competitors_table`, `add_sub_subsectors_to_compspro`, etc.). AD-3 argues for MCP + versioned file from first principles (CLI too heavy, hand-rolled runner unjustified) without noting that MCP-applied migrations are already the project's de facto mechanism — only the "save the SQL to git" half is actually new. Not a defect, just a missed opportunity to ground the AD even more solidly; doesn't change the verdict.

## Checklist walk-through

- **Real divergence points fixed, none missed:** Mostly yes (Supabase access point, verdict vocabulary, diagnosis/decision separation, migration tracking, retroactive recalibration all map to real risks). RLS posture (#2) and the diagnose/write contradiction (#1) are the misses.
- **Every AD's Rule enforceable and prevents its divergence:** Yes for AD-1, 2, 3, 4, 6 — each is a checkable, human-reviewable rule consistent with a no-CI solo-dev project. AD-5 fails this test only because the Seed contradicts it (#1).
- **Nothing in Deferred could let two units diverge:** Confirmed — deployment, auth, FR-2, extraction-logic rewrite, Features D/E/G, Supabase CLI, structured logging are all genuinely inert this cycle; none affect FR-1–5 build consistency.
- **Named tech verified-current:** Confirmed by direct `pip show` against `.venv` — every version in the Stack table (Python 3.11.5, FastAPI 0.136.3, Uvicorn 0.49.0, supabase-py 2.30.1, mistralai 2.4.9, Playwright 1.60.0, playwright-stealth 2.0.3, networkx 3.6.1, httpx 0.28.1, tenacity 9.1.4, python-dotenv 1.2.2, html2text 2025.4.15) matches exactly. The spine states its verification method (live check, not training data) — checklist satisfied.
- **Ratifies rather than contradicts the brownfield codebase:** Confirmed — `storage.py` is the only file importing `supabase` (grep), `graph_analysis.py` has zero write calls (grep), `competitor_validator.py`'s raw-SQL RPC pattern is real (line 37-49, `/rest/v1/rpc/exec_sql` via httpx), `docs/index.md` independently corroborates "no migrations directory," "no deployment config," "`/api/ingest` unauthenticated." `COMPETITOR_THRESHOLD` is correctly attributed to `storage.py`. Minor: AD-6 cites "3,780" `competitors` rows checked on 2026-08-10; live count is now 3,799 — expected drift from ongoing ingestion, not a contradiction.
- **PRD FR-1 through FR-5 covered, none silently uncovered:** Yes — the Capability → Architecture Map lists all five; FR-2 is explicitly marked "not built this cycle / Deferred" rather than omitted.
- **Deployment/infra/ops dimensions not silently blank:** Deployment & environments and Auth are both explicitly named (twice each — inline note plus Deferred bullet). Testing is the one dimension left genuinely blank (#3).
