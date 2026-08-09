# API Contracts

**Part:** root (monolith) · **Framework:** FastAPI (`graph_app.py`) · **Server:** Uvicorn, run via `python graph_app.py` (binds `0.0.0.0:8000`)

All endpoints are unauthenticated — there is no auth/session layer in this codebase. `/api/ingest` in particular triggers a real scrape + paid LLM call for anyone who can reach the process; treat that as a known gap if this is ever exposed beyond localhost.

## Endpoints

### `GET /api/search`

Search startups by name or website substring, for the search bar's autocomplete.

| Query param | Type | Required | Notes |
|---|---|---|---|
| `q` | string | yes | Trimmed; returns `[]` if `len(q) < 2` |

**Response** `200` — array (max 10) of:
```json
{
  "name": "string",
  "sectors": ["string"],
  "subsectors": ["string"],
  "description": "string",
  "flaticon_url": "string",
  "website": "string"
}
```

Implementation: `compspro.select(...).or_("name.ilike.%q%,website.ilike.%q%").limit(10)`.

---

### `POST /api/ingest`

Scrape a URL, classify it with the LLM pipeline, persist it, and score it against existing competitors. Synchronous — the request blocks for the full scrape+LLM+DB round trip.

| Query param | Type | Required | Notes |
|---|---|---|---|
| `url` | string | yes | `https://` prepended if scheme missing |

**Responses**
- `200` — `{"name": str, "action": "saved"\|"updated", "competitors_found": int}`
- `400` — empty URL
- `422` — page scraped but no startup info could be extracted (`ValueError` from `main.ingest`)
- `502` — scraping or LLM extraction failed for any other reason

Calls straight into `main.ingest()` (the same function the CLI entrypoint uses) — see [Development Guide](./development-guide.md) for the full pipeline this triggers.

---

### `GET /api/graph/all`

Full competitor graph — every startup as a node, every validated-threshold competitor pair as an edge. Powers the `/graph` global view.

**Response** `200`:
```json
{
  "nodes": [
    {"name": "", "sectors": [], "flaticon_url": "", "logo_url": "", "description": "", "website": "", "linkedin_url": ""}
  ],
  "links": [
    {"source": "company_a name", "target": "company_b name", "score": 0.0}
  ]
}
```

Paginates Supabase reads in batches of 1000 for both `compspro` and `competitors` (Supabase's per-request row cap). Links whose `source`/`target` don't match a known node name are dropped before returning, to avoid crashing the D3 force-graph client.

---

### `GET /api/graph/{name}`

Single-startup competitor neighborhood (ego graph) — powers `/startup/{name}`.

**Path param:** `name` — exact startup name (used as the `compspro`/`competitors` join key, not an id).

**Response** `200`:
```json
{
  "center": {"name": "", "sectors": [], "subsectors": [], "description": "", "website": "", "flaticon_url": "", "logo_url": "", "linkedin_url": ""},
  "nodes":  ["...same shape as center, one per competitor..."],
  "links":  [{"source": "", "target": "", "score": 0.0}]
}
```

Reads `competitors` both as `company_a` and as `company_b` (the relationship is stored once, undirected) and dedupes pairs via a `frozenset` seen-set before resolving node details from `compspro`.

## Pages (non-API, HTML responses)

| Route | Returns |
|---|---|
| `GET /` | Search home page (`SEARCH_HTML`) |
| `GET /graph` | Global force-directed graph (`GLOBAL_GRAPH_HTML`) |
| `GET /startup/{name}` | Single-startup graph (`GRAPH_HTML_TEMPLATE`, name injected via JSON-escaped string replace) |

All three are server-rendered f-string HTML/CSS/D3.js bundled directly in `graph_app.py` — there is no separate frontend build step or static asset pipeline beyond `assets/` (served at `/assets` via `StaticFiles`).
