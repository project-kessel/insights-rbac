#!/usr/bin/env bash
# =============================================================================
# create-workspace.sh — Full workspace create + Read-Your-Writes (RYW) test
#
# Ensures local Docker infrastructure is running, wires RBAC to a *real* Kessel
# Inventory API (not the mock server), then creates workspace(s) via the v2 API
# and verifies the full pipeline:
#
#   Outbox → Debezium → Kafka → Consumer → Inventory API → SpiceDB → pg_notify → RYW
#
# Prerequisites (real Kessel):
#   Port-forward stage Kessel Inventory API to localhost before running, e.g.:
#     oc project kessel-stage
#     oc port-forward svc/kessel-inventory-api 9000:9000
#
#   Export OAuth credentials so the consumer can obtain JWT tokens:
#     export INVENTORY_API_CLIENT_ID=...
#     export INVENTORY_API_CLIENT_SECRET=...
#
# Usage:
#   make docker-local-up          # preferred: all services in Docker
#   ./scripts/validations/api/create-workspace.sh
#   ./scripts/validations/api/create-workspace.sh --count 1
#   ./scripts/validations/api/create-workspace.sh --no-start    # stack already up
#   ./scripts/validations/api/create-workspace.sh --user local-v2-non-admin
#   ./scripts/validations/api/create-workspace.sh --help
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
RYW_TEST_DIR="$PROJECT_DIR/scripts/local_ryw_test"
PIDS_DIR="$PROJECT_DIR/scripts/.create_workspace_pids"
LOG_DIR="$PROJECT_DIR/scripts/.create_workspace_logs"

# Defaults (override via environment)
API_URL="${API_URL:-http://localhost:9080}"
API_PORT="${API_PORT:-9080}"
API_PATH_PREFIX="${API_PATH_PREFIX:-/api/rbac}"
INVENTORY_API_ENDPOINT="${INVENTORY_API_ENDPOINT:-localhost:9081}"
CHECK_HBI="${CHECK_HBI:-auto}"
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-15432}"
KESSEL_HOST="${KESSEL_HOST:-localhost}"
KESSEL_PORT="${KESSEL_PORT:-9000}"
# Host-side Kessel address for connectivity checks
KESSEL_ADDR_HOST="${KESSEL_HOST}:${KESSEL_PORT}"
# Address RBAC containers use to reach Kessel on the host (port-forward)
KESSEL_ADDR_CONTAINER="${KESSEL_ADDR_CONTAINER:-host.docker.internal:${KESSEL_PORT}}"
WORKSPACE_COUNT="${WORKSPACE_COUNT:-1}"
READ_YOUR_WRITES_TIMEOUT_SECONDS="${READ_YOUR_WRITES_TIMEOUT_SECONDS:-30}"
RYW_USER="${RYW_USER:-local-v2-org-admin}"
NO_START=false
RUN_LISTEN=false

CONSUMER_CONTAINER_NAME="rbac_kafka_consumer_local"
RBAC_SERVER_CONTAINER="rbac_server"
ZED_LOCAL_SCRIPT="$PROJECT_DIR/scripts/zed_local.sh"

SPICEDB_HOST="${SPICEDB_HOST:-localhost}"
SPICEDB_PORT="${SPICEDB_PORT:-50051}"
RUN_ZED=false
RUN_CHECK_HBI=false
RESULTS_FILE="$LOG_DIR/workspace_results.json"

# shellcheck source=../../common/logging.sh
source "$PROJECT_DIR/scripts/common/logging.sh"

# ---------------------------------------------------------------------------
# Container runtime detection
# ---------------------------------------------------------------------------

