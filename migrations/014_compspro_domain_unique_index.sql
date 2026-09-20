-- Run ONLY after backfill_compspro_domain.py reports ZERO remaining
-- collisions. Partial + nullable (not NOT NULL): a few legacy rows may have
-- no website and are allowed domain = NULL indefinitely.
create unique index compspro_domain_uidx
  on compspro (domain)
  where domain is not null;

comment on index compspro_domain_uidx is
  'Enforces at most one compspro row per normalized domain -- the fix for two same-name startups silently overwriting each other via the old name-keyed fallback in save_startup(). Mirrors migrations/007''s ingestion_queue_active_domain_uidx.';
