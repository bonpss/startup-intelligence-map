-- Code review finding on story 6-2-en-attente-tab-with-per-row-status-badge
-- (2026-08-29): storage.enqueue_ingestion()'s SELECT-then-INSERT dedup check
-- (added in 004/story 6.1's code review) is an unlocked check-then-act race --
-- two concurrent POST /api/ingest calls for the same URL can both pass the
-- SELECT before either INSERT commits, producing two active rows and running
-- the ingestion pipeline twice.
--
-- A unique partial index on (url) restricted to the active statuses closes
-- the race at the database level: a second concurrent INSERT for the same URL
-- now fails atomically with a unique_violation (23505) instead of silently
-- succeeding, and storage.enqueue_ingestion() catches that error and reuses
-- the row the other request just inserted. Partial (not a full unique
-- constraint on url) because the same URL is expected to recur once a prior
-- attempt has reached 'done'/'error'.

create unique index ingestion_queue_active_url_uidx
  on ingestion_queue (url)
  where status in ('queued', 'processing');

comment on index ingestion_queue_active_url_uidx is
  'Enforces at most one queued/processing row per url -- closes the enqueue_ingestion race flagged in story 6.2''s code review. Does not restrict done/error rows, which may legitimately recur for the same url.';
