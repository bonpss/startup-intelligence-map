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

1. **Scrape**: `main.py <url>` pulls and cleans the startup's website content
2. **Classify**: a two-step LLM extraction (free-form labels → taxonomy matching) assigns sector / subsector / sub-subsector
3. **Match competitors**: new startups are scored against the existing database and linked bidirectionally
4. **Visualize**: a local web app renders the whole graph and lets you search any startup

Everything is stored in Supabase and browsable through the graph UI.

## Stack

Python · Mistral (LLM extraction) · Supabase (storage) · FastAPI (graph UI, email/password auth) · Playwright / Trafilatura (scraping)

## Scope of this repo

This is the pipeline and app code, not a data export. It does **not** include:
- A live database — there's no access to my own Supabase project or the startups already in it. Running this yourself means pointing it at your **own** Supabase project (schema via `migrations/`), starting from empty.
- The classification/matching logic — `taxonomy.py`, `competitor.py`, `competitor_validator.py`, and `graph_analysis.py` (the actual sector/subsector taxonomy and competitor-scoring rules) are proprietary and intentionally excluded (see `.gitignore`).

So this repo shows the architecture — scraping, LLM extraction pipeline, storage layer, auth, graph UI — but isn't a drop-in clone of the real thing.

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in MISTRAL_API_KEY, SUPABASE_URL, SUPABASE_KEY, SESSION_SECRET_KEY, OWNER_EMAIL

# apply migrations/*.sql to your own Supabase project, in order (SQL editor or CLI)

python seed_owner.py                 # one-time: create the owner account (exempt from the signup cap)
python main.py https://startup.com   # add a startup
python graph_app.py                  # launch the graph UI → http://localhost:8000, log in with the owner account
```

## Access & demo mode

`graph_app.py` is gated behind email/password auth (`auth.py`). `MAX_USERS` caps how many non-owner accounts can sign up (blank/`0` = signups closed) — meant for sharing a public demo link without opening it to unlimited signups. The owner account (`OWNER_EMAIL`, created via `seed_owner.py`) is exempt from the cap and gets access to `/admin`, an owner-only dashboard (`dashboard.py`) showing usage stats and Mistral API cost tracking (`pricing.py`).

## Key modules

| File | Role |
|---|---|
| `extractor.py` | LLM extraction: free labels → taxonomy matching |
| `storage.py` | Supabase read/write helpers |
| `graph_app.py` | Web app: auth-gated graph UI + admin dashboard |
| `auth.py` | Email/password auth and the public-demo signup cap |
| `dashboard.py` | Owner-only usage and API cost aggregation |
| `pricing.py` | Mistral pricing table used for cost tracking |

Not included: `taxonomy.py` (3-level sector → subsector → sub-subsector taxonomy) and `competitor.py` (competitor scoring and relationship saving) — see [Scope of this repo](#scope-of-this-repo).
