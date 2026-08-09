# Development Guide

## Prerequisites

- **Python 3.11** (the project's `.venv` is built against 3.11.5)
- A Supabase project with `compspro` and `competitors` tables (see [Data Models](./data-models.md)) — no schema/seed script exists in-repo, so this has to be created manually or pulled from an existing project
- A Mistral AI API key

## Environment setup

Copy the variables `.env` expects (values redacted, keys confirmed present):

```
MISTRAL_API_KEY=...
SUPABASE_URL=...
SUPABASE_KEY=...
```

`storage.py::_client()` also honors an optional `SUPABASE_SERVICE_KEY` (preferred over `SUPABASE_KEY` when present) — not currently set in `.env` but supported by every script that needs elevated write access.

## Installation

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium   # main.py's scraper needs the browser binary
```

**`requirements.txt` is incomplete** — the installed `.venv` has several packages actually imported by the code that aren't declared:

| Installed but undeclared | Used by |
|---|---|
| `fastapi`, `uvicorn` | `graph_app.py` (noted as a comment at the top of that file instead of in requirements.txt) |
| `networkx` | `graph_analysis.py` |
| `playwright-stealth` | (imported nowhere found in the exhaustive scan — installed but apparently unused, or used by a script not present in this tree) |
| `pandas`, `numpy` | Not found used in any scanned source file — likely leftover from removed/renamed scripts (see the `taxonomy_agent.py`/`reprocess_all.py` gap in [Source Tree Analysis](./source-tree-analysis.md)) |

Recommend reconciling `requirements.txt` against the actual `.venv` (`pip freeze`) next time it's touched, so a fresh clone doesn't silently fail on `graph_app.py` or `graph_analysis.py`.

## Running things

There's no single "start the app" command — pick the script for the task:

```bash
python main.py https://startup.com              # add one startup
python graph_app.py                              # serve the search/graph UI at http://localhost:8000
python audit_taxonomy.py                         # taxonomy coverage/anomaly report → audit_report.json
python graph_analysis.py                         # graph structure report → graph_analysis_report.json
python competitor_validator.py                   # sample-QA 5 unchecked competitor pairs
python backfill_competitors.py --dry-run         # estimate LLM calls before a bulk backfill
python backfill_competitors.py --start 0 --end 150
python backfill_specific.py websites.txt         # targeted backfill for a specific list
python reprocess_list.py [subsector]             # re-run ingest over the hardcoded list, or every startup in a given subsector
```

## Testing

**No test suite exists for this project.** (The only `test_*.py` files under this working directory belong to the BMAD tooling in `.claude/skills/`, not to SM Project itself.) There is no CI configuration either — see the gap noted in [Architecture](./architecture.md).

## Common development tasks

- **Add a new sector/subsector:** edit `taxonomy.py::TAXONOMY` (and `SUBSECTOR_DEFINITIONS` if the subsector needs disambiguation guidance for the LLM) — no code changes needed elsewhere, `extractor.py` reads the dict dynamically.
- **Change competitor sensitivity:** `storage.py::COMPETITOR_THRESHOLD` (currently 0.85).
- **Re-run classification for one company:** `python reprocess_list.py` after adding its URL to `reprocess_list.URLS`, or by subsector via `python reprocess_list.py "<Subsector Name>"`.
- **Inspect current taxonomy health:** `python audit_taxonomy.py`, then check `audit_report.json`'s `anomalies` section (empty subsectors, over-tagging, "Uncategorized", taxonomy drift).
