---
title: Input Reconciliation — ARCHITECTURE-SPINE.md vs prd.md
step: Finalize step 2 (input reconciliation)
input: prd.md (prd-SM Project-2026-08-09)
target: ARCHITECTURE-SPINE.md (architecture-SM Project-2026-08-10)
date: 2026-08-10
---

# Input Reconciliation: PRD → Architecture Spine

**Input reconciled against:** `prd.md` (PRD: SM Project, status: final, 2026-08-09/10)
**Spine reconciled:** `ARCHITECTURE-SPINE.md` (SM Project — Pipeline Stabilization A/C/B/F, 2026-08-10)

## Overall assessment

The spine is well-grounded and faithful on the majority of load-bearing PRD content: all 5 FRs appear in the Capability → Architecture Map with no gaps, FR-1's diagnostic-only scope is correctly modeled as read-only (AD-5), FR-3's Precondition 2 (reading rubric) is explicitly cited in AD-5's rationale, FR-5's migration-traceability requirement is correctly modeled (AD-3), the "B reliable ≠ ready for beta" distinction is explicitly preserved in Deferred, and the Deferred section does not defer anything the PRD actually wanted in v1 scope. However, four gaps/discrepancies were found, detailed below.

## Gaps / Discrepancies

### 1. FR-3's Precondition 1 (FR-1 sequencing guardrail) is not architecturally encoded

