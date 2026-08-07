#!/bin/bash

# Call RBAC internal API endpoints in stage or prod via Turnpike.
#
# Internal Django paths under /_private/api/... are exposed at:
#   ${STAGE_DOMAIN}/api/rbac/...
#
# Example:
#   sh internal-api.sh stage POST utils/bootstrap_users_from_user_ids/ dry_run=true '{"user_ids":["12345"]}'

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: internal-api.sh <stage|prod> <METHOD> <path> [query_string] [json_body]

  path         Path after /api/rbac/ (from rbac/internal/urls.py, drop the leading "api/")
  query_string Optional query params without leading "?" (e.g. dry_run=true&limit=10)
  json_body    Optional JSON request body (for POST/PUT/PATCH)

Examples:
  # GET
  sh internal-api.sh stage GET utils/bootstrap_pending_tenants/

  # POST with body only (query omitted)
  sh internal-api.sh stage POST utils/bootstrap_users_from_user_ids/ '{"user_ids":["12345"]}'

  # POST with query and body
  sh internal-api.sh stage POST utils/bootstrap_users_from_user_ids/ dry_run=true '{"user_ids":["12345"]}'

  # Relations API (same routing as relationship skill)
  sh internal-api.sh stage POST relations/read_tuples/ '{"filter": {...}}'

Endpoint reference: rbac/internal/urls.py
EOF
}

if [ "$#" -lt 3 ]; then
    usage
    exit 1
fi

ENVIRONMENT=$1
METHOD=$(echo "$2" | tr '[:lower:]' '[:upper:]')
PATH_SUFFIX=$3
shift 3

QUERY=""
BODY=""

if [ "$#" -ge 1 ]; then
    if [[ "$1" == "{"* || "$1" == "["* ]]; then
        BODY=$1
    else
        QUERY=$1
        shift
        if [ "$#" -ge 1 ]; then
            BODY=$1
        fi
    fi
fi

# Normalize path: accept with or without leading/trailing slashes.
PATH_SUFFIX="${PATH_SUFFIX#/}"
PATH_SUFFIX="${PATH_SUFFIX%/}/"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/../../config.env"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "Error: config.env not found at $CONFIG_FILE"
    echo "Create .cursor/skills/config.env with STAGE_DOMAIN, PROD_DOMAIN, and PROXY."
    exit 1
fi

# shellcheck source=/dev/null
source "$CONFIG_FILE"

for var in STAGE_DOMAIN PROD_DOMAIN PROXY; do
    if [ -z "${!var:-}" ]; then
        echo "Error: $var is not set in config.env"
        exit 1
    fi
done

TURNPIKE_STAGE_URL="${STAGE_DOMAIN}/api/turnpike/session/"
TURNPIKE_PROD_URL="${PROD_DOMAIN}/api/turnpike/session/"

if [ "$ENVIRONMENT" = "stage" ]; then
    BASE_URL="${STAGE_DOMAIN}/api/rbac"
elif [ "$ENVIRONMENT" = "prod" ]; then
    BASE_URL="${PROD_DOMAIN}/api/rbac"
else
    echo "Invalid environment. Use 'stage' or 'prod'."
    exit 1
fi

if [ -z "${SESSION:-}" ]; then
    echo "Error: SESSION environment variable is not set."
    echo ""
    echo "Get a Turnpike session token by pasting this URL into your browser:"
    if [ "$ENVIRONMENT" = "stage" ]; then
        echo "  $TURNPIKE_STAGE_URL"
    else
        echo "  $TURNPIKE_PROD_URL"
    fi
    echo ""
    echo "Then: export SESSION=<token_value>"
    exit 1
fi

API_URL="${BASE_URL}/${PATH_SUFFIX}"
if [ -n "$QUERY" ]; then
    API_URL="${API_URL}?${QUERY}"
fi

CURL_ARGS=(
    -sS
    -X "$METHOD"
    -H "Content-Type: application/json"
    -b "session=$SESSION"
    -w $'\n%{http_code}'
    "$API_URL"
)

if [ -n "$BODY" ]; then
    CURL_ARGS+=(-d "$BODY")
fi

if [ "$ENVIRONMENT" = "stage" ]; then
    RAW=$(curl --proxy "$PROXY" "${CURL_ARGS[@]}")
else
    RAW=$(curl "${CURL_ARGS[@]}")
fi

HTTP_CODE=$(echo "$RAW" | tail -1)
RESPONSE_BODY=$(echo "$RAW" | sed '$d')

if [ "$HTTP_CODE" -lt 200 ] || [ "$HTTP_CODE" -ge 300 ]; then
    echo "HTTP $HTTP_CODE" >&2
    if [ -n "$RESPONSE_BODY" ]; then
        echo "$RESPONSE_BODY" | jq . 2>/dev/null || echo "$RESPONSE_BODY"
    fi
    exit 1
fi

if [ -n "$RESPONSE_BODY" ]; then
    echo "$RESPONSE_BODY" | jq . 2>/dev/null || echo "$RESPONSE_BODY"
fi