detect_runtime() {
    # Prefer the runtime that already owns the full local stack.  In Podman
    # development environments `podman info` can be unavailable to the host
    # shell even though container commands used by the validation work.
    if command -v docker &> /dev/null && docker container inspect full-kessel-rbac-server-1 &> /dev/null 2>&1; then
        CONTAINER_RUNTIME="docker"
        if docker compose version &> /dev/null 2>&1; then
            COMPOSE_CMD="docker compose"
        else
            COMPOSE_CMD="docker-compose"
        fi
    elif command -v podman &> /dev/null && podman container inspect full-kessel-rbac-server-1 &> /dev/null 2>&1; then
        CONTAINER_RUNTIME="podman"
        COMPOSE_CMD="podman compose"
    elif command -v docker &> /dev/null && docker info &> /dev/null 2>&1; then
        CONTAINER_RUNTIME="docker"
        if docker compose version &> /dev/null 2>&1; then
            COMPOSE_CMD="docker compose"
        else
            COMPOSE_CMD="docker-compose"
        fi
    elif command -v podman &> /dev/null && podman info &> /dev/null 2>&1; then
        CONTAINER_RUNTIME="podman"
        COMPOSE_CMD="podman compose"
    else
        log-err "Docker or Podman is not running. Start Docker Desktop or podman machine."
        exit 1
    fi
    log-info "Using ${CONTAINER_RUNTIME} (${COMPOSE_CMD})"
}

detect_python() {
    if [ -n "${VIRTUAL_ENV:-}" ]; then
        PYTHON="python"
    elif PIPENV_VENV=$(cd "$PROJECT_DIR" && pipenv --venv 2>/dev/null); then
        PYTHON="$PIPENV_VENV/bin/python"
    else
        FOUND_VENV=""
        for venv_dir in "$HOME/.local/share/virtualenvs"/insights-rbac-*/; do
            if [ -x "$venv_dir/bin/python" ] && "$venv_dir/bin/python" -c "import django" 2>/dev/null; then
                FOUND_VENV="$venv_dir"
            fi
        done
        if [ -n "$FOUND_VENV" ]; then
            PYTHON="${FOUND_VENV}bin/python"
        else
            log-err "No Python virtualenv found. Run: cd $PROJECT_DIR && pipenv install --dev"
            exit 1
        fi
    fi
    log-info "Using Python: $PYTHON"
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

is_container_running() {
    $CONTAINER_RUNTIME ps --format '{{.Names}}' | grep -qx "$1"
}

is_full_kessel_stack_running() {
    if $CONTAINER_RUNTIME ps --format '{{.Names}}' | grep -qE '^full-kessel-rbac-server-'; then
        return 0
    fi
    curl -sf "http://localhost:${API_PORT}/metrics" >/dev/null 2>&1
}

is_full_kessel_database_running() {
    $CONTAINER_RUNTIME ps --format '{{.Names}}' | grep -qE '^full-kessel-rbac-database-'
}

resolve_ryw_user() {
    local user_id is_org_admin
    case "$RYW_USER" in
        local-v1-org-admin)
            user_id=local-v1-org-admin-10001
            is_org_admin=true
            ;;
        local-v1-non-org-admin)
            user_id=local-v1-non-org-admin-10001
            is_org_admin=false
            ;;
        local-v2-org-admin)
            user_id=local-v2-org-admin-10001
            is_org_admin=true
            ;;
        local-v2-non-admin)
            user_id=local-v2-non-admin-10001
            is_org_admin=false
            ;;
        *)
            log-err "Unknown user '${RYW_USER}'. Choose one of the four default full-stack users."
            exit 1
            ;;
    esac

    RYW_ORG_ID="${RYW_ORG_ID:-local-full-stack}"
    RYW_ACCOUNT_ID="${RYW_ACCOUNT_ID:-10001}"
    RYW_USERNAME="${RYW_USERNAME:-${RYW_USER}}"
    RYW_USER_ID="${RYW_USER_ID:-${user_id}}"
    RYW_IS_ORG_ADMIN="${RYW_IS_ORG_ADMIN:-${is_org_admin}}"
    export RYW_ORG_ID RYW_ACCOUNT_ID RYW_USERNAME RYW_USER_ID RYW_IS_ORG_ADMIN
    log-info "Using RYW user=${RYW_USERNAME} user_id=${RYW_USER_ID} org=${RYW_ORG_ID} org_admin=${RYW_IS_ORG_ADMIN}"
}

is_hbi_stack_running() {
    $CONTAINER_RUNTIME ps --format '{{.Names}}' | grep -qE '^hbi-kessel-local-hbi-web-'
}

