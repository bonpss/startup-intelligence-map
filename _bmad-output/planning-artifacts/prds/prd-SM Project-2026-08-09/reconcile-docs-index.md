# Reconciliation: prd.md vs. docs/index.md (brownfield documentation)

**Input reconciled:** `docs/index.md` and its linked files (`architecture.md`, `data-models.md`, `api-contracts.md`, `source-tree-analysis.md`, `development-guide.md`, `project-overview.md`)
**Against:** `_bmad-output/planning-artifacts/prds/prd-SM Project-2026-08-09/prd.md`

## Summary

The PRD's factual descriptions of the current system (taxonomy structure, competitor threshold, pipeline order, graph analysis mechanics, logo/favicon split) are consistent with and well-grounded in the brownfield docs — no outright contradictions found. The material problems are **omissions**: several "Known Gaps" from `docs/index.md` are silently unaddressed by PRD sections that depend on them, most notably the missing-migrations gap colliding with FR-5's new-table proposal.

---

## 1. [HIGH] FR-5's new Supabase table silently assumes schema-change tooling that doesn't exist

- **PRD:** §4.6 (Feature F), FR-5 "Persistent, queryable quality-review log" — proposes creating a new table `quality_review_log` in Supabase, with a "Consequences (testable)" bullet stating simply "`quality_review_log` table exists in Supabase with the schema above." No mechanism for *how* the table gets created is specified.
- **Brownfield docs:** `docs/index.md` Known Gaps: *"No migrations directory — `compspro`/`competitors` schema lives only in application code."* `docs/data-models.md` "Schema evolution note": *"`competitor_validator.py::_run_migration()` is the only place that alters schema, and it does so at runtime via a raw SQL RPC call..., silently degrading to a printed manual-SQL instruction if the RPC isn't available. If this project grows, the biggest structural gap is the absence of a real migrations directory."* `docs/architecture.md` "Data Architecture": *"No migrations directory — schema changes happen through raw SQL run ad hoc ... or manually in the Supabase dashboard."*
- **Gap:** This is exactly the scenario the docs flag as the project's single biggest structural data-layer risk, and FR-5 triggers it directly (a brand-new table, not just a column add like the existing `checked`/`validated` precedent). The PRD neither (a) references the existing ad-hoc precedent pattern as the fallback mechanism, nor (b) decides whether FR-5 should finally introduce a tracked migration file/directory, nor (c) flags the absence of migration tooling as a prerequisite risk to FR-5's "table exists" consequence. It reads as if table creation is a non-event, when the brownfield docs describe it as the codebase's least-solid area.

## 2. [MODERATE-HIGH] FR-4's "opening to outside testers" language doesn't engage the unauthenticated-ingest / no-deployment-config gaps

- **PRD:** §4.3, FR-4 Consequences: *"'B reliable' ... is defined as the exit milestone that would justify opening the tool to outside testers — even though multi-user itself stays out of v1 scope."* §4.4's access note also flags `/startup/{name}` as "eligible to eventually go public/beta-facing."
- **Brownfield docs:** `docs/index.md` Known Gaps: *"`/api/ingest` is unauthenticated."* `docs/api-contracts.md`: *"All endpoints are unauthenticated — there is no auth/session layer in this codebase. `/api/ingest` in particular triggers a real scrape + paid LLM call for anyone who can reach the process."* `docs/architecture.md` "Deployment Architecture": *"No deployment configuration exists in this repository ... `graph_app.py` ... binds `0.0.0.0:8000` ... but there's nothing in-repo describing how it's actually deployed ... This is the single biggest gap for anyone trying to reproduce or harden the production setup."*
- **Gap:** The PRD explicitly gestures toward a future state (outside testers reaching the tool) that the docs say the current system is structurally unprepared for (no auth anywhere, `/api/ingest` triggers real paid LLM spend for any caller, no documented deployment/hardening path despite binding `0.0.0.0`). Non-Goals §5 defers "opening ingestion to the public" but never connects that deferral to *why* — the concrete unauthenticated-endpoint and no-deployment-config gaps — leaving it unclear whether the PRD has actually accounted for these as blockers to the FR-4 exit milestone or is unaware of them.

