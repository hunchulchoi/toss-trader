#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
env_file=${INFISICAL_MACHINE_ENV_FILE:-$repo_root/.env}
n8n_container=${N8N_CONTAINER:-n8n}
n8n_project_id=${N8N_PROJECT_ID:-YNUiWwcSPiect3LE}
auth_error=$(mktemp /tmp/toss-trader-infisical-auth.XXXXXX)

cleanup() {
    unset INFISICAL_TOKEN HERMES_API_KEY TOSS_CLIENT_ID TOSS_CLIENT_SECRET
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

get_secret() {
    infisical secrets get "$1" \
        --domain="$INFISICAL_DOMAIN" \
        --projectId="$PROJECT_ID" \
        --env=prod \
        --path=/ \
        --plain \
        --silent \
        --token="$INFISICAL_TOKEN"
}

HERMES_API_KEY=$(get_secret HERMES_API_KEY)
TOSS_CLIENT_ID=$(get_secret TOSS_CLIENT_ID)
TOSS_CLIENT_SECRET=$(get_secret TOSS_CLIENT_SECRET)
export HERMES_API_KEY TOSS_CLIENT_ID TOSS_CLIENT_SECRET

if [ ${#HERMES_API_KEY} -lt 16 ] || [ -z "$TOSS_CLIENT_ID" ] || [ -z "$TOSS_CLIENT_SECRET" ]; then
    echo "Infisical returned an empty or invalid Toss Trader credential" >&2
    exit 1
fi

jq -n '[
  {
    id: "toss-trader-hermes-auth",
    name: "Toss Trader Hermes Bearer",
    type: "httpHeaderAuth",
    data: {
      name: "Authorization",
      value: ("Bearer " + env.HERMES_API_KEY)
    }
  },
  {
    id: "toss-trader-toss-oauth2",
    name: "Toss Trader Toss OAuth2",
    type: "oAuth2Api",
    data: {
      grantType: "clientCredentials",
      accessTokenUrl: "https://openapi.tossinvest.com/oauth2/token",
      clientId: env.TOSS_CLIENT_ID,
      clientSecret: env.TOSS_CLIENT_SECRET,
      scope: "",
      authentication: "body",
      sendAdditionalBodyProperties: false,
      ignoreSSLIssues: false,
      tokenExpiredStatusCode: 401
    }
  }
]' | docker exec -i "$n8n_container" sh -c '
    set -eu
    credential_file=$(mktemp /tmp/toss-trader-credentials.XXXXXX)
    trap '\''rm -f "$credential_file"'\'' EXIT HUP INT TERM
    chmod 600 "$credential_file"
    tee "$credential_file" >/dev/null
    n8n import:credentials --input="$credential_file" --projectId="$1"
' sh "$n8n_project_id"

echo "Synced 2 encrypted n8n credentials from Infisical"
