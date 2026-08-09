# SM Project

## Pipeline overview
- `main.py <url>` — scrape a startup URL, extract and classify it, detect competitors
- `taxonomy_agent.py` — scan all subsectors for coherence, split incoherent ones into sub-subsectors, run taxonomy audit
- `reprocess_all.py` — re-run extraction on all startups in Supabase to update sectors/subsectors/sub_subsectors
- `competitor_validator.py` — validate 5 random competitor relationships via LLM, flag false positives
- `graph_app.py` — local web app to search startups and visualize competitor graphs (run with `python graph_app.py`, open http://localhost:8000)

## Core modules
- `extractor.py` — two-step LLM extraction (free labels → taxonomy matching)
- `competitor.py` — competitor scoring and bidirectional relationship saving
- `storage.py` — Supabase read/write helpers
- `taxonomy.py` — 3-level taxonomy: sector → subsector → sub-subsectors

## Data (Supabase)
- `compspro` — startup profiles (name, sectors, subsectors, sub_subsectors, description, website)
- `competitors` — validated competitor relationships (company_a, company_b, score, checked, validated)

## Key commands
```bash
python main.py https://startup.com           # add a startup
python taxonomy_agent.py                     # run taxonomy cleanup
python reprocess_all.py --start 0 --end 150  # reprocess in parallel
python competitor_validator.py               # validate competitor links
python graph_app.py                          # launch graph UI
```
