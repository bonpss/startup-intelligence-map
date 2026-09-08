-- Embedding pre-filter for competitor scoring (cost-reduction chantier,
-- 2026-09-01 conversation). competitor.py's compare() currently sends EVERY
-- same-sector/subsector candidate to mistral-large-latest in chunks of 20 --
-- for a company in a dense subsector (e.g. 144 candidates), that's 8 full
-- chunk calls just to find the handful of real competitors. The fix: rank
-- candidates by embedding similarity first (mistral-embed, ~15x cheaper than
-- Large) and only send the top N to the expensive scoring step.

alter table compspro add column embedding jsonb;

comment on column compspro.embedding is 'mistral-embed vector (1024 floats, as a JSON array) computed from this startup''s description at ingestion time (main.py''s _ingest_sync). Used by competitor.py to rank same-subsector candidates by similarity before the expensive mistral-large-latest scoring step, so only the most plausible candidates are sent -- not recomputed per comparison, only once per startup. NULL for rows created before this column existed, backfilled once via backfill_embeddings.py. A NULL embedding must never cause a candidate to be silently dropped from scoring -- it is kept, not excluded, until backfilled.';
