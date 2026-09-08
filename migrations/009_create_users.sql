-- Capped-signup authentication for the public demo (spec-public-demo-auth.md).
-- Server-side sessions are a signed httponly cookie (Starlette SessionMiddleware)
-- carrying {id, email, is_owner} directly -- this table exists to authenticate
-- at login/signup time and to back the MAX_USERS non-owner signup cap
-- (storage.count_non_owner_users()), not to back a session lookup on every
-- request. No session table is introduced.

create table users (
  id bigint generated always as identity primary key,
  email text unique not null,
  password_hash text not null,
  is_owner boolean not null default false,
  created_at timestamptz not null default now()
);

alter table users enable row level security;

comment on table users is 'Email/password accounts for the public demo (spec-public-demo-auth.md). Row Level Security is enabled with zero policies by design: this table is only ever accessed via the app''s service_role Supabase key, which bypasses RLS entirely -- matching every other table in this project. Do not add a policy.';
comment on column users.email is 'Unique account identifier, matched case-insensitively against OWNER_EMAIL at signup by application code (auth.py) -- the column itself has no case-folding, callers normalize before reading/writing.';
comment on column users.password_hash is 'bcrypt hash only (auth.py) -- the plaintext password is never persisted, logged, or returned in any response.';
comment on column users.is_owner is 'True only for the single account seeded out-of-band via seed_owner.py (never set via POST /api/signup, which rejects any email matching OWNER_EMAIL -- see spec-public-demo-auth.md Spec Change Log, iteration 1). Exempt from the MAX_USERS signup cap (storage.count_non_owner_users() excludes it) and from the /graph, /api/graph/all block enforced by graph_app.py''s auth-gating middleware.';
comment on column users.created_at is 'Set once at signup; no update trigger, consistent with this project having no ORM/trigger tooling elsewhere.';
