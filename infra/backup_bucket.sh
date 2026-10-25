#!/usr/bin/env bash
# Private backup bucket + keyless (Workload Identity Federation) auth for the GitHub backup job.
# No service-account key is created or stored anywhere.
set -euo pipefail
PROJECT="${PROJECT:-clearpath-ai-adel}"
REGION="${REGION:-us-central1}"
REPO="${REPO:-adelsherif8/clearpath-ai}"
BUCKET="gs://${PROJECT}-db-backups"
BSA="clearpath-backup@${PROJECT}.iam.gserviceaccount.com"
NUM="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"

gcloud services enable iamcredentials.googleapis.com sts.googleapis.com --project "$PROJECT"
gcloud storage buckets describe "$BUCKET" >/dev/null 2>&1 || \
  gcloud storage buckets create "$BUCKET" --project "$PROJECT" --location "$REGION" \
    --default-storage-class STANDARD --uniform-bucket-level-access --public-access-prevention
# keep 30 days of backups (stays far inside the 5 GB free tier)
printf '{"rule":[{"action":{"type":"Delete"},"condition":{"age":30}}]}' > /tmp/cp-lifecycle.json
gcloud storage buckets update "$BUCKET" --lifecycle-file=/tmp/cp-lifecycle.json && rm /tmp/cp-lifecycle.json

gcloud iam service-accounts describe "$BSA" --project "$PROJECT" >/dev/null 2>&1 || \
  gcloud iam service-accounts create clearpath-backup --project "$PROJECT" --display-name "ClearPath DB backup job"
# write-only on the bucket (objectCreator cannot read, overwrite or delete existing backups)
gcloud storage buckets add-iam-policy-binding "$BUCKET" --member "serviceAccount:$BSA" --role roles/storage.objectCreator
gcloud secrets add-iam-policy-binding backup-database-url --project "$PROJECT" \
  --member "serviceAccount:$BSA" --role roles/secretmanager.secretAccessor

gcloud iam workload-identity-pools describe github --project "$PROJECT" --location global >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools create github --project "$PROJECT" --location global --display-name "GitHub Actions"
gcloud iam workload-identity-pools providers describe clearpath --project "$PROJECT" --location global \
  --workload-identity-pool github >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools providers create-oidc clearpath --project "$PROJECT" --location global \
    --workload-identity-pool github --issuer-uri "https://token.actions.githubusercontent.com" \
    --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition "assertion.repository=='${REPO}' && assertion.ref=='refs/heads/master'"
gcloud iam service-accounts add-iam-policy-binding "$BSA" --project "$PROJECT" --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/${NUM}/locations/global/workloadIdentityPools/github/attribute.repository/${REPO}"

echo
echo "Set these as GitHub repository *variables* (Settings > Secrets and variables > Actions > Variables):"
echo "  GCP_WIF_PROVIDER = projects/${NUM}/locations/global/workloadIdentityPools/github/providers/clearpath"
echo "  GCP_BACKUP_SA    = ${BSA}"
echo "  BACKUP_BUCKET    = ${BUCKET}"
