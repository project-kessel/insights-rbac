#!/usr/bin/env bash
# Create N workspaces via the V2 API with a prefixed name and unique UUID suffix.
#
# Usage:
#   ./scripts/create_workspaces.sh -n 10 -p "test-ws" -e local
#   ./scripts/create_workspaces.sh -n 5 -e stage
#
# Options:
#   -n  Number of workspaces to create (required)
#   -p  Name prefix (default: "perf-test-ws")
#   -e  Environment: local | stage | prod (default: local)
#   -b  Override base URL (optional, auto-set per environment)
#   -P  Parent workspace UUID (optional, defaults to the org's default workspace)
#   -o  Output file to store created workspace IDs (default: created_workspaces.txt)
#
# Environment variables:
#   RBAC_STAGE_URL   Base URL for stage (e.g. https://rbac.stage.example.com)
#   RBAC_PROD_URL    Base URL for prod
#   SQUID_PROXY      HTTP proxy for stage/prod (e.g. proxy.example.com:3128)
#
# Authentication (resolved in priority order):
#   local:      $X_RH_IDENTITY, or built-in dev identity
#   stage/prod: $XHR (raw bearer token)
#               $HCC_REFRESH_TOKEN + $HCC_SSO_URL (refresh_token grant)
#               $BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME + $BENTO_BASIC_AUTH_CONSOLE_DOT_PASSWORD + $HCC_SSO_URL
#               interactive paste

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── .env loader ─────────────────────────────────────────────────────────────
_load_dotenv() {
    local envfile
    for envfile in "$SCRIPT_DIR/.env" "$SCRIPT_DIR/../.env"; do
        if [[ -f "$envfile" ]]; then
            while IFS= read -r line || [[ -n "$line" ]]; do
                line="${line##"${line%%[![:space:]]*}"}"
                [[ -z "$line" || "$line" == \#* ]] && continue
                if [[ "$line" =~ ^(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*) ]]; then
                    local key="${BASH_REMATCH[2]}"
                    local val="${BASH_REMATCH[3]}"
                    val="${val%\"}" ; val="${val#\"}"
                    val="${val%\'}" ; val="${val#\'}"
                    [[ -z "${!key:-}" ]] && export "$key=$val"
                fi
            done < "$envfile"
            break
        fi
    done
}

# ── SSO token exchange ──────────────────────────────────────────────────────
TOKEN_PATH="/auth/realms/redhat-external/protocol/openid-connect/token"

_fetch_sso_token() {
    local sso_url="$1" ; shift
    local url="${sso_url%/}${TOKEN_PATH}"

    # Join remaining args with & to form the POST body
    local IFS='&'
    local post_data="$*"

    local proxy_args=()
    if [[ -n "${SQUID_PROXY:-}" ]]; then
        proxy_args=(-x "http://${SQUID_PROXY}")
    fi

    local response
    response=$(curl -sk "${proxy_args[@]}" -X POST "$url" -d "$post_data" 2>&1)
    echo "$response" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
except json.JSONDecodeError:
    print('SSO error: non-JSON response', file=sys.stderr)
    sys.exit(1)
if 'access_token' not in data:
    err = data.get('error_description', data.get('error', json.dumps(data)))
    print(f'SSO error: {err}', file=sys.stderr)
    sys.exit(1)
print(data['access_token'])
"
}

# ── auth resolution ─────────────────────────────────────────────────────────
resolve_auth() {
    local env="$1"

    if [[ "$env" == "local" ]]; then
        if [[ -n "${X_RH_IDENTITY:-}" ]]; then
            echo "identity:${X_RH_IDENTITY}"
            return
        fi
        local default_id
        default_id=$(echo -n '{"identity":{"account_number":"10001","org_id":"11111","type":"User","user":{"username":"user_dev","email":"user_dev@foo.com","is_org_admin":true,"is_internal":true,"user_id":"51736777"},"internal":{"cross_access":false}}}' | base64)
        echo "identity:${default_id}"
        return
    fi

    # stage / prod — bearer token
    if [[ -n "${XHR:-}" ]]; then
        echo >&2 "  Using \$XHR token (${#XHR} chars)"
        echo "bearer:${XHR}"
        return
    fi

    local sso_host="${HCC_SSO_URL:-}"

    if [[ -n "$sso_host" && -n "${HCC_REFRESH_TOKEN:-}" ]]; then
        echo >&2 "  Exchanging refresh token via ${sso_host##*/}…"
        local token
        if ! token=$(_fetch_sso_token "$sso_host" \
            "client_id=cloud-services" \
            "grant_type=refresh_token" \
            "refresh_token=${HCC_REFRESH_TOKEN}"); then
            echo >&2 "  SSO token exchange failed. Aborting."
            exit 1
        fi
        echo >&2 "  Got SSO token (${#token} chars)"
        echo "bearer:${token}"
        return
    fi

    local username="${BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME:-}"
    local password="${BENTO_BASIC_AUTH_CONSOLE_DOT_PASSWORD:-}"

    if [[ -n "$sso_host" && -n "$username" && -n "$password" ]]; then
        echo >&2 "  Fetching token via password grant from ${sso_host##*/}…"
        local token
        if ! token=$(_fetch_sso_token "$sso_host" \
            "client_id=cloud-services" \
            "grant_type=password" \
            "username=${username}" \
            "password=${password}"); then
            echo >&2 "  SSO token exchange failed. Aborting."
            exit 1
        fi
        echo >&2 "  Got SSO token (${#token} chars)"
        echo "bearer:${token}"
        return
    fi

    # interactive fallback
    echo >&2 "  No \$XHR, \$HCC_REFRESH_TOKEN, or username/password credentials found."
    echo >&2 "  Paste your bearer token:"
    read -r -s -p "  token> " token_input
    echo >&2
    if [[ -z "$token_input" ]]; then
        echo >&2 "  Error: no token provided"
        exit 1
    fi
    echo "bearer:${token_input}"
}

_build_auth_header() {
    local auth_value="$1"
    local auth_type="${auth_value%%:*}"
    local auth_token="${auth_value#*:}"

    if [[ "$auth_type" == "identity" ]]; then
        echo "X-RH-Identity: ${auth_token}"
    else
        echo "Authorization: Bearer ${auth_token}"
    fi
}

# ── defaults ────────────────────────────────────────────────────────────────
COUNT=""
PREFIX="perf-test-ws"
ENVIRONMENT="local"
BASE_URL=""
PARENT_ID=""
OUTPUT_FILE="created_workspaces.txt"

usage() {
    echo "Usage: $0 -n <count> [-p <prefix>] [-e local|stage|prod] [-b <base_url>] [-P <parent_id>] [-o <output_file>]"
    echo ""
    echo "  -n  Number of workspaces to create (required)"
    echo "  -p  Name prefix (default: perf-test-ws)"
    echo "  -e  Environment: local, stage, prod (default: local)"
    echo "  -b  Override base URL"
    echo "  -P  Parent workspace UUID"
    echo "  -o  Output file for created IDs (default: created_workspaces.txt)"
    echo ""
    echo "Auth env vars (stage/prod):"
    echo "  XHR                                      Raw bearer token"
    echo "  HCC_SSO_URL + HCC_REFRESH_TOKEN          SSO refresh_token grant"
    echo "  HCC_SSO_URL + BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME/PASSWORD  SSO password grant"
    exit 1
}

while getopts "n:p:e:b:P:o:h" opt; do
    case $opt in
        n) COUNT="$OPTARG" ;;
        p) PREFIX="$OPTARG" ;;
        e) ENVIRONMENT="$OPTARG" ;;
        b) BASE_URL="$OPTARG" ;;
        P) PARENT_ID="$OPTARG" ;;
        o) OUTPUT_FILE="$OPTARG" ;;
        h) usage ;;
        *) usage ;;
    esac
