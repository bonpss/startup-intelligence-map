# Project Overview

## What this is

SM Project is a startup-intelligence pipeline and internal tool: given a startup's URL, it scrapes the site, classifies the company into a 3-level taxonomy (sector → subsector → sub-subsector) via a multi-step LLM pipeline, stores the profile in Supabase, and automatically detects and scores competitor relationships against every other startup already tracked. A small FastAPI app exposes a search UI and a D3.js force-directed graph for exploring the resulting competitor network.

## Repository structure

**Monolith**, single part, flat script layout at the repo root (no `src/`). See [Source Tree Analysis](./source-tree-analysis.md) for the annotated tree.

## Tech stack summary

Python 3.11 · FastAPI/Uvicorn · Mistral AI (LLM classification & competitor scoring) · Supabase (Postgres) · Playwright (scraping) · networkx (graph analysis). Full table in [Architecture](./architecture.md#technology-stack).

## Architecture type

Pipeline / shared-core-library — four core modules (`storage.py`, `taxonomy.py`, `extractor.py`, `competitor.py`) are imported by eight independent entry-point scripts plus one long-running server. Full detail in [Architecture](./architecture.md).

## Generated documentation

- [Architecture](./architecture.md)
- [Source Tree Analysis](./source-tree-analysis.md)
- [Development Guide](./development-guide.md)
- [API Contracts](./api-contracts.md)
- [Data Models](./data-models.md)

## Existing documentation

- [README.md](../README.md) — author-maintained pipeline overview and command reference. Note: it references `taxonomy_agent.py` and `reprocess_all.py`, neither of which exists in the current tree — likely renamed or removed since the README was last updated.

## Getting started

1. Read [Development Guide](./development-guide.md) for environment setup (`.env` variables, `requirements.txt` gaps, Playwright browser install).
2. Read [Architecture](./architecture.md) for how the scrape → classify → persist → competitor-score pipeline fits together.
3. Read [Data Models](./data-models.md) before touching `compspro`/`competitors` — the schema lives only in application code, there's no migrations directory.
4. For adding new startups or running maintenance scripts, the command reference in [Development Guide](./development-guide.md#running-things) covers every entry point.
