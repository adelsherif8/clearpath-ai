-- Post-restore checks: the security model came back, not just the rows.
\set ON_ERROR_STOP on
do $$
declare n int;
begin
  select count(*) into n from pg_class
   where relnamespace = 'clearpath'::regnamespace and relkind = 'r'
     and relrowsecurity and relforcerowsecurity;
  if n <> 5 then raise exception 'expected 5 tables with forced RLS, found %', n; end if;

  select count(*) into n from pg_policies where schemaname = 'clearpath';
  if n < 11 then raise exception 'expected at least 11 RLS policies, found %', n; end if;

  select count(*) into n from pg_trigger t join pg_class c on c.oid = t.tgrelid
   where c.relnamespace = 'clearpath'::regnamespace and not t.tgisinternal;
  if n < 5 then raise exception 'expected at least 5 guard triggers, found %', n; end if;

  if has_table_privilege('clearpath_app', 'clearpath.audit_log', 'UPDATE')
     or has_table_privilege('clearpath_app', 'clearpath.audit_log', 'DELETE') then
    raise exception 'clearpath_app must not be able to modify the audit log';
  end if;
end $$;
select 'cases' as tbl, count(*) from clearpath.cases
union all select 'decisions', count(*) from clearpath.decisions
union all select 'audit_log', count(*) from clearpath.audit_log
union all select 'user_roles', count(*) from clearpath.user_roles
order by 1;
