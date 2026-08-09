---
title: SM Project
status: final
created: 2026-08-09
updated: 2026-08-10
---

# PRD: SM Project
*Working title — confirm.*

## 0. Document Purpose

PRD driven by Julien to challenge and clarify the vision and operating logic of SM Project — a personal startup-competitor-mapping tool, currently used solo, with an ambition toward a controlled beta. Built in coaching mode, Vision + Features entry point, grounded in the existing brownfield documentation (`docs/index.md`) and a prior adversarial code review (see `.memlog.md` for full decision trail).

## 1. Vision

SM Project began as a reaction to a concrete pain from Julien's time in VC: paid tools like Pitchbook and Dealroom categorize startups poorly, making competitor research slow and unreliable (see the Pitchbook/Dealroom comparison in §5 Non-Goals, and JTBD priority #1 in §2.1 — this origin is why both the comparison and the priority exist).

It has grown into a living map of the startup ecosystem, consulted to make better decisions: spotting opportunities, understanding a sector before an interview or an investment thesis, tracking how a market evolves. Today it's a personal intelligence tool, fed by automatic classification (LLM) and a sector/subsector/sub-subsector taxonomy refined continuously.

The long-term ambition is to turn it into a shared reference map that others could enrich — but that is not yet the tool as it exists today; it's a trajectory, not the present state.

## 2. Target User

Target user in v1: **Julien only**. Generalizing to a persona type (e.g. "investor doing due diligence") is deferred to a future iteration tied to the community opening — it is not the product's current vision. JTBDs are phrased in first person accordingly.

### 2.1 Jobs To Be Done

- **Spot a given startup's competitors** — when I'm evaluating a startup, I want to see its competitive landscape quickly so I don't miss a key player.
- **Spot white space / opportunities by browsing the graph** — I want to detect sparse or consolidating subsectors by exploring the map, not only by starting from a known name. *Emerging usage rather than an established practice — a real JTBD but less mature than the others; lower priority for associated features (e.g. a density/heatmap view per sector) compared to the search/filter features that serve the JTBDs below.*
- **Understand a sector before a decision** — before an interview or an investment thesis, I want to quickly grasp how a sector is structured (who does what, how it segments).
- **Track a market's evolution over time** — keep an eye on how a sector/subsector moves (new entrants, consolidation).
- **Capitalize on research** — feed a reliable base as I do my research, so I don't start from scratch every time.
- **(secondary) Grab a clean SVG logo** for a startup to use in a presentation or a post.

### 2.2 Non-Users (v1)

Explicit exclusions — named now so they can't sneak in later disguised as a "small feature":

- **Founders** looking to list their own startup.
- **Generic researchers/analysts** outside an investment-thesis context.
- **Any multi-user usage / shared graph view** — v1 is single-user by construction. *(Nuance: this excludes the shared/multi-user system as a whole, not every individual route — see the asymmetric access note in §4.4, Feature D: `/startup/{name}` is considered eligible for a future public/beta-facing opening, while `/graph` stays private regardless.)*
- **Real-time usage / alerting** (e.g. notification the moment a new player appears in a tracked sector) — tempting to attach to the "track a market's evolution" JTBD, but implies a different architecture (jobs, notifications); explicitly excluded from that JTBD.
- **Mobile usage** — not a current need; named out of scope now rather than discovered later via a feature that silently implies it.
- **Engineers/curious people evaluating the tech** (classification pipeline, taxonomy, stack) rather than for a real business use — the product serves a user JTBD, not a tech showcase.

### 2.3 Key User Journeys

**UJ-1. Julien spots a startup while browsing LinkedIn and wants its competitive landscape in 30 seconds.**
- **Persona + context:** Julien, passively browsing LinkedIn, sees an unfamiliar startup mentioned in a post. Realizes JTBD "spot a given startup's competitors," potentially "understand a sector before a decision."
- **Entry state:** no authentication (local tool, solo). Opens the search page (`/`).
- **Path:**
  1. Types the startup's name (or pastes its URL) into the search bar.
  2. If it already exists in the DB → clicks the result → lands on the startup's page, which loads its competitor graph (center node + known competitors, scored).
  3. If it doesn't exist yet → pastes the URL, triggers the add → waits for the scrape + LLM classification (synchronous call, potentially several seconds) → the page reloads with the freshly built graph.
  4. Looks at the neighboring nodes (competitors), their similarity score, and the displayed subsectors to judge whether the classification is coherent.
