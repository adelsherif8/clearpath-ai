#!/usr/bin/env bash
# A09: log-based metric counting authentication failures, and an alert when there are
# 10 or more in 5 minutes (credential stuffing / token brute force). Email notification.
# Uses the GA logging CLI and the Cloud Monitoring REST API (no gcloud alpha/beta needed).
set -euo pipefail
PROJECT="${PROJECT:-clearpath-ai-adel}"
EMAIL="${ALERT_EMAIL:?export ALERT_EMAIL=you@example.com}"
API="https://monitoring.googleapis.com/v3/projects/$PROJECT"
AUTH="Authorization: Bearer $(gcloud auth print-access-token)"

FILTER='resource.type="cloud_run_revision" AND resource.labels.service_name="clearpath" AND jsonPayload.audit=true AND (jsonPayload.event="login_failure" OR jsonPayload.event="auth_failure")'
gcloud logging metrics describe clearpath_auth_failures --project "$PROJECT" >/dev/null 2>&1 || \
  gcloud logging metrics create clearpath_auth_failures --project "$PROJECT" \
    --description "ClearPath login and token verification failures" --log-filter "$FILTER"

CHANNEL="$(curl -s -H "$AUTH" "$API/notificationChannels" | python3 -c "
import json,sys; e=sys.argv[1]
print(next((c['name'] for c in json.load(sys.stdin).get('notificationChannels',[]) if c.get('labels',{}).get('email_address')==e), ''))" "$EMAIL")"
if [[ -z "$CHANNEL" ]]; then
  CHANNEL="$(curl -s -X POST -H "$AUTH" -H 'content-type: application/json' "$API/notificationChannels" \
    -d "{\"type\":\"email\",\"displayName\":\"ClearPath security\",\"labels\":{\"email_address\":\"$EMAIL\"}}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')"
fi

EXISTING="$(curl -s -H "$AUTH" "$API/alertPolicies" | python3 -c "
import json,sys; print(next((p['name'] for p in json.load(sys.stdin).get('alertPolicies',[]) if p['displayName']=='ClearPath: repeated authentication failures'), ''))")"
if [[ -n "$EXISTING" ]]; then echo "alert policy already exists: $EXISTING"; exit 0; fi

curl -s -X POST -H "$AUTH" -H 'content-type: application/json' "$API/alertPolicies" -d @- <<JSON | python3 -c 'import json,sys; d=json.load(sys.stdin); print("alert policy:", d.get("name") or d)'
{
  "displayName": "ClearPath: repeated authentication failures",
  "documentation": {"content": "10+ failed logins or invalid tokens in 5 minutes. Check Cloud Logging for jsonPayload.event=login_failure/auth_failure, grouped by jsonPayload.ip. Rate limiting already throttles each IP to 5 logins/min.", "mimeType": "text/markdown"},
  "combiner": "OR",
  "conditions": [{
    "displayName": "auth failures >= 10 / 5 min",
    "conditionThreshold": {
      "filter": "metric.type=\"logging.googleapis.com/user/clearpath_auth_failures\" AND resource.type=\"cloud_run_revision\"",
      "aggregations": [{"alignmentPeriod": "300s", "perSeriesAligner": "ALIGN_SUM", "crossSeriesReducer": "REDUCE_SUM"}],
      "comparison": "COMPARISON_GT", "thresholdValue": 9, "duration": "0s",
      "trigger": {"count": 1}
    }
  }],
  "notificationChannels": ["$CHANNEL"],
  "alertStrategy": {"autoClose": "1800s"}
}
JSON
echo "notifications go to $EMAIL"
