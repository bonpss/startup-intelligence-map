-- competitors keys company_a/company_b by NAME TEXT -- the same collision
-- hazard one level removed. Adds FK id columns; storage.py now writes both
-- id (the lookup key) and name (kept only as a human-readable label).
--
-- compspro.id is uuid (not bigint, despite every other table in this repo's
-- migrations using a bigint identity pk) -- confirmed live: a first version
-- of this migration using bigint failed with "Key columns company_a_id and
-- id are of incompatible types: bigint and uuid".
alter table competitors add column company_a_id uuid references compspro(id);
alter table competitors add column company_b_id uuid references compspro(id);

create index competitors_company_a_id_idx on competitors (company_a_id);
create index competitors_company_b_id_idx on competitors (company_b_id);

comment on column competitors.company_a_id is
  'compspro.id -- the relational identity for this row going forward. company_a (text) is kept only as a human-readable label, no longer read for lookups.';
comment on column competitors.company_b_id is 'compspro.id -- see company_a_id.';

-- 1) Sanity check FIRST -- inspect the output before running the UPDATEs:
--   select name from compspro group by name having count(*) > 1;

-- 2) Backfill: skips (leaves NULL) any name that's still ambiguous in
--    compspro, rather than letting UPDATE...FROM silently pick one of
--    several matching rows. Self-healing: once compspro's collision is
--    resolved (migration 014's prerequisite), re-running this UPDATE
--    (idempotent -- WHERE ... company_a_id is null) picks it up automatically.
update competitors c
set company_a_id = p.id
from compspro p
where p.name = c.company_a
  and c.company_a_id is null
  and (select count(*) from compspro p2 where p2.name = c.company_a) = 1;

update competitors c
set company_b_id = p.id
from compspro p
where p.name = c.company_b
  and c.company_b_id is null
  and (select count(*) from compspro p2 where p2.name = c.company_b) = 1;
