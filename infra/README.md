# ClearPath cloud setup (run once, in order)

Nothing in this folder runs automatically. Each script creates cloud resources, so each is run
by hand after review. All of it stays inside free tiers: Secret Manager (6 active versions and
10k accesses/month free), Cloud Storage (5 GB-months in us-central1), Cloud Logging/Monitoring
(log-based metrics and alert policies are free), Supabase free plan.

| Step | Script | Creates |
|---|---|---|
| 1 | Supabase dashboard (manual, see below) | Supabase project, users, Auth settings |
| 2 | `python scripts/migrate.py` | `clearpath` schema, RLS policies, `clearpath_app` role |
| 3 | `infra/secrets.sh` | Secret Manager secrets + `clearpath-run` runtime service account |
| 4 | `infra/deploy.sh` | New Cloud Run revision using the runtime SA and mounted secrets |
| 5 | `infra/backup_bucket.sh` | Private GCS bucket, keyless GitHub→GCP auth for the backup job |
| 6 | `infra/alerts.sh` | Log-based metric + alert for repeated auth failures |
| 7 | `infra/verify.sh` | Read-only check of everything above |

## 1. Supabase (free plan)
1. supabase.com → New project → name `clearpath`, region closest to us-central1 (e.g. East US), strong DB password (save it in a password manager; it becomes the `MIGRATE_DATABASE_URL` / backup credential).
2. Authentication → Sign In / Providers → Email: **turn off "Allow new users to sign up"** (internal app: accounts are invited, not self-created). Keep "Confirm email" on.
3. Authentication → Users → Add user → create one account per role you want to demo (e.g. `submitter@…`, `reviewer@…`, `admin@…`).
4. Project Settings → API: copy the Project URL (`SUPABASE_URL`) and the `anon` public key (`SUPABASE_ANON_KEY`). Do **not** copy or use the `service_role` key anywhere: the app never needs it.
5. Project Settings → JWT Keys: if the project uses asymmetric signing keys (default for new projects) nothing else is needed (the app reads the JWKS). Only for a legacy HS256 project copy the JWT secret into Secret Manager as `supabase-jwt-secret`.
6. Project Settings → Database → Connection string → "Transaction pooler" (port 6543). The owner URL uses user `postgres.<ref>`; the app URL is the same host with user `clearpath_app.<ref>` and the password you set via `APP_DB_PASSWORD`.

## 2. Schema and the app role
```bash
MIGRATE_DATABASE_URL='postgresql://postgres.<ref>:<owner-pw>@<pooler-host>:5432/postgres' \
APP_DB_PASSWORD='<new long random password>' python scripts/migrate.py
```
Then set roles (users start as `submitter` on first login): as the owner in the Supabase SQL editor,
`update clearpath.user_roles set role = 'reviewer' where user_id = '<uuid>';` (and one `admin`).
After that, admins change roles through `POST /admin/users/{id}/role`.

Use port 5432 (session pooler) for migrations and backups, port 6543 (transaction pooler) for the app.