- **Climax:** sees at a glance the most relevant direct competitors, with their logo, without having to search for them himself.
- **Resolution:** either the answer is there and he closes the tab, or a listed competitor looks off → he notes a taxonomy anomaly to fix later (loops back into ongoing taxonomy maintenance).
- **Edge case:** no competitor found (sparse subsector, or a brand-new category) → the graph shows an isolated node; today nothing distinguishes "no competitor found" from "loading error" — friction identified, to address in §4/§8.

Other JTBDs — more diffuse over time, less suited to a detailed narrative:

- *Spot white space/opportunities:* Julien, hunting for new investment theses, browses the global graph to spot a sparse or consolidating subsector.
- *Track a market's evolution:* Julien periodically revisits a tracked subsector to see whether new players or links have appeared since his last visit. *(No "changes since my last visit" view exists today — potential gap, see §8.)*
- *Capitalize on research:* Julien adds a startup he came across during his research so he doesn't have to re-qualify it next time it comes up.
- *SVG logo (secondary):* Julien grabs a startup's logo to insert into a presentation or a post.

## 3. Glossary

- **Startup** — one `compspro` row: a company profile with sectors, subsectors, sub-subsectors, description, website, logo/favicon, LinkedIn URL.
- **Sector** — top-level taxonomy category (e.g. "AI & Machine Learning"). A startup can carry 1-3.
- **Subsector** — second-level category nested under a Sector (e.g. "MLOps & Model Serving"). Classified per-sector.
- **Sub-subsector** — optional third-level tag, only for subsectors that define one.
- **Taxonomy** — the full sector → subsector → sub-subsector tree, defined in `taxonomy.py`, plus its cleanup rules (`validate_subsectors`, `demote_generic_erp_tag`) and per-subsector definitions used to prompt the LLM classifier.
- **Chantier** — a targeted taxonomy cleanup initiative scoped to one subsector: diagnose (read a sample of descriptions against the definition) → decide fracture type → fix (tighten definition, add a sub-subsector or a whole new subsector, and/or add a deterministic cleanup rule) → reprocess affected startups → rebuild their competitor links.
- **Fracture** — a subsector whose members split across multiple communities when run through `graph_analysis.py`'s community detection — the signal that a chantier may be needed. Two fracture types: **isolated mis-tag** (a handful of startups tagged wrong, fixed by a definition tweak or targeted reprocess) vs. **structural split** (the subsector actually hides two distinct, self-competing markets, fixed by carving out a new subsector).
- **Competitor relationship** — one `competitors` row: an undirected, LLM-scored pair of startups (`company_a`, `company_b`, `score`), saved when `score >= COMPETITOR_THRESHOLD` (0.85).
- **Ingestion** — the end-to-end pipeline for adding or refreshing one startup: scrape → extract/classify (LLM) → save → competitor scoring.
- **`quality_review_log`** — generic, persistent Supabase table capturing the outcome of any quality-loop review (chantier diagnosis, future competitor-score audits, etc.): review type, subject, verdict, resolution. Distinct from `graph_analysis_report.json`, which is an ephemeral snapshot overwritten on every run.

## 4. Features

*Dependency chain: **A (Ingestion) → C (Taxonomy) → B (Competitor Detection)**, in that strict order — each link inherits the defects of the one before it. Fixing B in isolation without first stabilizing A and C is adjusting a symptom, not the cause.*

### 4.1 A — Ingestion
**Description:** Scrapes a startup's URL (Playwright), converts the page to markdown, and runs it through the LLM classification pipeline (extract → sectors → subsectors → sub-subsectors), then persists the result. Realizes JTBD "capitalize on research" and is the entry point for every other JTBD. First link in the dependency chain — its output quality bounds everything downstream.

