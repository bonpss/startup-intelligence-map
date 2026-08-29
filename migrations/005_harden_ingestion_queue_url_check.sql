-- Harden ingestion_queue.url's blank check, per code-review findings on story
-- 6-1-async-queue-status-persistence (2026-08-28). The original `url <> ''`
-- check (migration 004) is the exact weaker pattern migration 002 replaced for
-- quality_review_log -- a whitespace-only url ("   ") would pass it.

alter table ingestion_queue drop constraint ingestion_queue_url_check;

alter table ingestion_queue
  add constraint ingestion_queue_url_not_blank check (length(trim(url)) > 0);
