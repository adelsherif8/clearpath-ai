-- ClearPath schema. Applied by scripts/migrate.py as the database owner
-- (local: a superuser; Supabase: the `postgres` role). The app never runs this.
--
-- Everything lives in its own schema, `clearpath`, NOT in `public`: on Supabase the
-- `public` schema is exposed through the Data API (PostgREST) to the `anon` and
-- `authenticated` roles, and default privileges grant them access to new tables.
-- A private schema with no grants to those roles closes that path entirely.

create schema if not exists clearpath;
revoke all on schema clearpath from public;

create table if not exists clearpath.user_roles (
  user_id    uuid primary key,                     -- Supabase auth.users.id (JWT `sub`)
  role       text not null default 'submitter'
             check (role in ('submitter', 'reviewer', 'admin')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists clearpath.cases (
  id                   text primary key check (id ~ '^PA-[0-9A-F]{8}$'),
  submitter_id         uuid not null,
  assigned_reviewer_id uuid,
  status               text not null
                       check (status in ('pending_review', 'auto_approved', 'approved', 'denied')),
  requested_procedure  text not null check (length(requested_procedure) <= 120),
  plan                 text not null check (plan in ('Commercial', 'Medicare Advantage', 'Medicaid')),
  condition            text not null check (length(condition) <= 200),
  age                  smallint check (age between 0 and 120),
  sex                  text not null check (sex in ('F', 'M', 'U')),
  notes                text not null check (length(notes) <= 4000),
  agent_decision       text not null check (agent_decision in ('approve', 'deny', 'undetermined')),
  agent_confidence     real not null check (agent_confidence between 0 and 1),
  agent_route          text not null check (agent_route in ('auto_approved', 'needs_human')),
  agent_result         jsonb not null default '{}'::jsonb,
  attempt              smallint not null default 1 check (attempt between 1 and 10),
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);
create index if not exists cases_submitter_idx on clearpath.cases (submitter_id);
create index if not exists cases_reviewer_idx  on clearpath.cases (assigned_reviewer_id);

create table if not exists clearpath.decisions (
  id          bigint generated always as identity primary key,
  case_id     text not null references clearpath.cases (id),
  reviewer_id uuid not null,
  action      text not null check (action in ('approve', 'deny')),
  notes       text not null default '' check (length(notes) <= 2000),
  created_at  timestamptz not null default now()
);
create index if not exists decisions_case_idx on clearpath.decisions (case_id);

create table if not exists clearpath.audit_log (
  id         bigint generated always as identity primary key,
  ts         timestamptz not null default now(),
  event      text not null check (length(event) <= 64),
  actor_id   uuid,
  actor_role text not null check (length(actor_role) <= 16),
  ip         text check (length(ip) <= 64),
  target     text check (length(target) <= 64),
  outcome    text not null check (outcome in ('ok', 'denied', 'error')),
  meta       jsonb not null default '{}'::jsonb
);
create index if not exists audit_ts_idx on clearpath.audit_log (ts desc);

create table if not exists clearpath.llm_usage (
  day   date primary key,
  calls integer not null default 0 check (calls >= 0)
);
