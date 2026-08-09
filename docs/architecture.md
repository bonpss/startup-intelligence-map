# Architecture

## Executive Summary

SM Project is a startup-intelligence pipeline: it scrapes a startup's website, classifies it into a 3-level sector/subsector/sub-subsector taxonomy using a two-step Mistral LLM pipeline, persists it to Supabase, and scores it against existing entries to detect competitor relationships (also LLM-judged). A thin FastAPI layer exposes search, ingestion, and the competitor graph to a server-rendered D3.js UI, and a handful of standalone CLI scripts handle bulk backfill, taxonomy auditing, and graph-structure analysis over the same data.

It is a **single-part Python monolith** — one flat directory of scripts sharing four core modules, not a client/server split, not a package. There's no persistent worker process beyond the FastAPI server; the pipeline scripts are run manually or via cron/ad-hoc.

## Technology Stack

| Category | Technology | Version | Justification |
|---|---|---|---|
| Language | Python | 3.11.5 | — |
| Web framework | FastAPI + Uvicorn | fastapi 0.136.3, uvicorn 0.49.0 | Thin API + server-rendered HTML for the graph UI; run directly (`python graph_app.py`), not via a process manager |
| LLM | Mistral AI (`mistralai` SDK) | 2.4.9 | `mistral-large-latest` for extraction/competitor-scoring, `mistral-medium-latest` for sector/subsector classification |
| Database | Supabase (managed Postgres) | supabase-py 2.30.1 | Sole datastore; no local schema/migrations (see [Data Models](./data-models.md)) |
| Scraping | Playwright (+ playwright-stealth) | playwright 1.60.0 | Headless Chromium, cookie-banner removal, anti-bot evasion via stealth + spoofed UA |
| HTML→text | html2text | 2025.4.15 | Converts scraped HTML to markdown before it's fed to the LLM |
| HTTP client | httpx | 0.28.1 | Used both for direct favicon/logo fetches and as the transport error surface `tenacity` retries on |
| Retry | tenacity | 9.1.4 | Exponential backoff on transient LLM/HTTP failures (429/503/529, transport errors, JSON decode errors) |
| Config | python-dotenv | 1.2.2 | `.env` loaded by every entry-point module |
| Graph analysis | networkx | 3.6.1 | Betweenness centrality + Louvain community detection over the competitor graph (`graph_analysis.py`) |

See [Development Guide](./development-guide.md) for the `requirements.txt` vs. actually-installed discrepancy.

## Architecture Pattern

**Pipeline / shared-core-library pattern**, not a layered MVC or service architecture:

```
                     ┌──────────────┐
   URL ──────────────▶   scrape()   │  main.py (Playwright)
                     └──────┬───────┘
                            ▼
                     ┌──────────────┐
                     │  extract()   │  extractor.py — 4 sequential LLM calls:
                     │              │  step1 (free labels) → 2a (sectors) →
                     │              │  2b (subsectors, per sector) → 2c (sub-subsectors)
                     └──────┬───────┘
                            ▼
                     ┌──────────────┐
                     │ taxonomy.py  │  validate_subsectors() / demote_generic_erp_tag()
                     │ (post-proc)  │  post-process the LLM's raw picks against fixed rules
                     └──────┬───────┘
                            ▼
                     ┌──────────────┐
                     │save_startup()│  storage.py — upsert by website, fallback by name
                     └──────┬───────┘
                            ▼
                     ┌──────────────┐
                     │  compare()   │  competitor.py — candidate pool from storage.get_by_subsectors(),
                     │              │  scored in chunks of 20 via one LLM call each, saved ≥ 0.85
                     └──────────────┘
```

`graph_app.py`'s `/api/ingest` calls the exact same `main.ingest()` used by the CLI — there is one pipeline implementation, two triggers (CLI arg, HTTP POST).

## Data Architecture

See [Data Models](./data-models.md) for full column-level detail. In short: two tables, `compspro` (startup profiles, taxonomy tags, image URLs) and `competitors` (undirected scored pairs, with a human/LLM QA flag layered on after the fact). No migrations directory — schema changes happen through raw SQL run ad hoc (`competitor_validator.py::_run_migration()`) or manually in the Supabase dashboard.

## API Design

See [API Contracts](./api-contracts.md). Four JSON endpoints + three server-rendered HTML pages, all unauthenticated, all in one file (`graph_app.py`).

## Component Overview

Not applicable — this is a backend/pipeline project with no reusable UI component library. The three HTML pages in `graph_app.py` are self-contained f-string templates (HTML+CSS+D3.js inline), not componentized.

## Source Tree

See [Source Tree Analysis](./source-tree-analysis.md).

## Development Workflow

See [Development Guide](./development-guide.md).

## Deployment Architecture

**No deployment configuration exists in this repository** — no Dockerfile, no `docker-compose.yml`, no CI/CD pipeline (`.github/workflows/`, etc.). `graph_app.py` runs uvicorn directly and binds `0.0.0.0:8000`, which suggests it's intended to be reachable beyond localhost, but there's nothing in-repo describing how it's actually deployed (process manager, reverse proxy, TLS, secrets injection). This is the single biggest gap for anyone trying to reproduce or harden the production setup from this repo alone.

## Testing Strategy

None — no test suite exists for this project (see [Development Guide](./development-guide.md)). Validation currently happens two ways instead: `competitor_validator.py` spot-checks a random sample of saved competitor pairs against LLM judgment, and `audit_taxonomy.py` reports taxonomy-coverage anomalies. Neither is an automated test; both are manually-run reporting scripts.