- **PRD citation:** §4.2, FR-3, "Precondition 1 — sequencing (confirmed)": "FR-1 does not need to be *complete* before FR-3 starts, but must be *sufficiently advanced*... Without this guardrail, FR-3 risks misclassifying a scraping artifact... as a taxonomy structural gap, which would pollute `quality_review_log` from its very first use and corrupt the Scenario A/B decision." Also §4.2 Consequences: "Before recording a verdict for a subsector, FR-3's diagnosis explicitly checks 'is this explainable by a known scraping gap from FR-1?'" And §6.1: "FR-1 and FR-3/FR-5 are not run in parallel without a link between them."
- **Spine citation:** AD-2 ("`quality_review_log` access lives in `storage.py`") only specifies "a new `storage.save_quality_review()` (and a paired getter as needed)" — it does not state that this getter must expose already-logged FR-1 `scraping_diagnostic` rows to whoever is working the FR-3 queue. AD-5 ("Diagnosis is read-only, decoupled from decision") cites FR-3's Precondition 2 (reading rubric) by name but never mentions Precondition 1. The Capability Map's FR-3 row (`graph_analysis.py` + `log_review.py`, governed by AD-5/AD-2/AD-4) has no reference to `diagnose_scraping.py` or its output at all.
- **Why it matters:** this is a hard, confirmed gate in the PRD (explicitly promoted from an Open Question during Finalize, per §8's footnote about Q5 becoming a precondition) that protects the integrity of the very Scenario A/B decision `quality_review_log` exists to support. The spine's data-access design (AD-2) is silent on whether the FR-3 verdict-writing workflow can actually read FR-1's findings, leaving the PRD's required cross-check structurally unsupported rather than explicitly enabled.

### 2. FR-4's concrete start trigger is entirely absent from the spine

- **PRD citation:** §4.3, FR-4, "Start trigger (concrete, revisable)": "(1) At least 9 of the 11 queued subsectors (≥80%) are diagnosed and logged... — a deliberately cautious volume bar, raised from an initial 6/11 draft, because FR-4 gates the PRD's only Primary success metric (SM-1). (2) No new fracture type... has appeared across the last 3 subsectors worked." Also §4.3 Consequences: "Recalibration work on B does not start until both start-trigger conditions above are met."
- **Spine citation:** AD-6 ("Threshold recalibration re-scores retroactively") governs *how* FR-4 behaves once triggered (retroactive re-scoring) but never states *when* FR-4 may start. The Capability Map's FR-4 row (`competitor.py`, `storage.py`, governed by AD-6 only) and the Deferred section both omit the trigger entirely — a grep of the spine for "9/11", "80%", "start trigger", and "fracture type" returns no matches outside this reconciliation.
- **Why it matters:** the PRD calls this trigger out with unusual emphasis (raised from a 6/11 draft specifically because it gates the only Primary success metric). A reader who only has the spine, and treats the Capability Map as the authoritative "where FR-4 lives," has no way to know FR-4 work is gated at all — the spine's own AD-6 rationale even implies FR-4 is ready to reason about now ("verified zero-cost before deciding... run live against production on 2026-08-10"), which reads as work-readiness without surfacing that the PRD forbids starting it yet.

### 3. AD-3 misattributes the PRD's rationale for choosing a persistent table over an ephemeral report

- **PRD citation:** §4.6, FR-5, "Format decision": "a generic Supabase table (`quality_review_log`), not an append-only file — because the log's job is not passive audit trail, it's the instrument used to answer the open Scenario A vs. B question (§4.2), which requires querying/aggregating across the ~11 subsectors ('how many subsectors share the same fracture type?'). A SQL table supports that directly..." Separately, §4.6 Consequences gives a *different* reason for the migration-tracking requirement: avoiding the untracked, unreproducible pattern of `competitor_validator.py`'s raw-SQL RPC call.
- **Spine citation:** AD-3's "Why" states: "...which isn't enough given the whole reason `quality_review_log` is a persistent table rather than another ephemeral JSON report is git-independent traceability."
- **Why it matters:** this conflates two distinct PRD rationales into one. The PRD's stated reason for *table vs. file* is queryability/aggregation to adjudicate Scenario A vs. B; "git-independent traceability" is the PRD's separate reason for requiring a *tracked migration mechanism* (vs. ad hoc SQL), not for choosing a table over a report in the first place. AD-3's rule itself (MCP + versioned SQL file) is still a reasonable answer to the real migration-traceability requirement, but its stated "why" misrepresents why the table exists at all, dropping the Scenario A/B aggregation rationale that is the PRD's actual load-bearing justification for FR-5.

### 4. Scenario B is dropped; only Scenario A survives into the spine

- **PRD citation:** §4.2 note frames the open structural risk as a two-sided question — Scenario A (root cause upstream in extraction, per-subsector correction is endless) vs. Scenario B (isolated, heterogeneous cases, one-time catch-up). The PRD is explicit that `quality_review_log`'s entire purpose (§4.6) is to let this A vs. B decision be made from evidence.
- **Spine citation:** the only mention of "Scenario A" in the spine is in Deferred (line ~175: "even if the PRD's Scenario A... is eventually confirmed by FR-3's queue"). Scenario B is never named anywhere in the spine (confirmed via grep for "scenario b" — no match).
- **Why it matters:** minor relative to findings 1–3, but the spine's framing implicitly treats "Scenario A confirmed" as the only outcome worth naming a deferred consequence for, when the PRD deliberately holds both branches open and undecided. Not a contradiction, but an oversimplification of the very risk framing that motivates AD-3/AD-4's design.

## Items checked and found consistent (no gap)

- Capability → Architecture Map covers FR-1 through FR-5 with no missing FR.
- Deferred section correctly mirrors PRD §6.2 (FR-2, Step 2a/2b rewrite, Features D/E/G as-is) and does not defer anything the PRD put in v1 scope (§6.1: FR-1, FR-5, FR-3, FR-4 all appear as active capabilities, not Deferred).
- "B reliable ≠ ready for beta" (PRD §6.1) is explicitly preserved, with correct citation, in the spine's Deferred section (Auth, Deployment & environments).
- FR-1's "diagnosis only, not the fix" Out-of-Scope note (PRD §4.2) is correctly modeled via AD-5's read-only rule for `diagnose_scraping.py`.
- FR-3's Precondition 2 (reading rubric, hard gate) is explicitly named in AD-5's rationale.
- FR-5's four-value verdict set and its generic, extensible schema intent (PRD §4.6) are correctly modeled in AD-4, with correct section citation.
- FR-5's migration-mechanism requirement (tracked, reproducible, not ad hoc SQL) is correctly modeled in AD-3's rule.