**Functional Requirements:**

#### FR-1: Diagnose scraping heterogeneity

The system's scraped input must be characterized for quality/completeness issues across different site types before any fix is attempted.

**Status:** no dominant cause identified yet; a history of several distinct failure types has already been observed (not a one-off) — treated as its own diagnostic chantier, not a quick fix. **Priority: v1, high** — first link in the A → C → B chain; until heterogeneity is at least characterized, the noise it injects keeps propagating into the taxonomy (C) and therefore into competitor detection (B).

**Consequences (testable):**
- A characterization pass exists (even informal) enumerating the failure types observed across a sample of scraped sites.
- Findings feed a decision on whether/how to fix extraction, not just individual re-scrapes.

**Out of Scope:** the fix itself is not scoped yet — this FR is the diagnostic step only.

#### FR-2: Reduce LLM over-scraping cost

The system should send a targeted subset of a page's content to the LLM rather than the full page markdown (currently up to 30,000 characters of the entire page).

**Status:** deferred technical debt. **Priority: post-v1** — not blocking while usage stays solo; becomes a real problem only at multi-user scale, which is explicitly out of scope for v1 (§2.2). Logged here so it isn't forgotten when the community opening is revisited.

**Notes:** *[NOTE FOR PM]* Re-evaluate this FR's priority the moment multi-user/community scope re-enters discussion — cost scales with ingestion volume, which scales with users.

### 4.2 C — Taxonomy
**Description:** The sector/subsector/sub-subsector tree plus its cleanup rules and per-subsector definitions that ground the LLM classifier. Maintained through **chantiers** (see Glossary): `graph_analysis.py` (Louvain communities + betweenness centrality, zero LLM cost) flags subsectors whose members split across multiple communities → a sample of descriptions is read → a decision is made per-subsector between an isolated mis-tag fix (definition tweak, targeted reprocess) and a structural split (new subsector). Established practice, validated across the ERP & Business Operations, CRM & Sales, and AI Model Development Platforms chantiers.

**Functional Requirements:**

#### FR-3: Work the fracture queue while logging fracture type

For each of the ~11 large, never-diagnosed subsectors (AI Agent Platforms And Automation, AI Data & Training Infrastructure, Threat Detection & Intelligence, Supply Chain & Logistics Automation, Payment And Fraud Solutions, Field & Industrial Operations, Financial Compliance Automation, Cybersecurity Risk Management, Embedded Financial Services, API Infrastructure, MLOps & Model Serving), run the diagnose-then-decide chantier loop and record which fracture type was found.

**Precondition 1 — sequencing (confirmed):** FR-1 does not need to be *complete* before FR-3 starts, but must be *sufficiently advanced* — its characterization of already-identified scraping-heterogeneity cases becomes a required input to FR-3's per-subsector diagnosis. Without this guardrail, FR-3 risks misclassifying a scraping artifact (incomplete extraction on certain sites) as a taxonomy structural gap, which would pollute `quality_review_log` from its very first use and corrupt the Scenario A/B decision the log is meant to enable.

**Precondition 2 — reading rubric (confirmed, hard gate on starting FR-3, not an Open Question):** a consistent per-subsector reading method must exist before the queue is worked, not be improvised subsector-by-subsector — inconsistent judgment across the 11 diagnoses would quietly invalidate the Scenario A/B decision this FR is meant to support. Starting rubric (revisable after the first few entries if it proves miscalibrated in practice):
1. Read a sample of descriptions per community detected by Louvain.
2. Assess whether the communities reflect substantially different activities, or just wording variation around the same positioning.
3. Verdict **isolated mis-tag**: majority correctly classified, 1-2 startups mistagged → fixable with a cleanup rule.
4. Verdict **structural gap**: communities correspond to genuinely distinct activities → needs a new subsector or re-split.
5. Ambiguous case: log explicitly as **ambiguous** rather than forcing a binary verdict — preserves the integrity of the overall pattern for Question #1, and a recurring "ambiguous" outcome is itself a signal worth watching.