resolve_check_hbi() {
    case "${CHECK_HBI}" in
        true|1|yes|on)
            RUN_CHECK_HBI=true
            ;;
        false|0|no|off)
            RUN_CHECK_HBI=false
            ;;
        auto)
            if is_hbi_stack_running && is_full_kessel_stack_running; then
                RUN_CHECK_HBI=true
                log-info "HBI + full Kessel stack detected — enabling Kessel Inventory verification"
            else
                RUN_CHECK_HBI=false
            fi
            ;;
        *)
            log-warn "Unknown CHECK_HBI value '${CHECK_HBI}'; defaulting to auto"
            CHECK_HBI=auto
            resolve_check_hbi
            ;;
    esac
}

wait_for_port() {
    local host="$1"
    local port="$2"
    local label="$3"
    local max_attempts="${4:-60}"
    local attempt=0
    while [ "$attempt" -lt "$max_attempts" ]; do
        if (echo >"/dev/tcp/${host}/${port}") >/dev/null 2>&1; then
            log-info "OK: $label is reachable on ${host}:${port}"
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    log-err "$label not reachable on ${host}:${port} after ${max_attempts}s"
    return 1
}

wait_for_http() {
    local url="$1"
    local label="$2"
    local max_attempts="${3:-60}"
    local attempt=0
    while [ "$attempt" -lt "$max_attempts" ]; do
        if curl -sf "$url" > /dev/null 2>&1; then
            log-info "$label is ready ($url)"
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    log-err "$label not ready ($url) after ${max_attempts}s"
    return 1
}

docker_host_gateway_flag() {
    # Linux needs explicit host-gateway mapping for host.docker.internal
    if [ "$CONTAINER_RUNTIME" = "docker" ] && [ "$(uname -s)" = "Linux" ]; then
        echo "--add-host=host.docker.internal:host-gateway"
    fi
}

# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------

ensure_network() {
    if ! $CONTAINER_RUNTIME network ls --format '{{.Name}}' | grep -qx rbac-network; then
        log-info "Creating Docker network rbac-network..."
        $CONTAINER_RUNTIME network create rbac-network
    fi
    log-info "Network rbac-network exists"
}

ensure_database() {
    log-info "Checking PostgreSQL (rbac_db on port ${DB_PORT})..."

    if ! is_container_running rbac_db; then
        if $CONTAINER_RUNTIME ps -a --format '{{.Names}}' | grep -qx rbac_db; then
            log-info "Starting existing rbac_db container..."
            $CONTAINER_RUNTIME start rbac_db
        else
            log-info "Starting PostgreSQL via make start-db..."
            (cd "$PROJECT_DIR" && make start-db)
        fi
        sleep 3
    else
        log-info "rbac_db already running"
    fi

    if ! $CONTAINER_RUNTIME network inspect rbac-network 2>/dev/null | grep -q rbac_db; then
        log-info "Connecting rbac_db to rbac-network..."
        $CONTAINER_RUNTIME network connect rbac-network rbac_db 2>/dev/null || true
    fi

    wait_for_port "$DB_HOST" "$DB_PORT" "PostgreSQL" 45

    log-info "Configuring logical replication on PostgreSQL..."
    $CONTAINER_RUNTIME exec rbac_db psql -U postgres -c "ALTER SYSTEM SET wal_level = logical;" > /dev/null 2>&1 || true
    $CONTAINER_RUNTIME exec rbac_db psql -U postgres -c "ALTER SYSTEM SET max_replication_slots = 4;" > /dev/null 2>&1 || true
    $CONTAINER_RUNTIME exec rbac_db psql -U postgres -c "ALTER SYSTEM SET max_wal_senders = 4;" > /dev/null 2>&1 || true

    local wal_level
    wal_level=$($CONTAINER_RUNTIME exec rbac_db psql -U postgres -t -c "SHOW wal_level;" | xargs)
    if [ "$wal_level" != "logical" ]; then
        log-warn "wal_level is '$wal_level'; restarting PostgreSQL..."
        $CONTAINER_RUNTIME restart rbac_db
        sleep 3
        wait_for_port "$DB_HOST" "$DB_PORT" "PostgreSQL" 45
    fi
    log-info "PostgreSQL logical replication enabled"
}

