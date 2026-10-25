#!/usr/bin/env bash
# A09: log-based metric counting authentication failures, and an alert when there are
# 10 or more in 5 minutes (credential stuffing / token brute force). Email notification.
set -euo pipefail
PROJECT="${PROJECT:-clearpath-ai-adel}"
EMAIL="${ALERT_EMAIL:?export ALERT_EMAIL=you@example.com}"

FILTER='resource.type="cloud_run_revision" AND resource.labels.service_name="clearpath" AND jsonPayload.audit=true AND (jsonPayload.event="login_failure" OR jsonPayload.event="auth_failure")'
gcloud logging metrics describe clearpath_auth_failures --project "$PROJECT" >/dev/null 2>&1 || \
  gcloud logging metrics create clearpath_auth_failures --project "$PROJECT" \
    --description "ClearPath login and token verification failures" --log-filter "$FILTER"

CHANNEL="$(gcloud beta monitoring channels list --project "$PROJECT" --filter="labels.email_address=$EMAIL" --format='value(name)' | head -1)"
if [[ -z "$CHANNEL" ]]; then
  CHANNEL="$(gcloud beta monitoring channels create --project "$PROJECT" --display-name "ClearPath security" \
              --type email --channel-labels "email_address=$EMAIL" --format='value(name)')"
fi

cat > /tmp/cp-alert.json <<JSON
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
gcloud alpha monitoring policies create --project "$PROJECT" --policy-from-file=/tmp/cp-alert.json
rm /tmp/cp-alert.json
echo "alert created; notifications go to $EMAIL"
