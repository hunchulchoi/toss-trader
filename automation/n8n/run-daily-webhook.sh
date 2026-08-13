#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
env_file=${INFISICAL_MACHINE_ENV_FILE:-$repo_root/.env}
webhook_url=${N8N_DAILY_WEBHOOK_URL:-https://n8n.dgst.me/webhook/toss-trader-daily-run}
auth_error=$(mktemp /tmp/toss-trader-infisical-auth.XXXXXX)

cleanup() {
    unset INFISICAL_TOKEN N8N_MANUAL_TRIGGER_TOKEN
    rm -f "$auth_error"
}
trap cleanup EXIT HUP INT TERM

set -a
# shellcheck disable=SC1090
. "$env_file"
set +a

: "${INFISICAL_CLIENT_ID:?INFISICAL_CLIENT_ID is required}"
: "${INFISICAL_CLIENT_SECRET:?INFISICAL_CLIENT_SECRET is required}"
: "${INFISICAL_DOMAIN:?INFISICAL_DOMAIN is required}"
: "${PROJECT_ID:?PROJECT_ID is required}"

if ! INFISICAL_TOKEN=$(infisical login \
    --method=universal-auth \
    --client-id="$INFISICAL_CLIENT_ID" \
    --client-secret="$INFISICAL_CLIENT_SECRET" \
    --domain="$INFISICAL_DOMAIN" \
    --plain \
    --silent 2>"$auth_error"); then
    echo "Infisical universal-auth failed" >&2
    exit 1
fi
export INFISICAL_TOKEN

N8N_MANUAL_TRIGGER_TOKEN=$(infisical secrets get N8N_MANUAL_TRIGGER_TOKEN \
    --domain="$INFISICAL_DOMAIN" \
    --projectId="$PROJECT_ID" \
    --env=prod \
    --path=/ \
    --plain \
    --silent \
    --token="$INFISICAL_TOKEN")

if [ ${#N8N_MANUAL_TRIGGER_TOKEN} -lt 16 ]; then
    echo "Infisical N8N_MANUAL_TRIGGER_TOKEN is missing or invalid" >&2
    exit 1
fi

printf 'Authorization: Bearer %s\n' "$N8N_MANUAL_TRIGGER_TOKEN" | \
    curl --fail-with-body --silent --show-error \
        --header @- \
        --request POST \
        --max-time 1200 \
        "$webhook_url"
