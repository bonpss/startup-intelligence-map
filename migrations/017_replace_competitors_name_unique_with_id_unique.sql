-- Discovered live (2026-09-09), second hidden name-based constraint in this
-- same fix: competitors has a UNIQUE(company_a, company_b) constraint on the
-- TEXT label columns. Two startups sharing a display name (e.g. "Corma" at
-- corma.io and corma.ai) both saving a relationship to the same third company
-- (e.g. "Torq") collide on the literal pair ('Corma', 'Torq') even though
-- they're two different compspro rows -- confirmed live:
--   duplicate key value violates unique constraint "competitors_company_a_company_b_key"
--   Key (company_a, company_b)=(Corma, Torq) already exists.
--
-- Replaces it with the id-based equivalent -- company_a_id/company_b_id
-- (migrations/015) are the real relational identity now; company_a/company_b
-- text are pure display labels and must no longer carry a uniqueness
-- constraint. Nullable columns in a unique constraint is fine in Postgres
-- (NULLs are never considered equal to each other), so the handful of rows
-- migrations/015 left with company_a_id/company_b_id = NULL (ambiguous name
-- at backfill time) aren't blocked by this.
alter table competitors drop constraint competitors_company_a_company_b_key;

alter table competitors add constraint competitors_company_a_id_company_b_id_key
  unique (company_a_id, company_b_id);

comment on constraint competitors_company_a_id_company_b_id_key on competitors is
  'Replaces the old name-text-based competitors_company_a_company_b_key -- storage.relationship_exists()/save_relationships() already enforce this same pair-uniqueness at the app level by id, this is the DB-level backstop.';
