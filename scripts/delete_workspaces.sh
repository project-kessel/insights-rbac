#!/usr/bin/env bash
# Delete workspaces previously created by create_workspaces.sh.
#
# Usage:
#   ./scripts/delete_workspaces.sh -e local
#   ./scripts/delete_workspaces.sh -e stage -f created_workspaces.txt
#
# Options:
#   -f  Input file with workspace IDs (one "UUID name" per line, default: created_workspaces.txt)
#   -e  Environment: local | stage | prod (default: local)
#   -b  Override base URL (optional, auto-set per environment)
#
# Authentication: same as create_workspaces.sh (see that script for details).

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
INPUT_FILE="created_workspaces.txt"
ENVIRONMENT="local"
BASE_URL=""
COUNT=""

usage() {
    echo "Usage: $0 -n <count> [-f <input_file>] [-e local|stage|prod] [-b <base_url>]"
    echo ""
    echo "  -n  Number of workspaces to delete (required)"
    echo "  -f  File with workspace IDs to delete (default: created_workspaces.txt)"
    echo "  -e  Environment: local, stage, prod (default: local)"
    echo "  -b  Override base URL"
    exit 1
}

while getopts "n:f:e:b:h" opt; do
    case $opt in
        n) COUNT="$OPTARG" ;;
        f) INPUT_FILE="$OPTARG" ;;
        e) ENVIRONMENT="$OPTARG" ;;
        b) BASE_URL="$OPTARG" ;;
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

if [[ ! -f "$INPUT_FILE" ]]; then
    echo "Error: input file '$INPUT_FILE' not found"
    echo "Run create_workspaces.sh first to generate it."
    exit 1
fi

FILE_TOTAL=$(wc -l < "$INPUT_FILE" | tr -d ' ')
if [[ "$FILE_TOTAL" -eq 0 ]]; then
    echo "No workspaces to delete (file is empty)."
    exit 0
fi

if [[ "$COUNT" -gt "$FILE_TOTAL" ]]; then
    echo "Warning: requested $COUNT but only $FILE_TOTAL workspaces in file. Deleting all."
    COUNT="$FILE_TOTAL"
fi

if [[ ! "$ENVIRONMENT" =~ ^(local|stage|prod)$ ]]; then
    echo "Error: -e must be local, stage, or prod"
    exit 1
fi

_load_dotenv

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
    API_URL="${BASE_URL}/api/v2/workspaces"
else
    API_URL="${BASE_URL}/api/rbac/v2/workspaces"
fi

PROXY_ARGS=()
if [[ -n "${SQUID_PROXY:-}" ]]; then
    PROXY_ARGS=(-x "http://${SQUID_PROXY}")
fi

echo "Deleting $COUNT workspaces from '$INPUT_FILE' ($FILE_TOTAL in file) ..."
echo "Environment: $ENVIRONMENT"
echo "API: ${API_URL}/"
[[ -n "${SQUID_PROXY:-}" ]] && echo "Proxy: $SQUID_PROXY"
echo "---"

DELETED=0
FAILED=0
INDEX=0
DELETED_IDS=()

while IFS=' ' read -r WS_ID WS_NAME; do
    if [[ -z "$WS_ID" ]]; then
        continue
    fi

    if [[ "$INDEX" -ge "$COUNT" ]]; then
        break
    fi

    INDEX=$((INDEX + 1))

    HTTP_CODE=$(curl -s "${PROXY_ARGS[@]}" -o /dev/null -w "%{http_code}" -X DELETE \
        "${API_URL}/${WS_ID}/" \
        -H "$AUTH_HEADER")

    if [[ "$HTTP_CODE" == "204" ]]; then
        DELETED=$((DELETED + 1))
        DELETED_IDS+=("$WS_ID")
        echo "[$INDEX/$COUNT] Deleted: ${WS_NAME:-$WS_ID} (id: $WS_ID)"
    elif [[ "$HTTP_CODE" == "404" ]]; then
        DELETED=$((DELETED + 1))
        DELETED_IDS+=("$WS_ID")
        echo "[$INDEX/$COUNT] Already gone: ${WS_NAME:-$WS_ID} (id: $WS_ID)"
    else
        FAILED=$((FAILED + 1))
        echo "[$INDEX/$COUNT] FAILED: ${WS_NAME:-$WS_ID} — HTTP $HTTP_CODE"
    fi
done < "$INPUT_FILE"

echo "---"
echo "Done. Deleted: $DELETED, Failed: $FAILED"

# Remove deleted entries from the file
if [[ ${#DELETED_IDS[@]} -gt 0 ]]; then
    for id in "${DELETED_IDS[@]}"; do
        sed -i '' "/^${id} /d" "$INPUT_FILE"
    done
    REMAINING=$(wc -l < "$INPUT_FILE" | tr -d ' ')
    if [[ "$REMAINING" -eq 0 ]]; then
        rm -f "$INPUT_FILE"
        echo "All workspaces deleted. Cleaned up $INPUT_FILE"
    else
        echo "$REMAINING workspaces remaining in $INPUT_FILE"
    fi
fi
