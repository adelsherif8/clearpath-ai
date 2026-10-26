#!/usr/bin/env bash
# Dump the clearpath schema (structure + data + RLS policies + grants + triggers) to a
# compressed custom-format archive, and optionally upload it to a private GCS bucket.
#
#   BACKUP_DATABASE_URL  owner connection (pg_dump must be able to read every row; RLS is
#                        enforced for the app role, so the app's own credential can't be used)
#   BACKUP_BUCKET        gs://bucket-name   (optional; omit to keep the file locally)
#   OUT_DIR              where to write the dump (default: ./backups)
#   PG_DUMP              pg_dump binary (default: pg_dump; must be >= the server's major version)
set -euo pipefail
: "${BACKUP_DATABASE_URL:?set BACKUP_DATABASE_URL}"
OUT_DIR="${OUT_DIR:-./backups}"
PG_DUMP="${PG_DUMP:-pg_dump}"
mkdir -p "$OUT_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
file="$OUT_DIR/clearpath-$stamp.dump"

$PG_DUMP --dbname="$BACKUP_DATABASE_URL" --format=custom --compress=9 --schema=clearpath \
         --no-comments --file="$file"
# checksum against the bare file name so it verifies wherever the dump is downloaded
(cd "$OUT_DIR" && { sha256sum "$(basename "$file")" 2>/dev/null || shasum -a 256 "$(basename "$file")"; } > "$(basename "$file").sha256")
echo "dump written: $file ($(wc -c < "$file") bytes)"

if [[ -n "${BACKUP_BUCKET:-}" ]]; then
  # Upload with the JSON API's simple media upload: it needs only storage.objects.create. `gcloud storage cp`
  # also reads the destination object first (storage.objects.get), and the backup account is deliberately
  # create-only (roles/storage.objectCreator) so it can never read old backups back.
  bucket="${BACKUP_BUCKET#gs://}"; prefix="clearpath/$(date -u +%Y/%m/%d)"
  token="$(gcloud auth print-access-token)"
  for f in "$file" "$file.sha256"; do
    name="$prefix/$(basename "$f")"
    curl --fail-with-body -sS -X POST --data-binary @"$f" \
      -H "Authorization: Bearer $token" -H "Content-Type: application/octet-stream" \
      "https://storage.googleapis.com/upload/storage/v1/b/$bucket/o?uploadType=media&name=${name//\//%2F}" > /dev/null
  done
  echo "uploaded to gs://$bucket/$prefix/"
fi
echo "$file"
