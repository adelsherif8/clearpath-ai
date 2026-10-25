-- Row-level security, append-only audit log, and the least-privilege app role.
--
-- Identity model: the app verifies the Supabase JWT, then opens a transaction and sets
-- `app.user_id` (transaction-local, so it is safe behind the Supabase transaction
-- pooler). The database decides everything else from that id: the caller's role comes
-- from clearpath.user_roles, and every row is filtered by the policies below. A bug in
-- an app query therefore cannot return another user's case or rewrite the audit log.

-- ── helpers ────────────────────────────────────────────────────────────────
create or replace function clearpath.app_uid() returns uuid
language sql stable
set search_path = ''
as $$ select nullif(current_setting('app.user_id', true), '')::uuid $$;

create or replace function clearpath.app_role() returns text
language sql stable
set search_path = ''
as $$
  select coalesce(
    (select r.role from clearpath.user_roles r where r.user_id = clearpath.app_uid()),
    'anonymous')
$$;

-- ── the app role: no superuser, no RLS bypass, no DDL ──────────────────────
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'clearpath_app') then
    create role clearpath_app nologin nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
  end if;
end $$;
-- Verify (rather than ALTER, which needs superuser on managed Postgres such as Supabase)
-- that the role can never bypass RLS or escalate.
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'clearpath_app'
             and (rolsuper or rolbypassrls or rolcreatedb or rolcreaterole)) then
    raise exception 'clearpath_app has unsafe attributes; recreate it as NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE';
  end if;
end $$;

-- Supabase: make sure the Data API roles can never reach this schema.
do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on schema clearpath from %I', r);
      execute format('revoke all on all tables in schema clearpath from %I', r);
      execute format('revoke all on all functions in schema clearpath from %I', r);
    end if;
  end loop;
end $$;

revoke all on all tables    in schema clearpath from public;
revoke all on all functions in schema clearpath from public;
revoke all on all tables    in schema clearpath from clearpath_app;

grant usage on schema clearpath to clearpath_app;
grant execute on function clearpath.app_uid(), clearpath.app_role() to clearpath_app;

-- Table privileges: the minimum each code path needs. No DELETE anywhere, no
-- UPDATE on decisions or audit_log, no TRUNCATE, no REFERENCES, no TRIGGER.
grant select, insert                          on clearpath.user_roles to clearpath_app;
grant update (role, updated_at)               on clearpath.user_roles to clearpath_app;
grant select, insert                          on clearpath.cases      to clearpath_app;
grant update (status, assigned_reviewer_id, agent_decision, agent_confidence, agent_route,
              agent_result, attempt, updated_at)
                                              on clearpath.cases      to clearpath_app;
grant select, insert                          on clearpath.decisions  to clearpath_app;
grant select, insert                          on clearpath.audit_log  to clearpath_app;
grant select, insert, update (calls)          on clearpath.llm_usage  to clearpath_app;

-- ── RLS: enabled and forced on every table ─────────────────────────────────
alter table clearpath.user_roles enable row level security;
alter table clearpath.cases      enable row level security;
alter table clearpath.decisions  enable row level security;
alter table clearpath.audit_log  enable row level security;
alter table clearpath.llm_usage  enable row level security;
alter table clearpath.user_roles force row level security;
alter table clearpath.cases      force row level security;
alter table clearpath.decisions  force row level security;
alter table clearpath.audit_log  force row level security;
alter table clearpath.llm_usage  force row level security;

drop policy if exists user_roles_select on clearpath.user_roles;
drop policy if exists user_roles_self_register on clearpath.user_roles;
drop policy if exists user_roles_admin_update on clearpath.user_roles;
-- Role assignments (uuid -> role) are readable by the app; they hold no PHI.
create policy user_roles_select on clearpath.user_roles
  for select to clearpath_app using (true);
-- A signed-in user may register themself, and only as a submitter.
create policy user_roles_self_register on clearpath.user_roles
  for insert to clearpath_app
  with check (user_id = (select clearpath.app_uid()) and role = 'submitter');
-- Only an admin can change a role, and not their own (no self-demotion lockout / self-escalation games).
create policy user_roles_admin_update on clearpath.user_roles
  for update to clearpath_app
  using ((select clearpath.app_role()) = 'admin' and user_id <> (select clearpath.app_uid()))
  with check ((select clearpath.app_role()) = 'admin');

drop policy if exists cases_select on clearpath.cases;
drop policy if exists cases_insert on clearpath.cases;
drop policy if exists cases_update on clearpath.cases;
-- Submitters see their own cases; reviewers see cases assigned to them; admins see all.
create policy cases_select on clearpath.cases
  for select to clearpath_app
  using (
    submitter_id = (select clearpath.app_uid())
    or (assigned_reviewer_id = (select clearpath.app_uid())
        and (select clearpath.app_role()) = 'reviewer')
    or (select clearpath.app_role()) = 'admin'
  );
-- Submitters (and admins) create cases, only in their own name, never pre-finalized.
create policy cases_insert on clearpath.cases
  for insert to clearpath_app
  with check (
    submitter_id = (select clearpath.app_uid())
    and (select clearpath.app_role()) in ('submitter', 'admin')
    and status in ('pending_review', 'auto_approved')
  );