**Consequences (testable):**
- Before recording a verdict for a subsector, FR-3's diagnosis explicitly checks "is this explainable by a known scraping gap from FR-1?" before concluding "taxonomy structural gap." A third verdict value — **scraping artifact** — exists precisely so this cause isn't force-fit into the other two (see FR-5 schema).
- Each subsector in the queue has a recorded outcome: fracture type found (or none), fix applied, competitor links rebuilt.
- The accumulated log is enough to answer the open structural-risk question below (Scenario A vs. B) without deciding it blind now — and without the noise of misattributed scraping artifacts.

**Notes:** *[NOTE FOR PM]* **Unresolved structural risk, explicitly flagged rather than decided.** It is not yet known whether subsector-by-subsector manual correction is a *finite catch-up chantier* or a *recurring structural problem*:
- **Scenario A — root cause upstream in extraction:** if several of the ~11 queued subsectors show the same fracture pattern (same mis-tag type, or the same ambiguity in Step 2a/2b classification), then per-subsector correction is endless — every newly ingested startup will recreate the same problem. The real fix would be the initial extraction logic, not continued per-subsector patching.
- **Scenario B — isolated, heterogeneous cases:** if fractures share no common pattern, this is a one-time catch-up that ends once the queue is cleared, and the diagnose loop becomes an occasional QA tool rather than a standing remediation program.
- **Decision for v1:** work the queue while logging fracture type per subsector (FR-3's testable consequence); use that log as evidence to decide between Scenario A and B once the queue is processed — not before.
- **Dependency:** FR-3 execution relies on FR-5 (§4.6, Feature F) — the `quality_review_log` table must exist before/as the queue is worked, since it's the mechanism that captures fracture type per subsector.

### 4.3 B — Competitor Detection
**Description:** LLM-based pairwise scoring of startups sharing sector+subsector, saved as a competitor relationship above `COMPETITOR_THRESHOLD` (0.85), plus transitive (2nd-degree) exploration through direct competitors' own links. Gated behind A and C — recalibration attempted here before those are stabilized would not hold.

**Functional Requirements:**

#### FR-4: Recalibrate threshold and transitive exploration

Once A (FR-1) and C (FR-3) have progressed enough to reduce upstream noise, revisit `COMPETITOR_THRESHOLD` (currently 0.85) and the transitive-exploration logic (`explore_transitive`) for accuracy.

**Status:** explicitly sequenced last — **not to be started before A and C show measurable progress.** Attempting to fix B in isolation was identified as adjusting a symptom, not the cause.

**Start trigger (concrete, revisable):** FR-4 begins only once **both** hold:
1. At least **9 of the 11 queued subsectors (≥80%)** are diagnosed and logged in `quality_review_log` (FR-3) — a deliberately cautious volume bar, raised from an initial 6/11 draft, because FR-4 gates the PRD's only Primary success metric (SM-1).
2. **No new fracture type (verdict + cause) has appeared across the last 3 subsectors worked** — the stabilization signal that the observed pattern isn't still shifting.
The 80% figure is a starting point, not a fixed law — revisit it if early subsectors in the queue suggest it's miscalibrated.

**Consequences (testable):**
- Recalibration work on B does not start until both start-trigger conditions above are met.
- "B reliable" (see Success Metrics, §7) is defined as the exit milestone that would justify opening the tool to outside testers — even though multi-user itself stays out of v1 scope (§2.2).

### 4.4 D — Search & Consultation
**Description:** Search bar plus the single-startup page with its competitor mini-graph (realizes UJ-1). *[to build together — not yet walked in coaching]*

**Access note (asymmetric, not yet a Feature but worth recording now):** `/startup/{name}` (this feature) is the view considered eligible to eventually go public/beta-facing; `/graph` (§4.5, Feature E) stays private to Julien regardless of any future opening. This is an asymmetry between the two views, not a blanket "nothing is ever public" rule — see the cross-references added to §2.2 and §5 so those sections aren't misread as excluding this distinction.

### 4.5 E — Global Graph View
**Description:** Free exploration of the full map — personal use only per §2.2 (Non-Users). *[to build together — not yet walked in coaching]*

### 4.6 F — Quality Loop
**Description:** Taxonomy audit (`audit_taxonomy.py`), sampled competitor-link validation (`competitor_validator.py`), and backfill/reprocess scripts. Operating principle carried over from prior sessions: never hand-patch a row — fix `taxonomy.py` and reprocess. F is not independent of C — it is the execution mechanism for C's unresolved structural risk (§4.2 note): it must instrument how the fracture-type trace gets captured per subsector, across FR-5's full verdict set (isolated mis-tag / structural gap / **scraping artifact** / ambiguous), so that evidence can accumulate to decide Scenario A vs. B. The **scraping artifact** value in particular is what materializes F's link to FR-3's sequencing guardrail with A (§4.2, Precondition 1) — it exists so noise from not-yet-stabilized ingestion doesn't get miscounted as a real taxonomy fracture.

**Scope split with FR-3 (confirmed):** F owns the tooling (the persistent log mechanism). FR-3 (§4.2) owns the execution — working the ~11-subsector queue using that tooling. F does not itself process the queue; it supplies the instrument FR-3 depends on.

**Functional Requirements:**

#### FR-5: Persistent, queryable quality-review log

`graph_analysis.py`/`graph_analysis_report.json` only flags fracture *candidates* (subsector splits across Louvain communities) — it records no verdict and no outcome, and the report file is overwritten on every run, so it cannot itself serve as a historical decision log. Build a durable log to capture the diagnose-then-decide outcome of every chantier.

**Format decision:** a generic Supabase table (`quality_review_log`), not an append-only file — because the log's job is not passive audit trail, it's the instrument used to answer the open Scenario A vs. B question (§4.2), which requires querying/aggregating across the ~11 subsectors ("how many subsectors share the same fracture type?"). A SQL table supports that directly, and it stays on the existing Supabase stack rather than introducing a new tool. The schema is deliberately generic (not taxonomy-specific) so it can extend to future quality loops beyond C — e.g. auditing B's competitor scores once recalibrated (FR-4), or future checks on A — avoiding a migration later.

**Proposed schema — `quality_review_log`:**
| Column | Notes |
|---|---|
| `review_type` | e.g. `taxonomy_split`; future types e.g. `competitor_score_audit` — distinguishes which quality loop this row belongs to |
| `subject` | e.g. the subsector name; shape depends on `review_type` (could be a startup pair for a future competitor-score review) |
| `date_diagnosed` | |
| `source_snapshot` | copy/reference of the source metrics at diagnosis time — here, the relevant slice of `graph_analysis_report.json`'s `subsector_splits`, captured before it's overwritten by the next run |
| `verdict` | one of: isolated mis-tag / structural gap / **scraping artifact** (fracture explained by a known FR-1 scraping-heterogeneity case, not a real taxonomy issue — kept distinct per FR-3's sequencing precondition) / **ambiguous** (per FR-3's reading rubric — logged explicitly rather than forced into a binary call; a recurring "ambiguous" outcome is itself a signal) |
| `resolution` | e.g. cleanup rule added / new subsector created / other |
| `notes` | free text |

**Consequences (testable):**
- `quality_review_log` table exists in Supabase with the schema above.
- The write mechanism is instrumented at the point where a Louvain/betweenness diagnosis is read and a chantier decision is made (not a separate manual data-entry step disconnected from the actual work).
- FR-3's queue processing writes one row per subsector worked, enabling the Scenario A/B query once the queue is cleared.
- **The table is created via a minimal migration mechanism, not ad hoc SQL.** Today's only schema-change precedent (`competitor_validator.py`'s raw-SQL RPC call) is exactly the kind of untracked, unreproducible change that would undermine a table whose entire purpose is being a persistent, trustworthy decision record — reproducing that pattern for `quality_review_log` would recreate the problem the table exists to solve. This does not require re-tooling the whole project's schema management, only a tracked, reproducible way to create and evolve this one table — more important still given the schema is deliberately generic for future quality loops beyond C.