## 3. [LOW-MODERATE] No test suite / no CI gap never addressed, despite FR-5 introducing a decision-critical data instrument

- **PRD:** Throughout §4, FRs use "Consequences (testable)" language, but nowhere does the PRD state how any FR (especially FR-5's log-writing instrumentation, which is the evidence base for the Scenario A/B structural decision in §4.2) will be verified. No mention of tests, CI, or an explicit choice to keep using manual verification.
- **Brownfield docs:** `docs/index.md` Known Gaps: *"No test suite anywhere in the project."* `docs/architecture.md` "Testing Strategy": *"None ... Validation currently happens two ways instead: `competitor_validator.py` ... and `audit_taxonomy.py` ... Neither is an automated test; both are manually-run reporting scripts."* `docs/development-guide.md` "Testing": *"No test suite exists for this project. ... There is no CI configuration either."*
- **Gap:** Not necessarily wrong (manual verification via `competitor_validator.py`/`audit_taxonomy.py` is established practice per the docs, and SM-1 in §7 does correctly lean on `competitor_validator.py`), but the PRD never makes the choice explicit for FR-5's new write path, whose correctness matters more than the existing scripts since a structural go/no-go decision depends on it. Worth a deliberate line rather than silence.

## 4. [LOW] `requirements.txt` incompleteness not addressed anywhere

- **PRD:** No mention of dependency/installation state anywhere in §4 or elsewhere.
- **Brownfield docs:** `docs/index.md` Known Gaps: *"`requirements.txt` is missing `fastapi`, `uvicorn`, and `networkx` despite being imported by `graph_app.py`/`graph_analysis.py`."* `docs/development-guide.md` "Installation" has the full table of installed-but-undeclared packages.
- **Gap:** Low materiality to the FRs in scope, but notable that FR-3/FR-5 depend on `graph_analysis.py` (which needs the undeclared `networkx`), and the PRD doesn't flag environment reproducibility as a prerequisite risk for anyone other than Julien picking this up (relevant given §4.3's own "opening to outside testers" language, see finding #2).

## 5. [LOW] Orphaned README references (`taxonomy_agent.py`, `reprocess_all.py`) not mentioned

- **PRD:** Not referenced anywhere.
- **Brownfield docs:** `docs/index.md` Known Gaps and "Existing Documentation" note; `docs/source-tree-analysis.md`: *"README.md additionally references `taxonomy_agent.py` and `reprocess_all.py` — neither exists in the current tree."*
- **Gap:** Minor. The PRD's Glossary defines "Chantier" and the F/Quality Loop feature in terms of the scripts that *do* exist (`audit_taxonomy.py`, `competitor_validator.py`, backfill/reprocess scripts), so this doesn't materially mislead the PRD's own content — but a future implementer consulting the stale README alongside this PRD could be confused. Worth a one-line flag/fix-later note, not a blocker.

## 6. [MINOR] Imprecise characterization of competitor candidate-pool matching

- **PRD:** §4.3 (Feature B) describes scoring "startups sharing sector+subsector."
- **Brownfield docs:** `docs/architecture.md` pipeline diagram: candidate pool comes from `storage.get_by_subsectors()` — subsector-level match, not an explicit sector+subsector conjunction.
- **Gap:** Cosmetic — subsectors are nested under sectors so the practical effect is similar, but the PRD's phrasing is not the same mechanism the docs describe. Not worth blocking on, flagged for completeness.

## Not a gap (checked, consistent)

- Pipeline order (scrape → extract/classify → save → competitor-score), `COMPETITOR_THRESHOLD` = 0.85, `explore_transitive` transitive matching, `graph_analysis.py`'s zero-LLM-cost Louvain/betweenness mechanism, the `flaticon_url`/`logo_url` favicon-vs-logo split, and the `compspro`/`competitors` table shapes in the PRD's Glossary (§3) all match the brownfield docs. FR-1's "~30,000 characters" scraping-cost figure isn't traceable to any of the six docs, but the PRD's Document Purpose (§0) also cites a separate prior adversarial code review as a grounding source, so this is plausibly sourced from there rather than ungrounded.
