-- Code review finding (2026-08-29, story 6-3-manual-retry-on-failure):
-- enqueue_ingestion()'s dedup (and migrations/006's unique index) matched on
-- the raw url string, not the normalized domain -- "acme.com",
-- "https://acme.com/", and "http://acme.com" all produced distinct rows,
-- silently defeating the dedup mechanism's stated purpose (running the full
-- scrape/extract/competitor-score pipeline once per equivalent URL, not once
-- per exact string). storage.normalize_domain() is already the project's one
-- canonical place for this (AD-8) -- ingestion_queue now stores it alongside
-- the raw url so both the dedup check and the unique index can key on it.
-- The raw url is preserved unchanged: main.ingest() scrapes the full url,
-- path included, so the dedup key and the scrape target must stay separate
-- columns, not collapse into one.

-- Code review (2026-08-29, story 6-4-two-dot-notification-on-the-tab): this
-- ALTER has no DEFAULT, so it only succeeds against an empty table -- Postgres
-- can't backfill a NOT NULL column with no default against pre-existing rows.
-- Confirmed empty (`select count(*) from ingestion_queue` returned 0) before
-- this migration was applied live; not safe to replay as-is against a table
-- that already holds rows (a from-scratch environment would need a backfill
-- step first, e.g. populate domain from url, then add the NOT NULL constraint).
alter table ingestion_queue add column domain text not null;

comment on column ingestion_queue.domain is
  'storage.normalize_domain(url), computed at insert time by enqueue_ingestion() -- the dedup key. The raw url is stored separately and unchanged since main.ingest() scrapes the full url, path included.';

drop index ingestion_queue_active_url_uidx;

create unique index ingestion_queue_active_domain_uidx
  on ingestion_queue (domain)
  where status in ('queued', 'processing');

comment on index ingestion_queue_active_domain_uidx is
  'Enforces at most one queued/processing row per normalized domain -- supersedes migrations/006''s url-keyed index, which missed equivalent urls with different schemes/paths/trailing slashes for the same site.';