-- Only the assigned reviewer or an admin may touch a case after creation.
create policy cases_update on clearpath.cases
  for update to clearpath_app
  using (
    (assigned_reviewer_id = (select clearpath.app_uid()) and (select clearpath.app_role()) = 'reviewer')
    or (select clearpath.app_role()) = 'admin'
  )
  with check (
    (assigned_reviewer_id = (select clearpath.app_uid()) and (select clearpath.app_role()) = 'reviewer')
    or (select clearpath.app_role()) = 'admin'
  );

drop policy if exists decisions_select on clearpath.decisions;
drop policy if exists decisions_insert on clearpath.decisions;
create policy decisions_select on clearpath.decisions
  for select to clearpath_app
  using (
    reviewer_id = (select clearpath.app_uid())
    or (select clearpath.app_role()) = 'admin'
    or exists (select 1 from clearpath.cases c
               where c.id = case_id and c.submitter_id = (select clearpath.app_uid()))
  );
-- A decision can only be recorded by a reviewer, in their own name, on a case assigned to them.
create policy decisions_insert on clearpath.decisions
  for insert to clearpath_app
  with check (
    reviewer_id = (select clearpath.app_uid())
    and (select clearpath.app_role()) = 'reviewer'
    and exists (select 1 from clearpath.cases c
                where c.id = case_id and c.assigned_reviewer_id = (select clearpath.app_uid()))
  );

drop policy if exists audit_insert on clearpath.audit_log;
drop policy if exists audit_select on clearpath.audit_log;
-- Anyone may append, but only in their own name (or anonymously).
create policy audit_insert on clearpath.audit_log
  for insert to clearpath_app
  with check (actor_id is not distinct from (select clearpath.app_uid()));
create policy audit_select on clearpath.audit_log
  for select to clearpath_app
  using ((select clearpath.app_role()) = 'admin');

drop policy if exists llm_usage_all on clearpath.llm_usage;
-- Global spend counter: no per-user data; the app maintains it.
create policy llm_usage_all on clearpath.llm_usage
  for all to clearpath_app using (true) with check (true);

-- ── triggers: rules that must hold even for the table owner ────────────────
create or replace function clearpath.forbid_change() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception 'clearpath.% is append-only', tg_table_name using errcode = '42501';
end $$;

drop trigger if exists audit_log_append_only on clearpath.audit_log;
create trigger audit_log_append_only
  before update or delete on clearpath.audit_log
  for each row execute function clearpath.forbid_change();
drop trigger if exists audit_log_no_truncate on clearpath.audit_log;
create trigger audit_log_no_truncate
  before truncate on clearpath.audit_log
  for each statement execute function clearpath.forbid_change();
drop trigger if exists decisions_append_only on clearpath.decisions;
create trigger decisions_append_only
  before update or delete on clearpath.decisions
  for each row execute function clearpath.forbid_change();

-- Case lifecycle: finalized cases are immutable; only the assigned reviewer can
-- finalize; nobody can finalize a case the agent did not route to a human.
create or replace function clearpath.guard_case_update() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if old.status in ('approved', 'denied') then
    raise exception 'case % is already finalized', old.id using errcode = '42501';
  end if;
  if new.status is distinct from old.status then
    -- After creation the only legal transitions are pending_review -> approved | denied,
    -- made by the assigned reviewer. In particular nothing (agent re-run included) can
    -- move a case to auto_approved after a human was asked to look at it.
    if new.status not in ('approved', 'denied') or old.status <> 'pending_review' then
      raise exception 'illegal status transition % -> %', old.status, new.status using errcode = '42501';
    end if;
    if clearpath.app_role() <> 'reviewer' or new.assigned_reviewer_id is distinct from clearpath.app_uid() then
      raise exception 'only the assigned reviewer can finalize a case' using errcode = '42501';
    end if;
  end if;
  if new.assigned_reviewer_id is distinct from old.assigned_reviewer_id then
    if not exists (select 1 from clearpath.user_roles r
                   where r.user_id = new.assigned_reviewer_id and r.role = 'reviewer') then
      raise exception 'cases can only be assigned to a reviewer' using errcode = '42501';
    end if;
  end if;
  new.updated_at := now();
  return new;
end $$;

drop trigger if exists cases_guard_update on clearpath.cases;
create trigger cases_guard_update
  before update on clearpath.cases
  for each row execute function clearpath.guard_case_update();

-- Escalated cases are assigned to a reviewer at insert time, so the reviewer
-- queue is just "cases assigned to me" and RLS stays simple.
create or replace function clearpath.assign_reviewer() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.status = 'pending_review' and new.assigned_reviewer_id is null then
    select r.user_id into new.assigned_reviewer_id
      from clearpath.user_roles r
     where r.role = 'reviewer'
     order by random()
     limit 1;
  end if;
  return new;
end $$;

drop trigger if exists cases_assign_reviewer on clearpath.cases;
create trigger cases_assign_reviewer
  before insert on clearpath.cases
  for each row execute function clearpath.assign_reviewer();

revoke all on function clearpath.forbid_change(), clearpath.guard_case_update(),
                       clearpath.assign_reviewer() from public;
