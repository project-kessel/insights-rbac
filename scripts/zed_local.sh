#!/usr/bin/env bash
# =============================================================================
# zed_local.sh — Zed CLI for local Docker + stage Kessel SpiceDB
#
# Pairs with scripts/validations/api/create-workspace.sh. Zed talks to SpiceDB (gRPC on
# localhost:50051), not the Inventory API (localhost:9000). Both port-forwards
# target the same stage cluster.
#
# Usage:
#   ./scripts/zed_local.sh setup              # configure zed context kessel-local
#   ./scripts/zed_local.sh ensure-forwards    # start oc port-forwards if ports closed
#   ./scripts/zed_local.sh status             # ports, zed context, credentials
#   ./scripts/zed_local.sh check rbac/workspace:<id> view rbac/principal:redhat/1111111
#   ./scripts/zed_local.sh schema             # print the schema loaded by local SpiceDB
#   ./scripts/zed_local.sh read-workspace <workspace_uuid>
#   ./scripts/zed_local.sh verify-results <results.json>
#   ./scripts/zed_local.sh <any zed subcommand...>  # passthrough with context
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PIDS_DIR="$SCRIPT_DIR/.create_workspace_pids"
CONFIG_FILE="$PROJECT_DIR/.cursor/skills/config.env"

ZED_CONTEXT_NAME="${ZED_CONTEXT_NAME:-kessel-local}"
SPICEDB_HOST="${SPICEDB_HOST:-localhost}"
SPICEDB_PORT="${SPICEDB_PORT:-50051}"
LOCAL_KESSEL_ENV_FILE="${LOCAL_KESSEL_ENV_FILE:-}"
KESSEL_HOST="${KESSEL_HOST:-localhost}"
KESSEL_PORT="${KESSEL_PORT:-9000}"
OC_PROJECT="${OC_PROJECT:-kessel-stage}"
SPICEDB_SERVICE="${SPICEDB_SERVICE:-kessel-relations-spicedb}"
INVENTORY_SERVICE="${INVENTORY_SERVICE:-kessel-inventory-api}"

# shellcheck source=common/logging.sh
source "$SCRIPT_DIR/common/logging.sh"

mkdir -p "$PIDS_DIR"

require_zed() {
    if ! command -v zed &> /dev/null; then
        log-err "zed CLI not found. Install: https://github.com/authzed/zed"
        exit 1
    fi
}

local_spicedb_is_running() {
    local runtime
    for runtime in podman docker; do
        command -v "$runtime" &>/dev/null || continue
        if [ "$("$runtime" container inspect --format '{{.State.Running}}' full-kessel-spicedb-1 2>/dev/null)" = "true" ]; then
            return 0
        fi
    done
    return 1
}

resolve_spicedb_token() {
    SPICEDB_TOKEN_SOURCE=""
    RESOLVED_SPICEDB_TOKEN=""

    if [ -n "${ZED_SPICEDB_PSK:-}" ]; then
        RESOLVED_SPICEDB_TOKEN="$ZED_SPICEDB_PSK"
        SPICEDB_TOKEN_SOURCE="ZED_SPICEDB_PSK"
        return 0
    fi

    # Do not read a local token while connected to a stage port-forward. The
    # local container check ensures this fallback is only for the Podman/Docker
    # full-Kessel stack.
    local_spicedb_is_running || return 1

    local env_file token
    local env_files=(
        "$LOCAL_KESSEL_ENV_FILE"
        "${INVENTORY_API_REPO:-}/development/full-kessel/.env"
        "$(dirname "$PROJECT_DIR")/inventory-api/development/full-kessel/.env"
        "$PROJECT_DIR/.local-deps/inventory-api/development/full-kessel/.env"
    )
    for env_file in "${env_files[@]}"; do
        [ -n "$env_file" ] && [ -r "$env_file" ] || continue
        token=$(awk -F= '$1 == "SPICEDB_GRPC_PRESHARED_KEY" {print substr($0, index($0, "=") + 1); exit}' "$env_file")
        if [ -n "$token" ]; then
            RESOLVED_SPICEDB_TOKEN="$token"
            SPICEDB_TOKEN_SOURCE="local full-Kessel configuration"
            return 0
        fi
    done
    return 1
}

port_open() {
    local host="$1"
    local port="$2"
    (echo >"/dev/tcp/${host}/${port}") >/dev/null 2>&1
}

