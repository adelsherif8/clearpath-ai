#!/usr/bin/env bash
# Backup/restore drill: seed -> backup -> restore into an empty DB -> verify rows AND security.
#
#   ADMIN_URL=postgresql://postgres:pw@localhost:5432/postgres scripts/restore_drill.sh
#
# Creates and drops two scratch databases (clearpath_drill_src, clearpath_drill_dst).
set -euo pipefail
: "${ADMIN_URL:?set ADMIN_URL to a superuser/owner URL on the drill server}"
here="$(cd "$(dirname "$0")" && pwd)"
PY="${PY:-python}"
url_for() { "$PY" -c "import sys,psycopg.conninfo as c; d=c.conninfo_to_dict(sys.argv[1]); d.update(dbname=sys.argv[2]); print(c.make_conninfo(**d))" "$ADMIN_URL" "$1"; }
SRC="$(url_for clearpath_drill_src)"; DST="$(url_for clearpath_drill_dst)"
psql "$ADMIN_URL" -qc "drop database if exists clearpath_drill_src" -c "drop database if exists clearpath_drill_dst" \
                  -c "create database clearpath_drill_src" -c "create database clearpath_drill_dst"

echo "== 1. migrate + seed source"
MIGRATE_DATABASE_URL="$SRC" APP_DB_PASSWORD="drill-only-pw" "$PY" "$here/migrate.py"
psql "$SRC" -v ON_ERROR_STOP=1 -q <<'SQL'
insert into clearpath.user_roles (user_id, role) values
  ('00000000-0000-0000-0000-0000000000a1', 'submitter'),
  ('00000000-0000-0000-0000-0000000000c1', 'reviewer'),
  ('00000000-0000-0000-0000-0000000000d1', 'admin');
insert into clearpath.cases (id, submitter_id, status, requested_procedure, plan, condition, age, sex, notes,
                             agent_decision, agent_confidence, agent_route)
select 'PA-' || upper(lpad(to_hex(g), 8, '0')), '00000000-0000-0000-0000-0000000000a1', 'pending_review',
       'MRI Lumbar Spine', 'Commercial', 'Low back pain', 40, 'F', 'synthetic drill row ' || g, 'deny', 0.9, 'needs_human'
  from generate_series(1, 25) g;
insert into clearpath.decisions (case_id, reviewer_id, action)
select id, '00000000-0000-0000-0000-0000000000c1', 'deny' from clearpath.cases limit 10;
insert into clearpath.audit_log (event, actor_role, outcome)
select 'drill_event', 'anonymous', 'ok' from generate_series(1, 40);
SQL
before="$(psql "$SRC" -Atc "select (select count(*) from clearpath.cases)||','||(select count(*) from clearpath.decisions)||','||(select count(*) from clearpath.audit_log)")"
echo "source rows (cases,decisions,audit): $before"

echo "== 2. backup"
out="$(mktemp -d)"
dump="$(BACKUP_DATABASE_URL="$SRC" OUT_DIR="$out" "$here/backup.sh" | tail -1)"

echo "== 3. restore into empty database"
"$here/restore.sh" "$dump" "$DST"
after="$(psql "$DST" -Atc "select (select count(*) from clearpath.cases)||','||(select count(*) from clearpath.decisions)||','||(select count(*) from clearpath.audit_log)")"
echo "restored rows (cases,decisions,audit): $after"
[[ "$before" == "$after" ]] || { echo "ROW COUNT MISMATCH"; exit 1; }

echo "== 4. security survived the restore"
MIGRATE_DATABASE_URL="$DST" APP_DB_PASSWORD="drill-only-pw" "$PY" "$here/migrate.py" >/dev/null   # idempotent re-apply + login
APP_DST="$("$PY" -c "import sys,psycopg.conninfo as c; d=c.conninfo_to_dict(sys.argv[1]); d.update(user='clearpath_app', password='drill-only-pw'); print(c.make_conninfo(**d))" "$DST")"
seen="$(psql "$APP_DST" -qAt -c "begin" -c "select set_config('app.user_id','00000000-0000-0000-0000-0000000000ff',true)" -c "select count(*) from clearpath.cases" -c "commit" | tail -1)"
[[ "$seen" == "0" ]] || { echo "RLS FAILED after restore: stranger sees $seen cases"; exit 1; }
if psql "$APP_DST" -qc "update clearpath.audit_log set outcome='denied'" 2>/dev/null; then echo "AUDIT LOG WRITABLE after restore"; exit 1; fi
if psql "$DST" -qc "delete from clearpath.audit_log" 2>/dev/null; then echo "AUDIT TRIGGER MISSING after restore"; exit 1; fi
echo "RLS hides rows from a stranger: ok; audit log append-only for app and owner: ok"

psql "$ADMIN_URL" -qc "drop database clearpath_drill_src" -c "drop database clearpath_drill_dst"
rm -rf "$out"
echo "DRILL PASSED"
