-- Bug: two startups sharing a display name (e.g. "Corma" at corma.io and
-- corma.ai) collide in storage.save_startup()'s name-based fallback lookup,
-- silently overwriting one with the other's data. Same root problem
-- migrations/007 already solved for ingestion_queue -- normalize_domain() is
-- the one canonical identity key (AD-8), name never is.
--
-- Nullable, NO backfill in this file: normalize_domain() is Python logic
-- (protocol-relative URLs, userinfo, IPv6 literals, trailing dots -- see
-- storage.py, covered by test_storage.py) not worth reimplementing in SQL.
-- Run backfill_compspro_domain.py AFTER this migration, BEFORE
-- migrations/014 (the unique index) -- compspro is NOT empty (unlike
-- ingestion_queue was for migration 007), so NOT NULL here would fail
-- outright, and the unique index would fail on the very collision this fix
-- targets until backfill_compspro_domain.py's report is clean.
alter table compspro add column domain text;

comment on column compspro.domain is
  'storage.normalize_domain(website) -- the identity key for compspro (never `name`, which two unrelated startups can share). Backfilled by backfill_compspro_domain.py; written going forward by storage.save_startup(). See migrations/014 for the uniqueness constraint.';