### 4.7 G — Logos/Favicons *(secondary)*
**Description:** Download and distinguish favicon vs. real logo. Secondary per §2.1 JTBD. *[to build together — not yet walked in coaching]*

## 5. Non-Goals (Explicit)

- **Not a notification/alerting system.** No real-time monitoring or push notifications when a new competitor appears in a tracked sector (§2.2).
- **Not a mobile product.** No mobile app, no mobile-optimized UI in v1 (§2.2).
- **Not yet a multi-user/shared platform.** No auth, no per-user accounts, no shared graph access — the full graph view stays Julien-only until the community ambition (§1) is deliberately revisited, not as a byproduct of another feature. *(Nuance: applies to the system as a whole, asymmetrically — see §4.4's access note: `/startup/{name}` is the view eligible for a future public/beta-facing opening, `/graph` is excluded from that regardless.)*
- **Not opening ingestion to the public yet.** The "search triggers ingestion" community vision (§1) is a distinct future iteration, not something v1 quietly backs into.
- **Not fixing LLM over-scraping cost now.** Deferred to post-v1 (FR-2) — not a v1 workstream.
- **Not committing to a monetization model.** Stays an open, opportunistic question (§1) — v1 does not build pricing, billing, or a paid tier.
- **Not competing with Pitchbook/Dealroom's full commercial suite.** The comparison driving this project is specifically about the quality of their competitor/mapping feature (judged poorly categorized) — not an ambition to replicate deal data, fundraising, or valuations coverage. V1 stays focused on that one point of comparison, not equivalent functional coverage. Subsumes the earlier framing of "not a general VC deal-sourcing/portfolio-management platform" — same boundary, this is the precise version.

## 6. MVP Scope

### 6.1 In Scope

- **FR-1** — Diagnose scraping heterogeneity (characterization, not the fix itself).
- **FR-5** — `quality_review_log` table + instrumentation (prerequisite for FR-3).
- **FR-3** — Work the ~11-subsector fracture queue, logging fracture type per subsector.
- **FR-4** — Recalibrate B's threshold (0.85) and transitive exploration, sequenced last, gated on a concrete start trigger (§4.3: ≥9/11 subsectors diagnosed + no new fracture type in the last 3) — not a fixed date, A/C's measured progress triggers FR-4's start.
- The rest of the existing product (base ingestion, search, startup page, global graph view, logos) **keeps operating as-is** — no new work on it in v1 beyond what's listed above.
- Exit milestone: **"B reliable"** (§4.3 FR-4) — a quality condition reached, not a date.

**"B reliable" is necessary but not sufficient for beta — named explicitly to prevent misreading.** Reaching SM-1 is a data-quality gate, not an operational green light: opening the tool to outside testers would also require authenticating `/api/ingest` (currently open to anyone who can reach the port) and standing up a deployment configuration (currently none exists) — neither is a chantier this PRD starts. This is consistent with, not a change to, the multi-user Non-Goal already locked in §5; it's stated here so a future reader doesn't conflate "B is reliable" with "ready to invite testers."

**Sequencing (confirmed):** FR-1 and FR-3/FR-5 are not run in parallel without a link between them — see FR-3's Precondition 1 (§4.2) for the guardrail and its rationale.

### 6.2 Out of Scope for MVP

- FR-2 (LLM over-scraping cost) — already deferred.
- Everything listed under Non-Goals (§5): multi-user, public ingestion, mobile, alerting, monetization.
- Rewriting the initial extraction logic (Step 2a/2b), even if Scenario A turns out to be true — that decision itself is out of scope for MVP; only the diagnosis (FR-3, informed by FR-1) is in scope.
- The two UX frictions identified in UJ-1 (blocking ingest, ambiguous empty state) — deliberately not fixed until the real cause (A/C) is known.
- The "changes since my last visit" view (Open Question #2) — undecided, therefore not committed.

## 7. Success Metrics

**Primary**
- **SM-1**: "B reliable" — **precision** (not recall) on a stratified sample validated via `competitor_validator.py`, deliberately including pairs drawn from the FR-3-processed subsectors (not a pure random draw across the whole DB). Target: 90-95% *(exact threshold still open for discussion)*, measured after FR-4's threshold (0.85) recalibration. Precision is chosen over recall because false positives are the original pain point this project exists to fix — consistent with SM-C2 below. Validates FR-4.

**Secondary**
- **SM-2**: Fracture queue processed — 11/11 subsectors diagnosed with a logged verdict in `quality_review_log`. Validates FR-3, FR-5.
- **SM-3**: Personal usage — Julien uses the tool at least weekly without abandoning it. Validates the overall Vision (§1).

**Counter-metrics (do not optimize)**
- **SM-C1**: Database size / startup count — not a metric to grow for its own sake. Nuance: continued ingestion stays legitimate because volume serves a diagnostic function for C — more startups covered surfaces more edge cases that reveal taxonomy definition errors and refine sector/subsector boundaries. Volume is a useful *input* to the C diagnostic, not an *output* goal — it must never become a success proxy that overrides categorization quality. Counterbalances the temptation to prioritize coverage over SM-1/SM-2, consistent with the founding critique of Pitchbook/Dealroom (poor categorization, not insufficient coverage).
- **SM-C2**: Competitor-link recall (more links surfaced) — FR-4's threshold recalibration must not be judged by "more links found"; false positives are precisely the original pain point this project corrects. Counterbalances SM-1.

## 8. Open Questions

1. Is the taxonomy fracture pattern (FR-3) a finite catch-up chantier or a recurring structural problem rooted in extraction (Step 2a/2b)? To be decided from the fracture-type log after working the ~11-subsector queue — see §4.2 note.
2. No "changes since my last visit" view exists for the "track a market's evolution" JTBD — is this a real gap to fill, or does the JTBD stay served informally (re-browsing) for v1?
3. What does "database size is large enough" actually mean as a threshold before beta? **Refined:** not raw volume — consistent with SM-C1 (§7, "don't optimize size for its own sake"), the real criterion is **link density**:
   - Mean number of competitor links per startup — is the graph dense enough to produce a useful competitive landscape per startup, not just isolated nodes?
   - Proportion of startups with at least one link — effective coverage; a large-but-sparse DB would hollow out JTBD "spot a given startup's competitors" of its value.
   Still open: the numeric thresholds for each, and whether they're read globally or per-subsector — a sparse subsector can legitimately be an opportunity signal (JTBD "spot white space"), so a single global threshold risks masking that.
4. SM-1's exact precision threshold for "B reliable" — 90-95% proposed, not yet fixed. Revisit condition: before FR-4 actually starts (i.e., once FR-4's start trigger in §4.3 is reached) — no need to pin down before then.

*(Former Question #5 — verdict-criterion consistency across subsectors — was reclassified during Finalize from an Open Question to a hard precondition of FR-3, since starting the queue without it would invalidate the evidence Question #1 depends on. See §4.2 FR-3 "Precondition 2 — reading rubric.")*

**Revisit conditions for the questions above:** Q1 resolves naturally once the fracture queue (FR-3) is processed, not before. Q2 is tied to Feature D, already out of v1 scope — revisit only if D gets formal FRs. Q3 is a beta-graduation criterion, not a v1-execution blocker — revisit when beta planning starts. Q4 — see above.

## 9. Assumptions Index

None outstanding. Two `[ASSUMPTION]` tags were raised during drafting of §5 Non-Goals (scope boundary vs. a general VC deal-sourcing/portfolio platform, and the Pitchbook/Dealroom comparison framing) — both were confirmed and refined in conversation and folded into §5's final wording, with the tags removed accordingly. Coaching-path discipline (confirm as you go) is why this index is empty rather than a batch of unresolved tags, unlike a Fast-path draft would typically produce.
