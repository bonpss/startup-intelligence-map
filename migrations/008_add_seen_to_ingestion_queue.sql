-- Story 6-4-two-dot-notification-on-the-tab: the "En attente" tab needs to
-- distinguish done rows Julien has already looked at from ones he hasn't, so
-- the tab can show an unseen-completions count separate from the (always
-- visible) failure count. Numbered 008, not epics.md's original "005" --
-- that number was already taken by the time this story was actually scoped
-- (005 harden-url-check, 006 url-uniqueness [superseded], 007 domain dedup).

alter table ingestion_queue add column seen boolean not null default false;

comment on column ingestion_queue.seen is
  'Whether a done row has been shown in the "En attente" tab yet -- marked true in bulk when the tab opens (storage.mark_done_rows_seen()), not on a per-row basis. Irrelevant for queued/processing/error rows: an error row always counts toward the tab''s red badge regardless of seen.';