ensure_redis() {
    log-info "Checking Redis..."

    if ! is_container_running rbac_redis; then
        log-info "Starting Redis container..."
        (cd "$PROJECT_DIR" && $COMPOSE_CMD up -d --no-deps redis)
        sleep 2
    else
        log-info "rbac_redis already running"
    fi

    if ! $CONTAINER_RUNTIME network inspect rbac-network 2>/dev/null | grep -q rbac_redis; then
        $CONTAINER_RUNTIME network connect rbac-network rbac_redis 2>/dev/null || true
    fi

    local attempt=0
    while [ "$attempt" -lt 30 ]; do
        if $CONTAINER_RUNTIME exec rbac_redis redis-cli ping 2>/dev/null | grep -q PONG; then
            log-info "Redis is ready"
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    log-err "Redis failed to become ready"
    exit 1
}

build_rbac_image() {
    log-info "Building rbac-server image if needed..."
    local build_log
    build_log=$(mktemp)
    if ! (cd "$PROJECT_DIR" && $COMPOSE_CMD build rbac-server >"$build_log" 2>&1); then
        log-err "Failed to build rbac-server image:"
        cat "$build_log"
        rm -f "$build_log"
        exit 1
    fi
    rm -f "$build_log"
}

run_migrations() {
    log-info "Running database migrations and seeds..."

    build_rbac_image

    local gateway_flag
    gateway_flag=$(docker_host_gateway_flag)

    (cd "$PROJECT_DIR" && $COMPOSE_CMD -f docker-compose.yml run --rm --no-deps \
        $gateway_flag \
        -e DATABASE_HOST=db \
        -e POSTGRES_SQL_SERVICE_HOST=db \
        -e DATABASE_PORT=5432 \
        -e DATABASE_USER=postgres \
        -e DATABASE_PASSWORD=postgres \
        -e DATABASE_NAME=postgres \
        -e REDIS_HOST=redis \
        -e DJANGO_LOG_HANDLERS=console \
        -e DJANGO_LOG_FILE=/tmp/migrate.log \
        rbac-server python rbac/manage.py migrate --noinput)

    (cd "$PROJECT_DIR" && $COMPOSE_CMD -f docker-compose.yml run --rm --no-deps \
        $gateway_flag \
        -e DATABASE_HOST=db \
        -e POSTGRES_SQL_SERVICE_HOST=db \
        -e DATABASE_PORT=5432 \
        -e DATABASE_USER=postgres \
        -e DATABASE_PASSWORD=postgres \
        -e DATABASE_NAME=postgres \
        -e REDIS_HOST=redis \
        -e DJANGO_LOG_HANDLERS=console \
        -e DJANGO_LOG_FILE=/tmp/seeds.log \
        rbac-server python rbac/manage.py seeds)

    log-info "Migrations and seeds complete"
}

ensure_kafka_debezium() {
    log-info "Checking Kafka + Debezium stack..."

    if ! is_container_running insights-rbac-kafka-1; then
        log-info "Starting Kafka, Zookeeper, Kafka Connect, Kafdrop..."
        (cd "$PROJECT_DIR" && $COMPOSE_CMD -f docker-compose.debezium-local.yml up -d)
    else
        log-info "Kafka stack already running"
    fi

    wait_for_http "http://localhost:8083/connectors" "Kafka Connect" 90

    # Create topic if missing
    $CONTAINER_RUNTIME exec insights-rbac-kafka-1 kafka-topics \
        --bootstrap-server localhost:9092 --create \
        --topic outbox.event.relations-replication-event \
        --partitions 1 --replication-factor 1 2>/dev/null || true

    log-info "Kafka topic ready"
}