done

if [[ -z "$COUNT" ]]; then
    echo "Error: -n <count> is required"
    usage
fi

if ! [[ "$COUNT" =~ ^[0-9]+$ ]] || [[ "$COUNT" -lt 1 ]]; then
    echo "Error: count must be a positive integer"
    exit 1
fi

if [[ ! "$ENVIRONMENT" =~ ^(local|stage|prod)$ ]]; then
    echo "Error: -e must be local, stage, or prod"
    exit 1
fi

_load_dotenv

# Resolve base URL per environment if not overridden
if [[ -z "$BASE_URL" ]]; then
    case "$ENVIRONMENT" in
        local) BASE_URL="${RBAC_LOCAL_URL:-http://localhost:8000}" ;;
        stage) BASE_URL="${RBAC_STAGE_URL:-}" ;;
        prod)  BASE_URL="${RBAC_PROD_URL:-}" ;;
    esac
fi

if [[ -z "$BASE_URL" ]]; then
    echo "Error: no base URL for environment '$ENVIRONMENT'."
    echo "Set -b or \$RBAC_STAGE_URL / \$RBAC_PROD_URL in .env"
    exit 1
fi

BASE_URL="${BASE_URL%/}"

# Proxy — default for stage/prod, skip for local
if [[ "$ENVIRONMENT" != "local" ]]; then
    SQUID_PROXY="${SQUID_PROXY:-}"
