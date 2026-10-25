#!/usr/bin/env bash
# Read-only verification of the deployed security configuration.
set -uo pipefail
PROJECT="${PROJECT:-clearpath-ai-adel}"; REGION="${REGION:-us-central1}"
URL="${URL:-https://clearpath-798991357908.us-central1.run.app}"
echo "== runtime service account and its project roles"
SA="$(gcloud run services describe clearpath --project "$PROJECT" --region "$REGION" --format='value(spec.template.spec.serviceAccountName)')"
echo "  $SA"
gcloud projects get-iam-policy "$PROJECT" --flatten=bindings --filter="bindings.members:serviceAccount:$SA" --format="value(bindings.role)" | sed 's/^/  project role: /'
echo "== env vars vs secret refs (no secret values are printed)"
gcloud run services describe clearpath --project "$PROJECT" --region "$REGION" --format=json \
 | python3 -c "import json,sys; c=json.load(sys.stdin)['spec']['template']['spec']['containers'][0]
for e in c.get('env',[]): print('  ', e['name'], '-> secret '+e['valueFrom']['secretKeyRef']['name'] if 'valueFrom' in e else '-> plain env')"
echo "== live checks"
curl -s "$URL/health"; echo
curl -s -o /dev/null -w "  /docs -> %{http_code} (want 404)\n" "$URL/docs"
curl -sI "$URL/" | grep -iE "content-security-policy|strict-transport|x-content-type|referrer-policy" | sed 's/^/  /'
curl -s -X POST "$URL/review" -H 'content-type: application/json' -d '{"action":"approve","id":"PA-0000ABCD"}' -w "  unauthenticated approve -> %{http_code} (want 401)\n" -o /dev/null
curl -s -X POST "$URL/mcp/" -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' -o /dev/null -w "  /mcp/ tools/list -> %{http_code} (want 200)\n"
