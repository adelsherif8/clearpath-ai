#!/usr/bin/env bash
# Create the runtime service account and the Secret Manager secrets, and grant the SA access
# to exactly these secrets (no project-wide roles). Values are read from your terminal and
# never echoed, written to disk or put in shell history.
set -euo pipefail
PROJECT="${PROJECT:-clearpath-ai-adel}"
REGION="${REGION:-us-central1}"
SA="clearpath-run@${PROJECT}.iam.gserviceaccount.com"

gcloud services enable secretmanager.googleapis.com --project "$PROJECT"
gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 || \
  gcloud iam service-accounts create clearpath-run --project "$PROJECT" \
    --display-name "ClearPath Cloud Run runtime (least privilege)"

put_secret() {   # name, prompt
  local name="$1" prompt="$2" value
  read -r -s -p "$prompt (leave empty to skip): " value; echo
  [[ -z "$value" ]] && { echo "  skipped $name"; return; }
  gcloud secrets describe "$name" --project "$PROJECT" >/dev/null 2>&1 || \
    gcloud secrets create "$name" --project "$PROJECT" --replication-policy=user-managed --locations="$REGION"
  printf '%s' "$value" | gcloud secrets versions add "$name" --project "$PROJECT" --data-file=-
  gcloud secrets add-iam-policy-binding "$name" --project "$PROJECT" \
    --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor >/dev/null
  echo "  $name: new version stored, $SA can read it"
}

put_secret openai-api-key       "OpenAI API key (sk-...)"
put_secret langfuse-public-key  "Langfuse public key (pk-lf-...)"
put_secret langfuse-secret-key  "Langfuse secret key (sk-lf-...)"
put_secret database-url         "App DATABASE_URL (user clearpath_app.<ref>, port 6543)"
put_secret supabase-jwt-secret  "Supabase legacy JWT secret (only for HS256 projects)"

# Backup job reads the owner URL from Secret Manager too (via keyless GitHub auth, step 5),
# so no database credential is ever stored in GitHub.
put_secret backup-database-url  "Owner DB URL for pg_dump (user postgres.<ref>, port 5432)"
echo "done. Next: infra/deploy.sh"