wait_for_port() {
    local host="$1"
    local port="$2"
    local label="$3"
    local max="${4:-45}"
    local i=0
    while [ "$i" -lt "$max" ]; do
        if port_open "$host" "$port"; then
            log-info "OK: $label on ${host}:${port}"
            return 0
        fi
        i=$((i + 1))
        sleep 1
    done
    log-err "$label not reachable on ${host}:${port} after ${max}s"
    return 1
}

load_openshift_console_hint() {
    if [ -f "$CONFIG_FILE" ]; then
        # shellcheck source=/dev/null
        source "$CONFIG_FILE"
    fi
    OPENSHIFT_STAGE_CONSOLE="${OPENSHIFT_STAGE_CONSOLE:-https://console-openshift-console.apps.crc-eph.r9lp.p1.openshiftapps.com}"
}

start_port_forward() {
    local name="$1"
    local service="$2"
    local local_port="$3"
    local remote_port="$4"
    local pid_file="$PIDS_DIR/${name}.pid"
    local log_file="$PIDS_DIR/${name}.log"

    if [ -f "$pid_file" ]; then
        local old_pid
        old_pid=$(cat "$pid_file")
        if kill -0 "$old_pid" 2>/dev/null; then
            log-info "Port-forward $name already running (PID $old_pid)"
            return 0
        fi
        rm -f "$pid_file"
    fi

    if ! command -v oc &> /dev/null; then
        log-err "oc not found. Port-forward manually:"
        log-err "  oc project $OC_PROJECT"
        log-err "  oc port-forward svc/$service ${local_port}:${remote_port}"
        return 1
    fi

    if ! oc whoami &> /dev/null; then
        load_openshift_console_hint
        log-err "Not logged in to OpenShift. Get login command from:"
        log-err "  $OPENSHIFT_STAGE_CONSOLE"
        return 1
    fi

    log-info "Starting port-forward $service ${local_port}:${remote_port} (project $OC_PROJECT)..."
    oc project "$OC_PROJECT" >/dev/null
    nohup oc port-forward "svc/$service" "${local_port}:${remote_port}" >"$log_file" 2>&1 &
    echo $! >"$pid_file"
    sleep 2
}

ensure_spicedb_forward() {
    if port_open "$SPICEDB_HOST" "$SPICEDB_PORT"; then
        return 0
    fi
    start_port_forward spicedb "$SPICEDB_SERVICE" "$SPICEDB_PORT" "$SPICEDB_PORT"
    wait_for_port "$SPICEDB_HOST" "$SPICEDB_PORT" "SpiceDB (zed)" 45
}

ensure_inventory_forward() {
    if port_open "$KESSEL_HOST" "$KESSEL_PORT"; then
        return 0
    fi
    start_port_forward inventory "$INVENTORY_SERVICE" "$KESSEL_PORT" "9000"
    wait_for_port "$KESSEL_HOST" "$KESSEL_PORT" "Kessel Inventory API" 45
}

cmd_ensure_forwards() {
    ensure_inventory_forward
    ensure_spicedb_forward
}

cmd_setup() {
    require_zed

    if ! resolve_spicedb_token; then
        load_openshift_console_hint
        log-err "No local SpiceDB token or ZED_SPICEDB_PSK is available."
        log-err "Start the local full-Kessel stack, or export the stage SpiceDB PSK from Vault (see .cursor/skills/zed/SKILL.md)."
        exit 1
    fi

    ensure_spicedb_forward

    # Use environment variables instead of CLI arguments to avoid
    # exposing the PSK in process listings (/proc/*/cmdline)
    export ZED_TOKEN="$RESOLVED_SPICEDB_TOKEN"
    export ZED_ENDPOINT="${SPICEDB_HOST}:${SPICEDB_PORT}"
    export ZED_INSECURE=true
    log-info "Zed env configured from ${SPICEDB_TOKEN_SOURCE} -> ${SPICEDB_HOST}:${SPICEDB_PORT}"
}

