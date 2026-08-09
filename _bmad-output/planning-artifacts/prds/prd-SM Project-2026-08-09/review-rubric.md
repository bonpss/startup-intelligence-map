# PRD Quality Review — SM Project (2026-08-09)

## Overall verdict

This is an unusually disciplined PRD for a solo-use brownfield tool: it states real trade-offs, sequences work by causal dependency rather than convenience, and self-flags its own open risks (Scenario A/B, verdict-consistency) instead of smoothing them over. The main soft spot is done-ness: two of the five FRs (FR-1, FR-4) lack a concrete completion/trigger bar, which matters because FR-4's start condition gates the primary success metric (SM-1). Downstream usability is a secondary concern only — most of the product surface (D, E, G) is explicitly "keeps operating as-is," so the PRD correctly limits its own footprint.

## Decision-readiness — strong

Decisions are stated as decisions, not hedged as considerations: the A→C→B dependency chain (§4, opening line) is enforced with a stated rationale ("Fixing B in isolation... is adjusting a symptom, not the cause"), and §4.3 FR-4 explicitly forbids starting recalibration before A/C progress. Trade-offs name what's given up, not just what's chosen — e.g. FR-2 is deferred with an explicit reason ("not blocking while usage stays solo") and a `[NOTE FOR PM]` reminder to revisit it "the moment multi-user/community scope re-enters discussion" (§4.1). Open Questions (§8) are genuinely unresolved, not rhetorical — Q1 (finite chantier vs. structural problem) and Q5 (verdict-criterion consistency) both have real stakes and no answer smuggled into the next sentence. No findings.

## Substance over theater — strong

Single persona (Julien), not persona theater — the PRD is explicit that generalizing to a persona type is deferred (§2, "Generalizing to a persona type... is deferred to a future iteration"). The Vision (§1) is grounded in a specific, named pain ("paid tools like Pitchbook and Dealroom categorize startups poorly") rather than swappable boilerplate. No dedicated NFR section exists, and none was force-fitted in — appropriate given the shape (solo tool, no scale/security stakes yet), so its absence isn't a gap, it's correct scoping. §5's Non-Goals section for Pitchbook/Dealroom comparison is precise about what it does *not* claim ("not an ambition to replicate deal data, fundraising, or valuations coverage"), which is the opposite of innovation theater. No findings.

## Strategic coherence — strong

The thesis is explicit and singular: competitor categorization quality is the founding pain (§1), the feature dependency chain (§4 preamble) executes it in causal order, and Success Metrics validate the thesis rather than activity — SM-1 is precision (not recall), explicitly justified because "false positives are the original pain point this project exists to fix" (§7), and two counter-metrics (SM-C1 database size, SM-C2 recall) exist specifically to stop the team from optimizing the wrong proxy. This is the opposite of a backlog-with-headings.

### Findings
- **low** SM-3 is a thin validation of Vision (§7) — "Julien uses the tool at least weekly without abandoning it" validates *usage*, not whether the categorization-quality thesis is actually working. *Fix:* none required given solo-tool stakes, but if beta ambitions advance, replace or supplement with a metric closer to the thesis (e.g., self-reported trust in the competitor list).

## Done-ness clarity — adequate

FR-3 and FR-5 are unambiguous: FR-3's consequence ("each subsector in the queue has a recorded outcome: fracture type found (or none), fix applied, competitor links rebuilt") and FR-5's schema table (§4.6) give an engineer a clear finish line. FR-1 and FR-4 do not.

### Findings
- **medium** FR-1's completion bar is unquantified (§4.1) — "A characterization pass exists (**even informal**) enumerating the failure types observed **across a sample** of scraped sites" gives no sample size or coverage threshold. Since FR-3's diagnosis depends on FR-1 being "sufficiently advanced" (§4.2 precondition), an engineer can't tell when FR-1 has produced enough to unblock FR-3. *Fix:* name a minimum sample (e.g., "at least N sites per observed site-type") or a stopping rule ("stop when no new failure type appears in N consecutive samples").
- **high** FR-4's start trigger is undefined (§4.3, §6.1) — "Recalibration work on B does not start until FR-1's diagnostic pass and **a meaningful slice** of FR-3's queue are complete" and §6.1's "gated on measurable progress in FR-1/FR-3 (not a fixed date...)" both name a condition without a bar. This matters more than a typical vague-adjective FR because FR-4 is the direct predecessor to SM-1 ("B reliable"), the PRD's only Primary success metric — nobody can tell when FR-4 is unblocked. *Fix:* define "meaningful slice" concretely (e.g., "at least 6 of 11 subsectors diagnosed" or "no new fracture-type pattern seen in the last 3 subsectors worked"), even if the number itself stays revisable.