ensure_debezium_connector() {
    log-info "Checking Debezium connector..."

    local connector_json="$RYW_TEST_DIR/debezium-connector-local.json"

    if curl -s http://localhost:8083/connectors 2>/dev/null | grep -q "rbac-debezium"; then
        local status
        status=$(curl -s http://localhost:8083/connectors/rbac-debezium/status)
        if echo "$status" | grep -q '"state":"FAILED"'; then
            log-warn "Debezium connector FAILED — recreating..."
            curl -s -X DELETE http://localhost:8083/connectors/rbac-debezium > /dev/null
            sleep 3
        else
            log-info "Debezium connector already running"
            return 0
        fi
    fi

    local response
    response=$(curl -s -X POST http://localhost:8083/connectors \
        -H "Content-Type: application/json" \
        -d @"$connector_json")

    if echo "$response" | grep -q '"name":"rbac-debezium"'; then
        log-info "Debezium connector created"
    else
        log-err "Failed to create Debezium connector"
        echo "$response"
        exit 1
    fi

    sleep 5
}

check_real_kessel() {
    if is_container_running rbac_local_mock_kessel 2>/dev/null; then
        log-info "Docker local stack detected (mock Kessel) — skipping stage port-forwards"
        return 0
    fi

    if is_full_kessel_stack_running; then
        log-info "Full Kessel local stack detected — using local Inventory API (${INVENTORY_API_ENDPOINT}) and SpiceDB"
        wait_for_port "${INVENTORY_API_ENDPOINT%:*}" "${INVENTORY_API_ENDPOINT##*:}" "Kessel Inventory API" 10 || true
        wait_for_port "$SPICEDB_HOST" "$SPICEDB_PORT" "SpiceDB (zed)" 10 || true
        return 0
    fi

    log-info "Checking Kessel port-forwards (Inventory API + SpiceDB for zed)..."

    if [ -x "$ZED_LOCAL_SCRIPT" ]; then
        "$ZED_LOCAL_SCRIPT" ensure-forwards || {
            log-err "Could not open Kessel / SpiceDB ports."
            log-err "Run manually or: $ZED_LOCAL_SCRIPT ensure-forwards"
            exit 1
        }
    else
        if ! wait_for_port "$KESSEL_HOST" "$KESSEL_PORT" "Kessel Inventory API" 5; then
            log-err "Cannot reach Kessel Inventory API on ${KESSEL_ADDR_HOST}."
            log-err "Port-forward the Inventory API before running this script, e.g.:"
            log-err "  oc project kessel-stage"
            log-err "  oc port-forward svc/kessel-inventory-api ${KESSEL_PORT}:9000"
            exit 1
        fi
        if ! wait_for_port "$SPICEDB_HOST" "$SPICEDB_PORT" "SpiceDB (zed)" 3; then
            log-warn "SpiceDB not on ${SPICEDB_HOST}:${SPICEDB_PORT} — zed checks will not work until you port-forward:"
            log-warn "  oc port-forward svc/kessel-relations-spicedb ${SPICEDB_PORT}:50051"
        fi
    fi

    if [ -z "${INVENTORY_API_CLIENT_ID:-}" ] || [ -z "${INVENTORY_API_CLIENT_SECRET:-}" ]; then
        log-warn "INVENTORY_API_CLIENT_ID / INVENTORY_API_CLIENT_SECRET not set."
        log-warn "The Kafka consumer may fail JWT auth against stage Kessel without OAuth credentials."
        log-warn "Export credentials from Vault or your SSO client registration."
    else
        log-info "Kessel OAuth credentials are set"
    fi
}

# Shared RBAC runtime environment for Docker containers
rbac_container_env() {
    cat <<EOF
DEVELOPMENT=True
V2_APIS_ENABLED=True
V2_READ_ONLY_API_MODE=False
V2_BOOTSTRAP_TENANT=True
REPLICATION_TO_RELATION_ENABLED=true
READ_YOUR_WRITES_WORKSPACE_ENABLED=True
READ_YOUR_WRITES_TIMEOUT_SECONDS=${READ_YOUR_WRITES_TIMEOUT_SECONDS}
INVENTORY_API_SERVER=${KESSEL_ADDR_CONTAINER}
INVENTORY_API_CLIENT_ID=${INVENTORY_API_CLIENT_ID:-}
INVENTORY_API_CLIENT_SECRET=${INVENTORY_API_CLIENT_SECRET:-}
BYPASS_BOP_VERIFICATION=True
PRINCIPAL_USER_DOMAIN=redhat
WORKSPACE_HIERARCHY_DEPTH_LIMIT=5
WORKSPACE_ORG_CREATION_LIMIT=3000
SYSTEM_DEFAULT_ROOT_WORKSPACE_ROLE_UUID=e31b93d4-8570-4cfe-a79f-8421560e1487
SYSTEM_DEFAULT_TENANT_ROLE_UUID=1607fbde-7781-436e-8860-ef73b83c9aa1
SYSTEM_ADMIN_ROOT_WORKSPACE_ROLE_UUID=2c2d2bb1-24dc-42ca-b739-7c59ecd4a9ab
SYSTEM_ADMIN_TENANT_ROLE_UUID=51b5fd3b-9733-46e8-bbe2-f77f99871d49
DJANGO_LOG_HANDLERS=console
DJANGO_LOG_FILE=/tmp/rbac_local.log
DATABASE_HOST=db
POSTGRES_SQL_SERVICE_HOST=db
DATABASE_PORT=5432
DATABASE_USER=postgres
DATABASE_PASSWORD=postgres
DATABASE_NAME=postgres
REDIS_HOST=redis
KAFKA_ENABLED=false
EOF
}

ensure_rbac_server() {
    log-info "Starting RBAC API server (Docker, port ${API_PORT}) with RYW + real Kessel..."

    if is_container_running "$RBAC_SERVER_CONTAINER"; then
        log-info "Removing existing $RBAC_SERVER_CONTAINER to apply RYW/Kessel settings..."
        $CONTAINER_RUNTIME stop "$RBAC_SERVER_CONTAINER" 2>/dev/null || true
        $CONTAINER_RUNTIME rm "$RBAC_SERVER_CONTAINER" 2>/dev/null || true
    fi

    build_rbac_image

    local gateway_flag
    gateway_flag=$(docker_host_gateway_flag)

    log-info "Starting rbac_server container (real Kessel at ${KESSEL_ADDR_CONTAINER})..."

    (
        env_file=$(mktemp)
        trap 'rm -f "$env_file"' EXIT
        rbac_container_env > "$env_file"

        # shellcheck disable=SC2086
        cd "$PROJECT_DIR" && $COMPOSE_CMD -f docker-compose.yml run --no-deps -d \
            --name "$RBAC_SERVER_CONTAINER" \
            -p "${API_PORT}:8080" \
            $gateway_flag \
            --env-file "$env_file" \
            rbac-server
    )

    wait_for_http "http://localhost:${API_PORT}/metrics" "RBAC API" 90
}

ensure_kafka_consumer() {
    log-info "Checking RBAC Kafka consumer..."

    if is_container_running "$CONSUMER_CONTAINER_NAME"; then
        if $CONTAINER_RUNTIME exec "$CONSUMER_CONTAINER_NAME" pgrep -f launch-rbac-kafka-consumer >/dev/null 2>&1; then
            log-info "Kafka consumer already running in $CONSUMER_CONTAINER_NAME"
            return 0
        fi
        log-warn "Stale consumer container found — removing..."
        $CONTAINER_RUNTIME rm -f "$CONSUMER_CONTAINER_NAME" >/dev/null 2>&1 || true
    fi

    local gateway_flag
    gateway_flag=$(docker_host_gateway_flag)

    log-info "Starting Kafka consumer (writes tuples to real Kessel at ${KESSEL_ADDR_CONTAINER})..."

    mkdir -p "$LOG_DIR"

    (
        env_file=$(mktemp)
        trap 'rm -f "$env_file"' EXIT
        rbac_container_env > "$env_file"
        {
            echo "KAFKA_ENABLED=True"
            echo "RBAC_KAFKA_CUSTOM_CONSUMER_BROKER=kafka:9092"
            echo "RBAC_KAFKA_CONSUMER_TOPIC=outbox.event.relations-replication-event"
            echo "RBAC_KAFKA_CONSUMER_GROUP_ID=rbac-consumer-group-local"
        } >> "$env_file"

        # shellcheck disable=SC2086
        cd "$PROJECT_DIR" && $COMPOSE_CMD -f docker-compose.yml run --no-deps -d \
            --name "$CONSUMER_CONTAINER_NAME" \
            $gateway_flag \
            --env-file "$env_file" \
            rbac-server python rbac/manage.py launch-rbac-kafka-consumer
    )

    sleep 5

    if is_container_running "$CONSUMER_CONTAINER_NAME"; then
        log-info "Kafka consumer started (docker logs -f $CONSUMER_CONTAINER_NAME)"
    else
        log-err "Kafka consumer failed to start"
        exit 1
    fi
}

wait_for_full_kessel_consumer() {
    local timeout="${FULL_KESSEL_CONSUMER_WAIT_SECONDS:-30}"
    local elapsed=0
    local container_names
    local container_name

    while [ "$elapsed" -lt "$timeout" ]; do
        container_names=$($CONTAINER_RUNTIME ps --format '{{.Names}}')
        while IFS= read -r container_name; do
            if [[ "$container_name" == full-kessel-rbac-kafka-consumer-* ]]; then
                return 0
            fi
        done <<< "$container_names"

        sleep 1
        elapsed=$((elapsed + 1))
    done

    return 1
}

run_workspace_create_test() {
    log-info "Creating ${WORKSPACE_COUNT} workspace(s) and waiting for RYW..."

    local args=()
    if [ "$RUN_LISTEN" = true ]; then
        args+=("--listen")
    fi
    if [ "$RUN_ZED" = true ]; then
        args+=("--save-results" "$RESULTS_FILE")
    fi
    if [ "$RUN_CHECK_HBI" = true ]; then
        log-warn "Kessel Inventory verification is not supported by test_ryw.py; running the RYW check only"
    fi

    cd "$PROJECT_DIR"
    local test_args=(
        "$PYTHON" "$RYW_TEST_DIR/test_ryw.py"
        --api-url "$API_URL" \
        --api-path-prefix "$API_PATH_PREFIX" \
        --db-host "$DB_HOST" \
        --db-port "$DB_PORT" \
        --count "$WORKSPACE_COUNT"
    )
    if [ "${#args[@]}" -gt 0 ]; then
        test_args+=("${args[@]}")
    fi
    "${test_args[@]}"
}

run_zed_verification() {
    if [ "$RUN_ZED" != true ]; then
        return 0
    fi

    log-info "Running zed SpiceDB verification..."

    if [ ! -f "$RESULTS_FILE" ]; then
        log-warn "No results file at $RESULTS_FILE (workspace create may have failed)"
        return 1
    fi

    if [ -x "$ZED_LOCAL_SCRIPT" ]; then
        "$ZED_LOCAL_SCRIPT" verify-results "$RESULTS_FILE"
    else
        log-err "zed_local.sh not found at $ZED_LOCAL_SCRIPT"
        return 1
    fi
}

show_help() {
    cat <<EOF
Usage: $0 [OPTIONS]

Full local workspace create test with real Kessel and RYW.

Options:
  --count N       Number of workspaces to create (default: 1)
  --listen        Also run independent PostgreSQL LISTEN for pg_notify
  --zed           After create, verify tuples in SpiceDB with zed (local stack token is automatic)
  --check-hbi     Request HBI verification (currently informational; RYW only)
  --no-check-hbi  Skip the informational HBI check request
  --no-start      Skip Docker bootstrap (assume services already running)
  --user USER     Use one of the default full-stack users (default: local-v2-org-admin)
  --help          Show this help

Environment:
  KESSEL_HOST              Host for Kessel Inventory API port-forward (default: localhost)
  KESSEL_PORT              Kessel Inventory API port (default: 9000)
  SPICEDB_HOST/PORT         SpiceDB for zed (default: localhost:50051)
  KESSEL_ADDR_CONTAINER    Address containers use (default: host.docker.internal:\$KESSEL_PORT)
  API_URL                  RBAC API URL (default: http://localhost:9080)
  API_PATH_PREFIX          API path prefix (default: /api/rbac)
  INVENTORY_API_ENDPOINT   Kessel Inventory gRPC host:port for HBI check (default: localhost:9081)
  CHECK_HBI                auto|true|false — verify workspace in Kessel Inventory (default: auto)
  FULL_KESSEL_CONSUMER_WAIT_SECONDS
                           wait for the full-stack Kafka consumer (default: 30)
  INVENTORY_API_CLIENT_ID  OAuth client id for Kessel JWT (required for stage)
  INVENTORY_API_CLIENT_SECRET  OAuth client secret for Kessel JWT
  ZED_SPICEDB_PSK          SpiceDB PSK for a stage zed context (local stack token is automatic)

Prerequisites:
  Option A (all-in-docker, recommended):
    make docker-local-up
    ./scripts/validations/api/create-workspace.sh --no-start

  Option A2 (full Kessel + Debezium + RBAC + HBI):
    make docker-local-full-up rbac=local rbac-config=upstream
    ./scripts/validations/api/create-workspace.sh --no-start --count 1

  Option B (stage Kessel via port-forward):
    ./scripts/zed_local.sh ensure-forwards
    export INVENTORY_API_CLIENT_ID / INVENTORY_API_CLIENT_SECRET
    pipenv install --dev

Zed (SpiceDB):
  ./scripts/zed_local.sh setup
  ./scripts/zed_local.sh check rbac/workspace:<uuid> view rbac/principal:redhat/1111111
  ./scripts/zed_local.sh read-workspace <uuid>

Monitoring:
  Kafdrop:        http://localhost:9001
  Kafka Connect:  http://localhost:8083
  Consumer logs:  docker logs -f $CONSUMER_CONTAINER_NAME
EOF
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --count)
                WORKSPACE_COUNT="$2"
                shift 2
                ;;
            --listen)
                RUN_LISTEN=true
                shift
                ;;
            --zed)
                RUN_ZED=true
                shift
                ;;
            --check-hbi)
                CHECK_HBI=true
                shift
                ;;
            --no-check-hbi)
                CHECK_HBI=false
                shift
                ;;
            --no-start)
                NO_START=true
                shift
                ;;
            --user)
                [[ $# -ge 2 ]] || { log-err "--user requires a value"; exit 1; }
                RYW_USER="$2"
                shift 2
                ;;
            --help|-h)
                show_help
                exit 0
                ;;
            *)
                log-err "Unknown option: $1"
                show_help
                exit 1
                ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    parse_args "$@"

    log-info "========================================="
    log-info "  Workspace create + RYW (real Kessel)"
    log-info "========================================="

    detect_runtime
    detect_python
    resolve_ryw_user
    resolve_check_hbi
    mkdir -p "$LOG_DIR" "$PIDS_DIR"

    if [ "$NO_START" = false ] && is_full_kessel_stack_running; then
        log-info "Full Kessel local stack already running — reusing it instead of bootstrapping standalone RBAC services"
        NO_START=true
    elif [ "$NO_START" = false ] && is_full_kessel_database_running; then
        log-info "Full Kessel database detected — waiting for the full stack instead of starting legacy rbac_db"
        NO_START=true
    fi

    if [ "$NO_START" = false ]; then
        if is_container_running rbac_local_server 2>/dev/null; then
            log-info "docker-compose.local stack already running — use --no-start to skip bootstrap"
        fi
        ensure_network
        ensure_database
        ensure_redis
        run_migrations
        ensure_kafka_debezium
        ensure_debezium_connector
        check_real_kessel
        ensure_rbac_server
        ensure_kafka_consumer
    else
        log-info "Skipping infrastructure bootstrap (--no-start)"
        check_real_kessel
        local api_ready_timeout=30
        if is_full_kessel_stack_running; then
            api_ready_timeout=120
        fi
        wait_for_http "http://localhost:${API_PORT}/metrics" "RBAC API" "$api_ready_timeout"
        if is_full_kessel_stack_running; then
            if ! wait_for_full_kessel_consumer; then
                log-warn "full-kessel RBAC Kafka consumer not running — RYW may fail"
            fi
        elif ! is_container_running "$CONSUMER_CONTAINER_NAME"; then
            log-warn "Kafka consumer not running — start with full bootstrap or run ensure_kafka_consumer manually"
        fi
    fi

    sleep 2
    if ! run_workspace_create_test; then
        log-warn "Workspace create test reported failures (see output above)"
    fi
    run_zed_verification || true

    log-info "========================================="
    if is_full_kessel_stack_running; then
        log-info "  Consumer: podman logs -f full-kessel-rbac-kafka-consumer-1"
    else
        log-info "  Done. Consumer: podman logs -f $CONSUMER_CONTAINER_NAME"
    fi
    log-info "  Zed: $ZED_LOCAL_SCRIPT status"
    if is_full_kessel_stack_running; then
        log-info "  Kafka Connect: http://localhost:8083"
    else
        log-info "  Kafdrop: http://localhost:9001"
    fi
    log-info "========================================="
}

main "$@"
