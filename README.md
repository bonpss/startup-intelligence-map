# Startup Intelligence Map

A pipeline that scrapes startup websites, classifies them into a sector/subsector taxonomy with an LLM, and automatically detects competitor relationships, visualized as an interactive graph.

## Why

Built out of frustration with how competitor-mapping is handled in existing tools like Pitchbook or Dealroom: competitors are usually static, manually-tagged lists rather than a living graph that updates as new companies enter the market. This project explores whether an LLM-driven classification and scoring pipeline can maintain that graph automatically.

## Screenshots

**Competitor graph**: every startup in the database, linked to its closest competitors:

![Graph overview](docs/screenshots/graph-overview.png)

**Startup detail** (AMI Labs): profile, taxonomy, and local competitor neighborhood:

![Startup detail](docs/screenshots/startup-detail.png)

## How it works

1. **Queue**: `POST /api/ingest` (or `main.py <url>` from the CLI) drops a row into `ingestion_queue` and returns immediately (`202`). A single in-process background worker (concurrency 1) consumes the queue so two ingests never race on the same domain; failed rows land in an "En attente" tab in the UI with per-row retry/dismiss.
2. **Scrape** (`main.py`): a light `httpx` fetch runs first; if the page looks JS-rendered or content-thin, it falls back to a full Playwright browser. HTML is cleaned into markdown (boilerplate/nav-dense block stripping, paragraph dedup), and the startup's logo/favicon and LinkedIn URL are extracted from the raw HTML in the same pass.
3. **Extract** (`extractor.py`): a four-step LLM extraction over the cleaned text — Step 1 pulls free-form descriptive labels, Step 2a/2b/2c match them to the taxonomy's sector → subsector → sub-subsector levels (`taxonomy.py`, see [Scope of this repo](#scope-of-this-repo)). Taxonomy-side rules demote a handful of over-broad generic tags and drop redundant `Uncategorized` labels.
4. **Embed** (`embeddings.py`): a `mistral-embed` vector is computed for the description, used to pre-filter the competitor-candidate pool before the (more expensive) scoring call.
5. **Match competitors** (`competitor.py`, see below): the new startup is scored against same-subsector candidates and linked bidirectionally in `competitors`.
6. **Visualize** (`graph_app.py`): a FastAPI app serves the graph UI — search, per-startup detail with its competitor neighborhood, and the ingestion queue's live status.

Every Mistral call is retried with exponential backoff on transient errors (`retry.py`, shared across the extraction, embedding and matching steps) and logged with token counts + estimated cost (`api_call_log`, surfaced on `/admin`).

## API surface

| Route | Purpose |
|---|---|
| `POST /api/signup`, `/api/login`, `/api/logout` | Email/password auth for the public demo |
| `GET /api/search` | Search startups by name |
| `POST /api/ingest` | Queue a URL for ingestion (`202`, async) |
| `GET /api/ingestion-queue`, `/summary`, `POST .../retry`, `.../mark-seen`, `DELETE .../{id}` | Queue status, manual retry/dismiss of failed rows |
| `GET /api/graph/all`, `/api/graph/{name}` | Full graph / one startup's competitor neighborhood |
| `GET /api/dashboard` | Owner-only usage & Mistral cost stats |
| `GET /`, `/graph`, `/startup/{name}`, `/login`, `/signup`, `/admin` | Server-rendered pages |

## Data model

Schema lives in `migrations/*.sql`, applied in order. Core tables: `compspro` (one row per startup: description, taxonomy tags, embedding, logo/LinkedIn URLs), `competitors` (bidirectional scored links, `active` flag for soft-delete), `ingestion_queue` (async queue with status/retry/dedup-by-domain), `users` (bcrypt password hashes, session-based auth), `api_call_log` (per-call Mistral token/cost tracking), `quality_review_log` (audit trail for retroactive data corrections).

## Access & demo mode