## Scope honesty — strong

§5 Non-Goals is doing real work, not template filler — each bullet has a stated reason and several carry explicit nuance callouts pointing back to where the boundary is more subtle (e.g., the `/startup/{name}` vs `/graph` asymmetry cross-referenced in §2.2, §4.4, and §5). §6.1's "B reliable is necessary but not sufficient for beta" paragraph is a good example of de-scoping stated honestly rather than left to be inferred — it explicitly separates the data-quality gate from the unbuilt auth/deployment work. The Assumptions Index (§9) explains its own emptiness rather than leaving it silently blank. Open-items density (5 Open Questions + 2 `[NOTE FOR PM]`) is proportionate to a personal tool with a real unresolved technical-risk question (Scenario A/B), not inflated. No findings.

## Downstream usability — adequate

Standalone-usability matters more than chain-top usability here: §6.1 confirms D, E, G (search, startup page, global graph, logos) "keep operating as-is — no new work on it in v1," so the PRD is honestly not the source document for those surfaces right now. For the FRs that are in scope, cross-references resolve correctly (e.g., §4.2's "Dependency: FR-3 execution relies on FR-5 (§4.6)" matches §4.6's actual content; §4.4's access-note cross-refs to §2.2 and §5 both check out). Glossary terms (chantier, fracture, verdict types) are used consistently in FR-3/FR-5.

### Findings
- **low** JTBD/UJ structure is inconsistent (§2.3) — only "Spot a given startup's competitors" gets a full UJ card (UJ-1); the other four JTBDs ("Spot white space," "Track a market's evolution," "Capitalize on research," "SVG logo") are informal bullets with no UJ ID, though each names Julien as protagonist inline. The PRD explains this ("more diffuse over time, less suited to a detailed narrative"), so it's a defensible choice, not an oversight — but if Features D/E/G ever get their own FRs, downstream story creation will have no UJ-2..UJ-5 to cite. *Fix:* no action needed now; assign UJ IDs to the informal JTBDs if/when D/E/G get formal FRs.

## Shape fit — strong

Capability-spec shape fits a single-operator brownfield tool: one formal UJ instead of a full UJ roster (correctly resisting persona/UJ inflation for a v1 with a target user of "Julien only," §2), Success Metrics that are operational (SM-1 precision, SM-2 queue completion) rather than forced into consumer-product engagement metrics. Brownfield references check out against what's independently known about this codebase — `taxonomy.py`, `graph_analysis.py`, `competitor_validator.py`, the "never hand-patch a row" principle (§4.6) — all consistent with established project conventions, which supports confidence in the rest of the brownfield claims. No findings.

## Mechanical notes

- Glossary (§3) terms are used consistently in case and form throughout (Sector/Subsector/Sub-subsector, Chantier, Fracture, Ingestion) — no drift observed.
- FR IDs (FR-1 through FR-5) are contiguous and unique; all cross-references to them resolve to the section cited.
- SM IDs (SM-1/2/3, SM-C1/C2) are contiguous and each has a "Validates ..." line pointing to an FR or the Vision.
- Assumptions Index (§9) roundtrips cleanly: it states two `[ASSUMPTION]` tags were raised and resolved during drafting with tags removed from the final text — no orphaned inline tags found, no orphaned index entries.
- UJ-1 (§2.3) has a named protagonist (Julien) with context carried inline, per the checklist's expectation.
- Features D, E, G (§4.4, §4.5, §4.7) intentionally carry no FRs, marked `[to build together — not yet walked in coaching]` — expected given §6.1 confirms no new v1 work on them.
