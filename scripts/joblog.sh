#!/usr/bin/env bash
# joblog.sh — fetch a controller job's full output from the AAP API.
#
# The web UI renders only a window of a long job's output; this pulls the
# complete stdout (every task, every host, the recap) as text.
#
# Usage:
#   joblog.sh <JOB_ID>              # metadata summary + full output to stdout
#   joblog.sh <JOB_ID> -o FILE      # save the output to a file
#
# Credentials: a readable file (default ~/.aap_api_credentials) must define:
#   AAP_URL=https://aap27.lab.example.com
#   AAP_USER=admin
#   AAP_PASSWORD=...
# Never commit that file — it lives outside the repo, in $HOME
# (same convention as ~/.vault_pass).
#
# Notes:
#   - Works for playbook jobs. Workflow runs have their own stdout endpoint
#     and are handled, but their output is just the child-job listing.

set -euo pipefail

CRED_FILE="${AAP_API_CRED_FILE:-$HOME/.aap_api_credentials}"
if [ ! -r "$CRED_FILE" ]; then
    echo "ERROR: credentials file not readable: $CRED_FILE" >&2
    echo "Create it with:  cp /dev/null $CRED_FILE && chmod 600 $CRED_FILE" >&2
    echo "  then add AAP_URL / AAP_USER / AAP_PASSWORD" >&2
    exit 2
fi
# shellcheck disable=SC1090
source "$CRED_FILE"

: "${AAP_URL:?AAP_URL not set in $CRED_FILE}"
: "${AAP_USER:?AAP_USER not set in $CRED_FILE}"
: "${AAP_PASSWORD:?AAP_PASSWORD not set in $CRED_FILE}"

JOB_ID="${1:-}"
[ -n "$JOB_ID" ] || { echo "Usage: $0 <JOB_ID> [-o FILE]" >&2; exit 2; }
shift || true
OUT_FILE=""
if [ "${1:-}" = "-o" ] && [ -n "${2:-}" ]; then
    OUT_FILE="$2"
fi

AUTH="${AAP_USER}:${AAP_PASSWORD}"
BASE="${AAP_URL}/api/controller/v2"

# --- metadata: discover the job type and friendly details -------------------
META=$(curl -sk -u "$AUTH" "$BASE/jobs/$JOB_ID/")

TYPE=$(echo "$META" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("type",""))')
NAME=$(echo "$META" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("name",""))')
STATUS=$(echo "$META" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))')
STARTED=$(echo "$META" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("started") or "")')
FINISHED=$(echo "$META" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("finished") or "")')

echo "job $JOB_ID: $NAME | status=$STATUS | started=$STARTED | finished=$FINISHED"

# --- stdout: playbook jobs and workflow jobs have different endpoints ------
case "$TYPE" in
    job)          STDOUT_URL="$BASE/jobs/$JOB_ID/stdout/?format=txt" ;;
    workflow_job) STDOUT_URL="$BASE/workflow_jobs/$JOB_ID/stdout/?format=txt" ;;
    *)
        echo "ERROR: job type '$TYPE' has no stdout endpoint" >&2
        exit 1
        ;;
esac

if [ -n "$OUT_FILE" ]; then
    curl -sk -u "$AUTH" "$STDOUT_URL" -o "$OUT_FILE"
    echo "saved: $OUT_FILE ($(wc -l < "$OUT_FILE") lines)"
else
    curl -sk -u "$AUTH" "$STDOUT_URL"
fi