`graph_app.py` is gated behind email/password auth (`auth.py`), sessions via a signed httponly cookie. `MAX_USERS` caps how many non-owner accounts can sign up (blank/`0` = signups closed) — meant for sharing a public demo link without opening it to unlimited signups. The owner account (`OWNER_EMAIL`, created via `seed_owner.py`) is exempt from the cap and gets access to `/admin`, an owner-only dashboard (`dashboard.py`) showing usage stats and Mistral API cost tracking (`pricing.py`).

## Stack

Python · Mistral (LLM extraction, embeddings) · Supabase (storage) · FastAPI + Starlette sessions (graph UI, auth) · Playwright / Trafilatura (scraping) · Tenacity (retry/backoff) · Pytest (tests)

## Scope of this repo

This is the pipeline and app code, not a data export. It does **not** include:
- A live database — there's no access to my own Supabase project or the startups already in it. Running this yourself means pointing it at your **own** Supabase project (schema via `migrations/`), starting from empty.
- My real classification/matching logic — the actual, refined `taxonomy.py` (the real sector/subsector tree, iterated on for months) and `competitor.py` (the tuned scoring prompt) are proprietary and stay private. What's in this repo under those names is a small **illustrative placeholder**: a generic example taxonomy and a simplified scoring prompt, with the exact same function signatures the rest of the pipeline expects — so the app is genuinely runnable end-to-end, just classifying into example categories instead of the real ones. `competitor_validator.py` and `graph_analysis.py` (unused by the runnable path above) are excluded entirely (see `.gitignore`).

So this repo shows the real architecture — scraping, LLM extraction pipeline, storage layer, auth, graph UI — running against placeholder classification logic instead of the real one.

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in MISTRAL_API_KEY, SUPABASE_URL, SUPABASE_KEY, SESSION_SECRET_KEY, OWNER_EMAIL

# apply migrations/*.sql to your own Supabase project, in order (SQL editor or CLI)

python seed_owner.py                 # one-time: create the owner account (exempt from the signup cap)
python main.py https://startup.com   # add a startup
python graph_app.py                  # launch the graph UI → http://localhost:8000, log in with the owner account
```

Tests (`test_auth.py`, `test_retry.py`, `test_storage.py`) cover auth hashing/sessions, the retry predicate, and storage helpers/query-building logic — run with `pytest`.

## Key modules

| File | Role |
|---|---|
| `extractor.py` | LLM extraction: free labels → taxonomy matching |
| `taxonomy.py` | **Placeholder** 3-level sector → subsector → sub-subsector taxonomy |
| `competitor.py` | **Placeholder** competitor scoring and relationship saving |
| `embeddings.py` | `mistral-embed` vectors for the competitor pre-filter |
| `storage.py` | Supabase read/write helpers, ingestion queue, cost logging |
| `retry.py` | Shared Mistral retry/backoff predicate |
| `graph_app.py` | FastAPI app: auth-gated graph UI, ingestion queue, admin dashboard |
| `auth.py` | Email/password auth and the public-demo signup cap |
| `dashboard.py` | Owner-only usage and API cost aggregation |
| `pricing.py` | Mistral pricing table used for cost tracking |

`taxonomy.py`/`competitor.py` are placeholders, not my real classification logic — see [Scope of this repo](#scope-of-this-repo).

## Ops tooling

A handful of one-off/maintenance scripts round out the repo: `audit_stale_competitors.py` flags competitor pairs whose companies have drifted apart taxonomically, `delete_stale_competitor_pairs.py` / `delete_stale_fine_subsector_pairs.py` clean those up, `backfill_competitors.py` / `backfill_embeddings.py` / `backfill_linkedin_urls.py` retroactively fill in data for rows added before a given feature existed, `reprocess_list.py` re-runs the pipeline on an explicit list of startups, and `diagnose_scraping.py` inspects why a given URL scraped poorly. Each is a standalone CLI script with its own `--dry-run` where relevant.
