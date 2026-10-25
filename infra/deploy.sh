#!/usr/bin/env bash
# Deploy a new revision of the existing Cloud Run service with:
#   * the dedicated least-privilege service account (not the default compute SA, which has Editor)
#   * secrets mounted from Secret Manager (never plain env vars)
#   * the same free-tier sizing as before (scale to zero, 1 instance max)
set -euo pipefail
PROJECT="${PROJECT:-clearpath-ai-adel}"
REGION="${REGION:-us-central1}"
SA="clearpath-run@${PROJECT}.iam.gserviceaccount.com"
HOSTS="clearpath-798991357908.us-central1.run.app,clearpath-bdizbv4eyq-uc.a.run.app"
: "${SUPABASE_URL:?export SUPABASE_URL (public, not a secret)}"
: "${SUPABASE_ANON_KEY:?export SUPABASE_ANON_KEY (public by design, not a secret)}"

secrets="DATABASE_URL=database-url:latest"
for pair in OPENAI_API_KEY=openai-api-key LANGFUSE_PUBLIC_KEY=langfuse-public-key \
            LANGFUSE_SECRET_KEY=langfuse-secret-key SUPABASE_JWT_SECRET=supabase-jwt-secret; do
  name="${pair#*=}"
  if gcloud secrets versions list "$name" --project "$PROJECT" --filter="state=ENABLED" --format="value(name)" 2>/dev/null | grep -q .; then
    secrets="$secrets,${pair%%=*}=${name}:latest"
  fi
done
echo "mounting secrets: $secrets"

gcloud run deploy clearpath --project "$PROJECT" --region "$REGION" --source . \
  --service-account "$SA" \
  --allow-unauthenticated \
  --min-instances 0 --max-instances 1 --memory 512Mi --cpu 1 --cpu-throttling --concurrency 10 \
  --set-secrets "$secrets" \
  --set-env-vars "^@^CP_ENV=production@MCP_ALLOWED_HOSTS=$HOSTS@ALLOWED_HOSTS=$HOSTS@SUPABASE_URL=$SUPABASE_URL@SUPABASE_ANON_KEY=$SUPABASE_ANON_KEY@TRUST_PROXY=cloudrun@CP_DAILY_LLM_CALL_CAP=${CP_DAILY_LLM_CALL_CAP:-200}"
