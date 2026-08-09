-- Schema hardening + audit column, per code-review findings on story
-- 1-1-create-the-quality-review-log-table-via-migration (2026-08-10).

alter table quality_review_log alter column id set generated always;

alter table quality_review_log
  add constraint quality_review_log_review_type_not_blank check (length(trim(review_type)) > 0),
  add constraint quality_review_log_subject_not_blank check (length(trim(subject)) > 0),
  add constraint quality_review_log_source_snapshot_is_object check (source_snapshot is null or jsonb_typeof(source_snapshot) = 'object');

alter table quality_review_log add column updated_at timestamptz;

comment on table quality_review_log is 'Generic, persistent record of every quality-loop review decision (taxonomy fracture diagnosis, scraping-heterogeneity characterization, and future review types). Row Level Security is enabled with zero policies by design: this table is only ever accessed via the app''s service_role Supabase key, which bypasses RLS entirely -- matching the compspro/competitors pattern. Do not add a policy.';
comment on column quality_review_log.review_type is 'Small, fixed vocabulary maintained in application code (storage.py), not a DB enum -- e.g. taxonomy_split, scraping_diagnostic.';
comment on column quality_review_log.subject is 'Format defined per review_type: exact TAXONOMY key for taxonomy_split, normalized site domain for scraping_diagnostic.';
comment on column quality_review_log.verdict is 'Free text by design (AD-4) -- taxonomy_split has a closed 4-value vocabulary enforced in application code; scraping_diagnostic''s vocabulary is still open/evolving. Do not add a CHECK/enum here.';
comment on column quality_review_log.updated_at is 'Set explicitly by application code (storage.py) when resolution/verdict changes after initial write -- no DB trigger, consistent with this project having no ORM/trigger tooling elsewhere.';
