-- Async ingestion queue, per Epic 6 / Story 6.1 (Async queue + status persistence).
-- /api/ingest enqueues a row here and returns immediately; an in-process
-- asyncio.Queue worker (concurrency=1) consumes it in the background and writes
-- status transitions here as they happen -- this table is the source of truth,
-- the in-memory queue is just a low-latency trigger and is disposable.

create table ingestion_queue (
  id bigint generated always as identity primary key,
  url text not null check (url <> ''),
  status text not null default 'queued',
  result jsonb,
  error_message text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table ingestion_queue enable row level security;

comment on table ingestion_queue is 'Async ingestion queue for the home-page "add startup" flow (Epic 6). Row Level Security is enabled with zero policies by design: this table is only ever accessed via the app''s service_role Supabase key, which bypasses RLS entirely -- matching every other table in this project. Do not add a policy.';
comment on column ingestion_queue.status is 'Small, fixed vocabulary maintained in application code (storage.py), not a DB enum/check constraint -- queued, processing, done, error. No CHECK here, consistent with quality_review_log.verdict (AD-4).';
comment on column ingestion_queue.result is 'Set on status=done: the {"name","action","competitors_found"} dict main.ingest() returns.';
comment on column ingestion_queue.error_message is 'Set on status=error: the exception message main.ingest() raised (ValueError''s message, or str(e) for anything else).';
comment on column ingestion_queue.updated_at is 'Set explicitly by application code (storage.py) on every status transition -- no DB trigger, consistent with quality_review_log.updated_at (migration 002) and this project having no ORM/trigger tooling elsewhere.';