cmd_status() {
    require_zed

    echo "Ports:"
    if port_open "$KESSEL_HOST" "$KESSEL_PORT"; then
        echo "  Inventory API (${KESSEL_HOST}:${KESSEL_PORT}): open"
    else
        echo "  Inventory API (${KESSEL_HOST}:${KESSEL_PORT}): closed"
    fi
    if port_open "$SPICEDB_HOST" "$SPICEDB_PORT"; then
        echo "  SpiceDB (${SPICEDB_HOST}:${SPICEDB_PORT}): open"
    else
        echo "  SpiceDB (${SPICEDB_HOST}:${SPICEDB_PORT}): closed"
    fi

    echo "Zed:"
    zed context list 2>/dev/null || true
    if resolve_spicedb_token; then
        echo "  SpiceDB token: ${SPICEDB_TOKEN_SOURCE}"
    else
        echo "  SpiceDB token: unavailable"
    fi

    echo "Port-forward PIDs:"
    for pidfile in "$PIDS_DIR"/*.pid; do
        [ -f "$pidfile" ] || continue
        name=$(basename "$pidfile" .pid)
        pid=$(cat "$pidfile")
        if kill -0 "$pid" 2>/dev/null; then
            echo "  $name: PID $pid (running)"
        else
            echo "  $name: PID $pid (stale)"
        fi
    done
}

cmd_read_workspace() {
    require_zed
    local workspace_id="$1"
    cmd_setup
    log-info "Relationships for rbac/workspace:${workspace_id}"
    zed relationship read "rbac/workspace:${workspace_id}"
}

cmd_verify_results() {
    require_zed
    local results_file="$1"
    if [ ! -f "$results_file" ]; then
        log-err "Results file not found: $results_file"
        exit 1
    fi

    cmd_setup

    local ids
    ids=$(python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
for r in data.get('results', []):
    if r.get('ok') and r.get('id'):
        print(r['id'])
" "$results_file")

    if [ -z "$ids" ]; then
        log-warn "No successful workspace IDs in $results_file"
        exit 1
    fi

    local failed=0
    while IFS= read -r workspace_id; do
        log-info "Verifying SpiceDB tuples for workspace ${workspace_id}..."
        if zed relationship read "rbac/workspace:${workspace_id}" | grep -q .; then
            log-info "OK: tuples found for ${workspace_id}"
        else
            log-warn "No tuples returned for ${workspace_id} (may still be replicating)"
            failed=$((failed + 1))
        fi
    done <<< "$ids"

    if [ "$failed" -gt 0 ]; then
        log-warn "$failed workspace(s) had no readable tuples in SpiceDB"
        exit 1
    fi
    log-info "All workspace(s) have SpiceDB relationships"
}

cmd_check() {
    require_zed
    if [ $# -lt 1 ]; then
        log-err "Usage: $0 check <zed permission check args...>"
        log-err "Example: $0 check rbac/workspace:<uuid> view rbac/principal:redhat/1111111"
        exit 1
    fi
    cmd_setup
    zed permission check "$@"
}

cmd_schema() {
    require_zed
    cmd_setup
    zed schema read
}

cmd_passthrough() {
    require_zed
    cmd_setup
    zed "$@"
}

show_help() {
    cat <<EOF
Usage: $0 <command> [args...]

Commands:
  setup              Configure zed context '$ZED_CONTEXT_NAME' (SpiceDB on ${SPICEDB_HOST}:${SPICEDB_PORT})
  ensure-forwards    oc port-forward Inventory API + SpiceDB if ports are closed
  status             Show port, credential, and port-forward status
  schema             Print the schema loaded by SpiceDB
  read-workspace ID  zed relationship read rbac/workspace:ID
  verify-results F   Verify workspaces from create-workspace JSON results
  check ARGS...      zed permission check ARGS (with local context)
  help               Show this help

Any other invocation runs: zed <args> with context '$ZED_CONTEXT_NAME'.

Environment:
  ZED_SPICEDB_PSK       SpiceDB PSK token for a stage port-forward
  LOCAL_KESSEL_ENV_FILE Local full-Kessel .env file containing the PSK
  ZED_CONTEXT_NAME    Zed context name (default: kessel-local)
  SPICEDB_HOST/PORT     SpiceDB port-forward target (default: localhost:50051)
  KESSEL_HOST/PORT      Inventory API port-forward (default: localhost:9000)
  OC_PROJECT            OpenShift project (default: kessel-stage)

Used with:
  ./scripts/validations/api/create-workspace.sh --zed

When full-kessel is running locally, the token is read automatically from its
.env file. No Vault setup is required.
EOF
}

main() {
    local cmd="${1:-help}"
    shift || true

    case "$cmd" in
        setup)
            cmd_setup
            ;;
        ensure-forwards)
            cmd_ensure_forwards
            ;;
        status)
            cmd_status
            ;;
        read-workspace)
            [ $# -ge 1 ] || { log-err "workspace uuid required"; exit 1; }
            cmd_read_workspace "$1"
            ;;
        verify-results)
            [ $# -ge 1 ] || { log-err "results json path required"; exit 1; }
            cmd_verify_results "$1"
            ;;
        check)
            cmd_check "$@"
            ;;
        schema)
            cmd_schema
            ;;
        help|-h|--help)
            show_help
            ;;
        *)
            cmd_passthrough "$cmd" "$@"
            ;;
    esac
}

main "$@"
