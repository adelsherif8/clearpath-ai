"""Apply db/migrations/*.sql in order, then give the app role its login password.

    MIGRATE_DATABASE_URL=postgresql://postgres:...@host:5432/postgres \
    APP_DB_PASSWORD=... python scripts/migrate.py

MIGRATE_DATABASE_URL is the owner connection (local superuser / Supabase `postgres`).
It is used only here and by the backup job, never by the running app. The app gets
DATABASE_URL, which logs in as `clearpath_app`.
"""
import glob
import os
import sys

import psycopg
from psycopg import sql

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    url = os.environ.get("MIGRATE_DATABASE_URL")
    if not url:
        print("MIGRATE_DATABASE_URL is required", file=sys.stderr)
        return 2
    files = sorted(glob.glob(os.path.join(HERE, "..", "db", "migrations", "*.sql")))
    with psycopg.connect(url, autocommit=True) as conn:
        for path in files:
            with open(path) as f:
                conn.execute(f.read())
            print(f"applied {os.path.basename(path)}")
        password = os.environ.get("APP_DB_PASSWORD")
        if password:
            conn.execute(sql.SQL("alter role clearpath_app login password {}").format(sql.Literal(password)))
            print("clearpath_app: login enabled")
        else:
            print("APP_DB_PASSWORD not set: clearpath_app left as-is")
    return 0


if __name__ == "__main__":
    sys.exit(main())
