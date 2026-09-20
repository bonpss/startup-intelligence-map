-- Track who added each startup, ahead of the public demo going live (currently
-- every row comes from Julien via the owner account, but any capped-signup
-- user will soon be able to trigger an ingestion too).

alter table compspro add column added_by_user_id bigint references users(id) on delete set null;
alter table ingestion_queue add column requested_by_user_id bigint references users(id) on delete set null;

comment on column compspro.added_by_user_id is 'users.id of the account that triggered this startup''s ingestion (main.py''s _ingest_sync, via storage.save_startup''s added_by_user_id param) -- set only on first insert, never overwritten by a later re-ingestion/update of an existing row, so attribution stays with whoever first added it. NULL for rows created before this column existed (backfilled below to the owner account) and for any startup added by a CLI script with no logged-in user.';
comment on column ingestion_queue.requested_by_user_id is 'users.id of the account that submitted this URL via POST /api/ingest (graph_app.py), captured at enqueue time (storage.enqueue_ingestion) and threaded through the worker to compspro.added_by_user_id when the ingestion completes. NULL for rows re-enqueued by the startup-recovery sweep after a restart, or submitted by a CLI run.';

-- Every startup ingested before this column existed came from Julien via the
-- owner account (spec-public-demo-auth.md, seeded by seed_owner.py). If no
-- owner account has been seeded yet in this environment, this is a no-op and
-- added_by_user_id stays NULL -- fine, since main.ingest() never had a user
-- to attribute to at the time these rows were created either.
update compspro
set added_by_user_id = (select id from users where is_owner = true order by id limit 1)
where added_by_user_id is null;
