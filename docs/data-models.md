# Data Models

**Store:** Supabase (managed Postgres), accessed via `supabase-py` (`storage.py::_client()`). **No local migrations** — the schema is not version-controlled in this repo; the tables below are reverse-engineered from every `.select()` / `.insert()` / `.update()` call across the codebase, not from a source-of-truth schema file. Treat this document as a snapshot that can drift from the live DB.

## `compspro` — startup profiles

One row per startup. Primary lookup keys in practice are `name` and `website` (see [Architecture](./architecture.md) for the upsert logic in `storage.save_startup`).

| Column | Type (inferred) | Written by | Notes |
|---|---|---|---|
| `id` | int/uuid (pk) | auto | Used as the update target once a row is matched |
| `name` | text | `extract()` step 1 | Primary human-facing identifier; also used as a de-facto foreign key in `competitors` |
| `country` | text \| null | `extract()` step 1 | Standardized English short name, single country |
| `description` | text | `extract()` step 1 | 2-3 sentence product description, also the text competitor scoring reasons over |
| `website` | text | caller (`main.ingest`) | Secondary match key for upsert |
| `sectors` | text[] | `extract()` step 2a | 1-3 values, must be from `taxonomy.TAXONOMY` top-level keys |
| `subsectors` | text[] | `extract()` step 2b (+ `validate_subsectors`, `demote_generic_erp_tag`) | Validated against `TAXONOMY[sector]` keys |
| `sub_subsectors` | text[] | `extract()` step 2c | Top 4 by confidence, only for subsectors that define a sub-subsector list |
| `sector_confidences` | — | `extract()` | Present in the extractor's return value but stripped by both `main.ingest` and `reprocess_list.process` before saving — **not actually persisted** |
| `subsector_confidences` | — | `extract()` | Same as above — computed, never written to `compspro` |
| `taxonomy_version` | text | `storage.save_startup` | Hardcoded `"v2"` on every insert/update |
| `flaticon_url` | text | `main.fetch_and_save_favicon` | Small icon for graph nodes — Google favicon service first, HTML `<link rel=icon>` fallback. See [logos-vs-favicons distinction] — this is the *favicon*, not the brand logo |
| `logo_url` | text | `main.fetch_and_save_real_logo` | The company's actual logo, as chosen by the LLM from scraped `<img>`/`<link>`/`og:image` candidates |
| `linkedin_url` | text \| null | `extract()` step 1 | Scraped from `linkedin.com/company/` links on the page |

**Local asset side-effect:** both logo functions write files to `assets/logos/{slug}.{ext}` (favicon) and `assets/logos/{slug}_logo.{ext}` (real logo) *before* the DB update — the filesystem is a second, implicit store for this data, and `main.ingest`/`reprocess_list.process` both check for an already-downloaded file before re-fetching.

## `competitors` — validated competitor relationships

One row per unordered `(company_a, company_b)` pair that scored ≥ `COMPETITOR_THRESHOLD` (0.85, `storage.py`). Stored directionally (`company_a`/`company_b`) but treated as undirected everywhere it's read (`storage.get_known_competitors` queries both directions; `graph_app.api_graph` merges both).

| Column | Type (inferred) | Written by | Notes |
|---|---|---|---|
| `id` | int/uuid (pk) | auto | |
| `company_a` | text | `storage.save_relationships` | References `compspro.name`, not enforced at DB level in code |
| `company_b` | text | `storage.save_relationships` | Same |
| `score` | float | `competitor.score_candidates` | Symmetric LLM-assigned similarity score, 0-1 |
| `checked` | boolean, default `false` | `competitor_validator.py` migration | Added via an ad-hoc `ALTER TABLE ... IF NOT EXISTS` run at the top of `competitor_validator.main()` — **not a tracked migration file** |
| `validated` | boolean, default `null` | `competitor_validator.py` | Human/LLM-in-the-loop QA flag set by the validator script |

**Dedup guarantee:** `storage.relationship_exists(a, b)` is checked before every insert, keyed on the exact `(company_a, company_b)` order passed in — callers are responsible for consistent ordering (`backfill_competitors.py` enforces alphabetical order explicitly; `competitor.save_competitors` does not, relying instead on `get_known_competitors` to catch the reverse direction first).

## Schema evolution note

`competitor_validator.py::_run_migration()` is the only place that alters schema, and it does so at runtime via a raw SQL RPC call (`exec_sql`), silently degrading to a printed manual-SQL instruction if the RPC isn't available. If this project grows, the biggest structural gap is the absence of a real migrations directory — every schema change currently has to be inferred from application code rather than read off disk.
