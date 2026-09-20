-- Per-call Mistral usage/cost log, for the owner-only /admin dashboard's cost
-- metrics (2026-09-04 conversation). No cost/token tracking existed before this --
-- extractor.py/competitor.py/embeddings.py all discarded the API response's
-- `usage` field. This table captures it going forward; historical rows (every
-- startup ingested before this migration) have no cost data and never will.

create table api_call_log (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  ingestion_queue_id bigint references ingestion_queue(id) on delete set null,
  label text,
  call_type text not null,
  model text not null,
  prompt_tokens integer not null default 0,
  completion_tokens integer not null default 0,
  total_tokens integer not null default 0,
  item_count integer,
  cost_usd numeric(12, 6) not null default 0
);

create index api_call_log_ingestion_queue_id_idx on api_call_log (ingestion_queue_id);
create index api_call_log_created_at_idx on api_call_log (created_at);

alter table api_call_log enable row level security;

comment on table api_call_log is 'One row per Mistral API call (chat completion or embedding), for the owner-only /admin dashboard''s cost/usage metrics. Row Level Security is enabled with zero policies by design: this table is only ever accessed via the app''s service_role Supabase key, which bypasses RLS entirely -- matching every other table in this project. Do not add a policy.';
comment on column api_call_log.ingestion_queue_id is 'FK to the ingestion_queue row this call happened during, set from storage.CURRENT_API_CALL_CONTEXT (a contextvar main.ingest() sets for the duration of one ingestion, mirroring INTERACTIVE_REQUEST''s propagation through asyncio.to_thread). NULL for a call made outside main.ingest() -- a backfill/fix-up script (backfill_competitors.py, fix_*.py, etc.) importing extractor.py/competitor.py/embeddings.py directly.';
comment on column api_call_log.label is 'Free-text context for a call with no ingestion_queue_id (the url being ingested, set at CURRENT_API_CALL_CONTEXT creation time before the startup''s name is known) -- a convenience for reading raw rows, not used by the dashboard''s aggregation, which joins on ingestion_queue_id instead.';
comment on column api_call_log.call_type is 'Small, fixed vocabulary maintained in application code (storage.log_api_call''s callers), not a DB enum/check constraint -- extract_step1, extract_step2a, extract_step2b, extract_step2c, competitor_score, embedding. Same no-CHECK convention as quality_review_log.verdict and ingestion_queue.status (AD-4).';
comment on column api_call_log.item_count is 'Number of domain items this call processed, where that concept applies: candidates scored in this chunk (call_type=competitor_score) or texts embedded in this batch (call_type=embedding). NULL for the extract_* steps (always exactly one startup, not a meaningful count). Used by the dashboard to show "candidates sent to the competitor batch" per startup -- SUM(item_count) WHERE call_type=''competitor_score'' AND ingestion_queue_id=X.';
comment on column api_call_log.cost_usd is 'prompt_tokens/completion_tokens priced via pricing.MODEL_PRICING_PER_1M_TOKENS at log time (storage.log_api_call) -- NOT recomputed if Mistral''s pricing changes later, so this is a point-in-time estimate, not a source of truth for actual billing.';
