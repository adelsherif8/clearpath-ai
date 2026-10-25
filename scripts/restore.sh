#!/usr/bin/env bash
# Restore a clearpath dump into a target database and verify it.
#
#   scripts/restore.sh <dump-file | gs://.../clearpath-....dump> <target-owner-url>
#
# The target should be an EMPTY database (a scratch DB for a drill, or a new Supabase project
# for disaster recovery). The app role `clearpath_app` is created (NOLOGIN) if missing; give it
# a password afterwards with `APP_DB_PASSWORD=... python scripts/migrate.py`.
set -euo pipefail
src="${1:?dump file or gs:// URL}"
target="${2:?target owner database URL}"
PG_RESTORE="${PG_RESTORE:-pg_restore}"
PSQL="${PSQL:-psql}"
here="$(cd "$(dirname "$0")" && pwd)"

if [[ "$src" == gs://* ]]; then
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  gcloud storage cp "$src" "$src.sha256" "$tmp/"
  src="$tmp/$(basename "$src")"
  (cd "$tmp" && (sha256sum -c "$(basename "$src").sha256" 2>/dev/null || shasum -a 256 -c "$(basename "$src").sha256"))
fi

$PSQL "$target" -v ON_ERROR_STOP=1 -q <<'SQL'
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'clearpath_app') then
    create role clearpath_app nologin nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
  end if;
end $$;
SQL

$PG_RESTORE --dbname="$target" --exit-on-error --no-owner --single-transaction "$src"
echo "restored $src"
$PSQL "$target" -v ON_ERROR_STOP=1 -f "$here/verify_restore.sql"
