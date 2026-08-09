# Source Tree Analysis

Single-part monolith — a flat collection of Python scripts at the repo root rather than a `src/`-style layout. No package structure (`__init__.py` absent); everything is imported by module name off the root.

```
SM Project/
├── main.py                    # Entry point: `python main.py <url>` — scrape → extract → save → competitor-score one startup
│                               # Also exports scrape()/ingest()/fetch_and_save_*() reused by graph_app.py and reprocess_list.py
├── extractor.py                # Two-step LLM extraction: free-form labels (step 1) → taxonomy-constrained classification (steps 2a/2b/2c)
├── taxonomy.py                 # TAXONOMY dict: sector → subsector → [sub-subsectors], + SUBSECTOR_DEFINITIONS, + validate_subsectors()/demote_generic_erp_tag() post-processing rules
├── competitor.py                # LLM-based pairwise competitor scoring (compare/score_candidates), save_competitors(), explore_transitive() for 2nd-degree matches
├── storage.py                   # All Supabase reads/writes — sole owner of the `compspro` and `competitors` table access (see data-models.md)
├── graph_app.py                 # FastAPI app: /api/search, /api/ingest, /api/graph/all, /api/graph/{name} + 3 server-rendered HTML pages (search/global-graph/startup-graph)
├── graph_analysis.py             # CLI report: networkx betweenness centrality + Louvain community detection over the competitor graph → graph_analysis_report.json
├── audit_taxonomy.py             # CLI report: taxonomy coverage/anomaly audit (empty subsectors, over-tagging, drift vs. current TAXONOMY) → audit_report.json
├── competitor_validator.py       # LLM-in-the-loop QA: samples 5 unchecked `competitors` rows, asks the LLM to confirm/deny, writes checked/validated flags
├── backfill_competitors.py       # Bulk backfill: alphabetical sweep over all `compspro` rows, scores each unordered pair once, resumable via backfill_progress.txt
├── backfill_specific.py          # Targeted backfill: re-scores a given list of startups (by website, from a file) against their full current candidate pool
├── reprocess_list.py             # Re-run the full ingest pipeline (minus initial insert-vs-update ambiguity) over a hardcoded or subsector-filtered URL list
├── requirements.txt              # Declared deps — NOTE: incomplete, see development-guide.md
├── README.md                     # Existing pipeline overview (author-maintained, predates this scan)
├── assets/
│   └── logos/                    # Downloaded favicons (`{slug}.{ext}`) and real logos (`{slug}_logo.{ext}`) — see data-models.md for the flaticon_url/logo_url split
├── docs/                         # This documentation set (generated)
├── .venv/                        # Local virtualenv — NOT the project's own dependency manifest source of truth (see dev guide)
├── audit_report.json             # Latest audit_taxonomy.py output (data, not source)
├── graph_analysis_report.json    # Latest graph_analysis.py output (data, not source)
├── backfill_progress.txt         # Resume-cursor for backfill_competitors.py (data, not source)
└── backfill_isolates_live.log    # Ad-hoc run log (data, not source)
```

## Entry points

There is no single application entry point — this is a collection of independent CLI scripts plus one long-running server, all sharing the same core modules:

| Script | Invocation | Role |
|---|---|---|
| `main.py` | `python main.py <url>` | Add one startup |
| `graph_app.py` | `python graph_app.py` | Long-running FastAPI/Uvicorn server on `:8000` |
| `audit_taxonomy.py` | `python audit_taxonomy.py` | One-shot taxonomy audit report |
| `graph_analysis.py` | `python graph_analysis.py` | One-shot graph-structure report |
| `competitor_validator.py` | `python competitor_validator.py` | Sample-based competitor QA pass |
| `backfill_competitors.py` | `python backfill_competitors.py [--start N --end N] [--dry-run]` | Bulk competitor backfill |
| `backfill_specific.py` | `python backfill_specific.py <websites.txt>` | Targeted competitor backfill |
| `reprocess_list.py` | `python reprocess_list.py [subsector]` | Re-run ingest over a hardcoded or subsector-filtered list |

README.md additionally references `taxonomy_agent.py` and `reprocess_all.py` — **neither exists in the current tree**; either renamed/removed or the README has drifted. Worth a quick check with the project owner (flagged again in the [Development Guide](./development-guide.md)).

## Shared core (imported by everything above)

`storage.py` → `taxonomy.py` → `extractor.py` → `competitor.py` form the shared dependency spine: every entry-point script imports some subset of these four rather than duplicating scrape/classify/persist/score logic.

## Integration points

Single external service integrations, no internal service boundaries (it's a monolith):
- **Supabase** — the only database, reached exclusively through `storage.py`
- **Mistral AI** — the only LLM provider, reached directly from `extractor.py` and `competitor.py` (not funneled through `storage.py`)
- **Google favicon service + target sites' own HTML** — logo/favicon sourcing, in `main.py`