fi

AUTH_VALUE=$(resolve_auth "$ENVIRONMENT")
AUTH_HEADER=$(_build_auth_header "$AUTH_VALUE")

# Local runs RBAC directly; stage/prod go through the console gateway at /api/rbac
if [[ "$ENVIRONMENT" == "local" ]]; then
    API_URL="${BASE_URL}/api/v2/workspaces/"
else
    API_URL="${BASE_URL}/api/rbac/v2/workspaces/"
fi
PROXY_ARGS=()
if [[ -n "${SQUID_PROXY:-}" ]]; then
    PROXY_ARGS=(-x "http://${SQUID_PROXY}")
fi
CREATED=0
FAILED=0

> "$OUTPUT_FILE"

echo "Creating $COUNT workspaces with prefix '$PREFIX' ..."
echo "Environment: $ENVIRONMENT"
echo "API: $API_URL"
[[ -n "${SQUID_PROXY:-}" ]] && echo "Proxy: $SQUID_PROXY"
echo "Output: $OUTPUT_FILE"
echo "---"

for i in $(seq 1 "$COUNT"); do
    UNIQUE_SUFFIX=$(python3 -c "import uuid; print(uuid.uuid4().hex[:8])")
    WS_NAME="${PREFIX}-${UNIQUE_SUFFIX}"

    BODY="{\"name\": \"${WS_NAME}\"}"
    if [[ -n "$PARENT_ID" ]]; then
        BODY="{\"name\": \"${WS_NAME}\", \"parent_id\": \"${PARENT_ID}\"}"
    fi

    RESPONSE=$(curl -s "${PROXY_ARGS[@]}" -w "\n%{http_code}" -X POST "$API_URL" \
        -H "Content-Type: application/json" \
        -H "$AUTH_HEADER" \
        -d "$BODY")

    HTTP_CODE=$(echo "$RESPONSE" | tail -1)
    BODY_RESPONSE=$(echo "$RESPONSE" | sed '$d')

    if [[ "$HTTP_CODE" == "201" ]]; then
        WS_ID=$(echo "$BODY_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
        echo "$WS_ID $WS_NAME" >> "$OUTPUT_FILE"
        CREATED=$((CREATED + 1))
        echo "[$i/$COUNT] Created: $WS_NAME (id: $WS_ID)"
    else
        FAILED=$((FAILED + 1))
        echo "[$i/$COUNT] FAILED: $WS_NAME — HTTP $HTTP_CODE"
        echo "  Response: $BODY_RESPONSE"
    fi
done

echo "---"
echo "Done. Created: $CREATED, Failed: $FAILED"
echo "Workspace IDs saved to: $OUTPUT_FILE"
