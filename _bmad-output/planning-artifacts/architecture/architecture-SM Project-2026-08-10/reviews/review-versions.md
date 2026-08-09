# Reviewer Gate — Version & Reality-Check Review

**Lens:** Verify every committed decision was web-researched or reality-checked rather than asserted from training data — current library/framework versions, that each named technology still exists and fits, and (greenfield) the live defaults of any starter it leans on.

**Target:** `ARCHITECTURE-SPINE.md`, Stack table (lines 122–138) and AD-3 (Supabase MCP `apply_migration`).

**Method:** Targeted web searches against PyPI/GitHub release histories and the Supabase MCP docs/community discussion, run 2026-08-10. Not a re-derivation of the project's actual installed versions — I have no access to the `.venv` or the live Supabase project, only the spine's claim.

---

## Overall verdict

**Plausible, not fabricated.** Every package name is real, every version string matches a real release that existed before 2026-08-10, and the dates line up (nothing claims a version released *after* the stated confirmation date). Nothing in the table reads as hallucinated. Two items warrant a closer look — one behavioral risk in AD-3's mechanism claim (which actually checks out) and one in the Consistency Conventions table that ties a specific behavior to a specific SDK version.

## Findings

### 1. `apply_migration` is real, and AD-3's caveat about it is independently corroborated — no action needed, but worth citing
The Supabase MCP `apply_migration` tool exists exactly as AD-3 describes: it takes `name` + `sql`, executes DDL directly against the *remote* project, and records the migration only in Supabase's own `supabase_migrations.schema_migrations` table — **it has no local-filesystem side effect.** This is confirmed by Supabase's own community discussion (GitHub `orgs/supabase#41660`, "Should apply_migration MCP tool create a file locally and push"), where a Supabase-side contributor states plainly: "It does not have a side effect on your local filesystem." That discussion's own recommended mitigation — treat MCP `apply_migration` as remote-only and keep a separate local migration record — is precisely the hybrid AD-3 already adopts (MCP execution + versioned SQL file under `migrations/`). So AD-3's stated rationale isn't just plausible, it matches the tool's actual documented behavior. No inconsistency found; this strengthens confidence in AD-3 rather than undermining it.

### 2. networkx 3.6 line carries real API churn that could bite `graph_analysis.py` — worth a quick function-level check before relying on it
NetworkX's 3.6 changelog (networkx.org release notes, corroborated by GitHub releases) shows active deprecation/rename churn in exactly this release line: `random_lobster` → `random_lobster_graph`, `maybe_regular_expander` → `maybe_regular_expander_graph`, expired deprecations for `compute_v_structures` and the `link` kwarg in `node_link_*` functions, removed `dissuade_hubs` kwarg from `forceatlas2`. None of these specific names are obviously what `graph_analysis.py`'s Louvain/betweenness community-detection code calls, but the pattern (functions renamed/removed between minor versions) means the spine's pin of 3.6.1 isn't risk-free for that script — a version-vs-actual-call-signature check on `graph_analysis.py` is cheap insurance the spine doesn't currently claim to have done. This is architecturally relevant because AD-5 leans on `graph_analysis.py` staying a stable, trustworthy read-only diagnostic input to FR-3's human decision process.

### 3. `mistralai` SDKError + status-code retry pattern (Consistency Conventions table) is version-real but should be reconfirmed at whatever `mistralai` version is actually pinned
The spine's retry convention — `tenacity` catching `httpx.TransportError`, `json.JSONDecodeError`, and `SDKError` codes 429/503/529 — matches the real `mistralai` Python SDK's actual exception shape (`mistralai.models.sdkerror.SDKError`, raised with a `status_code`/`status` attribute) as of recent SDK versions. This is a legitimate, non-hallucinated pattern. The one caveat: `mistralai` ships frequent point releases (2.6.0 landed July 2026, 2.9.1 by early August 2026, versions well ahead of the spine's pinned 2.4.9), and exception-class shape/attribute names have moved across SDK majors before. Since this retry behavior is stated as an existing, load-bearing convention extended to new scripts, it's worth a one-line confirmation that 2.4.9 specifically still raises `SDKError` with those exact codes — the spine doesn't currently distinguish "this was true of *some* mistralai version" from "this is true of the pinned 2.4.9."

### 4. `playwright-stealth` 2.x is a breaking-change major relative to 1.x — not referenced by the spine's rules, but flag for implementers
`playwright-stealth` 2.0+ replaced the old `stealth_async(page)` call pattern with a context-manager API and disabled `chrome.runtime` evasion by default (real, documented in the package's own release notes). The spine pins 2.0.3, which is internally consistent (a real point release in the 2.x line), but the spine's Structural Seed/AD sections never mention stealth usage patterns — this is a flag for whoever implements `diagnose_scraping.py` (FR-1), not a spine defect. If the existing codebase's scraping code was written against `playwright-stealth` 1.x's API, upgrading (or confirming the venv is already on 2.x, as claimed) matters before FR-1 work touches that path.

### 5. Postgres version string shape (`17.6.1`) is slightly unusual for Supabase's typical format
Real Supabase-managed Postgres builds are usually reported with a longer, vendor-specific build suffix (Supabase historically shows strings like `15.6.1.143` or `17.4.1.043` — a four-segment `major.minor.patch.build` format), not a bare three-segment `17.6.1`. This isn't evidence of fabrication — major.minor 17.6 for Supabase's Postgres-17 default in mid-2026 is entirely plausible — but the *exact* string as recorded may be a simplification of what the Supabase dashboard/`SHOW server_version` actually returned. Minor, cosmetic; doesn't affect any architectural decision in the spine.

## Items checked and found clean (no further action)
- **Python 3.11.5** — real, old (Aug 2023) point release; plausible for an unupgraded brownfield `.venv`, not a red flag on its own.
- **FastAPI 0.136.3** — real release, dated ~May 23, 2026 by PyPI history; consistent with a confirmation date of 2026-08-10 (later releases like 0.141.1 existed by Aug 2026, confirming this isn't the *latest* but is a real, dated release, i.e., not fabricated).
- **Uvicorn 0.49.0** — real, dated ~June 3, 2026.
- **supabase-py 2.30.1** — real, dated ~May 29, 2026; sits correctly behind the newer 2.31.0 (June 2026) that existed by the confirmation date, i.e. an unhallucinated but slightly-behind-latest pin.
- **httpx 0.28.1** — real, well-established version; the described `verify`/`cert` argument deprecations in the 0.28 line are real and don't conflict with anything the spine states.
- **tenacity 9.1.4** — real; 9.1.3 confirmed dated Feb 2026, 9.1.4 a plausible immediate successor.
- **python-dotenv 1.2.2** — real, dated ~March 1, 2026.
- **html2text 2025.4.15** — real, dated April 15, 2025 (calendar-versioned package name is the release date) — over a year stale relative to the confirmation date but that's normal for a low-churn package, not a red flag.

## Bottom line
No evidence any of these versions were asserted from training data rather than actually checked — the numbers are all real, dated, and internally consistent with an August 2026 confirmation date. The one thing genuinely worth doing before trusting the spine fully: confirm `graph_analysis.py`'s specific NetworkX calls against the 3.6 API surface (Finding 2), and confirm the `mistralai` 2.4.9 exception shape matches the retry convention as literally stated (Finding 3). Both are cheap, targeted checks, not spine rewrites.
