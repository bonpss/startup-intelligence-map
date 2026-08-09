# Adversarial Review — ARCHITECTURE-SPINE.md (Reviewer Gate)

**Target:** `_bmad-output/planning-artifacts/architecture/architecture-SM Project-2026-08-10/ARCHITECTURE-SPINE.md`
**Lens:** construct two units one level down that each obey every AD to the letter yet build incompatibly.
**Verdict: FAIL — 5 real seams found, at least 3 with concrete incompatible-build scenarios that break stated guarantees (the FR-3 precondition and AD-3's own anti-drift purpose).**

---

## Finding 1 — `quality_review_log.subject` has no defined grain/format, so the FR-3 precondition can silently fail

**AD implicated:** AD-2 (tighten)

**The scenario:** AD-2's entire justification for merging `taxonomy_split` and `scraping_diagnostic` into one queryable table is FR-3's Precondition 1: before recording a `taxonomy_split` verdict, check the log for an existing `scraping_diagnostic` entry "on the same site." That join only works if both review types write `subject` in the *same identifier shape*.

Nothing in AD-2 or AD-4 says what `subject` actually is. Two implementers building `diagnose_scraping.py`/`log_review.py` and the taxonomy-split path independently, both fully AD-2-compliant:

- **Dev A** implements `scraping_diagnostic` entries with `subject = "acme.com"` (bare root domain, since FR-1 diagnoses *sites*).
- **Dev B** implements `taxonomy_split` entries with `subject = "Fintech > Payments"` (the subsector string, since FR-3 diagnoses *taxonomy fractures*, which are naturally subsector-scoped, not site-scoped).

Both are "queryable by `review_type` and `subject`" per AD-2's literal rule. But the precondition query — "does a `scraping_diagnostic` row exist for the same site as this `taxonomy_split` verdict" — has no shared key to join on. It either silently returns zero rows forever (guardrail always fails open, defeating FR-3 Precondition 1) or forces a third piece of ad hoc mapping logic that isn't specified anywhere and that a future session could implement differently again.

**Fix:** AD-2's Rule needs an explicit statement of `subject`'s canonical grain — e.g. "`subject` is always the startup's root domain (scheme/`www.` stripped), for every `review_type`, including `taxonomy_split`" — or, if subsector-level subjects are unavoidable for taxonomy work, an explicit secondary key/column for the cross-reference instead of overloading one untyped `subject` column for two different entity grains.

---

## Finding 2 — `review_type` vocabulary is only prose, never codified, and open-vs-closed validation is unspecified

**AD implicated:** AD-2 + AD-4 (tighten, or new AD)

**The scenario:** The literal strings `taxonomy_split` and `scraping_diagnostic` appear only in the *Why* prose of AD-2 and AD-4 — never as a Rule-level, enumerated, shared constant. Two future sessions implementing `diagnose_scraping.py` and `log_review.py` independently could each invent their own spelling/casing (`scraping_diagnostic` vs `ScrapingDiagnostic` vs `scraping-diagnostic`) and both would be "AD-4 compliant" (verdict is free text; review_type isn't even mentioned as governed).

Compounding this: AD-4 says the 4-value verdict constraint is enforced in application code inside `storage.save_quality_review()`, but only for `review_type = 'taxonomy_split'`. It's silent on what `save_quality_review()` does with an *unrecognized* `review_type` — reject it (closed allow-list) or accept anything (open, per the "free text" spirit)? Dev A writes a strict allow-list gate (new review_types must be registered in code first); Dev B writes a permissive pass-through (any caller can invent a new review_type string on the fly). Under Dev B's version, a typo in either script silently creates a third, orphaned review_type that AD-2's cross-referencing (Finding 1) can never match against — with no error, ever.

**Fix:** Add a Rule to AD-2 or AD-4 that pins the canonical `review_type` vocabulary as a literal enumerated list (even though the column itself stays `text` per AD-4's DB-enum rationale), and states explicitly whether `storage.save_quality_review()` validates `review_type` against that list (reject unknown) or accepts arbitrary values.

---

## Finding 3 — AD-2's function-level chokepoint and AD-5's script-level chokepoint aren't the same gate, and nothing enforces AD-5 in code

**AD implicated:** AD-5 (tighten)

**The scenario:** AD-2's Rule grants access through the *function* `storage.save_quality_review()` — any script is licensed to call it. AD-5's Rule grants verdict-writing to the *script* `log_review.py` "alone." These are two different chokepoints, and only the narrower one (AD-5) carries the "deliberate human-in-the-loop" intent — but AD-5 only names the two *diagnosis* scripts (`graph_analysis.py`, `diagnose_scraping.py`) as forbidden writers. It never states that *no other script, present or future,* may call `save_quality_review()` directly.

Concretely: a future session builds an automated batch-classifier (say, an "auto-resolve obvious cases" script for FR-4/AD-6 recalibration bookkeeping, or a CI-style sweep) that calls `storage.save_quality_review()` directly, bypassing `log_review.py` entirely. Per AD-1/AD-2 this is fully compliant — it went through the single Supabase access point, through the designated function. Per AD-5's *intent* it violates "verdict is a considered human judgment call... invoked deliberately after a human has read, discussed, and applied the reading rubric" — but AD-5's *Rule* text never actually forbids it, because it only lists `graph_analysis.py` and `diagnose_scraping.py` by name. Two devs reading "to the letter" reach opposite conclusions about whether this new script is allowed.

**Fix:** Tighten AD-5's Rule to state the constraint at the chokepoint that actually matters: either (a) `storage.save_quality_review()` itself takes a required human-confirmation flag/caller identity and refuses silent/automated calls, or (b) explicitly state "no script other than `log_review.py` may call `storage.save_quality_review()`, full stop" rather than naming today's two scripts as if the list were exhaustive.

---

## Finding 4 — AD-5's "read-only" doesn't say where run-state/progress lives, and the loophole routes straight through the one door AD-2 opened

**AD implicated:** AD-5 (tighten)

**The scenario:** FR-1's `diagnose_scraping.py` is characterizing scraping heterogeneity across (per the PRD) many sites/subsectors — a task that plausibly wants resumable progress tracking (don't re-diagnose a site already characterized in a prior run). AD-5 says it "never write[s] to `quality_review_log` or any table — they only read and report," but is silent on *where* progress/checkpoint state persists between invocations.

- **Dev A** reads AD-5 strictly and keeps state in a local untracked file (e.g. `.diagnose_progress.json`) at repo root. That state is invisible to `log_review.py`, doesn't survive a fresh checkout/CI run, and isn't backed up — arguably violating the same "don't lose track of decisions" spirit AD-2/AD-3 exist to protect, just for diagnosis progress instead of verdicts.
- **Dev B**, wanting durability, has `diagnose_scraping.py` call `storage.py` (not literally `diagnose_scraping.py` "writing to `quality_review_log`" in the narrow sense — `storage.py` does the write) to persist progress markers into `quality_review_log` itself, using some placeholder `review_type` like `scraping_diagnostic_progress`. This is technically defensible under AD-5's literal wording (the script "reports," it doesn't "write" — `storage.py` writes) and under AD-2 (any write to the table must go through `storage.save_quality_review()`, which this uses). But it pollutes the table AD-2 promises is cleanly "queryable by `review_type` and `subject`" for verdicts, and directly undermines Finding 2's vocabulary problem.

Both builds are defensible readings of AD-5; they are also incompatible and one of them quietly breaks AD-2's guarantee.

**Fix:** AD-5 should state explicitly whether diagnosis scripts may persist any state at all, and if so, where (e.g., "diagnosis scripts may cache run state only in local files outside version control and never via `storage.py`") — closing the "storage.py writes it, not the script" loophole.

---

## Finding 5 — AD-3's migration mechanism is scoped to quality-loop tables only, leaving AD-6's likely schema needs — and future migration filenames — unspecified

**AD implicated:** AD-3 (tighten)

**The scenario A (scope gap):** AD-3's Binds is `FR-5` and its Rule text is scoped to "`quality_review_log` (and future quality-loop tables)." AD-6 (FR-4, competitor recalibration) says re-scoring is retroactive across all `competitors` rows, but says nothing about *auditability* — whether the old score is preserved anywhere. If a future session decides recalibration should record which threshold produced which score (a reasonable and arguably necessary extension, since otherwise nobody can tell after the fact whether a `competitors` row reflects the current or a stale threshold — exactly the kind of silent heterogeneity AD-6 exists to prevent), that requires a schema change to `competitors`. Is `competitors` a "quality-loop table" under AD-3? Nothing says so.

- **Dev A** generalizes AD-3's rationale (all schema changes must be MCP-applied + versioned-file-tracked) to the whole repo and uses it for the `competitors` migration too.
- **Dev B** reads AD-3 literally — Binds: FR-5, scoped to quality-loop tables — concludes `competitors` isn't covered, and hand-runs the schema change via ad hoc SQL/RPC, i.e. exactly the `competitor_validator.py` anti-pattern AD-3's own *Why* section calls out as the failure mode it exists to prevent. The result: schema drift on `competitors`, untracked in git, the very thing AD-3 was written to close — just one table over.

**The scenario B (naming/numbering gap):** the Structural Seed shows one example file, `001_create_quality_review_log.sql`, but no AD specifies the numbering scheme (zero-padded sequential vs. date-based), the source of truth for "next number" (highest existing file vs. some registry), or a collision rule. Two future sessions each adding a migration independently (e.g., one adding an index to `quality_review_log`, another adding the AD-6 audit column to `competitors`) could both author `002_*.sql`, or one could switch to date-prefixed naming that no longer sorts correctly against the existing zero-padded sequence — breaking the "versioned" guarantee AD-3 relies on for ordering.

**Fix:** Either broaden AD-3's Binds/Rule to cover *all* schema changes in the repo (not just quality-loop tables), explicitly deciding whether `competitors` migrations for AD-6 route through it, and add a concrete numbering rule (e.g., "next integer = 1 + highest existing prefix in `migrations/`, zero-padded to 3 digits, verified by directory listing before authoring").

---

## Summary Table

| # | Seam | AD to tighten | Failure mode if unaddressed |
|---|------|---------------|------------------------------|
| 1 | `subject` column grain undefined across review types | AD-2 | FR-3 Precondition 1 (cross-reference guardrail) silently never matches |
| 2 | `review_type` vocabulary not codified; open vs. closed validation unspecified | AD-2 / AD-4 | Typos/variants create orphaned review_types, same silent-match failure |
| 3 | Function-level (AD-2) vs. script-level (AD-5) chokepoints diverge | AD-5 | Future automated script can write verdicts without human review, fully "compliant" |
| 4 | Diagnosis run-state/progress location unspecified under "read-only" | AD-5 | State lost across sessions, or smuggled into `quality_review_log` as non-verdict rows |
| 5 | AD-3 migration mechanism scoped to quality-loop tables only; no numbering rule | AD-3 | `competitors`-table schema changes for AD-6 may reintroduce untracked schema drift; filename collisions across sessions |
