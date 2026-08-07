#!/bin/bash
# RBAC Disaster Recovery Toolkit
#
# Toolkit for testing and simulating RBAC disaster recovery scenarios on
# ephemeral clusters. Covers two recovery paths:
#
#   rbac-kessel   RBAC ↔ Kessel Relations out-of-sync after DB restore
#   rbac-hbi      RBAC ↔ HBI (Host-Based Inventory) out-of-sync after DB restore
#
# Each scenario follows five phases:
#   1. setup      — create test data (workspaces, role bindings, etc.)
#   2. simulate   — simulate a DB restore (add + remove records directly in DB)
#   3. pre-check  — confirm the two systems are out of sync
#   4. fix        — run the corrective operation
#   5. post-check — confirm the two systems are back in sync
#   +  cleanup    — remove all test data and reset state
#
# Usage:
#   DR_STEP=setup ./scripts/ephemeral/rbac-dr-toolkit.sh --rbac-kessel
#   DR_STEP=setup ./scripts/ephemeral/rbac-dr-toolkit.sh --rbac-hbi
#
# Other utility commands:
#   --workspaces, --replication, --create-workspace, --delete-workspace-db,
#   --bootstrap-tenant, --run-seeds, --watch-worker, --watch-server
#
# Prerequisites:
#   oc, curl, jq

set -euo pipefail

# ── Environment setup ─────────────────────────────────────────────────────────

EPHEMERAL_NAMESPACE=$(oc project -q 2>/dev/null || true)
BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME="${BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME:-jdoe}"
EPHEMERAL_PASSWORD=$(oc get secret "env-${EPHEMERAL_NAMESPACE:-none}-keycloak" -o json 2>/dev/null \
  | jq -r '.data.defaultPassword // empty' | base64 -d 2>/dev/null || true)
EPHEMERAL_HOST_NAME=$(oc get frontendenvironment "env-${EPHEMERAL_NAMESPACE:-none}" -o json 2>/dev/null \
  | jq -r '.spec.hostname // empty' 2>/dev/null || true)
BENTO_URL=https://${EPHEMERAL_HOST_NAME}

# ── Config (overridable via env) ──────────────────────────────────────────────

RBAC_PSK_SECRET_NAME="${RBAC_PSK_SECRET_NAME:-rbac-psks}"
# ClowdApp labels: pod=<clowdapp-name>-<deployment-name>
RBAC_SERVICE_POD_LABEL="${RBAC_SERVICE_POD_LABEL:-pod=rbac-service}"
RBAC_WORKER_POD_LABEL="${RBAC_WORKER_POD_LABEL:-pod=rbac-worker-service}"
# ClowdApp DB pod label: pod=<clowdapp-name>-db
RBAC_DB_POD_LABEL="${RBAC_DB_POD_LABEL:-pod=rbac-db}"

# ── Colors ────────────────────────────────────────────────────────────────────
# Use colors only when stdout is a terminal (not piped/redirected).

if [[ -t 1 ]]; then
  RED='\033[0;31m'
  GREEN='\033[0;32m'
  YELLOW='\033[0;33m'
  CYAN='\033[0;36m'
  BOLD='\033[1m'
  DIM='\033[2m'
  NC='\033[0m'  # No Color / reset
else
  RED='' GREEN='' YELLOW='' CYAN='' BOLD='' DIM='' NC=''
fi

# ── Helpers ───────────────────────────────────────────────────────────────────

err()  { echo -e "${RED}[ERROR]${NC} $*" >&2; }
info() { echo -e "${CYAN}[INFO]${NC}  $*" >&2; }
good() { echo -e "  ${GREEN}[GOOD]${NC} $*"; }
bad()  { echo -e "  ${RED}[BAD]${NC}  $*"; }
warn() { echo -e "  ${YELLOW}[WARN]${NC} $*"; }
banner_ok()   { echo -e "${GREEN}${BOLD}$*${NC}"; }
banner_fail() { echo -e "${RED}${BOLD}$*${NC}"; }
banner_dim()  { echo -e "${DIM}$*${NC}"; }

check_deps() {
  for cmd in oc curl jq; do
    if ! command -v "$cmd" &>/dev/null; then
      err "Required command not found: $cmd"
      exit 1
    fi
  done
}

check_bonfire_env() {
  if [[ -z "${EPHEMERAL_NAMESPACE}" ]]; then
    err "Not logged into an OpenShift cluster or no project selected."
    err "Run: oc login ... && oc project <namespace>"
    exit 1
  fi

  if ! oc get namespace "${EPHEMERAL_NAMESPACE}" &>/dev/null; then
    err "Namespace '${EPHEMERAL_NAMESPACE}' does not exist. Has the bonfire environment expired?"
    err "Reserve a new one with: bonfire deploy rbac -n <namespace>"
    exit 1
  fi

  if ! oc get frontendenvironment "env-${EPHEMERAL_NAMESPACE}" &>/dev/null; then
    err "No bonfire environment found in namespace '${EPHEMERAL_NAMESPACE}'."
    err "The FrontendEnvironment CRD 'env-${EPHEMERAL_NAMESPACE}' is missing."
    err "Deploy with: bonfire deploy rbac -n ${EPHEMERAL_NAMESPACE}"
    exit 1
  fi

  local pod_count
  pod_count=$(oc get pods -l "${RBAC_SERVICE_POD_LABEL}" --field-selector=status.phase=Running \
              -o name 2>/dev/null | wc -l | tr -d ' ')
  if [[ "${pod_count}" -eq 0 ]]; then
    err "No running RBAC service pods found (label: ${RBAC_SERVICE_POD_LABEL})."
    err "Check pod status: oc get pods -l ${RBAC_SERVICE_POD_LABEL}"
    exit 1
  fi
}

get_pod_by_label() {
  local label="$1"
  oc get pods -l "${label}" \
    --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null
}

# Reads PSK credentials from the pod's live SERVICE_PSKS env var.
# This is more reliable than reading the k8s secret because ClowdApp injects
# secrets at pod startup — the k8s secret may have been rotated since then.
# Sets CLIENT_ID and PSK_SECRET in the caller's scope.
get_psk_credentials() {
  local pod="${1:-}"
  if [[ -z "$pod" ]]; then
    pod=$(get_pod_by_label "${RBAC_SERVICE_POD_LABEL}")
  fi
  if [[ -z "$pod" ]]; then
    err "No running RBAC service pod found (label: ${RBAC_SERVICE_POD_LABEL})"
    return 1
  fi

  local raw
  raw=$(oc exec "${pod}" -- sh -c 'echo "$SERVICE_PSKS"' 2>/dev/null)
  if [[ -z "$raw" || "$raw" == "null" ]]; then
    err "SERVICE_PSKS env var is empty in pod '${pod}'"
    return 1
  fi

  # Pick the first entry that has 'secret', fall back to 'alt-secret'.
  CLIENT_ID=$(echo "$raw" | jq -r '
    to_entries
    | map(select(.value.secret != null)) [0].key
    // (to_entries | map(select(.value["alt-secret"] != null)) [0].key)
    // empty
  ')
  if [[ -z "$CLIENT_ID" || "$CLIENT_ID" == "null" ]]; then
    err "No client_id with a usable secret found in SERVICE_PSKS. Raw content:"
    echo "$raw" | jq . >&2
    return 1
  fi

  PSK_SECRET=$(echo "$raw" | jq -r --arg k "$CLIENT_ID" '
    .[$k].secret // .[$k]["alt-secret"]
  ')

  if [[ -z "$PSK_SECRET" || "$PSK_SECRET" == "null" ]]; then
    err "client_id '${CLIENT_ID}' found but has no usable secret value."
    return 1
  fi
}

# Show the identity header that will be sent for internal requests.
debug_psk() {
  info "Auth method for /_private/api/: X-RH-Identity (type=Associate)"
  info "Auth method for /_private/_s2s/: PSK headers"
  echo ""
  echo "=== X-RH-Identity header that will be sent ==="
  local header
  header=$(build_internal_identity_header)
  echo "${header}" | base64 -d | jq .
  echo ""
  echo "(base64 value): ${header}"
}

# Build a base64-encoded X-RH-Identity header for internal /_private/api/ requests.
# The internal middleware (InternalIdentityHeaderMiddleware) requires type "Associate"
# or "X509" — it does NOT accept PSK on this path (PSK is only for /_private/_s2s/).
build_internal_identity_header() {
  local identity_json
  identity_json=$(jq -nc \
    --arg email "admin@redhat.com" \
    --arg org_id "${EPHEMERAL_NAMESPACE}" \
    '{
      identity: {
        type: "Associate",
        associate: { email: $email },
        account_number: "000000",
        org_id: $org_id
      }
    }')
  echo -n "${identity_json}" | base64
}

# Runs curl inside the RBAC service pod using X-RH-Identity (Associate) auth.
# Prints the response body to stdout; returns non-zero on HTTP error.
# Usage: exec_private_curl <method> <path> [extra curl args...]
exec_private_curl() {
  local method="$1" path="$2"; shift 2

  local pod
  pod=$(get_pod_by_label "${RBAC_SERVICE_POD_LABEL}")
  if [[ -z "$pod" ]]; then
    err "No running RBAC service pod found (label: ${RBAC_SERVICE_POD_LABEL})"
    err "Hint: oc get pods -l pod=rbac-service"
    return 1
  fi
  info "  Using pod: ${pod}"

  # RBAC service inside the ClowdApp pod listens on port 8000.
  local url="http://localhost:8000${path}"

  local identity_header
  identity_header=$(build_internal_identity_header)

  # Capture body + HTTP code in a single exec.
  # curl appends "\n<code>" as the last line via -w; we split on the final newline.
  local raw
  raw=$(oc exec "${pod}" -- curl -s \
    -w $'\n%{http_code}' \
    -X "${method}" \
    -H "X-RH-Identity: ${identity_header}" \
    "${url}" "$@" 2>/dev/null)

  local http_code body
  http_code=$(echo "${raw}" | tail -1)
  body=$(echo "${raw}" | sed '$d')

  if [[ "${http_code}" -lt 200 || "${http_code}" -ge 300 ]]; then
    err "Request failed: HTTP ${http_code}"
    err "Response body: ${body}"
    return 1
  fi

  echo "${body}"
}

# ── Workspace listing (public v2 API) ─────────────────────────────────────────

list_workspaces() {
  info "Listing workspaces via public v2 API..."
  info "  URL: ${BENTO_URL}/api/rbac/v2/workspaces/"

  local response
  response=$(curl -sf \
    -u "${BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME}:${EPHEMERAL_PASSWORD}" \
    "${BENTO_URL}/api/rbac/v2/workspaces/") || {
      err "Failed to fetch workspaces"
      return 1
    }

  echo ""
  echo "=== Workspaces ==="
  echo "$response" | jq .
}

# ── Replication data (internal endpoint) ─────────────────────────────────────

fetch_replication_data() {
  info "Fetching replication data via internal endpoint..."

  local response
  response=$(exec_private_curl GET /_private/api/utils/fetch_replication_data/) || return 1

  echo ""
  echo "=== Replication Data ==="
  if echo "$response" | jq . 2>/dev/null; then
    :
  else
    echo "(response is not JSON — raw output below)"
    echo "$response"
  fi
}

# ── Bootstrap tenant ──────────────────────────────────────────────────────────
# POST /_private/api/utils/bootstrap_tenant/
# Body: {"org_ids": ["<id1>", "<id2>"]}
# Optional query params: ?force=false&force_admin_only=false
#
# This call is synchronous — the HTTP response returns once all org_ids are
# bootstrapped. The OutboxReplicator writes events to the outbox table, which
# Debezium then replicates to Kessel asynchronously in the background.
# To see what the RBAC service logs during bootstrap, use --watch-server in
# a separate terminal before calling --bootstrap-tenant.

bootstrap_tenant() {
  if [[ $# -eq 0 ]]; then
    err "Usage: $0 --bootstrap-tenant ORG_ID [ORG_ID ...]"
    err "  Optional env vars:"
    err "    BOOTSTRAP_FORCE=true|false (default: false)"
    err "    BOOTSTRAP_FORCE_ADMIN_ONLY=true|false (default: false)"
    return 1
  fi

  local force="${BOOTSTRAP_FORCE:-false}"
  local force_admin_only="${BOOTSTRAP_FORCE_ADMIN_ONLY:-false}"

  # Build JSON array of org_ids from positional args.
  local org_ids_json
  org_ids_json=$(printf '%s\n' "$@" | jq -R . | jq -sc .)

  local body="{\"org_ids\": ${org_ids_json}}"
  local query="?force=${force}&force_admin_only=${force_admin_only}"
  local path="/_private/api/utils/bootstrap_tenant/${query}"

  info "Bootstrapping tenants: $*"
  info "  force=${force}  force_admin_only=${force_admin_only}"
  info "  Endpoint: ${path}"
  echo ""

  local response
  response=$(exec_private_curl POST "${path}" \
    -H "Content-Type: application/json" \
    -d "${body}") || {
      err "bootstrap_tenant request failed"
      return 1
    }

  echo ""
  echo "=== Bootstrap Result ==="
  echo "${response}"
  echo ""
  info "Tip: run --watch-server in a separate terminal to follow RBAC server logs"
  info "Tip: run --watch-worker in a separate terminal to follow Celery worker logs"
}

# ── Log watchers ──────────────────────────────────────────────────────────────

watch_worker() {
  local pod
  pod=$(get_pod_by_label "${RBAC_WORKER_POD_LABEL}")
  if [[ -z "$pod" ]]; then
    err "No running RBAC Celery worker pod found (label: ${RBAC_WORKER_POD_LABEL})"
    err "Hint: oc get pods -l pod=rbac-worker-service"
    return 1
  fi
  info "Following logs for Celery worker pod: ${pod}"
  info "Press Ctrl+C to stop."
  echo ""
  oc logs -f "${pod}" &
  local log_pid=$!
  trap "kill ${log_pid} 2>/dev/null; trap - INT; return 0" INT
  wait "${log_pid}" 2>/dev/null
  trap - INT
}

watch_server() {
  local pod
  pod=$(get_pod_by_label "${RBAC_SERVICE_POD_LABEL}")
  if [[ -z "$pod" ]]; then
    err "No running RBAC service pod found (label: ${RBAC_SERVICE_POD_LABEL})"
    return 1
  fi
  info "Following logs for RBAC service pod: ${pod}"
  info "Press Ctrl+C to stop."
  echo ""
  oc logs -f "${pod}"
}

# ── Create workspace (public v2 API) ─────────────────────────────────────────
# POST /api/rbac/v2/workspaces/
# Required: NAME
# Optional env vars:
#   WORKSPACE_DESCRIPTION   description field (default: empty)
#   WORKSPACE_PARENT_ID     parent_id UUID (default: omitted → uses default workspace)

create_workspace() {
  local name="${1:-}"
  if [[ -z "$name" ]]; then
    err "Usage: $0 --create-workspace <name>"
    err "  Optional env: WORKSPACE_DESCRIPTION, WORKSPACE_PARENT_ID"
    return 1
  fi

  local body
  body=$(jq -nc \
    --arg name "$name" \
    --arg desc "${WORKSPACE_DESCRIPTION:-}" \
    --arg parent "${WORKSPACE_PARENT_ID:-}" \
    '{name: $name}
     + (if $desc   != "" then {description: $desc}   else {} end)
     + (if $parent != "" then {parent_id:   $parent} else {} end)')

  info "Creating workspace '${name}'..."
  info "  URL: ${BENTO_URL}/api/rbac/v2/workspaces/"
  info "  Body: ${body}"

  local response
  response=$(curl -sf \
    -X POST \
    -u "${BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME}:${EPHEMERAL_PASSWORD}" \
    -H "Content-Type: application/json" \
    -d "${body}" \
    "${BENTO_URL}/api/rbac/v2/workspaces/") || {
      err "Failed to create workspace"
      return 1
    }

  echo ""
  echo "=== Created Workspace ==="
  echo "$response" | jq .
}

# ── Delete workspace directly from DB pod ────────────────────────────────────
# Finds the PostgreSQL pod (RBAC_DB_POD_LABEL) and runs a DELETE via psql.
# Credentials are read from the RBAC service pod's ClowdApp env vars so we
# don't need to hard-code or look them up separately.
#
# Usage: --delete-workspace-db <UUID|name>
#   Accepts either a UUID (matched against management_workspace.id)
#   or a name (matched against management_workspace.name, case-insensitive).

delete_workspace_db() {
  local identifier="${1:-}"
  if [[ -z "$identifier" ]]; then
    err "Usage: $0 --delete-workspace-db <UUID|workspace-name>"
    return 1
  fi

  # Find both pods.
  local service_pod db_pod
  service_pod=$(get_pod_by_label "${RBAC_SERVICE_POD_LABEL}")
  db_pod=$(get_pod_by_label "${RBAC_DB_POD_LABEL}")

  if [[ -z "$db_pod" ]]; then
    err "No DB pod found (label: ${RBAC_DB_POD_LABEL})"
    err "Hint: oc get pods | grep db"
    return 1
  fi
  info "DB pod: ${db_pod}"

  # Read DB credentials from the RBAC service pod (injected by ClowdApp).
  local db_user db_password db_name
  if [[ -n "$service_pod" ]]; then
    db_user=$(oc exec "${service_pod}" -- sh -c 'echo "$DATABASE_USER"' 2>/dev/null | tr -d '\r')
    db_password=$(oc exec "${service_pod}" -- sh -c 'echo "$DATABASE_PASSWORD"' 2>/dev/null | tr -d '\r')
    db_name=$(oc exec "${service_pod}" -- sh -c 'echo "$DATABASE_NAME"' 2>/dev/null | tr -d '\r')
  fi

  # Fall back to defaults if env vars are empty (ClowdApp ephemeral common defaults).
  db_user="${db_user:-rbac}"
  db_name="${db_name:-rbac}"

  info "Database: ${db_name}  user: ${db_user}"

  # Build WHERE clause: UUID format → match id, otherwise match name.
  local where_clause
  if [[ "$identifier" =~ ^[0-9a-fA-F-]{36}$ ]]; then
    where_clause="id = '${identifier}'"
    info "Matching by UUID: ${identifier}"
  else
    where_clause="LOWER(name) = LOWER('${identifier}')"
    info "Matching by name: ${identifier}"
  fi

  # First show what will be deleted (dry-run SELECT).
  echo ""
  echo "=== Rows to be deleted ==="
  PGPASSWORD="${db_password}" oc exec "${db_pod}" -- \
    psql -U "${db_user}" -d "${db_name}" -x \
    -c "SELECT id, name, type, parent_id, tenant_id, created FROM management_workspace WHERE ${where_clause};"

  echo ""
  read -r -p "Confirm DELETE? [y/N] " confirm
  if [[ "${confirm}" != "y" && "${confirm}" != "Y" ]]; then
    info "Aborted."
    return 0
  fi

  echo ""
  echo "=== Delete Result ==="
  PGPASSWORD="${db_password}" oc exec "${db_pod}" -- \
    psql -U "${db_user}" -d "${db_name}" \
    -c "DELETE FROM management_workspace WHERE ${where_clause} RETURNING id, name, type;"
}

# ── Run seeds (Celery background job) ────────────────────────────────────────
# POST /_private/api/seeds/run/?seed_types=permissions,roles,groups
# Returns HTTP 202 immediately; actual work runs in the Celery worker pod.
#
# Optional env vars:
#   SEED_TYPES                    comma-separated: permissions,roles,groups (default: all)
#   SEED_FORCE_CREATE=true|false  force_create_relationships (default: false)
#   SEED_FORCE_UPDATE=true|false  force_update_relationships (default: false)
#   SEED_SKIP_NOTIFICATIONS=true  skip_notifications (default: false)
#
# After triggering, the function tails the Celery worker logs so you can
# watch progress in real time. Ctrl+C stops the log tail; the job keeps running.

run_seeds() {
  local seed_types="${SEED_TYPES:-permissions,roles,groups}"
  local force_create="${SEED_FORCE_CREATE:-false}"
  local force_update="${SEED_FORCE_UPDATE:-false}"
  local skip_notif="${SEED_SKIP_NOTIFICATIONS:-false}"

  local query="?seed_types=${seed_types}"
  query+="&force_create_relationships=${force_create}"
  query+="&force_update_relationships=${force_update}"
  query+="&skip_notifications=${skip_notif}"

  local path="/_private/api/seeds/run/${query}"

  info "Triggering run_seeds_in_worker Celery task..."
  info "  seed_types=${seed_types}"
  info "  force_create=${force_create}  force_update=${force_update}  skip_notifications=${skip_notif}"
  echo ""

  local response
  response=$(exec_private_curl POST "${path}") || {
    err "Failed to trigger seeds endpoint"
    return 1
  }

  echo "=== Response ==="
  echo "${response}"
  echo ""

  # The job is now queued in Celery. Tail the worker logs so the user can
  # watch it run. Ctrl+C exits the tail but the Celery task keeps running.
  local worker_pod
  worker_pod=$(get_pod_by_label "${RBAC_WORKER_POD_LABEL}")
  if [[ -z "${worker_pod}" ]]; then
    err "No Celery worker pod found (label: ${RBAC_WORKER_POD_LABEL})"
    err "Run manually: oc logs -f \$(oc get pods -l pod=rbac-worker-service -o name | head -1)"
    return 1
  fi

  info "Following Celery worker logs: ${worker_pod}"
  info "Press Ctrl+C to stop watching — the job will keep running."
  echo ""
  oc logs -f "${worker_pod}"
}


# ══════════════════════════════════════════════════════════════════════════════
# DR state — shared across phases via a file so each step can be run separately
# ══════════════════════════════════════════════════════════════════════════════

DR_STATE_FILE="${DR_STATE_FILE:-${HOME}/.cache/rbac-dr-state.env}"

# Auto-clear stale state when namespace changes (e.g. new bonfire deploy).
if [[ -f "${DR_STATE_FILE}" ]]; then
  _saved_ns=$(grep '^DR_STATE_NAMESPACE=' "${DR_STATE_FILE}" 2>/dev/null | cut -d= -f2) || true
  if [[ -n "${_saved_ns}" && "${_saved_ns}" != "${EPHEMERAL_NAMESPACE}" ]]; then
    warn "Namespace changed (${_saved_ns} → ${EPHEMERAL_NAMESPACE}) — clearing stale state file."
    rm -f "${DR_STATE_FILE}"
  fi
fi

dr_state_save() {
  local key="$1" value="$2"
  if [[ -f "${DR_STATE_FILE}" ]]; then
    grep -v "^${key}=" "${DR_STATE_FILE}" > "${DR_STATE_FILE}.tmp" 2>/dev/null || true
    mv "${DR_STATE_FILE}.tmp" "${DR_STATE_FILE}"
  fi
  # Ensure namespace is always tracked so stale-state detection works.
  if ! grep -q '^DR_STATE_NAMESPACE=' "${DR_STATE_FILE}" 2>/dev/null; then
    echo "DR_STATE_NAMESPACE=${EPHEMERAL_NAMESPACE}" >> "${DR_STATE_FILE}"
  fi
  echo "${key}=${value}" >> "${DR_STATE_FILE}"
  info "State saved: ${key}=${value}  (${DR_STATE_FILE})"
}

dr_state_load() {
  if [[ -f "${DR_STATE_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${DR_STATE_FILE}"
    info "State loaded from ${DR_STATE_FILE}"
  fi
}

dr_state_show() {
  echo ""
  echo "=== DR State (${DR_STATE_FILE}) ==="
  if [[ -f "${DR_STATE_FILE}" ]]; then cat "${DR_STATE_FILE}"; else echo "(empty)"; fi
}

# ══════════════════════════════════════════════════════════════════════════════
# DR SIMULATION -- RBAC <-> KESSEL
# ══════════════════════════════════════════════════════════════════════════════
#
# Fix endpoint: POST /_private/api/disaster_recovery/reconcile/
# Body: { "restore_timestamp": "<ISO8601>", "buffer_seconds": 300, "dry_run": false }
# Requires: DR_RELATIONS_RECONCILE_ENABLED=True in pod env
#
# The reconcile Celery task reads Kafka relation-replication events in the
# window [restore_timestamp - buffer_seconds, restore_timestamp], checks
# whether each referenced resource still exists in the RBAC DB, and writes
# corrective add/remove events to re-align Kessel.

DR_KESSEL_WORKSPACE_PREFIX="${DR_KESSEL_WORKSPACE_PREFIX:-dr-test-kessel}"
DR_KESSEL_WORKSPACE_COUNT="${DR_KESSEL_WORKSPACE_COUNT:-4}"
DR_KESSEL_ORG_ID="${DR_KESSEL_ORG_ID:-}"
DR_BUFFER_SECONDS="${DR_BUFFER_SECONDS:-300}"
DR_DRY_RUN="${DR_DRY_RUN:-false}"
DR_FAST="${DR_FAST:-false}"
DR_NO_DRY_RUN="${DR_NO_DRY_RUN:-false}"
DR_MINIMAL_DATA="${DR_MINIMAL_DATA:-false}"
DR_SYNC_WAIT_TIMEOUT="${DR_SYNC_WAIT_TIMEOUT:-10}"

dr_kessel_cleanup() {
  # Remove all test data created by DR Kessel steps and reset state.
  # Safe to call at any point — skips missing resources gracefully.
  dr_state_load

  echo ""
  echo -e "${BOLD}==================================================================${NC}"
  echo -e "${BOLD}  CLEANUP: Removing DR Kessel test data${NC}"
  echo -e "${BOLD}==================================================================${NC}"
  echo ""

  local ws_ids_csv="${DR_KESSEL_WORKSPACE_IDS:-${DR_KESSEL_WORKSPACE_ID:-}}"
  local ghost_id="${DR_GHOST_WORKSPACE_ID:-}"
  local cleaned=0

  if [[ -n "${ws_ids_csv}" || -n "${ghost_id}" ]]; then
    _db_creds 2>/dev/null || {
      warn "Could not get DB credentials — skipping DB cleanup."
      echo ""
      echo "  State file still contains references. Clear manually:"
      echo "    rm -f ${DR_STATE_FILE}"
      return 0
    }

    # Delete test workspaces from DB.
    if [[ -n "${ws_ids_csv}" ]]; then
      IFS=',' read -ra ws_ids <<< "${ws_ids_csv}"
      for ws_id in "${ws_ids[@]}"; do
        local result
        result=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
          psql -U "${_db_user}" -d "${_db_name}" -t -A \
          -c "DELETE FROM management_workspace WHERE id='${ws_id}' RETURNING id;" 2>/dev/null | tr -d '\r')
        if [[ -n "${result}" ]]; then
          good "Deleted workspace ${ws_id}"
          cleaned=$((cleaned + 1))
        else
          echo -e "  ${DIM}[----]${NC} ${ws_id} (not found, already removed)"
        fi
      done
    fi

    # Delete ghost workspace from DB.
    if [[ -n "${ghost_id}" ]]; then
      local result
      result=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "DELETE FROM management_workspace WHERE id='${ghost_id}' RETURNING id;" 2>/dev/null | tr -d '\r')
      if [[ -n "${result}" ]]; then
        good "Deleted ghost workspace ${ghost_id}"
        cleaned=$((cleaned + 1))
      else
        echo -e "  ${DIM}[----]${NC} ghost ${ghost_id} (not found, already removed)"
      fi
    fi
  else
    echo -e "  ${DIM}No workspace IDs in state — nothing to clean from DB.${NC}"
  fi

  # Clear state file.
  if [[ -f "${DR_STATE_FILE}" ]]; then
    rm -f "${DR_STATE_FILE}"
    good "State file removed: ${DR_STATE_FILE}"
  fi

  echo ""
  echo -e "  Cleaned ${GREEN}${cleaned}${NC} workspace(s) from DB. State reset."
  echo "  Ready for a fresh run."
}

# Resolve the single non-public org_id from the DB.
# In ephemeral clusters there is exactly one ready tenant.
# Sets ORG_ID_RESOLVED in the caller's scope; saves to state file.
_auto_resolve_org_id() {
  # Already resolved in this shell session?
  if [[ -n "${ORG_ID_RESOLVED:-}" ]]; then return 0; fi

  # Try state file first (avoids a DB round-trip on repeated calls).
  dr_state_load
  if [[ -n "${DR_AUTO_ORG_ID:-}" ]]; then
    # Validate the state-file org_id against the current DB — it may be
    # stale from a previous ephemeral deployment (different namespace).
    _db_creds || return 1
    local _chk
    _chk=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
      psql -U "${_db_user}" -d "${_db_name}" -t -A \
      -c "SELECT 1 FROM api_tenant WHERE org_id='${DR_AUTO_ORG_ID}';" 2>/dev/null | tr -d '\r')
    if [[ -n "${_chk}" ]]; then
      ORG_ID_RESOLVED="${DR_AUTO_ORG_ID}"
      return 0
    fi
    warn "Stale org_id=${DR_AUTO_ORG_ID} from state file (not in current DB) — re-resolving..."
    DR_AUTO_ORG_ID=""
  fi

  info "Auto-resolving org_id from DB..."
  _db_creds || return 1

  local org_id
  org_id=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
    psql -U "${_db_user}" -d "${_db_name}" -t -A \
    -c "SELECT org_id FROM api_tenant
        WHERE tenant_name <> 'public'
          AND ready = true
          AND org_id IS NOT NULL
        LIMIT 1;" 2>/dev/null | tr -d '\r')

  # No ready tenant — look for any tenant (ready=false) and bootstrap it first.
  if [[ -z "$org_id" ]]; then
    info "No ready tenant found — looking for an unbootstrapped tenant to bootstrap..."
    org_id=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
      psql -U "${_db_user}" -d "${_db_name}" -t -A \
      -c "SELECT org_id FROM api_tenant
          WHERE tenant_name <> 'public'
            AND org_id IS NOT NULL
          LIMIT 1;" 2>/dev/null | tr -d '\r')

    if [[ -z "$org_id" ]]; then
      info "No tenant row found — seeding tenant row directly in DB..."
      # Insert a bare tenant row so bootstrap_tenant (called below) has
      # something to work with. Uses the namespace as org_id, matching the
      # internal identity header. No external route needed.
      local insert_out
      insert_out=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -c \
        "INSERT INTO api_tenant (tenant_name, org_id, ready) VALUES ('${EPHEMERAL_NAMESPACE}', '${EPHEMERAL_NAMESPACE}', false) ON CONFLICT (org_id) DO NOTHING;" 2>&1) || {
          err "SQL INSERT failed: ${insert_out}"
          return 1
        }
      info "INSERT result: ${insert_out}"

      org_id=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT org_id FROM api_tenant WHERE tenant_name <> 'public' AND org_id IS NOT NULL LIMIT 1;" 2>/dev/null | tr -d '\r')

      if [[ -z "$org_id" ]]; then
        err "Tenant still not created after DB insert."
        err "Debug: PGPASSWORD=\${pw} oc exec ${_db_pod} -- psql -U ${_db_user} -d ${_db_name} -c 'SELECT * FROM api_tenant;'"
        return 1
      fi
      info "Tenant row created: ${org_id}"
    fi

    # Ensure seeds exist (permissions, roles, groups in the public tenant).
    # Bootstrap relies on platform_default groups for default access bindings.
    info "Running seeds (permissions, roles, groups)..."
    exec_private_curl POST "/_private/api/seeds/run/?seed_types=permissions,roles,groups" > /dev/null 2>&1 || {
      info "Seeds endpoint returned non-200 (may already be running or completed). Continuing..."
    }

    info "Bootstrapping tenant ${org_id}..."
    local bootstrap_resp
    bootstrap_resp=$(exec_private_curl POST /_private/api/utils/bootstrap_tenant/ \
      -H "Content-Type: application/json" \
      -d "{\"org_ids\": [\"${org_id}\"]}") || {
        err "Bootstrap failed for org_id=${org_id}"
        return 1
      }
    echo "${bootstrap_resp}"

    # bootstrap_tenant does not set ready=true (only the identity middleware
    # does, on a real user request). Set it directly since the DR toolkit
    # operates via internal APIs that don't go through that middleware.
    PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
      psql -U "${_db_user}" -d "${_db_name}" -t -A \
      -c "UPDATE api_tenant SET ready = true WHERE org_id = '${org_id}';" 2>/dev/null || {
        err "Failed to set ready=true for tenant ${org_id}"
        return 1
      }
    info "Tenant ${org_id} is now ready."
  fi

  ORG_ID_RESOLVED="${org_id}"
  dr_state_save DR_AUTO_ORG_ID "${org_id}"
  echo ""
  echo "=== Resolved org_id ==="
  echo "  org_id : ${org_id}"
  echo "  db pod : ${_db_pod}"
  echo "  db name: ${_db_name}"
  echo ""
}

_kessel_require_org_id() {
  dr_state_load
  # Prefer explicit env var, then state file, then auto-resolve from DB.
  if [[ -z "${DR_KESSEL_ORG_ID:-}" ]]; then
    _auto_resolve_org_id || return 1
    DR_KESSEL_ORG_ID="${ORG_ID_RESOLVED}"
  fi
}

_db_creds() {
  # Reads DB connection info from the RBAC service pod env (ClowdApp injection).
  # Discovers the DB pod from the DATABASE_SERVICE_HOST the RBAC pod already knows.
  # Sets: _db_pod _db_user _db_password _db_name
  local svc_pod
  svc_pod=$(get_pod_by_label "${RBAC_SERVICE_POD_LABEL}")
  if [[ -z "$svc_pod" ]]; then
    err "No running RBAC service pod found (label: ${RBAC_SERVICE_POD_LABEL})"
    return 1
  fi

  _db_user=$(oc exec "${svc_pod}" -- sh -c 'echo "$DATABASE_USER"'     2>/dev/null | tr -d '\r')
  _db_password=$(oc exec "${svc_pod}" -- sh -c 'echo "$DATABASE_PASSWORD"' 2>/dev/null | tr -d '\r')
  _db_name=$(oc exec "${svc_pod}" -- sh -c 'echo "$DATABASE_NAME"'     2>/dev/null | tr -d '\r')
  local db_host
  db_host=$(oc exec "${svc_pod}" -- sh -c 'echo "${DATABASE_SERVICE_HOST:-$DATABASE_HOST}"' 2>/dev/null | tr -d '\r')

  if [[ -z "${_db_user}" || -z "${_db_password}" || -z "${_db_name}" ]]; then
    info "ClowdApp env vars incomplete (user=${_db_user:-<empty>}, db=${_db_name:-<empty>}, pass=${_db_password:+***})"
    info "Trying cdappconfig.json..."
    local cdapp_json
    cdapp_json=$(oc exec "${svc_pod}" -- cat /cdapp/cdappconfig.json 2>/dev/null || true)
    if [[ -n "${cdapp_json}" ]]; then
      _db_user="${_db_user:-$(echo "${cdapp_json}" | jq -r '.database.username // empty')}"
      _db_password="${_db_password:-$(echo "${cdapp_json}" | jq -r '.database.password // empty')}"
      _db_name="${_db_name:-$(echo "${cdapp_json}" | jq -r '.database.name // empty')}"
    fi
  fi

  _db_user="${_db_user:-rbac}"; _db_name="${_db_name:-rbac}"
  info "DB creds: user=${_db_user}, db=${_db_name}, pass=${_db_password:+***}, host=${db_host:-<empty>}"

  # Try the configured label first (fast path).
  _db_pod=$(get_pod_by_label "${RBAC_DB_POD_LABEL}" 2>/dev/null)

  # If that fails, find the pod whose service matches DATABASE_SERVICE_HOST.
  if [[ -z "${_db_pod}" && -n "${db_host}" ]]; then
    info "Label '${RBAC_DB_POD_LABEL}' found no pod — discovering DB pod from host '${db_host}'..."
    # The host is a k8s service name; find which service it is and get its pod selector.
    local svc_name
    svc_name="${db_host%%.*}"   # strip any .namespace.svc.cluster.local suffix
    local selector
    selector=$(oc get svc "${svc_name}" -o jsonpath='{.spec.selector}' 2>/dev/null \
      | jq -r 'to_entries | map("\(.key)=\(.value)") | join(",")' 2>/dev/null)
    if [[ -n "$selector" ]]; then
      _db_pod=$(oc get pods -l "${selector}" --field-selector=status.phase=Running \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    fi
  fi

  # Last resort: grep for a pod whose name contains both "rbac" and "db".
  if [[ -z "${_db_pod}" ]]; then
    _db_pod=$(oc get pods --no-headers -o custom-columns=NAME:.metadata.name \
      | grep -i 'rbac.*db\|db.*rbac' | head -1 || true)
  fi

  if [[ -z "${_db_pod}" ]]; then
    err "Could not find DB pod. Tried label '${RBAC_DB_POD_LABEL}' and host '${db_host:-?}'."
    err "Set RBAC_DB_POD_LABEL to the correct label. Current pods:"
    oc get pods --no-headers -o custom-columns=NAME:.metadata.name,LABELS:.metadata.labels
    return 1
  fi
  info "DB pod: ${_db_pod}"
}

_db_query() {
  # Usage: _db_query "<SQL>"  (requires _db_pod/_db_user/_db_password/_db_name to be set)
  PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
    psql -U "${_db_user}" -d "${_db_name}" -c "$1"
}

_workspaces_for_org() {
  # Print management_workspace rows for a given org_id
  local org_id="$1"
  _db_query "SELECT w.id, w.name, w.type
             FROM management_workspace w
             JOIN api_tenant t ON w.tenant_id = t.id
             WHERE t.org_id = '${org_id}'
             ORDER BY w.type, w.name;"
}

_deployment_name_from_pod_label() {
  # ClowdApp sets the 'pod' label to <clowdapp>-<deployment-name>.
  # The k8s Deployment name matches this label value.
  local label="$1"
  echo "${label#pod=}"
}

_ensure_env_var_on_deployment() {
  # Ensure an env var is set on a deployment. Sets it and waits for rollout
  # if missing. Returns 0 if already set or successfully set.
  #
  # Uses `oc patch` with JSON patch instead of `oc set env` because ClowdApp
  # deployments often have duplicate env var entries that break strategic
  # merge patch ordering.
  local deploy="$1" var_name="$2" var_value="$3"

  local current
  current=$(oc get "deployment/${deploy}" -o jsonpath='{.spec.template.spec.containers[0].env}' 2>/dev/null \
    | jq -r --arg n "${var_name}" '.[] | select(.name == $n) | .value' 2>/dev/null | tail -1 || true)
  if [[ "${current}" == "${var_value}" ]]; then
    info "${var_name} already set on ${deploy}"
    return 0
  fi

  info "Setting ${var_name}=${var_value} on deployment/${deploy} (triggers pod restart)..."

  # Build a JSON patch: find the env array, append the new var (or update existing).
  local env_json patch_json
  env_json=$(oc get "deployment/${deploy}" -o jsonpath='{.spec.template.spec.containers[0].env}' 2>/dev/null || echo "[]")

  # Remove any existing entries with this name, then append the new one.
  patch_json=$(echo "${env_json}" | jq --arg n "${var_name}" --arg v "${var_value}" \
    '[.[] | select(.name != $n)] + [{"name": $n, "value": $v}]')

  oc patch "deployment/${deploy}" --type='json' \
    -p "[{\"op\": \"replace\", \"path\": \"/spec/template/spec/containers/0/env\", \"value\": ${patch_json}}]" || {
    err "Failed to set ${var_name} on ${deploy}"
    return 1
  }
  info "Waiting for rollout of ${deploy}..."
  oc rollout status "deployment/${deploy}" --timeout=120s || {
    err "Rollout timed out for ${deploy}"
    return 1
  }
  info "Rollout complete for ${deploy}."
}

_is_truthy() {
  # Normalize env var value: strip whitespace, colons, quotes, then check for true/True/TRUE.
  local val
  val=$(echo "$1" | tr -d '\r\n\t :"'"'" | tr '[:upper:]' '[:lower:]')
  [[ "${val}" == "true" ]]
}

_check_env_var_on_pod() {
  local pod="$1" var_name="$2" label="$3"
  echo -n "  ${var_name} on ${label} pod... "
  local val
  val=$(oc exec "${pod}" -- sh -c "echo \"\$${var_name}\"" 2>/dev/null)
  if _is_truthy "${val}"; then
    good "True"
    return 0
  else
    bad "${val:-not set}"
    return 1
  fi
}

_verify_dr_env_vars() {
  local svc_pod="$1" worker_pod="$2"
  local rc=0
  _check_env_var_on_pod "${svc_pod}" "DR_RELATIONS_RECONCILE_ENABLED" "service" || rc=1
  _check_env_var_on_pod "${svc_pod}" "DR_WORKSPACE_RECONCILE_ENABLED" "service" || rc=1
  _check_env_var_on_pod "${worker_pod}" "DR_RELATIONS_RECONCILE_ENABLED" "worker" || rc=1
  _check_env_var_on_pod "${worker_pod}" "DR_WORKSPACE_RECONCILE_ENABLED" "worker" || rc=1
  _check_env_var_on_pod "${worker_pod}" "KAFKA_ENABLED" "worker" || rc=1
  return "${rc}"
}

_check_dr_enabled() {
  # Verify DR_RELATIONS_RECONCILE_ENABLED and KAFKA_ENABLED are set on the RBAC pods.
  # If missing, auto-fix by patching the deployments directly. The patch is
  # ephemeral — the ClowdApp operator will override it on next reconciliation —
  # but it's sufficient for one-off DR testing on ephemeral clusters.
  local svc_pod worker_pod
  svc_pod=$(get_pod_by_label "${RBAC_SERVICE_POD_LABEL}")
  worker_pod=$(get_pod_by_label "${RBAC_WORKER_POD_LABEL}")

  if [[ -z "${worker_pod}" ]]; then
    # Try to recover: check if the deployment exists but is scaled to 0.
    local wrk_deploy
    wrk_deploy=$(_deployment_name_from_pod_label "${RBAC_WORKER_POD_LABEL}")
    local replicas
    replicas=$(oc get "deployment/${wrk_deploy}" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "")

    if [[ "${replicas}" == "0" ]]; then
      info "Worker deployment '${wrk_deploy}' has 0 replicas — scaling up..."
      oc scale "deployment/${wrk_deploy}" --replicas=1 || {
        err "Failed to scale worker deployment"
        return 1
      }
      info "Waiting for worker rollout..."
      oc rollout status "deployment/${wrk_deploy}" --timeout=120s || {
        err "Worker rollout timed out"
        return 1
      }
      worker_pod=$(get_pod_by_label "${RBAC_WORKER_POD_LABEL}")
    fi

    if [[ -z "${worker_pod}" ]]; then
      bad "No running worker pod found (label: ${RBAC_WORKER_POD_LABEL})"
      echo ""
      if [[ -z "${replicas}" ]]; then
        info "Deployment '${wrk_deploy}' not found either."
      else
        info "Deployment '${wrk_deploy}' has ${replicas} replica(s) but no Running pods."
        info "Check: oc describe pod -l ${RBAC_WORKER_POD_LABEL}"
      fi
      echo ""
      info "Running RBAC pods:"
      oc get pods --no-headers 2>/dev/null | grep -i rbac || echo "  (none found)"
      return 1
    fi
  fi

  if _verify_dr_env_vars "${svc_pod}" "${worker_pod}"; then
    return 0
  fi

  echo ""
  info "Attempting to set missing env vars via deployment patch..."

  local svc_deploy wrk_deploy
  svc_deploy=$(_deployment_name_from_pod_label "${RBAC_SERVICE_POD_LABEL}")
  wrk_deploy=$(_deployment_name_from_pod_label "${RBAC_WORKER_POD_LABEL}")

  _ensure_env_var_on_deployment "${svc_deploy}" "DR_RELATIONS_RECONCILE_ENABLED" "True" || return 1
  _ensure_env_var_on_deployment "${svc_deploy}" "DR_WORKSPACE_RECONCILE_ENABLED" "True" || return 1
  _ensure_env_var_on_deployment "${wrk_deploy}" "DR_RELATIONS_RECONCILE_ENABLED" "True" || return 1
  _ensure_env_var_on_deployment "${wrk_deploy}" "DR_WORKSPACE_RECONCILE_ENABLED" "True" || return 1
  _ensure_env_var_on_deployment "${wrk_deploy}" "KAFKA_ENABLED" "True" || return 1

  # Pods restarted during rollout — re-fetch and re-verify.
  svc_pod=$(get_pod_by_label "${RBAC_SERVICE_POD_LABEL}")
  worker_pod=$(get_pod_by_label "${RBAC_WORKER_POD_LABEL}")

  if [[ -z "${worker_pod}" ]]; then
    bad "Worker pod not found after deployment patch — rollout may still be in progress."
    info "Wait and retry: $0 --rbac-kessel"
    return 1
  fi

  echo ""
  info "Re-checking env vars on restarted pods..."
  if _verify_dr_env_vars "${svc_pod}" "${worker_pod}"; then
    echo ""
    good "All env vars set (via deployment patch)."
    warn "Note: patch will reset on next ClowdApp operator reconciliation."
    return 0
  fi

  echo ""
  bad "Required env vars still missing after deployment patch."
  echo ""
  echo "  Redeploy with bonfire to set them permanently:"
  echo ""
  echo "    bonfire deploy rbac \\"
  echo "      -p rbac/DR_RELATIONS_RECONCILE_ENABLED=True \\"
  echo "      -p rbac/DR_WORKSPACE_RECONCILE_ENABLED=True \\"
  echo "      -p rbac/KAFKA_ENABLED=True"
  return 1
}

_read_kessel_workspace_tuples() {
  # Read Kessel relation tuples for a given workspace UUID.
  # Returns JSON with tuples or empty if none found.
  local ws_id="$1"
  local filter_body
  filter_body=$(jq -nc --arg ws_id "${ws_id}" '{
    filter: {
      resource_namespace: "rbac",
      resource_type: "workspace",
      resource_id: $ws_id,
      relation: "parent",
      subject_filter: {
        subject_namespace: "rbac",
        subject_type: "workspace",
        subject_id: "",
        relation: ""
      }
    }
  }')
  local result
  result=$(exec_private_curl POST /_private/api/relations/read_tuples/ \
    -H "Content-Type: application/json" -d "${filter_body}" 2>/dev/null) || {
    echo '{"tuples":[]}'
    return
  }
  # Handle 204 "No tuples found" (plain string, not JSON)
  if ! echo "${result}" | jq . &>/dev/null; then
    echo '{"tuples":[]}'
    return
  fi
  echo "${result}"
}

_read_kessel_role_binding_tuples() {
  # Read Kessel relation tuples for a given role binding UUID.
  # Queries the "role" relation: rbac/role_binding:<uuid>#role@rbac/role:<role_uuid>
  local rb_uuid="$1"
  local filter_body
  filter_body=$(jq -nc --arg rb_uuid "${rb_uuid}" '{
    filter: {
      resource_namespace: "rbac",
      resource_type: "role_binding",
      resource_id: $rb_uuid,
      relation: "role",
      subject_filter: {
        subject_namespace: "rbac",
        subject_type: "role",
        subject_id: "",
        relation: ""
      }
    }
  }')
  local result
  result=$(exec_private_curl POST /_private/api/relations/read_tuples/ \
    -H "Content-Type: application/json" -d "${filter_body}" 2>/dev/null) || {
    echo '{"tuples":[]}'
    return
  }
  if ! echo "${result}" | jq . &>/dev/null; then
    echo '{"tuples":[]}'
    return
  fi
  echo "${result}"
}

_read_kessel_role_tuples() {
  local role_uuid="$1"
  local filter_body
  filter_body=$(jq -nc --arg role_uuid "${role_uuid}" '{
    filter: {
      resource_namespace: "rbac",
      resource_type: "role",
      resource_id: $role_uuid,
      relation: "",
      subject_filter: {
        subject_namespace: "",
        subject_type: "",
        subject_id: "",
        relation: ""
      }
    }
  }')
  local result
  result=$(exec_private_curl POST /_private/api/relations/read_tuples/ \
    -H "Content-Type: application/json" -d "${filter_body}" 2>/dev/null) || {
    echo '{"tuples":[]}'
    return
  }
  if ! echo "${result}" | jq . &>/dev/null; then
    echo '{"tuples":[]}'
    return
  fi
  echo "${result}"
}

_read_kessel_group_tuples() {
  local group_uuid="$1"
  local filter_body
  filter_body=$(jq -nc --arg group_uuid "${group_uuid}" '{
    filter: {
      resource_namespace: "rbac",
      resource_type: "group",
      resource_id: $group_uuid,
      relation: "member",
      subject_filter: {
        subject_namespace: "rbac",
        subject_type: "principal",
        subject_id: "",
        relation: ""
      }
    }
  }')
  local result
  result=$(exec_private_curl POST /_private/api/relations/read_tuples/ \
    -H "Content-Type: application/json" -d "${filter_body}" 2>/dev/null) || {
    echo '{"tuples":[]}'
    return
  }
  if ! echo "${result}" | jq . &>/dev/null; then
    echo '{"tuples":[]}'
    return
  fi
  echo "${result}"
}

_diagnose_replication() {
  # Check each link in the outbox → Debezium → Kafka → Kessel chain.
  # Args: workspace_id (any of the created ones, used to check outbox entries)
  local ws_id="${1:-}"

  echo ""
  echo -e "${BOLD}--- Replication diagnostics ---${NC}"
  echo ""

  # 1. Outbox table pattern: Debezium captures WAL INSERT, then the row is
  #    immediately deleted (write-then-delete). The table is always empty.
  #    Check WAL activity via pg_stat_user_tables instead.
  echo -n "  1. Outbox table (write-then-delete pattern)... "
  local outbox_ops
  outbox_ops=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
    psql -U "${_db_user}" -d "${_db_name}" -t -A \
    -c "SELECT n_tup_ins FROM pg_stat_user_tables WHERE relname='management_outbox';" 2>/dev/null | tr -d '\r' || echo "error")

  if [[ "${outbox_ops}" == "error" ]]; then
    bad "could not query pg_stat_user_tables"
  elif [[ "${outbox_ops}" == "0" || -z "${outbox_ops}" ]]; then
    bad "0 inserts to outbox table (no events written since DB start)"
    echo "         Workspace creation may not be triggering the outbox replicator."
  else
    good "${outbox_ops} total inserts to outbox (table is always empty — Debezium captures WAL)"
  fi

  # 2. Debezium connector: is it configured in this cluster?
  echo -n "  2. Debezium connector... "
  local connector_pods
  connector_pods=$(oc get pods 2>/dev/null | grep -i 'debezium\|kafka-connect' | grep -c Running || echo "0")
  if [[ "${connector_pods}" -gt 0 ]]; then
    good "${connector_pods} connector pod(s) found"
  else
    warn "no Debezium/Kafka Connect pods found"
    echo "         Outbox events may be delivered via a different mechanism."
  fi

  # 3. Key env vars on service pod
  local svc_pod
  svc_pod=$(get_pod_by_label "${RBAC_SERVICE_POD_LABEL}")
  echo -e "  ${DIM}(checking pod: ${svc_pod})${NC}"

  echo -n "  3. REPLICATION_TO_RELATION_ENABLED... "
  local repl_enabled
  repl_enabled=$(oc exec "${svc_pod}" -- sh -c 'echo "$REPLICATION_TO_RELATION_ENABLED"' 2>/dev/null | tr -d '\r' || echo "?")
  if [[ "${repl_enabled}" == "True" || "${repl_enabled}" == "true" || "${repl_enabled}" == "TRUE" ]]; then
    good "True"
  elif [[ -z "${repl_enabled}" || "${repl_enabled}" == "?" ]]; then
    bad "not set (default=False — replication is OFF)"
  else
    bad "${repl_enabled} — replication to Kessel is disabled!"
  fi

  echo -n "  4. KAFKA_ENABLED... "
  local kafka_enabled
  kafka_enabled=$(oc exec "${svc_pod}" -- sh -c 'echo "$KAFKA_ENABLED"' 2>/dev/null | tr -d '\r' || echo "?")
  if [[ "${kafka_enabled}" == "True" || "${kafka_enabled}" == "true" || "${kafka_enabled}" == "TRUE" ]]; then
    good "True"
  elif [[ -z "${kafka_enabled}" || "${kafka_enabled}" == "?" ]]; then
    warn "not set (may be needed for Debezium)"
  else
    echo -e "  ${DIM}[----]${NC} ${kafka_enabled}"
  fi

  # 5. Kessel Relations server: can RBAC reach it?
  echo -n "  5. Kessel Relations (gRPC)... "
  local kessel_server
  kessel_server=$(oc exec "${svc_pod}" -- sh -c 'echo "$RELATION_API_SERVER"' 2>/dev/null | tr -d '\r' || echo "?")
  if [[ -n "${kessel_server}" && "${kessel_server}" != "?" ]]; then
    good "configured → ${kessel_server}"
  else
    bad "RELATION_API_SERVER not set"
  fi

  # 6. Try read_tuples for the actual workspace to see what Kessel returns
  local test_filter
  test_filter=$(jq -nc --arg id "${ws_id}" '{
    filter: {
      resource_namespace: "rbac",
      resource_type: "workspace",
      resource_id: $id,
      relation: "parent",
      subject_filter: {
        subject_namespace: "rbac",
        subject_type: "workspace",
        subject_id: "",
        relation: ""
      }
    }
  }')

  echo "  6. read_tuples query:"
  echo -e "  ${DIM}   POST /_private/api/relations/read_tuples/${NC}"
  echo -e "  ${DIM}   Filter: $(echo "${test_filter}" | jq -c .filter)${NC}"
  echo -e "  ${DIM}   Looking for: rbac/workspace:${ws_id}#parent@rbac/workspace:* ${NC}"
  echo ""

  # Call read_tuples directly via oc exec (not through exec_private_curl)
  # to capture raw HTTP status + body without helper interference.
  local test_pod test_identity test_url
  test_pod=$(get_pod_by_label "${RBAC_SERVICE_POD_LABEL}")
  test_identity=$(build_internal_identity_header 2>/dev/null)
  test_url="http://localhost:8000/_private/api/relations/read_tuples/"

  local test_raw_full test_raw test_http_code test_stderr
  test_stderr=$(mktemp)
  test_raw_full=$(oc exec "${test_pod}" -- curl -s -w '\n%{http_code}' \
    -X POST \
    -H "X-RH-Identity: ${test_identity}" \
    -H "Content-Type: application/json" \
    -d "${test_filter}" \
    "${test_url}" 2>"${test_stderr}")
  test_http_code=$(echo "${test_raw_full}" | tail -1)
  test_raw=$(echo "${test_raw_full}" | sed '$d')
  if [[ -s "${test_stderr}" ]]; then
    echo -e "     ${DIM}stderr: $(cat "${test_stderr}")${NC}"
  fi
  rm -f "${test_stderr}"

  echo -e "     ${DIM}HTTP ${test_http_code}${NC}"
  echo ""

  echo -n "     Result: "
  if echo "${test_raw}" | jq . &>/dev/null; then
    local tuple_count
    tuple_count=$(echo "${test_raw}" | jq '.tuples | length' 2>/dev/null || echo "0")
    if [[ "${tuple_count}" -gt 0 ]]; then
      good "${tuple_count} tuple(s) found"
      echo -e "  ${DIM}   Tuples:${NC}"
      echo "${test_raw}" | jq -r '.tuples[]? | "     \(.tuple.resourceAndRelation.namespace // "?")/\(.tuple.resourceAndRelation.objectType // .tuple.resourceAndRelation.resource_type // "?"):\(.tuple.resourceAndRelation.objectId // .tuple.resourceAndRelation.resource_id // "?")#\(.tuple.resourceAndRelation.relation // .tuple.relation // "?") → \(.tuple.subject.object.objectType // .tuple.subject.subject.type // "?"):\(.tuple.subject.object.objectId // .tuple.subject.subject.id // "?")"' 2>/dev/null \
        | while read -r line; do echo -e "  ${DIM}   ${line}${NC}"; done
    else
      warn "0 tuples returned"
    fi
  else
    bad "unexpected response (not JSON)"
  fi
  echo -e "  ${DIM}   Raw response: ${test_raw:0:500}${NC}"

  echo ""
}

_wait_for_kessel_sync() {
  # Wait for all workspace IDs to appear in Kessel (Debezium replication lag).
  # Args: timeout_seconds workspace_id [workspace_id ...]
  # Returns 0 if all synced within timeout, 1 if timed out.
  local timeout="$1"; shift
  local ids=("$@")
  local elapsed=0 interval=5

  echo -e "  ${DIM}Waiting up to ${timeout}s for Debezium → Kafka → Kessel replication...${NC}"

  while [[ "${elapsed}" -lt "${timeout}" ]]; do
    local all_ok=true
    for ws_id in "${ids[@]}"; do
      local tuple_count
      tuple_count=$(echo "$(_read_kessel_workspace_tuples "${ws_id}")" \
        | jq '.tuples | length' 2>/dev/null || echo "0")
      if [[ "${tuple_count}" == "0" ]]; then
        all_ok=false
        break
      fi
    done

    if [[ "${all_ok}" == "true" ]]; then
      echo -e "  ${DIM}Replication complete after ${elapsed}s.${NC}"
      return 0
    fi

    sleep "${interval}"
    elapsed=$((elapsed + interval))
    echo -ne "\r  ${DIM}Waiting... ${elapsed}s / ${timeout}s${NC}    "
  done
  echo ""
  return 1
}

_check_workspace_sync() {
  # Check if a list of workspace IDs are in sync between DB and Kessel.
  # Args: workspace_id [workspace_id ...]
  # Prints status for each workspace. Returns 0 if all in sync, 1 if any divergent.
  local all_synced=0
  for ws_id in "$@"; do
    local db_count
    db_count=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
      psql -U "${_db_user}" -d "${_db_name}" -t -A \
      -c "SELECT count(*) FROM management_workspace WHERE id='${ws_id}';" 2>/dev/null | tr -d '\r')

    local tuples tuple_count
    tuples=$(_read_kessel_workspace_tuples "${ws_id}")
    tuple_count=$(echo "${tuples}" | jq '.tuples | length' 2>/dev/null || echo "0")

    local in_db="no" in_kessel="no"
    [[ "${db_count}" != "0" ]] && in_db="yes"
    [[ "${tuple_count}" != "0" ]] && in_kessel="yes"

    if [[ "${in_db}" == "${in_kessel}" ]]; then
      good "${ws_id}: DB=${in_db}, Kessel=${in_kessel} (in sync)"
    else
      bad "${ws_id}: DB=${in_db}, Kessel=${in_kessel} (OUT OF SYNC)"
      all_synced=1
    fi
  done
  return "${all_synced}"
}

_check_role_binding_sync() {
  local all_synced=0
  for rb_id in "$@"; do
    local db_count
    db_count=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
      psql -U "${_db_user}" -d "${_db_name}" -t -A \
      -c "SELECT count(*) FROM management_rolebinding WHERE uuid='${rb_id}';" 2>/dev/null | tr -d '\r')

    local tuples tuple_count
    tuples=$(_read_kessel_role_binding_tuples "${rb_id}")
    tuple_count=$(echo "${tuples}" | jq '.tuples | length' 2>/dev/null || echo "0")

    local in_db="no" in_kessel="no"
    [[ "${db_count}" != "0" ]] && in_db="yes"
    [[ "${tuple_count}" != "0" ]] && in_kessel="yes"

    if [[ "${in_db}" == "${in_kessel}" ]]; then
      good "rb:${rb_id}: DB=${in_db}, Kessel=${in_kessel} (in sync)"
    else
      bad "rb:${rb_id}: DB=${in_db}, Kessel=${in_kessel} (OUT OF SYNC)"
      all_synced=1
    fi
  done
  return "${all_synced}"
}

_check_role_sync() {
  local all_synced=0
  for role_uuid in "$@"; do
    local db_count
    db_count=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
      psql -U "${_db_user}" -d "${_db_name}" -t -A \
      -c "SELECT count(*) FROM management_rolev2 WHERE uuid='${role_uuid}';" 2>/dev/null | tr -d '\r')

    local tuples tuple_count
    tuples=$(_read_kessel_role_tuples "${role_uuid}")
    tuple_count=$(echo "${tuples}" | jq '.tuples | length' 2>/dev/null || echo "0")

    local in_db="no" in_kessel="no"
    [[ "${db_count}" != "0" ]] && in_db="yes"
    [[ "${tuple_count}" != "0" ]] && in_kessel="yes"

    if [[ "${in_db}" == "${in_kessel}" ]]; then
      good "role:${role_uuid}: DB=${in_db}, Kessel=${in_kessel} (in sync)"
    else
      bad "role:${role_uuid}: DB=${in_db}, Kessel=${in_kessel} (OUT OF SYNC)"
      all_synced=1
    fi
  done
  return "${all_synced}"
}

_check_group_sync() {
  local all_synced=0
  for group_uuid in "$@"; do
    local db_count
    db_count=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
      psql -U "${_db_user}" -d "${_db_name}" -t -A \
      -c "SELECT count(*) FROM management_group WHERE uuid='${group_uuid}';" 2>/dev/null | tr -d '\r')

    local tuples tuple_count
    tuples=$(_read_kessel_group_tuples "${group_uuid}")
    tuple_count=$(echo "${tuples}" | jq '.tuples | length' 2>/dev/null || echo "0")

    local in_db="no" in_kessel="no"
    [[ "${db_count}" != "0" ]] && in_db="yes"
    [[ "${tuple_count}" != "0" ]] && in_kessel="yes"

    if [[ "${in_db}" == "${in_kessel}" ]]; then
      good "group:${group_uuid}: DB=${in_db}, Kessel=${in_kessel} (in sync)"
    else
      bad "group:${group_uuid}: DB=${in_db}, Kessel=${in_kessel} (OUT OF SYNC)"
      all_synced=1
    fi
  done
  return "${all_synced}"
}

_show_time_window() {
  # Print a visual timeline of the DR reconciliation window.
  local restore_ts="$1" buffer="$2"
  local restore_epoch window_start_epoch window_start_ts

  restore_epoch=$(date -j -u -f "%Y-%m-%dT%H:%M:%SZ" "${restore_ts}" "+%s" 2>/dev/null \
    || date -d "${restore_ts}" "+%s" 2>/dev/null || echo "0")

  if [[ "${restore_epoch}" == "0" ]]; then
    info "Could not parse restore timestamp for timeline."
    return
  fi

  window_start_epoch=$((restore_epoch - buffer))
  window_start_ts=$(date -j -u -f "%s" "${window_start_epoch}" "+%Y-%m-%dT%H:%M:%SZ" 2>/dev/null \
    || date -u -d "@${window_start_epoch}" "+%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "?")

  echo ""
  echo "=== DR Reconciliation Time Window ==="
  echo ""
  echo "  The reconciler reads Kafka events in this window and checks whether"
  echo "  the resources they reference still exist in the RBAC database."
  echo ""
  echo "  Events BEFORE the window are assumed committed before the backup."
  echo "  Events AFTER the window are assumed created after the restore."
  echo "  Events INSIDE the window may be inconsistent — these get reconciled."
  echo ""
  echo "  ──────────────────────────────────────────────────────────────────"
  echo "  time ──►"
  echo ""
  echo "  ···── committed ──┤ buffer=${buffer}s │──── post-restore ────►"
  echo "                    │◄──── reconcile ──►│"
  echo "                    │                   │"
  echo "                    window_start        restore_point"
  echo "                    ${window_start_ts}  ${restore_ts}"
  echo "  ──────────────────────────────────────────────────────────────────"
  echo ""
  echo "  Kafka events in [${window_start_ts}, ${restore_ts}] will be checked."
  echo ""
}

dr_kessel_setup() {
  # Create multiple test workspaces via the public v2 API.
  # Each creation generates Kafka relation events (Debezium outbox).
  _kessel_require_org_id || return 1

  # --minimal-data: 1 per scenario instead of 2.
  if [[ "${DR_MINIMAL_DATA}" == "true" ]]; then
    DR_KESSEL_WORKSPACE_COUNT=2
  fi

  local ws_ids=()
  local i

  echo ""
  echo -e "${BOLD}==================================================================${NC}"
  echo -e "${BOLD}  SETUP: Creating ${DR_KESSEL_WORKSPACE_COUNT} test workspaces${NC}"
  echo -e "${BOLD}==================================================================${NC}"
  echo ""
  echo "  Each workspace creation sends relation tuples to Kessel via Kafka."
  echo "  These events are what the reconciler will read during the fix step."
  echo ""

  # Append short timestamp to workspace names so re-runs don't conflict.
  local run_tag
  run_tag=$(date +%s | tail -c 5)

  for i in $(seq 1 "${DR_KESSEL_WORKSPACE_COUNT}"); do
    local ws_name="${DR_KESSEL_WORKSPACE_PREFIX}-${run_tag}-${i}"
    info "Creating workspace ${i}/${DR_KESSEL_WORKSPACE_COUNT}: '${ws_name}'..."

    local http_code response
    response=$(curl -s -w '\n%{http_code}' \
      -X POST \
      -u "${BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME}:${EPHEMERAL_PASSWORD}" \
      -H "Content-Type: application/json" \
      -d "{\"name\": \"${ws_name}\"}" \
      "${BENTO_URL}/api/rbac/v2/workspaces/")
    http_code=$(echo "${response}" | tail -1)
    response=$(echo "${response}" | sed '$d')

    if [[ "${http_code}" -lt 200 || "${http_code}" -ge 300 ]]; then
      err "Failed to create workspace '${ws_name}' (HTTP ${http_code})"
      echo "  Response: ${response}" >&2
      return 1
    fi

    local ws_id
    ws_id=$(echo "$response" | jq -r '.id')
    ws_ids+=("${ws_id}")
    good "Created: ${ws_name} (${ws_id})"
  done

  # Save all workspace IDs (comma-separated) and org_id.
  local ws_ids_csv
  ws_ids_csv=$(IFS=,; echo "${ws_ids[*]}")
  dr_state_save DR_KESSEL_ORG_ID       "${DR_KESSEL_ORG_ID}"
  dr_state_save DR_KESSEL_WORKSPACE_IDS "${ws_ids_csv}"
  # Keep single-ID var for backward compat.
  dr_state_save DR_KESSEL_WORKSPACE_ID  "${ws_ids[0]}"
  dr_state_save DR_SETUP_TIMESTAMP     "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  # Wait for Debezium → Kafka → Kessel replication, then validate.
  _db_creds || return 1

  if [[ "${DR_FAST}" != "true" ]]; then
    echo ""
    echo -e "${BOLD}=== Validation: Initial sync check ===${NC}"
    echo ""
    echo -e "  ${BOLD}Expected:${NC} All workspaces ${GREEN}IN SYNC${NC} (DB=yes, Kessel=yes)"
    echo "  Before simulating a disaster, we verify that RBAC DB and Kessel"
    echo "  are consistent. All created workspaces should exist in both."
    echo ""

    # Workspace creation writes to outbox → Debezium → Kafka → Kessel.
    # Wait for this replication to complete before checking sync.
    _wait_for_kessel_sync "${DR_SYNC_WAIT_TIMEOUT}" "${ws_ids[@]}" || true

    if _check_workspace_sync "${ws_ids[@]}"; then
      echo ""
      good "All ${DR_KESSEL_WORKSPACE_COUNT} workspaces are IN SYNC between DB and Kessel."
    else
      echo ""
      bad "Validation FAILED: workspaces are NOT in sync after creation."
      echo "         Kessel replication did not complete within ${DR_SYNC_WAIT_TIMEOUT}s."
      echo "         Cannot proceed — the baseline must be consistent."

      _diagnose_replication "${ws_ids[0]}"

      echo "  Retry: DR_STEP=setup $0 --rbac-kessel"
      echo "  Increase timeout: DR_SYNC_WAIT_TIMEOUT=60 $0 --rbac-kessel"
      return 1
    fi
  fi

  # --- Activate V2 writes for this tenant ---
  # V2 role/role-binding endpoints require workspaces to be enabled.
  # WorkspaceViewSet doesn't enforce this, but RoleV2ViewSet does via
  # V2WriteRequiresWorkspacesEnabled which checks TenantMapping.v2_write_activated_at.
  echo ""
  echo -e "${BOLD}=== Setup: Activating V2 writes for tenant ===${NC}"
  echo ""
  local _act_tenant_id
  _act_tenant_id=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
    psql -U "${_db_user}" -d "${_db_name}" -t -A \
    -c "SELECT id FROM api_tenant WHERE org_id='${DR_KESSEL_ORG_ID}';" 2>/dev/null | tr -d '\r')
  if [[ -z "${_act_tenant_id}" ]]; then
    err "Cannot find tenant for org_id=${DR_KESSEL_ORG_ID}"
    err "Tenants in DB:"
    PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
      psql -U "${_db_user}" -d "${_db_name}" -c \
      "SELECT id, org_id, tenant_name, ready FROM api_tenant WHERE tenant_name <> 'public';" 2>/dev/null || true
    return 1
  fi
  _db_query "UPDATE management_tenantmapping SET v2_write_activated_at = NOW() WHERE tenant_id = ${_act_tenant_id} AND v2_write_activated_at IS NULL;" 2>/dev/null || true
  local _act_check
  _act_check=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
    psql -U "${_db_user}" -d "${_db_name}" -t -A \
    -c "SELECT v2_write_activated_at IS NOT NULL FROM management_tenantmapping WHERE tenant_id = ${_act_tenant_id};" 2>/dev/null | tr -d '\r')
  if [[ "${_act_check}" == "t" ]]; then
    good "V2 writes activated for tenant ${DR_KESSEL_ORG_ID}"
  else
    warn "Could not verify V2 activation — role creation may fail with 403"
  fi

  if [[ "${DR_MINIMAL_DATA}" == "true" ]]; then
    echo ""
    info "Minimal data mode — skipping role binding, role, and group setup."
  else
  # --- Create a RoleV2 and Group for role binding scenarios ---
  echo ""
  echo -e "${BOLD}=== Setup: Role + Group for role binding scenarios ===${NC}"
  echo ""

  local role_name="dr-role-${run_tag}"
  info "Creating v2 role: '${role_name}'..."
  local role_response role_http
  role_response=$(curl -s -w '\n%{http_code}' \
    -X POST \
    -u "${BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME}:${EPHEMERAL_PASSWORD}" \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"${role_name}\", \"permissions\": [{\"application\": \"inventory\", \"resource_type\": \"hosts\", \"operation\": \"read\"}]}" \
    "${BENTO_URL}/api/rbac/v2/roles/")
  role_http=$(echo "${role_response}" | tail -1)
  role_response=$(echo "${role_response}" | sed '$d')

  if [[ "${role_http}" -lt 200 || "${role_http}" -ge 300 ]]; then
    err "Failed to create v2 role (HTTP ${role_http})"
    echo "  Response: ${role_response}" >&2
    return 1
  fi

  local role_uuid
  role_uuid=$(echo "${role_response}" | jq -r '.id')
  dr_state_save DR_ROLE_UUID "${role_uuid}"
  good "Created role: ${role_name} (${role_uuid})"

  local group_name="dr-group-${run_tag}"
  info "Creating group: '${group_name}'..."
  local group_response group_http
  group_response=$(curl -s -w '\n%{http_code}' \
    -X POST \
    -u "${BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME}:${EPHEMERAL_PASSWORD}" \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"${group_name}\"}" \
    "${BENTO_URL}/api/rbac/v1/groups/")
  group_http=$(echo "${group_response}" | tail -1)
  group_response=$(echo "${group_response}" | sed '$d')

  if [[ "${group_http}" -lt 200 || "${group_http}" -ge 300 ]]; then
    err "Failed to create group (HTTP ${group_http})"
    echo "  Response: ${group_response}" >&2
    return 1
  fi

  local group_uuid
  group_uuid=$(echo "${group_response}" | jq -r '.uuid')
  dr_state_save DR_GROUP_UUID "${group_uuid}"
  good "Created group: ${group_name} (${group_uuid})"

  # --- Create role bindings (one per workspace) ---
  echo ""
  echo -e "${BOLD}=== Setup: Role bindings (${DR_KESSEL_WORKSPACE_COUNT} bindings) ===${NC}"
  echo ""
  echo "  Creating one role binding per workspace."
  echo "  Each binds role '${role_name}' to a workspace with group '${group_name}' as subject."
  echo ""

  local rb_requests=""
  for ws_id in "${ws_ids[@]}"; do
    [[ -n "${rb_requests}" ]] && rb_requests="${rb_requests},"
    rb_requests="${rb_requests}{\"resource\":{\"id\":\"${ws_id}\",\"type\":\"workspace\"},\"subject\":{\"id\":\"${group_uuid}\",\"type\":\"group\"},\"role\":{\"id\":\"${role_uuid}\"}}"
  done

  local rb_response rb_http
  local rb_attempt
  for rb_attempt in 1 2 3; do
    rb_response=$(curl -s -w '\n%{http_code}' --max-time 120 \
      -X POST \
      -u "${BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME}:${EPHEMERAL_PASSWORD}" \
      -H "Content-Type: application/json" \
      -d "{\"requests\": [${rb_requests}]}" \
      "${BENTO_URL}/api/rbac/v2/role-bindings:batchCreate/")
    rb_http=$(echo "${rb_response}" | tail -1)
    rb_response=$(echo "${rb_response}" | sed '$d')
    if [[ "${rb_http}" -ge 200 && "${rb_http}" -lt 300 ]]; then
      break
    fi
    warn "Batch create attempt ${rb_attempt}/3 failed (HTTP ${rb_http}), retrying in 10s..."
    sleep 10
  done

  if [[ "${rb_http}" -lt 200 || "${rb_http}" -ge 300 ]]; then
    err "Failed to create role bindings (HTTP ${rb_http})"
    echo "  Response: ${rb_response}" >&2
    return 1
  fi

  # The batch create response does not include role binding UUIDs.
  # Query the DB to retrieve UUIDs for the bindings we just created.
  local rb_ids=()
  local ws_id_in=""
  for ws_id in "${ws_ids[@]}"; do
    [[ -n "${ws_id_in}" ]] && ws_id_in="${ws_id_in},"
    ws_id_in="${ws_id_in}'${ws_id}'"
  done
  local rb_id_list
  rb_id_list=$(_db_query "SELECT uuid FROM management_rolebinding WHERE resource_id IN (${ws_id_in}) AND role_id = (SELECT id FROM management_rolev2 WHERE uuid='${role_uuid}');" 2>/dev/null)
  while IFS= read -r rid; do
    rid=$(echo "${rid}" | xargs)
    [[ -z "${rid}" ]] && continue
    [[ "${rid}" == "uuid" ]] && continue
    [[ "${rid}" == -* ]] && continue
    [[ "${rid}" == "("*")" ]] && continue
    rb_ids+=("${rid}")
  done <<< "${rb_id_list}"

  if [[ "${#rb_ids[@]}" -eq 0 ]]; then
    warn "Could not find role binding UUIDs in DB after batch create."
  else
    good "Created ${#rb_ids[@]} role bindings"
    for rid in "${rb_ids[@]}"; do
      echo "    ${rid}"
    done
  fi

  local rb_ids_csv=""
  if [[ "${#rb_ids[@]}" -gt 0 ]]; then
    rb_ids_csv=$(IFS=,; echo "${rb_ids[*]}")
  fi
  dr_state_save DR_ROLE_BINDING_IDS "${rb_ids_csv}"

  # Wait for role binding tuples to appear in Kessel.
  if [[ "${DR_FAST}" != "true" && "${#rb_ids[@]}" -gt 0 ]]; then
    info "Waiting for role binding tuples to sync to Kessel..."
    local rb_sync_wait=0 rb_sync_max=2
    local rb_synced=0
    while [[ "${rb_sync_wait}" -lt "${rb_sync_max}" ]]; do
      rb_synced=0
      for rid in "${rb_ids[@]}"; do
        local tc
        tc=$(echo "$(_read_kessel_role_binding_tuples "${rid}")" | jq '.tuples | length' 2>/dev/null || echo "0")
        [[ "${tc}" -gt 0 ]] && rb_synced=$((rb_synced + 1))
      done
      if [[ "${rb_synced}" -eq "${#rb_ids[@]}" ]]; then
        good "All ${#rb_ids[@]} role binding tuples synced to Kessel."
        break
      fi
      sleep 2
      rb_sync_wait=$((rb_sync_wait + 2))
    done
    if [[ "${rb_synced}" -lt "${#rb_ids[@]}" ]]; then
      warn "Only ${rb_synced}/${#rb_ids[@]} role binding tuples synced after ${rb_sync_max}s."
    fi
  elif [[ "${#rb_ids[@]}" -eq 0 ]]; then
    warn "No role binding IDs to sync — skipping Kessel wait."
  fi

  fi  # end of non-minimal-data block (roles, groups, role bindings)

  # Create extra workspaces, then delete them via API.
  # Each generates a Kafka remove event for the corrective ADD scenario.
  local api_del_count=2
  [[ "${DR_MINIMAL_DATA}" == "true" ]] && api_del_count=1
  echo ""
  echo -e "${BOLD}=== Setup: Corrective ADD scenario (${api_del_count} workspaces) ===${NC}"
  echo ""
  echo "  Creating ${api_del_count} extra workspaces and deleting them via API."
  echo "  Each API deletion generates a 'remove' event in Kafka."
  echo "  During simulate, we re-insert them into DB to trigger corrective ADD."
  echo ""

  local api_del_ids=() api_del_names=()
  for api_i in $(seq 1 "${api_del_count}"); do
    local extra_name="${DR_KESSEL_WORKSPACE_PREFIX}-${run_tag}-api-del-${api_i}"
    info "Creating workspace ${api_i}/${api_del_count}: '${extra_name}'..."
    local extra_response extra_http
    extra_response=$(curl -s -w '\n%{http_code}' \
      -X POST \
      -u "${BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME}:${EPHEMERAL_PASSWORD}" \
      -H "Content-Type: application/json" \
      -d "{\"name\": \"${extra_name}\"}" \
      "${BENTO_URL}/api/rbac/v2/workspaces/")
    extra_http=$(echo "${extra_response}" | tail -1)
    extra_response=$(echo "${extra_response}" | sed '$d')

    if [[ "${extra_http}" -lt 200 || "${extra_http}" -ge 300 ]]; then
      err "Failed to create extra workspace (HTTP ${extra_http})"
      echo "  Response: ${extra_response}" >&2
      return 1
    fi

    local extra_id
    extra_id=$(echo "${extra_response}" | jq -r '.id')
    api_del_ids+=("${extra_id}")
    api_del_names+=("${extra_name}")
    good "Created: ${extra_name} (${extra_id})"
  done

  # Wait for all extra workspaces to sync to Kessel before deleting.
  if [[ "${DR_FAST}" != "true" ]]; then
    info "Waiting for extra workspaces to sync to Kessel..."
    _wait_for_kessel_sync "${DR_SYNC_WAIT_TIMEOUT}" "${api_del_ids[@]}" || true
  fi

  # Delete each via API — generates relations_to_remove events in Kafka.
  for api_i in $(seq 0 $(( api_del_count - 1 ))); do
    local eid="${api_del_ids[$api_i]}"
    local ename="${api_del_names[$api_i]}"
    info "Deleting workspace '${ename}' via API (generates remove event)..."
    local del_http
    del_http=$(curl -s -o /dev/null -w '%{http_code}' \
      -X DELETE \
      -u "${BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME}:${EPHEMERAL_PASSWORD}" \
      "${BENTO_URL}/api/rbac/v2/workspaces/${eid}/")

    if [[ "${del_http}" -ge 200 && "${del_http}" -lt 300 ]] || [[ "${del_http}" == "204" ]]; then
      good "Deleted via API (HTTP ${del_http}): ${eid}"
    else
      err "Failed to delete workspace via API (HTTP ${del_http}): ${eid}"
      return 1
    fi
  done

  # Wait for all delete events to propagate through Debezium → Kafka → Kessel.
  if [[ "${DR_FAST}" != "true" ]]; then
    info "Waiting for delete events to propagate (tuples removed from Kessel)..."
    local del_confirmed=0
    for eid in "${api_del_ids[@]}"; do
      local del_wait=0 del_max_wait=2
      while [[ "${del_wait}" -lt "${del_max_wait}" ]]; do
        local del_tuple_count
        del_tuple_count=$(echo "$(_read_kessel_workspace_tuples "${eid}")" | jq '.tuples | length' 2>/dev/null || echo "0")
        if [[ "${del_tuple_count}" == "0" ]]; then
          good "Delete confirmed — ${eid}"
          del_confirmed=$((del_confirmed + 1))
          break
        fi
        sleep 2
        del_wait=$((del_wait + 2))
        if (( del_wait % 10 == 0 )); then
          info "Still waiting for ${eid} (${del_wait}s)..."
        fi
      done
      if [[ "${del_wait}" -ge "${del_max_wait}" ]]; then
        warn "${eid}: Kessel still has tuples after ${del_max_wait}s."
      fi
    done
    if [[ "${del_confirmed}" -eq "${api_del_count}" ]]; then
      good "All ${api_del_count} delete events confirmed in Kessel."
  else
    warn "Only ${del_confirmed}/${api_del_count} deletes confirmed. Some corrective ADDs may not trigger."
  fi
  fi

  local api_del_ids_csv api_del_names_csv
  api_del_ids_csv=$(IFS=,; echo "${api_del_ids[*]}")
  api_del_names_csv=$(IFS=,; echo "${api_del_names[*]}")
  dr_state_save DR_API_DELETED_WS_IDS   "${api_del_ids_csv}"
  dr_state_save DR_API_DELETED_WS_NAMES "${api_del_names_csv}"

  if [[ "${DR_MINIMAL_DATA}" != "true" ]]; then
  # --- Role binding corrective ADD scenario ---
  # Create role bindings on the API-deleted workspaces (before they were deleted),
  # then remove them via by-subject update. This generates Kafka remove events.
  echo ""
  echo -e "${BOLD}=== Setup: Role binding corrective ADD scenario (${api_del_count} bindings) ===${NC}"
  echo ""
  echo "  Creating role bindings on API-deleted workspaces, then removing them."
  echo "  The removal generates 'remove' events in Kafka."
  echo "  During simulate, we re-insert them into DB to trigger corrective ADD."
  echo ""

  # The API-deleted workspaces are already gone, so we need role bindings
  # that were created and then removed via the by-subject update API.
  # We create bindings on the REGULAR workspaces and then remove them,
  # using a separate group to avoid conflicting with the kept bindings.
  local add_group_name="dr-group-add-${run_tag}"
  info "Creating second group for ADD scenario: '${add_group_name}'..."
  local add_grp_response add_grp_http
  add_grp_response=$(curl -s -w '\n%{http_code}' \
    -X POST \
    -u "${BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME}:${EPHEMERAL_PASSWORD}" \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"${add_group_name}\"}" \
    "${BENTO_URL}/api/rbac/v1/groups/")
  add_grp_http=$(echo "${add_grp_response}" | tail -1)
  add_grp_response=$(echo "${add_grp_response}" | sed '$d')

  if [[ "${add_grp_http}" -lt 200 || "${add_grp_http}" -ge 300 ]]; then
    err "Failed to create second group (HTTP ${add_grp_http})"
    echo "  Response: ${add_grp_response}" >&2
    return 1
  fi

  local add_group_uuid
  add_group_uuid=$(echo "${add_grp_response}" | jq -r '.uuid')
  dr_state_save DR_ADD_GROUP_UUID "${add_group_uuid}"
  good "Created group: ${add_group_name} (${add_group_uuid})"

  # Create bindings on workspaces with the ADD group.
  local add_rb_requests=""
  local add_rb_ws_ids=()
  local _rb_add_max=2
  [[ "${DR_MINIMAL_DATA}" == "true" ]] && _rb_add_max=1
  for aidx in $(seq 0 $((_rb_add_max - 1))); do
    local awid="${ws_ids[$aidx]}"
    add_rb_ws_ids+=("${awid}")
    [[ -n "${add_rb_requests}" ]] && add_rb_requests="${add_rb_requests},"
    add_rb_requests="${add_rb_requests}{\"resource\":{\"id\":\"${awid}\",\"type\":\"workspace\"},\"subject\":{\"id\":\"${add_group_uuid}\",\"type\":\"group\"},\"role\":{\"id\":\"${role_uuid}\"}}"
  done

  local add_rb_response add_rb_http
  local add_rb_attempt
  for add_rb_attempt in 1 2 3; do
    add_rb_response=$(curl -s -w '\n%{http_code}' --max-time 120 \
      -X POST \
      -u "${BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME}:${EPHEMERAL_PASSWORD}" \
      -H "Content-Type: application/json" \
      -d "{\"requests\": [${add_rb_requests}]}" \
      "${BENTO_URL}/api/rbac/v2/role-bindings:batchCreate/")
    add_rb_http=$(echo "${add_rb_response}" | tail -1)
    add_rb_response=$(echo "${add_rb_response}" | sed '$d')
    if [[ "${add_rb_http}" -ge 200 && "${add_rb_http}" -lt 300 ]]; then
      break
    fi
    warn "ADD-scenario batch create attempt ${add_rb_attempt}/3 failed (HTTP ${add_rb_http}), retrying in 10s..."
    sleep 10
  done

  if [[ "${add_rb_http}" -lt 200 || "${add_rb_http}" -ge 300 ]]; then
    err "Failed to create ADD-scenario role bindings (HTTP ${add_rb_http})"
    echo "  Response: ${add_rb_response}" >&2
    return 1
  fi

  # Query DB for the created binding UUIDs via the group join table.
  local add_rb_ids=()
  local add_ws_id_in=""
  for awid in "${add_rb_ws_ids[@]}"; do
    [[ -n "${add_ws_id_in}" ]] && add_ws_id_in="${add_ws_id_in},"
    add_ws_id_in="${add_ws_id_in}'${awid}'"
  done
  local add_rb_id_list
  add_rb_id_list=$(_db_query "SELECT rb.uuid FROM management_rolebinding rb JOIN management_rolebindinggroup rbg ON rbg.binding_id = rb.id JOIN management_group g ON g.id = rbg.group_id WHERE rb.resource_id IN (${add_ws_id_in}) AND g.uuid = '${add_group_uuid}';" 2>/dev/null)
  while IFS= read -r rid; do
    rid=$(echo "${rid}" | xargs)
    [[ -z "${rid}" ]] && continue
    [[ "${rid}" == "uuid" ]] && continue
    [[ "${rid}" == -* ]] && continue
    [[ "${rid}" == "("*")" ]] && continue
    add_rb_ids+=("${rid}")
  done <<< "${add_rb_id_list}"
  good "Created ${#add_rb_ids[@]} role bindings for ADD scenario"

  # Wait for these bindings to sync to Kessel before removing.
  if [[ "${DR_FAST}" != "true" && "${#add_rb_ids[@]}" -gt 0 ]]; then
    info "Waiting for ADD-scenario bindings to sync to Kessel..."
    local arb_wait=0 arb_max=2
    while [[ "${arb_wait}" -lt "${arb_max}" ]]; do
      local arb_synced=0
      for rid in "${add_rb_ids[@]}"; do
        local tc
        tc=$(echo "$(_read_kessel_role_binding_tuples "${rid}")" | jq '.tuples | length' 2>/dev/null || echo "0")
        [[ "${tc}" -gt 0 ]] && arb_synced=$((arb_synced + 1))
      done
      [[ "${arb_synced}" -eq "${#add_rb_ids[@]}" ]] && break
      sleep 2
      arb_wait=$((arb_wait + 2))
    done
  elif [[ "${#add_rb_ids[@]}" -eq 0 ]]; then
    warn "No ADD-scenario role binding IDs found — skipping Kessel wait."
  fi

  # Remove bindings via by-subject update with empty roles.
  if [[ "${#add_rb_ids[@]}" -gt 0 ]]; then
    for awid in "${add_rb_ws_ids[@]}"; do
      info "Removing role bindings on workspace ${awid} via by-subject update..."
      local upd_http
      upd_http=$(curl -s -o /dev/null -w '%{http_code}' \
        -X PUT \
        -u "${BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME}:${EPHEMERAL_PASSWORD}" \
        -H "Content-Type: application/json" \
        -d '{"roles": []}' \
        "${BENTO_URL}/api/rbac/v2/role-bindings/by-subject/?subject_type=group&subject_id=${add_group_uuid}&resource_type=workspace&resource_id=${awid}")
      if [[ "${upd_http}" -ge 200 && "${upd_http}" -lt 300 ]]; then
        good "Removed bindings on workspace ${awid} (HTTP ${upd_http})"
      else
        warn "by-subject update returned HTTP ${upd_http} for workspace ${awid}"
      fi
    done

    # Wait for Kessel to process the removal.
    if [[ "${DR_FAST}" != "true" ]]; then
      info "Waiting for role binding removal to propagate to Kessel..."
      local del_rb_confirmed=0
      for rid in "${add_rb_ids[@]}"; do
        local drb_wait=0 drb_max=2
        while [[ "${drb_wait}" -lt "${drb_max}" ]]; do
          local tc
          tc=$(echo "$(_read_kessel_role_binding_tuples "${rid}")" | jq '.tuples | length' 2>/dev/null || echo "0")
          if [[ "${tc}" == "0" ]]; then
            del_rb_confirmed=$((del_rb_confirmed + 1))
            break
          fi
          sleep 2
          drb_wait=$((drb_wait + 2))
        done
      done
      if [[ "${del_rb_confirmed}" -eq "${#add_rb_ids[@]}" ]]; then
        good "All ${#add_rb_ids[@]} role binding removals confirmed in Kessel."
      else
        warn "Only ${del_rb_confirmed}/${#add_rb_ids[@]} removals confirmed."
      fi
    fi
  fi

  local add_rb_ids_csv=""
  if [[ "${#add_rb_ids[@]}" -gt 0 ]]; then
    add_rb_ids_csv=$(IFS=,; echo "${add_rb_ids[*]}")
  fi
  dr_state_save DR_API_DELETED_RB_IDS  "${add_rb_ids_csv}"
  # Save the workspace IDs these bindings were on (needed for re-insertion).
  local add_rb_ws_csv
  add_rb_ws_csv=$(IFS=,; echo "${add_rb_ws_ids[*]}")
  dr_state_save DR_API_DELETED_RB_WS_IDS "${add_rb_ws_csv}"

  # --- Role (RoleV2) scenario setup ---
  # Create 6 test roles. 2 will be API-deleted (for corrective ADD),
  # 2 will be DB-deleted in simulate (corrective REMOVE), 2 kept (SKIP).
  echo ""
  echo -e "${BOLD}=== Setup: Test Roles ===${NC}"
  echo ""
  local dr_role_count=6
  [[ "${DR_MINIMAL_DATA}" == "true" ]] && dr_role_count=3
  local dr_role_ids=()
  for ridx in $(seq 1 "${dr_role_count}"); do
    local rname="dr-role-${ridx}-${run_tag}"
    local r_resp r_http
    r_resp=$(curl -s -w '\n%{http_code}' \
      -X POST \
      -u "${BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME}:${EPHEMERAL_PASSWORD}" \
      -H "Content-Type: application/json" \
      -d "{\"name\": \"${rname}\", \"display_name\": \"DR Test Role ${ridx}\", \"permissions\": [{\"application\": \"inventory\", \"resource_type\": \"hosts\", \"operation\": \"read\"}]}" \
      "${BENTO_URL}/api/rbac/v2/roles/")
    r_http=$(echo "${r_resp}" | tail -1)
    r_resp=$(echo "${r_resp}" | sed '$d')
    if [[ "${r_http}" -lt 200 || "${r_http}" -ge 300 ]]; then
      err "Failed to create role ${rname} (HTTP ${r_http})"
      echo "  Response: ${r_resp}" >&2
      return 1
    fi
    local ruuid
    ruuid=$(echo "${r_resp}" | jq -r '.id // .uuid')
    dr_role_ids+=("${ruuid}")
    good "Created role: ${rname} (${ruuid})"
  done
  local dr_role_ids_csv
  dr_role_ids_csv=$(IFS=,; echo "${dr_role_ids[*]}")
  dr_state_save DR_ROLE_TEST_IDS "${dr_role_ids_csv}"

  # Wait for role tuples to appear in Kessel.
  if [[ "${DR_FAST}" != "true" ]]; then
    info "Waiting for role permission tuples to sync to Kessel..."
    local rl_wait=0 rl_max=2
    while [[ "${rl_wait}" -lt "${rl_max}" ]]; do
      local rl_synced=0
      for ruuid in "${dr_role_ids[@]}"; do
        local tc
        tc=$(echo "$(_read_kessel_role_tuples "${ruuid}")" | jq '.tuples | length' 2>/dev/null || echo "0")
        [[ "${tc}" -gt 0 ]] && rl_synced=$((rl_synced + 1))
      done
      [[ "${rl_synced}" -eq "${#dr_role_ids[@]}" ]] && break
      sleep 2
      rl_wait=$((rl_wait + 2))
    done
    if [[ "${rl_wait}" -ge "${rl_max}" ]]; then
      warn "Timed out waiting for role tuple sync."
    else
      good "All ${#dr_role_ids[@]} roles have tuples in Kessel."
    fi
  fi

  # API-delete last role(s) (for corrective ADD scenario) via batchDelete.
  local api_del_role_ids=()
  local batch_del_ids=""
  local _role_add_count=2
  [[ "${DR_MINIMAL_DATA}" == "true" ]] && _role_add_count=1
  local _role_del_start=$(( dr_role_count - _role_add_count ))
  for didx in $(seq "${_role_del_start}" $(( dr_role_count - 1 ))); do
    local drid="${dr_role_ids[$didx]}"
    [[ -n "${batch_del_ids}" ]] && batch_del_ids="${batch_del_ids},"
    batch_del_ids="${batch_del_ids}\"${drid}\""
    api_del_role_ids+=("${drid}")
  done
  info "Batch-deleting roles: ${api_del_role_ids[*]}..."
  local del_resp del_http
  del_resp=$(curl -s -w '\n%{http_code}' --max-time 120 \
    -X POST \
    -u "${BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME}:${EPHEMERAL_PASSWORD}" \
    -H "Content-Type: application/json" \
    -d "{\"ids\": [${batch_del_ids}]}" \
    "${BENTO_URL}/api/rbac/v2/roles:batchDelete/")
  del_http=$(echo "${del_resp}" | tail -1)
  if [[ "${del_http}" -ge 200 && "${del_http}" -lt 300 ]]; then
    good "Batch-deleted ${#api_del_role_ids[@]} roles (HTTP ${del_http})"
  else
    warn "Role batch delete returned HTTP ${del_http}"
    del_resp=$(echo "${del_resp}" | sed '$d')
    echo "  Response: ${del_resp}" >&2
    api_del_role_ids=()
  fi

  # Wait for Kessel tuple removal.
  if [[ "${DR_FAST}" != "true" && "${#api_del_role_ids[@]}" -gt 0 ]]; then
    info "Waiting for role tuple removal to propagate..."
    for drid in "${api_del_role_ids[@]}"; do
      local dr_wait=0 dr_max=2
      while [[ "${dr_wait}" -lt "${dr_max}" ]]; do
        local tc
        tc=$(echo "$(_read_kessel_role_tuples "${drid}")" | jq '.tuples | length' 2>/dev/null || echo "0")
        [[ "${tc}" == "0" ]] && break
        sleep 2
        dr_wait=$((dr_wait + 2))
      done
    done
    good "Role tuple removal confirmed."
  elif [[ "${#api_del_role_ids[@]}" -eq 0 ]]; then
    warn "No roles were API-deleted — skipping Kessel wait."
  fi

  local api_del_role_csv=""
  if [[ "${#api_del_role_ids[@]}" -gt 0 ]]; then
    api_del_role_csv=$(IFS=,; echo "${api_del_role_ids[*]}")
  fi
  dr_state_save DR_API_DELETED_ROLE_IDS "${api_del_role_csv}"

  # --- Group scenario setup ---
  # Create 6 test groups and add a principal to each (generating member tuples).
  echo ""
  echo -e "${BOLD}=== Setup: Test Groups ===${NC}"
  echo ""

  # Find an existing principal for the org to use as group member.
  info "Looking up a principal for the org..."
  local principals_resp
  principals_resp=$(curl -s \
    -u "${BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME}:${EPHEMERAL_PASSWORD}" \
    "${BENTO_URL}/api/rbac/v1/principals/?limit=1&type=user")
  local test_username
  test_username=$(echo "${principals_resp}" | jq -r '.data[0].username // empty' 2>/dev/null)
  if [[ -z "${test_username}" ]]; then
    warn "No principals found for org. Trying current user..."
    test_username="${BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME}"
  fi
  dr_state_save DR_TEST_USERNAME "${test_username}"
  good "Using principal: ${test_username}"

  local dr_group_count=6
  [[ "${DR_MINIMAL_DATA}" == "true" ]] && dr_group_count=3
  local dr_group_ids=()
  for gidx in $(seq 1 "${dr_group_count}"); do
    local gname="dr-group-${gidx}-${run_tag}"
    local g_resp g_http
    g_resp=$(curl -s -w '\n%{http_code}' \
      -X POST \
      -u "${BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME}:${EPHEMERAL_PASSWORD}" \
      -H "Content-Type: application/json" \
      -d "{\"name\": \"${gname}\"}" \
      "${BENTO_URL}/api/rbac/v1/groups/")
    g_http=$(echo "${g_resp}" | tail -1)
    g_resp=$(echo "${g_resp}" | sed '$d')
    if [[ "${g_http}" -lt 200 || "${g_http}" -ge 300 ]]; then
      err "Failed to create group ${gname} (HTTP ${g_http})"
      echo "  Response: ${g_resp}" >&2
      return 1
    fi
    local guuid
    guuid=$(echo "${g_resp}" | jq -r '.uuid')
    dr_group_ids+=("${guuid}")
    good "Created group: ${gname} (${guuid})"

    # Add principal to the group.
    local addp_http
    addp_http=$(curl -s -o /dev/null -w '%{http_code}' \
      -X POST \
      -u "${BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME}:${EPHEMERAL_PASSWORD}" \
      -H "Content-Type: application/json" \
      -d "{\"principals\": [{\"username\": \"${test_username}\"}]}" \
      "${BENTO_URL}/api/rbac/v1/groups/${guuid}/principals/")
    if [[ "${addp_http}" -lt 200 || "${addp_http}" -ge 300 ]]; then
      warn "Failed to add principal to group ${guuid} (HTTP ${addp_http})"
    fi
  done
  local dr_group_ids_csv
  dr_group_ids_csv=$(IFS=,; echo "${dr_group_ids[*]}")
  dr_state_save DR_GROUP_TEST_IDS "${dr_group_ids_csv}"

  # Wait for group member tuples to appear in Kessel.
  if [[ "${DR_FAST}" != "true" ]]; then
    info "Waiting for group member tuples to sync to Kessel..."
    local gw=0 gw_max=2
    while [[ "${gw}" -lt "${gw_max}" ]]; do
      local gs=0
      for guuid in "${dr_group_ids[@]}"; do
        local tc
        tc=$(echo "$(_read_kessel_group_tuples "${guuid}")" | jq '.tuples | length' 2>/dev/null || echo "0")
        [[ "${tc}" -gt 0 ]] && gs=$((gs + 1))
      done
      [[ "${gs}" -eq "${#dr_group_ids[@]}" ]] && break
      sleep 2
      gw=$((gw + 2))
    done
    if [[ "${gw}" -ge "${gw_max}" ]]; then
      warn "Timed out waiting for group tuple sync."
    else
      good "All ${#dr_group_ids[@]} groups have member tuples in Kessel."
    fi
  fi

  # API-delete last group(s) (for corrective ADD scenario).
  local api_del_group_ids=()
  local _grp_add_count=2
  [[ "${DR_MINIMAL_DATA}" == "true" ]] && _grp_add_count=1
  local _grp_del_start=$(( dr_group_count - _grp_add_count ))
  for didx in $(seq "${_grp_del_start}" $(( dr_group_count - 1 ))); do
    local dgid="${dr_group_ids[$didx]}"
    info "API-deleting group ${dgid}..."
    local del_http
    del_http=$(curl -s -o /dev/null -w '%{http_code}' \
      -X DELETE \
      -u "${BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME}:${EPHEMERAL_PASSWORD}" \
      "${BENTO_URL}/api/rbac/v1/groups/${dgid}/")
    if [[ "${del_http}" -ge 200 && "${del_http}" -lt 300 ]] || [[ "${del_http}" == "204" ]]; then
      good "Deleted group ${dgid} (HTTP ${del_http})"
      api_del_group_ids+=("${dgid}")
    else
      warn "Group delete returned HTTP ${del_http} for ${dgid}"
    fi
  done

  # Wait for Kessel tuple removal.
  if [[ "${DR_FAST}" != "true" ]]; then
    info "Waiting for group tuple removal to propagate..."
    for dgid in "${api_del_group_ids[@]}"; do
      local dg_wait=0 dg_max=2
      while [[ "${dg_wait}" -lt "${dg_max}" ]]; do
        local tc
        tc=$(echo "$(_read_kessel_group_tuples "${dgid}")" | jq '.tuples | length' 2>/dev/null || echo "0")
        [[ "${tc}" == "0" ]] && break
        sleep 2
        dg_wait=$((dg_wait + 2))
      done
    done
    good "Group tuple removal confirmed."
  fi

  local api_del_group_csv
  api_del_group_csv=$(IFS=,; echo "${api_del_group_ids[*]}")
  dr_state_save DR_API_DELETED_GROUP_IDS "${api_del_group_csv}"

  fi  # end of non-minimal-data block (role binding ADD, roles, groups)

  echo ""
  echo -e "${BOLD}=== Setup Summary ===${NC}"
  echo ""
  echo "  Workspaces:"
  echo "    ${DR_KESSEL_WORKSPACE_COUNT} created (Kafka add events)"
  echo "    ${api_del_count} extra created then API-deleted (Kafka add + remove events)"
  if [[ "${DR_MINIMAL_DATA}" != "true" ]]; then
    echo ""
    echo "  Role bindings:"
    echo "    ${#rb_ids[@]} created on workspaces (Kafka add events)"
    echo "    ${#add_rb_ids[@]} extra created then removed via API (Kafka add + remove events)"
    echo ""
    echo "  Roles:"
    echo "    ${#dr_role_ids[@]} created (Kafka add events)"
    echo "    ${#api_del_role_ids[@]} API-deleted (Kafka add + remove events)"
    echo ""
    echo "  Groups:"
    echo "    ${#dr_group_ids[@]} created with principal (Kafka add events)"
    echo "    ${#api_del_group_ids[@]} API-deleted (Kafka add + remove events)"
  fi
  echo ""
  echo "  Simulate step will create divergence scenarios."
  info "Next: DR_STEP=simulate $0 --rbac-kessel"
}

dr_kessel_simulate() {
  # Simulate a DB restore that creates three types of divergence:
  #   1. DELETE some workspaces from DB → corrective REMOVE scenario
  #   2. KEEP some workspaces in DB     → skip scenario (consistent)
  #   3. RE-INSERT API-deleted workspace → corrective ADD scenario
  #   4. INSERT ghost workspace          → not fixable by event-based reconcile
  _kessel_require_org_id || return 1
  dr_state_load

  local ws_ids_csv="${DR_KESSEL_WORKSPACE_IDS:-${DR_KESSEL_WORKSPACE_ID:-}}"
  if [[ -z "$ws_ids_csv" ]]; then
    err "No workspace IDs in state. Run setup first."
    return 1
  fi

  IFS=',' read -ra ws_ids <<< "${ws_ids_csv}"
  local api_deleted_ids_csv="${DR_API_DELETED_WS_IDS:-}"
  local api_deleted_names_csv="${DR_API_DELETED_WS_NAMES:-}"
  local api_deleted_ids=() api_deleted_names=()
  [[ -n "${api_deleted_ids_csv}" ]] && IFS=',' read -ra api_deleted_ids <<< "${api_deleted_ids_csv}"
  [[ -n "${api_deleted_names_csv}" ]] && IFS=',' read -ra api_deleted_names <<< "${api_deleted_names_csv}"

  echo ""
  echo -e "${BOLD}==================================================================${NC}"
  echo -e "${BOLD}  SIMULATE: Creating RBAC <-> Kessel divergence${NC}"
  echo -e "${BOLD}==================================================================${NC}"
  echo ""
  echo "  This simulates a DB restore from backup, creating three scenarios:"
  echo ""
  echo "    Scenario 1 — Corrective REMOVE:"
  echo "      Workspace created after backup, then DB restored → gone from DB"
  echo "      but stale tuple remains in Kessel."
  echo ""
  echo "    Scenario 2 — Skip (consistent):"
  echo "      Workspace existed at backup time → still in restored DB"
  echo "      and tuple exists in Kessel. No action needed."
  echo ""
  echo "    Scenario 3 — Corrective ADD:"
  echo "      Workspace deleted after backup, then DB restored → back in DB"
  echo "      but tuple was already removed from Kessel."
  echo ""

  local restore_ts
  restore_ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  dr_state_save DR_RESTORE_TIMESTAMP "${restore_ts}"
  info "Restore timestamp: ${restore_ts}"

  _db_creds || return 1

  # Split workspaces: first half deleted (REMOVE), rest stay (SKIP).
  local _ws_split=$(( ${#ws_ids[@]} / 2 ))
  local delete_ids=() keep_ids=()
  local idx=0
  for ws_id in "${ws_ids[@]}"; do
    if [[ "${idx}" -lt "${_ws_split}" ]]; then
      delete_ids+=("${ws_id}")
    else
      keep_ids+=("${ws_id}")
    fi
    idx=$((idx + 1))
  done

  # Scenario 1: Delete first workspace from DB (corrective REMOVE).
  echo ""
  echo -e "--- Scenario 1: Corrective REMOVE (delete from DB) ---"
  echo "  Kafka has 'add' events for these, but resource is gone from DB."
  echo "  Reconciler will emit corrective REMOVE to clean stale Kessel tuples."
  echo ""
  for ws_id in "${delete_ids[@]}"; do
    echo "  Deleting ${ws_id} from DB..."
    _db_query "DELETE FROM management_workspace WHERE id='${ws_id}' RETURNING id, name, type;" 2>/dev/null || true
    good "Deleted: ${ws_id}"
  done
  dr_state_save DR_CORRECTIVE_REMOVE_IDS "$(IFS=,; echo "${delete_ids[*]}")"

  # Scenario 2: Keep remaining workspaces in DB (skip).
  echo ""
  echo -e "--- Scenario 2: Skip / consistent (keep in DB) ---"
  echo "  Kafka has 'add' events for these, and resource still exists in DB."
  echo "  Reconciler will skip — both sides are consistent."
  echo ""
  for ws_id in "${keep_ids[@]}"; do
    good "Kept in DB: ${ws_id}"
  done
  dr_state_save DR_SKIP_IDS "$(IFS=,; echo "${keep_ids[*]}")"

  # Scenario 3: Re-insert API-deleted workspaces (corrective ADD).
  if [[ "${#api_deleted_ids[@]}" -gt 0 ]]; then
    echo ""
    echo -e "--- Scenario 3: Corrective ADD (re-insert ${#api_deleted_ids[@]} API-deleted workspaces) ---"
    echo "  Kafka has 'remove' events for these (API deletion during setup)."
    echo "  We re-insert them into DB to simulate being in the backup."
    echo "  Reconciler will emit corrective ADD to restore Kessel tuples."
    echo ""

    local tenant_id
    tenant_id=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
      psql -U "${_db_user}" -d "${_db_name}" -t -A \
      -c "SELECT id FROM api_tenant WHERE org_id='${DR_KESSEL_ORG_ID}';" 2>/dev/null | tr -d '\r')
    local parent_id
    parent_id=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
      psql -U "${_db_user}" -d "${_db_name}" -t -A \
      -c "SELECT id FROM management_workspace WHERE tenant_id=${tenant_id} AND type='root' LIMIT 1;" 2>/dev/null | tr -d '\r')

    if [[ -n "${tenant_id}" && -n "${parent_id}" ]]; then
      local add_ids=()
      for ai in $(seq 0 $(( ${#api_deleted_ids[@]} - 1 ))); do
        local aid="${api_deleted_ids[$ai]}"
        local aname="${api_deleted_names[$ai]:-dr-api-deleted-$((ai+1))}"
        PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
          psql -U "${_db_user}" -d "${_db_name}" -c \
          "INSERT INTO management_workspace (id, name, type, tenant_id, parent_id, created, modified) VALUES ('${aid}', '${aname}', 'standard', ${tenant_id}, '${parent_id}', NOW(), NOW()) ON CONFLICT DO NOTHING;" 2>/dev/null || true
        good "Re-inserted: ${aid} (simulates backup restore)"
        add_ids+=("${aid}")
      done
      dr_state_save DR_CORRECTIVE_ADD_IDS "$(IFS=,; echo "${add_ids[*]}")"
    else
      warn "Could not determine tenant_id/parent_id for re-insert."
    fi
  else
    warn "No API-deleted workspaces found in state — corrective ADD scenario skipped."
  fi

  # Ghost workspace (exists in DB but Kessel has no tuple).
  echo ""
  echo "--- Ghost workspace (DB only, no Kafka event) ---"
  echo "  Not fixable by event-based reconcile — demonstrates the limitation."
  echo ""
  local tenant_id_ghost="${tenant_id:-}"
  local parent_id_ghost="${parent_id:-}"
  if [[ -z "${tenant_id_ghost}" ]]; then
    tenant_id_ghost=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
      psql -U "${_db_user}" -d "${_db_name}" -t -A \
      -c "SELECT id FROM api_tenant WHERE org_id='${DR_KESSEL_ORG_ID}';" 2>/dev/null | tr -d '\r')
  fi
  if [[ -z "${parent_id_ghost}" ]]; then
    parent_id_ghost=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
      psql -U "${_db_user}" -d "${_db_name}" -t -A \
      -c "SELECT id FROM management_workspace WHERE tenant_id=${tenant_id_ghost} AND type='root' LIMIT 1;" 2>/dev/null | tr -d '\r')
  fi

  if [[ -n "${tenant_id_ghost}" && -n "${parent_id_ghost}" ]]; then
    local ghost_id
    ghost_id=$(python3 -c "import uuid; print(uuid.uuid4())" 2>/dev/null || echo "00000000-dead-beef-0000-$(date +%s)")
    PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
      psql -U "${_db_user}" -d "${_db_name}" -c \
      "INSERT INTO management_workspace (id, name, type, tenant_id, parent_id, created, modified) VALUES ('${ghost_id}', 'dr-ghost-workspace', 'standard', ${tenant_id_ghost}, '${parent_id_ghost}', NOW(), NOW()) ON CONFLICT DO NOTHING;" 2>/dev/null || true
    good "Ghost workspace: dr-ghost-workspace (${ghost_id})"
    dr_state_save DR_GHOST_WORKSPACE_ID "${ghost_id}"
  else
    warn "Could not determine tenant_id or root workspace for ghost insert."
  fi

  # Save legacy var for backward compat.
  dr_state_save DR_DELETED_WORKSPACE_IDS "$(IFS=,; echo "${delete_ids[*]}")"

  if [[ "${DR_MINIMAL_DATA}" != "true" ]]; then
  # ── Role binding scenarios ──
  local rb_ids_csv="${DR_ROLE_BINDING_IDS:-}"
  local api_del_rb_ids_csv="${DR_API_DELETED_RB_IDS:-}"
  local api_del_rb_ws_csv="${DR_API_DELETED_RB_WS_IDS:-}"
  local role_uuid="${DR_ROLE_UUID:-}"
  local group_uuid="${DR_GROUP_UUID:-}"
  local add_group_uuid="${DR_ADD_GROUP_UUID:-}"

  local rb_ids=() api_del_rb_ids=() api_del_rb_ws_ids=()
  [[ -n "${rb_ids_csv}" ]] && IFS=',' read -ra rb_ids <<< "${rb_ids_csv}"
  [[ -n "${api_del_rb_ids_csv}" ]] && IFS=',' read -ra api_del_rb_ids <<< "${api_del_rb_ids_csv}"
  [[ -n "${api_del_rb_ws_csv}" ]] && IFS=',' read -ra api_del_rb_ws_ids <<< "${api_del_rb_ws_csv}"

  if [[ "${#rb_ids[@]}" -gt 0 ]]; then
    # Split role bindings: first half deleted (REMOVE), rest kept (SKIP).
    local _rb_split=$(( ${#rb_ids[@]} / 2 ))
    local rb_delete_ids=() rb_keep_ids=()
    local rb_idx=0
    for rid in "${rb_ids[@]}"; do
      if [[ "${rb_idx}" -lt "${_rb_split}" ]]; then
        rb_delete_ids+=("${rid}")
      else
        rb_keep_ids+=("${rid}")
      fi
      rb_idx=$((rb_idx + 1))
    done

    # Scenario 1: Delete role bindings from DB (corrective REMOVE).
    echo ""
    echo -e "--- Role Binding Scenario 1: Corrective REMOVE (delete from DB) ---"
    echo "  Kafka has 'add' events for these, but role binding is gone from DB."
    echo ""
    for rid in "${rb_delete_ids[@]}"; do
      echo "  Deleting role binding ${rid} from DB..."
      _db_query "DELETE FROM management_rolebindinggroup WHERE binding_id IN (SELECT id FROM management_rolebinding WHERE uuid='${rid}');" 2>/dev/null || true
      _db_query "DELETE FROM management_rolebindingprincipal WHERE binding_id IN (SELECT id FROM management_rolebinding WHERE uuid='${rid}');" 2>/dev/null || true
      _db_query "DELETE FROM management_rolebinding WHERE uuid='${rid}' RETURNING uuid;" 2>/dev/null || true
      good "Deleted: ${rid}"
    done
    dr_state_save DR_RB_CORRECTIVE_REMOVE_IDS "$(IFS=,; echo "${rb_delete_ids[*]}")"

    # Scenario 2: Keep remaining role bindings in DB (skip).
    echo ""
    echo -e "--- Role Binding Scenario 2: Skip / consistent (keep in DB) ---"
    echo ""
    for rid in "${rb_keep_ids[@]}"; do
      good "Kept in DB: ${rid}"
    done
    dr_state_save DR_RB_SKIP_IDS "$(IFS=,; echo "${rb_keep_ids[*]}")"
  fi

  # Scenario 3: Re-insert API-deleted role bindings (corrective ADD).
  if [[ "${#api_del_rb_ids[@]}" -gt 0 && -n "${role_uuid}" ]]; then
    echo ""
    echo -e "--- Role Binding Scenario 3: Corrective ADD (re-insert API-deleted bindings) ---"
    echo "  Kafka has 'remove' events for these (API removal during setup)."
    echo "  We re-insert them into DB to simulate being in the backup."
    echo ""

    # Get the role's integer PK and group's integer PK for FK references.
    local role_int_id group_int_id
    role_int_id=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
      psql -U "${_db_user}" -d "${_db_name}" -t -A \
      -c "SELECT id FROM management_rolev2 WHERE uuid='${role_uuid}';" 2>/dev/null | tr -d '\r')
    group_int_id=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
      psql -U "${_db_user}" -d "${_db_name}" -t -A \
      -c "SELECT id FROM management_group WHERE uuid='${add_group_uuid}';" 2>/dev/null | tr -d '\r')

    if [[ -n "${role_int_id}" && -n "${group_int_id}" && -n "${tenant_id:-}" ]]; then
      local rb_add_ids=()
      for ai in $(seq 0 $(( ${#api_del_rb_ids[@]} - 1 ))); do
        local rbid="${api_del_rb_ids[$ai]}"
        local rbwsid="${api_del_rb_ws_ids[$ai]:-}"
        PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
          psql -U "${_db_user}" -d "${_db_name}" -c \
          "INSERT INTO management_rolebinding (uuid, role_id, resource_type, resource_id, tenant_id) VALUES ('${rbid}', ${role_int_id}, 'workspace', '${rbwsid}', ${tenant_id}) ON CONFLICT DO NOTHING;" 2>/dev/null || true
        # Re-create the join table entry.
        local rb_int_id
        rb_int_id=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
          psql -U "${_db_user}" -d "${_db_name}" -t -A \
          -c "SELECT id FROM management_rolebinding WHERE uuid='${rbid}';" 2>/dev/null | tr -d '\r')
        if [[ -n "${rb_int_id}" ]]; then
          PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
            psql -U "${_db_user}" -d "${_db_name}" -c \
            "INSERT INTO management_rolebindinggroup (binding_id, group_id, created) VALUES (${rb_int_id}, ${group_int_id}, NOW()) ON CONFLICT DO NOTHING;" 2>/dev/null || true
        fi
        good "Re-inserted: ${rbid} (simulates backup restore)"
        rb_add_ids+=("${rbid}")
      done
      dr_state_save DR_RB_CORRECTIVE_ADD_IDS "$(IFS=,; echo "${rb_add_ids[*]}")"
    else
      warn "Could not determine role/group/tenant IDs for role binding re-insert."
      warn "  role_int_id=${role_int_id:-?} group_int_id=${group_int_id:-?} tenant_id=${tenant_id:-?}"
    fi
  fi

  # ── Role (RoleV2) scenarios ──
  local role_test_ids_csv="${DR_ROLE_TEST_IDS:-}"
  local api_del_role_ids_csv="${DR_API_DELETED_ROLE_IDS:-}"
  local role_test_ids=() api_del_role_ids_sim=()
  [[ -n "${role_test_ids_csv}" ]] && IFS=',' read -ra role_test_ids <<< "${role_test_ids_csv}"
  [[ -n "${api_del_role_ids_csv}" ]] && IFS=',' read -ra api_del_role_ids_sim <<< "${api_del_role_ids_csv}"

  if [[ "${#role_test_ids[@]}" -gt 0 ]]; then
    # Split: first third deleted (REMOVE), next third kept (SKIP). Last third are API-deleted (ADD).
    local _role_third=$(( ${#role_test_ids[@]} / 3 ))
    local role_delete_ids=() role_keep_ids=()
    local ri=0
    for ruuid in "${role_test_ids[@]}"; do
      if [[ "${ri}" -lt "${_role_third}" ]]; then
        role_delete_ids+=("${ruuid}")
      elif [[ "${ri}" -lt $(( _role_third * 2 )) ]]; then
        role_keep_ids+=("${ruuid}")
      fi
      ri=$((ri + 1))
    done

    echo ""
    echo -e "--- Role Scenario 1: Corrective REMOVE (delete from DB) ---"
    echo ""
    for ruuid in "${role_delete_ids[@]}"; do
      echo "  Deleting role ${ruuid} from DB..."
      _db_query "DELETE FROM management_rolev2_permissions WHERE rolev2_id IN (SELECT id FROM management_rolev2 WHERE uuid='${ruuid}');" 2>/dev/null || true
      _db_query "DELETE FROM management_rolev2 WHERE uuid='${ruuid}' RETURNING uuid;" 2>/dev/null || true
      good "Deleted: ${ruuid}"
    done
    dr_state_save DR_ROLE_CORRECTIVE_REMOVE_IDS "$(IFS=,; echo "${role_delete_ids[*]}")"

    echo ""
    echo -e "--- Role Scenario 2: Skip / consistent (keep in DB) ---"
    echo ""
    for ruuid in "${role_keep_ids[@]}"; do
      good "Kept in DB: ${ruuid}"
    done
    dr_state_save DR_ROLE_SKIP_IDS "$(IFS=,; echo "${role_keep_ids[*]}")"
  fi

  # Role Scenario 3: Re-insert API-deleted roles (corrective ADD).
  if [[ "${#api_del_role_ids_sim[@]}" -gt 0 ]]; then
    echo ""
    echo -e "--- Role Scenario 3: Corrective ADD (re-insert API-deleted roles) ---"
    echo ""
    local role_tenant_id="${tenant_id:-}"
    if [[ -z "${role_tenant_id}" ]]; then
      role_tenant_id=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT id FROM api_tenant WHERE org_id='${DR_KESSEL_ORG_ID}';" 2>/dev/null | tr -d '\r')
    fi
    if [[ -n "${role_tenant_id}" ]]; then
      # Find or create the permission row for inventory:hosts:read.
      local perm_id
      perm_id=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT id FROM management_permission WHERE permission='inventory:hosts:read' LIMIT 1;" 2>/dev/null | tr -d '\r')
      local role_add_ids=()
      for ruuid in "${api_del_role_ids_sim[@]}"; do
        PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
          psql -U "${_db_user}" -d "${_db_name}" -c \
          "INSERT INTO management_rolev2 (uuid, tenant_id, name, type, created, modified) VALUES ('${ruuid}', ${role_tenant_id}, 'dr-readd-${ruuid:0:8}', 'custom', NOW(), NOW()) ON CONFLICT DO NOTHING;" 2>/dev/null || true
        # Re-create permission M2M if we have the permission ID.
        if [[ -n "${perm_id}" ]]; then
          local role_int
          role_int=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
            psql -U "${_db_user}" -d "${_db_name}" -t -A \
            -c "SELECT id FROM management_rolev2 WHERE uuid='${ruuid}';" 2>/dev/null | tr -d '\r')
          if [[ -n "${role_int}" ]]; then
            PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
              psql -U "${_db_user}" -d "${_db_name}" -c \
              "INSERT INTO management_rolev2_permissions (rolev2_id, permission_id) VALUES (${role_int}, ${perm_id}) ON CONFLICT DO NOTHING;" 2>/dev/null || true
          fi
        fi
        good "Re-inserted: ${ruuid}"
        role_add_ids+=("${ruuid}")
      done
      dr_state_save DR_ROLE_CORRECTIVE_ADD_IDS "$(IFS=,; echo "${role_add_ids[*]}")"
    else
      warn "Could not determine tenant_id for role re-insert."
    fi
  fi

  # ── Group scenarios ──
  local group_test_ids_csv="${DR_GROUP_TEST_IDS:-}"
  local api_del_group_ids_csv="${DR_API_DELETED_GROUP_IDS:-}"
  local test_username="${DR_TEST_USERNAME:-}"
  local group_test_ids=() api_del_group_ids_sim=()
  [[ -n "${group_test_ids_csv}" ]] && IFS=',' read -ra group_test_ids <<< "${group_test_ids_csv}"
  [[ -n "${api_del_group_ids_csv}" ]] && IFS=',' read -ra api_del_group_ids_sim <<< "${api_del_group_ids_csv}"

  if [[ "${#group_test_ids[@]}" -gt 0 ]]; then
    local _grp_third=$(( ${#group_test_ids[@]} / 3 ))
    local group_delete_ids=() group_keep_ids=()
    local gi=0
    for guuid in "${group_test_ids[@]}"; do
      if [[ "${gi}" -lt "${_grp_third}" ]]; then
        group_delete_ids+=("${guuid}")
      elif [[ "${gi}" -lt $(( _grp_third * 2 )) ]]; then
        group_keep_ids+=("${guuid}")
      fi
      gi=$((gi + 1))
    done

    echo ""
    echo -e "--- Group Scenario 1: Corrective REMOVE (delete from DB) ---"
    echo ""
    for guuid in "${group_delete_ids[@]}"; do
      echo "  Deleting group ${guuid} from DB..."
      _db_query "DELETE FROM management_group_principals WHERE group_id IN (SELECT id FROM management_group WHERE uuid='${guuid}');" 2>/dev/null || true
      _db_query "DELETE FROM management_group WHERE uuid='${guuid}' RETURNING uuid;" 2>/dev/null || true
      good "Deleted: ${guuid}"
    done
    dr_state_save DR_GROUP_CORRECTIVE_REMOVE_IDS "$(IFS=,; echo "${group_delete_ids[*]}")"

    echo ""
    echo -e "--- Group Scenario 2: Skip / consistent (keep in DB) ---"
    echo ""
    for guuid in "${group_keep_ids[@]}"; do
      good "Kept in DB: ${guuid}"
    done
    dr_state_save DR_GROUP_SKIP_IDS "$(IFS=,; echo "${group_keep_ids[*]}")"
  fi

  # Group Scenario 3: Re-insert API-deleted groups (corrective ADD).
  if [[ "${#api_del_group_ids_sim[@]}" -gt 0 ]]; then
    echo ""
    echo -e "--- Group Scenario 3: Corrective ADD (re-insert API-deleted groups) ---"
    echo ""
    local grp_tenant_id="${tenant_id:-}"
    if [[ -z "${grp_tenant_id}" ]]; then
      grp_tenant_id=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT id FROM api_tenant WHERE org_id='${DR_KESSEL_ORG_ID}';" 2>/dev/null | tr -d '\r')
    fi
    # Find the principal's integer ID for re-creating M2M.
    local principal_int_id=""
    if [[ -n "${test_username}" && -n "${grp_tenant_id}" ]]; then
      principal_int_id=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT id FROM management_principal WHERE username='${test_username}' AND tenant_id=${grp_tenant_id} LIMIT 1;" 2>/dev/null | tr -d '\r')
    fi
    if [[ -n "${grp_tenant_id}" ]]; then
      local grp_add_ids=()
      for guuid in "${api_del_group_ids_sim[@]}"; do
        PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
          psql -U "${_db_user}" -d "${_db_name}" -c \
          "INSERT INTO management_group (uuid, tenant_id, name, description, created, modified, platform_default, system, admin_default) VALUES ('${guuid}', ${grp_tenant_id}, 'dr-readd-${guuid:0:8}', '', NOW(), NOW(), FALSE, FALSE, FALSE) ON CONFLICT DO NOTHING;" 2>/dev/null || true
        # Re-create principal M2M entry.
        if [[ -n "${principal_int_id}" ]]; then
          local grp_int
          grp_int=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
            psql -U "${_db_user}" -d "${_db_name}" -t -A \
            -c "SELECT id FROM management_group WHERE uuid='${guuid}';" 2>/dev/null | tr -d '\r')
          if [[ -n "${grp_int}" ]]; then
            PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
              psql -U "${_db_user}" -d "${_db_name}" -c \
              "INSERT INTO management_group_principals (group_id, principal_id) VALUES (${grp_int}, ${principal_int_id}) ON CONFLICT DO NOTHING;" 2>/dev/null || true
          fi
        fi
        good "Re-inserted: ${guuid}"
        grp_add_ids+=("${guuid}")
      done
      dr_state_save DR_GROUP_CORRECTIVE_ADD_IDS "$(IFS=,; echo "${grp_add_ids[*]}")"
    else
      warn "Could not determine tenant_id for group re-insert."
    fi
  fi

  fi  # end of non-minimal-data simulate block

  echo ""
  echo "--- Current workspace state after simulated restore ---"
  _workspaces_for_org "${DR_KESSEL_ORG_ID}"

  echo ""
  echo -e "${BOLD}=== Simulate Summary ===${NC}"
  echo ""
  echo "  Workspaces:"
  echo -e "    ${RED}Corrective REMOVE${NC}: ${#delete_ids[@]} deleted from DB"
  echo -e "    ${GREEN}Skip${NC}             : ${#keep_ids[@]} kept in DB"
  echo -e "    ${YELLOW}Corrective ADD${NC}   : ${#api_deleted_ids[@]} re-inserted"
  echo "    Ghost            : 1 (not reconcilable)"
  if [[ "${DR_MINIMAL_DATA}" != "true" ]]; then
    if [[ "${#rb_ids[@]}" -gt 0 ]]; then
      echo ""
      echo "  Role bindings:"
      echo -e "    ${RED}Corrective REMOVE${NC}: ${#rb_delete_ids[@]} deleted from DB"
      echo -e "    ${GREEN}Skip${NC}             : ${#rb_keep_ids[@]} kept in DB"
      echo -e "    ${YELLOW}Corrective ADD${NC}   : ${#api_del_rb_ids[@]} re-inserted"
    fi
    if [[ "${#role_test_ids[@]}" -gt 0 ]]; then
      echo ""
      echo "  Roles:"
      echo -e "    ${RED}Corrective REMOVE${NC}: ${#role_delete_ids[@]} deleted from DB"
      echo -e "    ${GREEN}Skip${NC}             : ${#role_keep_ids[@]} kept in DB"
      echo -e "    ${YELLOW}Corrective ADD${NC}   : ${#api_del_role_ids_sim[@]} re-inserted"
    fi
    if [[ "${#group_test_ids[@]}" -gt 0 ]]; then
      echo ""
      echo "  Groups:"
      echo -e "    ${RED}Corrective REMOVE${NC}: ${#group_delete_ids[@]} deleted from DB"
      echo -e "    ${GREEN}Skip${NC}             : ${#group_keep_ids[@]} kept in DB"
      echo -e "    ${YELLOW}Corrective ADD${NC}   : ${#api_del_group_ids_sim[@]} re-inserted"
    fi
  fi
  info "Next: DR_STEP=pre-check $0 --rbac-kessel"
}

dr_kessel_pre_check() {
  # Show divergence state across all three scenarios after simulated restore.
  _kessel_require_org_id || return 1
  dr_state_load

  local ws_ids_csv="${DR_KESSEL_WORKSPACE_IDS:-${DR_KESSEL_WORKSPACE_ID:-}}"
  local ghost_id="${DR_GHOST_WORKSPACE_ID:-}"
  local restore_ts="${DR_RESTORE_TIMESTAMP:-}"
  local corrective_add_ids_csv="${DR_CORRECTIVE_ADD_IDS:-}"
  local corrective_add_ids=()
  [[ -n "${corrective_add_ids_csv}" ]] && IFS=',' read -ra corrective_add_ids <<< "${corrective_add_ids_csv}"

  # Load scenario-specific IDs (new state vars from updated simulate).
  local remove_ids_csv="${DR_CORRECTIVE_REMOVE_IDS:-}"
  local skip_ids_csv="${DR_SKIP_IDS:-}"

  IFS=',' read -ra ws_ids <<< "${ws_ids_csv}"

  _db_creds || return 1

  echo ""
  echo -e "${BOLD}==================================================================${NC}"
  echo -e "${BOLD}  PRE-CHECK: Verifying divergence across all DR scenarios${NC}"
  echo -e "${BOLD}==================================================================${NC}"

  echo ""
  echo "--- RBAC Database: workspaces for org ${DR_KESSEL_ORG_ID} ---"
  _workspaces_for_org "${DR_KESSEL_ORG_ID}"

  # Scenario 1: Corrective REMOVE — deleted from DB, stale tuples in Kessel
  local stale_count=0 total_stale_tuples=0
  if [[ -n "${remove_ids_csv}" ]]; then
    local remove_ids
    IFS=',' read -ra remove_ids <<< "${remove_ids_csv}"
    echo ""
    echo -e "--- Scenario 1: Corrective REMOVE (${#remove_ids[@]} workspace(s)) ---"
    echo ""
    echo "  Expected: MISSING from DB, stale tuples still in Kessel."
    echo "  Reconciler will emit corrective REMOVE."
    echo ""
    for ws_id in "${remove_ids[@]}"; do
      local db_count
      db_count=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_workspace WHERE id='${ws_id}';" 2>/dev/null | tr -d '\r')
      local tuple_count
      tuple_count=$(echo "$(_read_kessel_workspace_tuples "${ws_id}")" | jq '.tuples | length' 2>/dev/null || echo "0")

      if [[ "${db_count}" == "0" && "${tuple_count}" -gt 0 ]]; then
        bad "${ws_id}: DB=no, Kessel=${tuple_count} tuples (OUT OF SYNC)"
        stale_count=$((stale_count + 1))
        total_stale_tuples=$((total_stale_tuples + tuple_count))
      elif [[ "${db_count}" == "0" && "${tuple_count}" == "0" ]]; then
        echo -e "  ${DIM}[----]${NC} ${ws_id}: absent from both (already consistent)"
      else
        warn "${ws_id}: unexpectedly in DB (db=${db_count})"
      fi
    done
  fi

  # Scenario 2: Skip — kept in DB, tuples in Kessel (consistent)
  if [[ -n "${skip_ids_csv}" ]]; then
    local skip_ids
    IFS=',' read -ra skip_ids <<< "${skip_ids_csv}"
    echo ""
    echo -e "--- Scenario 2: Skip / consistent (${#skip_ids[@]} workspace(s)) ---"
    echo ""
    echo "  Expected: present in DB AND tuples in Kessel (both consistent)."
    echo "  Reconciler will skip these — no action needed."
    echo ""
    for ws_id in "${skip_ids[@]}"; do
      local db_count
      db_count=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_workspace WHERE id='${ws_id}';" 2>/dev/null | tr -d '\r')
      local tuple_count
      tuple_count=$(echo "$(_read_kessel_workspace_tuples "${ws_id}")" | jq '.tuples | length' 2>/dev/null || echo "0")

      if [[ "${db_count}" != "0" && "${tuple_count}" -gt 0 ]]; then
        good "${ws_id}: DB=yes, Kessel=${tuple_count} tuples (IN SYNC)"
      else
        warn "${ws_id}: unexpected state (db=${db_count}, kessel=${tuple_count})"
      fi
    done
  fi

  # Scenario 3: Corrective ADD — re-inserted in DB, no tuples in Kessel
  if [[ "${#corrective_add_ids[@]}" -gt 0 ]]; then
    echo ""
    echo -e "--- Scenario 3: Corrective ADD (${#corrective_add_ids[@]} workspace(s)) ---"
    echo ""
    echo "  Expected: present in DB (re-inserted), NO tuples in Kessel."
    echo "  Reconciler will emit corrective ADD to restore Kessel tuples."
    echo ""
    for add_id in "${corrective_add_ids[@]}"; do
      local db_count
      db_count=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_workspace WHERE id='${add_id}';" 2>/dev/null | tr -d '\r')
      local tuple_count
      tuple_count=$(echo "$(_read_kessel_workspace_tuples "${add_id}")" | jq '.tuples | length' 2>/dev/null || echo "0")

      if [[ "${db_count}" != "0" && "${tuple_count}" == "0" ]]; then
        bad "${add_id}: DB=yes, Kessel=0 tuples (OUT OF SYNC — needs ADD)"
      elif [[ "${db_count}" != "0" && "${tuple_count}" -gt 0 ]]; then
        warn "${add_id}: already has tuples in Kessel (db=${db_count}, kessel=${tuple_count})"
      else
        warn "${add_id}: unexpected state (db=${db_count}, kessel=${tuple_count})"
      fi
    done
  fi

  # Ghost workspace
  if [[ -n "${ghost_id}" ]]; then
    echo ""
    echo "--- Ghost workspace (DB only, no Kafka event) ---"
    echo ""
    echo "  Expected: present in DB, no tuples in Kessel."
    echo "  NOT fixable by event-based reconcile."
    echo ""
    local ghost_in_db
    ghost_in_db=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
      psql -U "${_db_user}" -d "${_db_name}" -t -A \
      -c "SELECT count(*) FROM management_workspace WHERE id='${ghost_id}';" 2>/dev/null | tr -d '\r')
    local ghost_tuple_count
    ghost_tuple_count=$(echo "$(_read_kessel_workspace_tuples "${ghost_id}")" | jq '.tuples | length' 2>/dev/null || echo "0")

    if [[ "${ghost_in_db}" != "0" && "${ghost_tuple_count}" == "0" ]]; then
      echo -e "  ${CYAN}[INFO]${NC} ${ghost_id}: DB=yes, Kessel=0 (expected — no event exists)"
    else
      echo -e "  ${DIM}[----]${NC} ${ghost_id}: db=${ghost_in_db}, kessel=${ghost_tuple_count}"
    fi
  fi

  # ── Role binding divergence checks ──
  local rb_remove_csv="${DR_RB_CORRECTIVE_REMOVE_IDS:-}"
  local rb_skip_csv="${DR_RB_SKIP_IDS:-}"
  local rb_add_csv="${DR_RB_CORRECTIVE_ADD_IDS:-}"
  local rb_remove_ids=() rb_skip_ids_arr=() rb_add_ids=()
  [[ -n "${rb_remove_csv}" ]] && IFS=',' read -ra rb_remove_ids <<< "${rb_remove_csv}"
  [[ -n "${rb_skip_csv}" ]] && IFS=',' read -ra rb_skip_ids_arr <<< "${rb_skip_csv}"
  [[ -n "${rb_add_csv}" ]] && IFS=',' read -ra rb_add_ids <<< "${rb_add_csv}"

  local rb_stale_count=0

  if [[ "${#rb_remove_ids[@]}" -gt 0 ]]; then
    echo ""
    echo -e "--- Role Binding: Corrective REMOVE (${#rb_remove_ids[@]} binding(s)) ---"
    echo ""
    echo "  Expected: MISSING from DB, stale tuples still in Kessel."
    echo ""
    for rid in "${rb_remove_ids[@]}"; do
      local db_count
      db_count=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_rolebinding WHERE uuid='${rid}';" 2>/dev/null | tr -d '\r')
      local tuple_count
      tuple_count=$(echo "$(_read_kessel_role_binding_tuples "${rid}")" | jq '.tuples | length' 2>/dev/null || echo "0")

      if [[ "${db_count}" == "0" && "${tuple_count}" -gt 0 ]]; then
        bad "${rid}: DB=no, Kessel=${tuple_count} tuples (OUT OF SYNC)"
        rb_stale_count=$((rb_stale_count + 1))
      elif [[ "${db_count}" == "0" && "${tuple_count}" == "0" ]]; then
        echo -e "  ${DIM}[----]${NC} ${rid}: absent from both (already consistent)"
      else
        warn "${rid}: unexpectedly in DB (db=${db_count})"
      fi
    done
  fi

  if [[ "${#rb_skip_ids_arr[@]}" -gt 0 ]]; then
    echo ""
    echo -e "--- Role Binding: Skip / consistent (${#rb_skip_ids_arr[@]} binding(s)) ---"
    echo ""
    echo "  Expected: present in DB AND tuples in Kessel."
    echo ""
    for rid in "${rb_skip_ids_arr[@]}"; do
      local db_count
      db_count=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_rolebinding WHERE uuid='${rid}';" 2>/dev/null | tr -d '\r')
      local tuple_count
      tuple_count=$(echo "$(_read_kessel_role_binding_tuples "${rid}")" | jq '.tuples | length' 2>/dev/null || echo "0")

      if [[ "${db_count}" != "0" && "${tuple_count}" -gt 0 ]]; then
        good "${rid}: DB=yes, Kessel=${tuple_count} tuples (IN SYNC)"
      else
        warn "${rid}: unexpected state (db=${db_count}, kessel=${tuple_count})"
      fi
    done
  fi

  if [[ "${#rb_add_ids[@]}" -gt 0 ]]; then
    echo ""
    echo -e "--- Role Binding: Corrective ADD (${#rb_add_ids[@]} binding(s)) ---"
    echo ""
    echo "  Expected: present in DB (re-inserted), NO tuples in Kessel."
    echo ""
    for rid in "${rb_add_ids[@]}"; do
      local db_count
      db_count=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_rolebinding WHERE uuid='${rid}';" 2>/dev/null | tr -d '\r')
      local tuple_count
      tuple_count=$(echo "$(_read_kessel_role_binding_tuples "${rid}")" | jq '.tuples | length' 2>/dev/null || echo "0")

      if [[ "${db_count}" != "0" && "${tuple_count}" == "0" ]]; then
        bad "${rid}: DB=yes, Kessel=0 tuples (OUT OF SYNC — needs ADD)"
      elif [[ "${db_count}" != "0" && "${tuple_count}" -gt 0 ]]; then
        warn "${rid}: already has tuples in Kessel (db=${db_count}, kessel=${tuple_count})"
      else
        warn "${rid}: unexpected state (db=${db_count}, kessel=${tuple_count})"
      fi
    done
  fi

  # ----- Role (RoleV2) divergence checks -----
  local role_remove_csv="${DR_ROLE_CORRECTIVE_REMOVE_IDS:-}"
  local role_skip_csv="${DR_ROLE_SKIP_IDS:-}"
  local role_add_csv="${DR_ROLE_CORRECTIVE_ADD_IDS:-}"
  local role_stale_count=0

  if [[ -n "${role_remove_csv}" ]]; then
    local role_remove_ids
    IFS=',' read -ra role_remove_ids <<< "${role_remove_csv}"
    echo ""
    echo -e "--- Role: Corrective REMOVE (${#role_remove_ids[@]} role(s)) ---"
    echo ""
    echo "  Expected: absent from DB, but tuples STILL in Kessel (stale)."
    echo ""
    for ruuid in "${role_remove_ids[@]}"; do
      local db_count
      db_count=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_rolev2 WHERE uuid='${ruuid}';" 2>/dev/null | tr -d '\r')
      local tuple_count
      tuple_count=$(echo "$(_read_kessel_role_tuples "${ruuid}")" | jq '.tuples | length' 2>/dev/null || echo "0")
      if [[ "${db_count}" == "0" && "${tuple_count}" -gt 0 ]]; then
        bad "${ruuid}: DB=no, Kessel=${tuple_count} tuples (STALE)"
        role_stale_count=$((role_stale_count + 1))
      elif [[ "${db_count}" == "0" && "${tuple_count}" == "0" ]]; then
        warn "${ruuid}: already clean (db=no, kessel=0)"
      else
        warn "${ruuid}: unexpected state (db=${db_count}, kessel=${tuple_count})"
      fi
    done
  fi

  local role_skip_ids_arr=()
  if [[ -n "${role_skip_csv}" ]]; then
    IFS=',' read -ra role_skip_ids_arr <<< "${role_skip_csv}"
    echo ""
    echo -e "--- Role: Skip / consistent (${#role_skip_ids_arr[@]} role(s)) ---"
    echo ""
    echo "  Expected: present in DB AND tuples in Kessel (in sync)."
    echo ""
    for ruuid in "${role_skip_ids_arr[@]}"; do
      local db_count
      db_count=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_rolev2 WHERE uuid='${ruuid}';" 2>/dev/null | tr -d '\r')
      local tuple_count
      tuple_count=$(echo "$(_read_kessel_role_tuples "${ruuid}")" | jq '.tuples | length' 2>/dev/null || echo "0")
      if [[ "${db_count}" != "0" && "${tuple_count}" -gt 0 ]]; then
        good "${ruuid}: DB=yes, Kessel=${tuple_count} tuples (consistent)"
      else
        warn "${ruuid}: unexpected state (db=${db_count}, kessel=${tuple_count})"
      fi
    done
  fi

  local role_add_ids=()
  if [[ -n "${role_add_csv}" ]]; then
    IFS=',' read -ra role_add_ids <<< "${role_add_csv}"
    echo ""
    echo -e "--- Role: Corrective ADD (${#role_add_ids[@]} role(s)) ---"
    echo ""
    echo "  Expected: present in DB (re-inserted), NO tuples in Kessel."
    echo ""
    for ruuid in "${role_add_ids[@]}"; do
      local db_count
      db_count=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_rolev2 WHERE uuid='${ruuid}';" 2>/dev/null | tr -d '\r')
      local tuple_count
      tuple_count=$(echo "$(_read_kessel_role_tuples "${ruuid}")" | jq '.tuples | length' 2>/dev/null || echo "0")
      if [[ "${db_count}" != "0" && "${tuple_count}" == "0" ]]; then
        bad "${ruuid}: DB=yes, Kessel=0 tuples (OUT OF SYNC — needs ADD)"
      else
        warn "${ruuid}: unexpected state (db=${db_count}, kessel=${tuple_count})"
      fi
    done
  fi

  # ----- Group divergence checks -----
  local grp_remove_csv="${DR_GROUP_CORRECTIVE_REMOVE_IDS:-}"
  local grp_skip_csv="${DR_GROUP_SKIP_IDS:-}"
  local grp_add_csv="${DR_GROUP_CORRECTIVE_ADD_IDS:-}"
  local grp_stale_count=0

  if [[ -n "${grp_remove_csv}" ]]; then
    local grp_remove_ids
    IFS=',' read -ra grp_remove_ids <<< "${grp_remove_csv}"
    echo ""
    echo -e "--- Group: Corrective REMOVE (${#grp_remove_ids[@]} group(s)) ---"
    echo ""
    echo "  Expected: absent from DB, but member tuples STILL in Kessel (stale)."
    echo ""
    for guuid in "${grp_remove_ids[@]}"; do
      local db_count
      db_count=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_group WHERE uuid='${guuid}';" 2>/dev/null | tr -d '\r')
      local tuple_count
      tuple_count=$(echo "$(_read_kessel_group_tuples "${guuid}")" | jq '.tuples | length' 2>/dev/null || echo "0")
      if [[ "${db_count}" == "0" && "${tuple_count}" -gt 0 ]]; then
        bad "${guuid}: DB=no, Kessel=${tuple_count} tuples (STALE)"
        grp_stale_count=$((grp_stale_count + 1))
      elif [[ "${db_count}" == "0" && "${tuple_count}" == "0" ]]; then
        warn "${guuid}: already clean (db=no, kessel=0)"
      else
        warn "${guuid}: unexpected state (db=${db_count}, kessel=${tuple_count})"
      fi
    done
  fi

  local grp_skip_ids_arr=()
  if [[ -n "${grp_skip_csv}" ]]; then
    IFS=',' read -ra grp_skip_ids_arr <<< "${grp_skip_csv}"
    echo ""
    echo -e "--- Group: Skip / consistent (${#grp_skip_ids_arr[@]} group(s)) ---"
    echo ""
    echo "  Expected: present in DB AND member tuples in Kessel (in sync)."
    echo ""
    for guuid in "${grp_skip_ids_arr[@]}"; do
      local db_count
      db_count=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_group WHERE uuid='${guuid}';" 2>/dev/null | tr -d '\r')
      local tuple_count
      tuple_count=$(echo "$(_read_kessel_group_tuples "${guuid}")" | jq '.tuples | length' 2>/dev/null || echo "0")
      if [[ "${db_count}" != "0" && "${tuple_count}" -gt 0 ]]; then
        good "${guuid}: DB=yes, Kessel=${tuple_count} tuples (consistent)"
      else
        warn "${guuid}: unexpected state (db=${db_count}, kessel=${tuple_count})"
      fi
    done
  fi

  local grp_add_ids=()
  if [[ -n "${grp_add_csv}" ]]; then
    IFS=',' read -ra grp_add_ids <<< "${grp_add_csv}"
    echo ""
    echo -e "--- Group: Corrective ADD (${#grp_add_ids[@]} group(s)) ---"
    echo ""
    echo "  Expected: present in DB (re-inserted), NO member tuples in Kessel."
    echo ""
    for guuid in "${grp_add_ids[@]}"; do
      local db_count
      db_count=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_group WHERE uuid='${guuid}';" 2>/dev/null | tr -d '\r')
      local tuple_count
      tuple_count=$(echo "$(_read_kessel_group_tuples "${guuid}")" | jq '.tuples | length' 2>/dev/null || echo "0")
      if [[ "${db_count}" != "0" && "${tuple_count}" == "0" ]]; then
        bad "${guuid}: DB=yes, Kessel=0 tuples (OUT OF SYNC — needs ADD)"
      else
        warn "${guuid}: unexpected state (db=${db_count}, kessel=${tuple_count})"
      fi
    done
  fi

  # Summary
  echo ""
  echo -e "${BOLD}--- Divergence Summary ---${NC}"
  echo ""
  echo "  Workspaces:"
  echo "    Corrective REMOVE (stale Kessel tuples)  : ${stale_count}"
  echo "    Corrective ADD    (missing Kessel tuples) : ${#corrective_add_ids[@]}"
  echo "    Skip              (already consistent)    : $([[ -n "${skip_ids_csv}" ]] && echo "$(echo "${skip_ids_csv}" | tr ',' '\n' | wc -l | tr -d ' ')" || echo 0)"
  echo "    Ghost             (not reconcilable)      : $([[ -n "${ghost_id}" ]] && echo 1 || echo 0)"
  echo ""
  echo "  Role bindings:"
  echo "    Corrective REMOVE (stale Kessel tuples)  : ${rb_stale_count}"
  echo "    Corrective ADD    (missing Kessel tuples) : ${#rb_add_ids[@]}"
  echo "    Skip              (already consistent)    : ${#rb_skip_ids_arr[@]}"
  echo ""
  echo "  Roles:"
  echo "    Corrective REMOVE (stale Kessel tuples)  : ${role_stale_count}"
  echo "    Corrective ADD    (missing Kessel tuples) : ${#role_add_ids[@]}"
  echo "    Skip              (already consistent)    : ${#role_skip_ids_arr[@]}"
  echo ""
  echo "  Groups:"
  echo "    Corrective REMOVE (stale Kessel tuples)  : ${grp_stale_count}"
  echo "    Corrective ADD    (missing Kessel tuples) : ${#grp_add_ids[@]}"
  echo "    Skip              (already consistent)    : ${#grp_skip_ids_arr[@]}"

  # Time window
  if [[ -n "${restore_ts}" ]]; then
    local buffer="${DR_BUFFER_SECONDS}"
    if [[ -n "${DR_SETUP_TIMESTAMP:-}" ]]; then
      local setup_epoch restore_epoch elapsed_gap
      setup_epoch=$(date -u -d "${DR_SETUP_TIMESTAMP}" +%s 2>/dev/null || date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "${DR_SETUP_TIMESTAMP}" +%s 2>/dev/null || echo "")
      restore_epoch=$(date -u -d "${restore_ts}" +%s 2>/dev/null || date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "${restore_ts}" +%s 2>/dev/null || echo "")
      if [[ -n "${setup_epoch}" && -n "${restore_epoch}" ]]; then
        elapsed_gap=$(( restore_epoch - setup_epoch + 60 ))
        if [[ "${elapsed_gap}" -gt "${buffer}" ]]; then
          buffer="${elapsed_gap}"
        fi
      fi
    fi
    _show_time_window "${restore_ts}" "${buffer}"
  fi

  info "Next: DR_STEP=fix $0 --rbac-kessel"
}

dr_kessel_fix() {
  # POST /_private/api/disaster_recovery/reconcile/
  # Returns 202; Celery task re-aligns Kessel by reading Kafka events in
  # [restore_timestamp - buffer_seconds, restore_timestamp] and correcting.
  _kessel_require_org_id || return 1
  dr_state_load

  local restore_ts="${DR_RESTORE_TIMESTAMP:-}"
  if [[ -z "$restore_ts" ]]; then
    err "DR_RESTORE_TIMESTAMP not set. Run simulate step first."
    err "  Or set manually: export DR_RESTORE_TIMESTAMP=2026-06-09T12:00:00Z"
    return 1
  fi

  # Auto-compute buffer to cover the full gap between setup and restore,
  # plus a 60s safety margin. Falls back to DR_BUFFER_SECONDS if no setup
  # timestamp is available.
  local buffer="${DR_BUFFER_SECONDS}"
  if [[ -n "${DR_SETUP_TIMESTAMP:-}" ]]; then
    local setup_epoch restore_epoch elapsed_gap
    setup_epoch=$(date -u -d "${DR_SETUP_TIMESTAMP}" +%s 2>/dev/null || date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "${DR_SETUP_TIMESTAMP}" +%s 2>/dev/null || echo "")
    restore_epoch=$(date -u -d "${restore_ts}" +%s 2>/dev/null || date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "${restore_ts}" +%s 2>/dev/null || echo "")
    if [[ -n "${setup_epoch}" && -n "${restore_epoch}" ]]; then
      elapsed_gap=$(( restore_epoch - setup_epoch + 60 ))
      if [[ "${elapsed_gap}" -gt "${buffer}" ]]; then
        info "Auto-adjusting buffer: ${buffer}s → ${elapsed_gap}s (setup was ${DR_SETUP_TIMESTAMP})"
        buffer="${elapsed_gap}"
      fi
    fi
  fi

  local ws_ids_csv="${DR_KESSEL_WORKSPACE_IDS:-${DR_KESSEL_WORKSPACE_ID:-}}"
  local corrective_add_ids_csv="${DR_CORRECTIVE_ADD_IDS:-}"
  IFS=',' read -ra ws_ids <<< "${ws_ids_csv}"
  # Include corrective ADD workspaces in the sync check.
  local all_check_ids=("${ws_ids[@]}")
  if [[ -n "${corrective_add_ids_csv}" ]]; then
    local add_ids
    IFS=',' read -ra add_ids <<< "${corrective_add_ids_csv}"
    all_check_ids+=("${add_ids[@]}")
  fi

  # Collect all role binding IDs for sync check
  local all_rb_check_ids=()
  local rb_remove_csv="${DR_RB_CORRECTIVE_REMOVE_IDS:-}"
  local rb_skip_csv="${DR_RB_SKIP_IDS:-}"
  local rb_add_csv="${DR_RB_CORRECTIVE_ADD_IDS:-}"
  if [[ -n "${rb_remove_csv}" ]]; then
    IFS=',' read -ra _tmp <<< "${rb_remove_csv}"; all_rb_check_ids+=("${_tmp[@]}")
  fi
  if [[ -n "${rb_skip_csv}" ]]; then
    IFS=',' read -ra _tmp <<< "${rb_skip_csv}"; all_rb_check_ids+=("${_tmp[@]}")
  fi
  if [[ -n "${rb_add_csv}" ]]; then
    IFS=',' read -ra _tmp <<< "${rb_add_csv}"; all_rb_check_ids+=("${_tmp[@]}")
  fi

  # Collect all role IDs for sync check
  local all_role_check_ids=()
  local role_remove_csv="${DR_ROLE_CORRECTIVE_REMOVE_IDS:-}"
  local role_skip_csv="${DR_ROLE_SKIP_IDS:-}"
  local role_add_csv="${DR_ROLE_CORRECTIVE_ADD_IDS:-}"
  if [[ -n "${role_remove_csv}" ]]; then
    IFS=',' read -ra _tmp <<< "${role_remove_csv}"; all_role_check_ids+=("${_tmp[@]}")
  fi
  if [[ -n "${role_skip_csv}" ]]; then
    IFS=',' read -ra _tmp <<< "${role_skip_csv}"; all_role_check_ids+=("${_tmp[@]}")
  fi
  if [[ -n "${role_add_csv}" ]]; then
    IFS=',' read -ra _tmp <<< "${role_add_csv}"; all_role_check_ids+=("${_tmp[@]}")
  fi

  # Collect all group IDs for sync check
  local all_grp_check_ids=()
  local grp_remove_csv="${DR_GROUP_CORRECTIVE_REMOVE_IDS:-}"
  local grp_skip_csv="${DR_GROUP_SKIP_IDS:-}"
  local grp_add_csv="${DR_GROUP_CORRECTIVE_ADD_IDS:-}"
  if [[ -n "${grp_remove_csv}" ]]; then
    IFS=',' read -ra _tmp <<< "${grp_remove_csv}"; all_grp_check_ids+=("${_tmp[@]}")
  fi
  if [[ -n "${grp_skip_csv}" ]]; then
    IFS=',' read -ra _tmp <<< "${grp_skip_csv}"; all_grp_check_ids+=("${_tmp[@]}")
  fi
  if [[ -n "${grp_add_csv}" ]]; then
    IFS=',' read -ra _tmp <<< "${grp_add_csv}"; all_grp_check_ids+=("${_tmp[@]}")
  fi

  echo ""
  echo -e "${BOLD}==================================================================${NC}"
  echo -e "${BOLD}  DISASTER RECOVERY: Starting reconciliation${NC}"
  echo -e "${BOLD}==================================================================${NC}"

  # Pre-reconciliation sync check: confirm at least some systems are out of sync.
  if [[ "${DR_FAST}" != "true" ]]; then
    _db_creds || return 1
    echo ""
    echo -e "${BOLD}--- Pre-reconciliation sync check ---${NC}"
    echo ""
    echo "  Expected: some resources OUT OF SYNC (deleted from DB or re-inserted)."
    echo "  Resources kept in DB should show as IN SYNC (these will be skipped)."
    echo ""
    local ws_divergent=false rb_divergent=false role_divergent=false grp_divergent=false
    if _check_workspace_sync "${all_check_ids[@]}"; then
      :
    else
      ws_divergent=true
    fi
    if [[ "${#all_rb_check_ids[@]}" -gt 0 ]]; then
      echo ""
      if _check_role_binding_sync "${all_rb_check_ids[@]}"; then
        :
      else
        rb_divergent=true
      fi
    fi
    if [[ "${#all_role_check_ids[@]}" -gt 0 ]]; then
      echo ""
      if _check_role_sync "${all_role_check_ids[@]}"; then
        :
      else
        role_divergent=true
      fi
    fi
    if [[ "${#all_grp_check_ids[@]}" -gt 0 ]]; then
      echo ""
      if _check_group_sync "${all_grp_check_ids[@]}"; then
        :
      else
        grp_divergent=true
      fi
    fi
    echo ""
    if [[ "${ws_divergent}" == "true" || "${rb_divergent}" == "true" || "${role_divergent}" == "true" || "${grp_divergent}" == "true" ]]; then
      good "Confirmed: divergence detected — reconciliation needed."
    else
      warn "All test resources appear in sync. Reconciliation may find"
      echo "         nothing to fix. Proceeding anyway..."
    fi

    _show_time_window "${restore_ts}" "${buffer}"
  fi

  # Step 1: Dry run — show what the reconciler would do.
  echo ""
  echo "--- Phase 1: Dry run (read-only) ---"
  echo "  Reading Kafka events in the time window and comparing against RBAC DB..."
  echo ""

  local dry_body
  dry_body=$(jq -nc \
    --arg  ts  "${restore_ts}" \
    --argjson buf "${buffer}" \
    '{restore_timestamp: $ts, buffer_seconds: $buf, dry_run: true}')

  local dry_response
  dry_response=$(exec_private_curl POST /_private/api/disaster_recovery/reconcile/ \
    -H "Content-Type: application/json" -d "${dry_body}") || {
      err "Dry run failed. Check RBAC server/worker logs."
      return 1
    }

  # If the API returned a 202 (async task), poll worker logs for the result.
  local task_id
  task_id=$(echo "${dry_response}" | jq -r '.task_id // empty')
  if [[ -n "${task_id}" ]] && echo "${dry_response}" | jq -e '.events_read' >/dev/null 2>&1; then
    : # Synchronous response — already has full result
  elif [[ -n "${task_id}" ]]; then
    info "Dry run enqueued as Celery task ${task_id} — waiting for worker result..."
    local worker_pod
    worker_pod=$(get_pod_by_label "${RBAC_WORKER_POD_LABEL}")
    if [[ -z "${worker_pod}" ]]; then
      err "No worker pod found — cannot retrieve task result."
      return 1
    fi
    local attempt=0 max_attempts=30 result_line=""
    while [[ "${attempt}" -lt "${max_attempts}" ]]; do
      result_line=$(oc logs "${worker_pod}" --tail=200 2>/dev/null \
        | grep "Task management.tasks.run_disaster_recovery_reconcile\[${task_id}\] succeeded" \
        | tail -1 || true)
      if [[ -n "${result_line}" ]]; then
        break
      fi
      sleep 2
      attempt=$((attempt + 1))
    done
    if [[ -z "${result_line}" ]]; then
      err "Timed out waiting for task ${task_id}. Check worker logs:"
      err "  oc logs ${worker_pod} | grep ${task_id}"
      return 1
    fi
    # Celery truncates large task results with "..." in logs, breaking JSON.
    # Parse the DR service summary log line instead — it has clean counts.
    local summary_line
    summary_line=$(oc logs "${worker_pod}" --tail=200 2>/dev/null \
      | grep -E "DR reconciliation (DRY RUN|completed):" | tail -1 || true)
    if [[ -n "${summary_line}" ]]; then
      local _ev _tp _adds _removes _skips _errs
      _ev=$(echo "${summary_line}" | sed -n 's/.*events=\([0-9]*\).*/\1/p')
      _tp=$(echo "${summary_line}" | sed -n 's/.*tuples=\([0-9]*\).*/\1/p')
      _adds=$(echo "${summary_line}" | sed -n 's/.*would_add=\([0-9]*\).*/\1/p')
      [[ -z "${_adds}" ]] && _adds=$(echo "${summary_line}" | sed -n 's/.*adds=\([0-9]*\).*/\1/p')
      _removes=$(echo "${summary_line}" | sed -n 's/.*would_remove=\([0-9]*\).*/\1/p')
      [[ -z "${_removes}" ]] && _removes=$(echo "${summary_line}" | sed -n 's/.*removes=\([0-9]*\).*/\1/p')
      _skips=$(echo "${summary_line}" | sed -n 's/.*skipped=\([0-9]*\).*/\1/p')
      _errs=$(echo "${summary_line}" | sed -n 's/.*errors=\([0-9]*\).*/\1/p')
      dry_response=$(jq -nc \
        --argjson ev "${_ev:-0}" \
        --argjson tp "${_tp:-0}" \
        --argjson adds "${_adds:-0}" \
        --argjson removes "${_removes:-0}" \
        --argjson skips "${_skips:-0}" \
        --argjson errs "${_errs:-0}" \
        '{status:"dry_run",events_read:$ev,tuples_processed:$tp,corrective_adds:$adds,corrective_removes:$removes,skipped:$skips,errors:$errs}')
      good "Task completed — result parsed from DR summary log."
    else
      # Fallback: try parsing the full task result (works for small results).
      local raw_result
      raw_result=$(echo "${result_line}" | sed "s/.*succeeded in [0-9.]*s: //" \
        | sed "s/'/\"/g" \
        | sed 's/\bTrue\b/true/g; s/\bFalse\b/false/g; s/\bNone\b/null/g')
      if echo "${raw_result}" | jq . >/dev/null 2>&1; then
        dry_response="${raw_result}"
        good "Task completed — result retrieved from worker logs."
      else
        warn "Could not parse task result from worker logs."
      fi
    fi
  fi

  # Check if the reconciliation task returned a failure (exception caught by Celery task wrapper).
  local _task_status
  _task_status=$(echo "${dry_response}" | jq -r '.status // empty')
  if [[ "${_task_status}" == "failed" ]]; then
    local _task_error
    _task_error=$(echo "${dry_response}" | jq -r '.error // "unknown error"')
    err "Reconciliation task failed: ${_task_error}"
    err "Check worker pod env vars (e.g. RBAC_KAFKA_CONSUMER_TOPIC) and logs."
    return 1
  fi

  local events_read corrective_adds corrective_removes skipped
  events_read=$(echo "${dry_response}" | jq -r '.events_read // 0')
  corrective_adds=$(echo "${dry_response}" | jq -r '.corrective_adds // 0')
  corrective_removes=$(echo "${dry_response}" | jq -r '.corrective_removes // 0')
  skipped=$(echo "${dry_response}" | jq -r '.skipped // 0')

  echo -e "${BOLD}--- Kafka Events in Reconciliation Window ---${NC}"
  echo ""
  echo "  Events found          : ${events_read}"

  # List individual Kafka events (if kafka_events field available).
  local kafka_event_count
  kafka_event_count=$(echo "${dry_response}" | jq '.kafka_events | length' 2>/dev/null || echo "0")
  if [[ "${kafka_event_count}" -gt 0 ]]; then
    echo ""
    echo "${dry_response}" | jq -r '
      .kafka_events[] |
      "  [\(.event_type)] partition=\(.partition) offset=\(.offset) org=\(.org_id)" +
      (if (.tuples_to_add | length) > 0
       then "\n" + (.tuples_to_add[] | "    → ADD    \(.)")
       else "" end) +
      (if (.tuples_to_remove | length) > 0
       then "\n" + (.tuples_to_remove[] | "    → REMOVE \(.)")
       else "" end)
    ' 2>/dev/null || true
  fi

  echo ""
  echo -e "${BOLD}--- Reconciliation Summary ---${NC}"
  echo ""
  echo "  Corrective ADDs       : ${corrective_adds}  (resource exists in DB, tuple was removed)"
  echo "  Corrective REMOVEs    : ${corrective_removes}  (resource gone from DB, stale tuple)"
  echo "  Skipped (consistent)  : ${skipped}  (no action needed)"

  # List individual corrective actions
  local action_count
  action_count=$(echo "${dry_response}" | jq '.actions | length' 2>/dev/null || echo "0")
  if [[ "${action_count}" -gt 0 ]]; then
    echo ""
    echo -e "${BOLD}--- Corrective Events to Emit ---${NC}"
    echo ""
    echo "${dry_response}" | jq -r '
      .actions[] |
      "  [\(.action | ascii_upcase)]  \(.tuple)" +
      "\n         reason: \(.reason)" +
      "\n         source: partition=\(.source_partition) offset=\(.source_offset)"
    ' 2>/dev/null || true
  fi

  if [[ "${corrective_adds}" == "0" && "${corrective_removes}" == "0" ]]; then
    echo ""
    good "No corrective actions needed — RBAC and Kessel are consistent."
    banner_ok "=================================================================="
    banner_ok "  DISASTER RECOVERY: Complete (nothing to fix)"
    banner_ok "=================================================================="
    return 0
  fi

  # Step 2: Actual reconciliation (unless DR_DRY_RUN=true).
  if [[ "${DR_DRY_RUN}" == "true" ]]; then
    echo ""
    echo "[INFO] DR_DRY_RUN=true — skipping actual reconciliation."
    echo "       Re-run with DR_DRY_RUN=false to apply corrections."
    return 0
  fi

  echo ""
  echo "--- Phase 2: Applying corrective events ---"

  local body
  body=$(jq -nc \
    --arg  ts  "${restore_ts}" \
    --argjson buf "${buffer}" \
    '{restore_timestamp: $ts, buffer_seconds: $buf, dry_run: false}')

  local response
  response=$(exec_private_curl POST /_private/api/disaster_recovery/reconcile/ \
    -H "Content-Type: application/json" -d "${body}") || {
      err "Reconciliation failed. Check RBAC server/worker logs."
      return 1
    }

  echo ""
  echo "=== Reconcile Enqueued (202) ==="
  echo "${response}" | jq .

  local task_id
  task_id=$(echo "${response}" | jq -r '.task_id // empty')
  [[ -n "$task_id" ]] && dr_state_save DR_RECONCILE_TASK_ID "${task_id}"

  echo ""
  echo -e "${BOLD}==================================================================${NC}"
  echo -e "${BOLD}  DISASTER RECOVERY: Reconciliation task submitted${NC}"
  echo -e "  Task ID: ${CYAN}${task_id:-unknown}${NC}"
  echo -e "${BOLD}==================================================================${NC}"

  if [[ -n "${task_id}" ]]; then
    info "Waiting for Celery task to complete..."
    local worker_pod
    worker_pod=$(get_pod_by_label "${RBAC_WORKER_POD_LABEL}")
    if [[ -z "${worker_pod}" ]]; then
      warn "No worker pod found — cannot track task. Check logs manually."
      return 0
    fi
    local attempt=0 max_attempts=60 result_line=""
    while [[ "${attempt}" -lt "${max_attempts}" ]]; do
      result_line=$(oc logs "${worker_pod}" --tail=200 2>/dev/null \
        | grep "Task management.tasks.run_disaster_recovery_reconcile\[${task_id}\]" \
        | grep -E "succeeded|failed" \
        | tail -1 || true)
      if [[ -n "${result_line}" ]]; then
        break
      fi
      sleep 2
      attempt=$((attempt + 1))
      if (( attempt % 5 == 0 )); then
        echo -n "."
      fi
    done
    echo ""

    if [[ -z "${result_line}" ]]; then
      warn "Timed out after 120s. Check worker logs: oc logs ${worker_pod} | grep ${task_id}"
      return 0
    fi

    if echo "${result_line}" | grep -q "succeeded"; then
      # Parse from DR service summary log (avoids Celery truncation of large results).
      local summary_line
      summary_line=$(oc logs "${worker_pod}" --tail=200 2>/dev/null \
        | grep -E "DR reconciliation (DRY RUN|completed):" | tail -1 || true)
      local fix_adds="?" fix_removes="?" fix_errors="?"
      if [[ -n "${summary_line}" ]]; then
        fix_adds=$(echo "${summary_line}" | sed -n 's/.*adds=\([0-9]*\).*/\1/p')
        [[ -z "${fix_adds}" ]] && fix_adds=$(echo "${summary_line}" | sed -n 's/.*would_add=\([0-9]*\).*/\1/p')
        fix_removes=$(echo "${summary_line}" | sed -n 's/.*removes=\([0-9]*\).*/\1/p')
        [[ -z "${fix_removes}" ]] && fix_removes=$(echo "${summary_line}" | sed -n 's/.*would_remove=\([0-9]*\).*/\1/p')
        fix_errors=$(echo "${summary_line}" | sed -n 's/.*errors=\([0-9]*\).*/\1/p')
        fix_adds="${fix_adds:-0}"; fix_removes="${fix_removes:-0}"; fix_errors="${fix_errors:-0}"
      else
        # Fallback: try parsing truncated task result for summary fields.
        local raw_result
        raw_result=$(echo "${result_line}" | sed "s/.*succeeded in [0-9.]*s: //" \
          | sed "s/'/\"/g" \
          | sed 's/\bTrue\b/true/g; s/\bFalse\b/false/g; s/\bNone\b/null/g')
        local _fix_status
        _fix_status=$(echo "${raw_result}" | jq -r '.status // empty' 2>/dev/null)
        if [[ "${_fix_status}" == "failed" ]]; then
          local _fix_err
          _fix_err=$(echo "${raw_result}" | jq -r '.error // "unknown error"' 2>/dev/null)
          err "Reconciliation task failed: ${_fix_err}"
          return 1
        fi
        fix_adds=$(echo "${raw_result}" | jq -r '.corrective_adds // 0' 2>/dev/null || echo "?")
        fix_removes=$(echo "${raw_result}" | jq -r '.corrective_removes // 0' 2>/dev/null || echo "?")
        fix_errors=$(echo "${raw_result}" | jq -r '.errors // 0' 2>/dev/null || echo "?")
      fi
      echo ""
      echo -e "${BOLD}=== Reconciliation Result ===${NC}"
      echo ""
      echo "  Corrective ADDs       : ${fix_adds}"
      echo "  Corrective REMOVEs    : ${fix_removes}"
      echo "  Errors                : ${fix_errors}"
      if [[ "${fix_errors}" != "0" ]]; then
        bad "Reconciliation completed with ${fix_errors} error(s)."
      else
        good "Reconciliation completed successfully."
      fi
    else
      bad "Task failed. Check worker logs: oc logs ${worker_pod} | grep ${task_id}"
    fi
  fi
}

dr_kessel_post_check() {
  # Verify all three reconciliation scenarios produced correct results.
  _kessel_require_org_id || return 1
  dr_state_load

  local ghost_id="${DR_GHOST_WORKSPACE_ID:-}"
  local corrective_add_ids_csv="${DR_CORRECTIVE_ADD_IDS:-}"
  local corrective_add_ids=()
  [[ -n "${corrective_add_ids_csv}" ]] && IFS=',' read -ra corrective_add_ids <<< "${corrective_add_ids_csv}"
  local remove_ids_csv="${DR_CORRECTIVE_REMOVE_IDS:-}"
  local skip_ids_csv="${DR_SKIP_IDS:-}"

  local rb_remove_ids_csv="${DR_RB_CORRECTIVE_REMOVE_IDS:-}"
  local rb_skip_ids_csv="${DR_RB_SKIP_IDS:-}"
  local rb_add_ids_csv="${DR_RB_CORRECTIVE_ADD_IDS:-}"
  local rb_add_ids=()
  [[ -n "${rb_add_ids_csv}" ]] && IFS=',' read -ra rb_add_ids <<< "${rb_add_ids_csv}"

  _db_creds || return 1

  echo ""
  echo -e "${BOLD}==================================================================${NC}"
  echo -e "${BOLD}  POST-CHECK: Verifying all DR scenarios after reconciliation${NC}"
  echo -e "${BOLD}==================================================================${NC}"

  echo ""
  echo "--- RBAC Database: workspaces for org ${DR_KESSEL_ORG_ID} ---"
  _workspaces_for_org "${DR_KESSEL_ORG_ID}"

  local pass_count=0 fail_count=0 total_checks=0

  # Scenario 1: Corrective REMOVE — should now be absent from both DB and Kessel
  if [[ -n "${remove_ids_csv}" ]]; then
    local remove_ids
    IFS=',' read -ra remove_ids <<< "${remove_ids_csv}"
    echo ""
    echo -e "--- Scenario 1: Corrective REMOVE (${#remove_ids[@]} workspace(s)) ---"
    echo ""
    echo -e "  ${BOLD}Expected:${NC} absent from DB ${GREEN}AND${NC} no tuples in Kessel"
    echo ""
    for ws_id in "${remove_ids[@]}"; do
      total_checks=$((total_checks + 1))
      local db_count
      db_count=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_workspace WHERE id='${ws_id}';" 2>/dev/null | tr -d '\r')
      local tuple_count
      tuple_count=$(echo "$(_read_kessel_workspace_tuples "${ws_id}")" | jq '.tuples | length' 2>/dev/null || echo "0")

      if [[ "${db_count}" == "0" && "${tuple_count}" == "0" ]]; then
        good "${ws_id}: DB=no, Kessel=0 (stale tuples removed)"
        pass_count=$((pass_count + 1))
      elif [[ "${db_count}" == "0" && "${tuple_count}" -gt 0 ]]; then
        bad "${ws_id}: DB=no, Kessel=${tuple_count} (STILL STALE)"
        fail_count=$((fail_count + 1))
      else
        warn "${ws_id}: unexpected (db=${db_count}, kessel=${tuple_count})"
        fail_count=$((fail_count + 1))
      fi
    done
  fi

  # Scenario 2: Skip — should remain in sync (DB=yes, Kessel=yes)
  if [[ -n "${skip_ids_csv}" ]]; then
    local skip_ids
    IFS=',' read -ra skip_ids <<< "${skip_ids_csv}"
    echo ""
    echo -e "--- Scenario 2: Skip / consistent (${#skip_ids[@]} workspace(s)) ---"
    echo ""
    echo -e "  ${BOLD}Expected:${NC} present in DB ${GREEN}AND${NC} tuples in Kessel (unchanged)"
    echo ""
    for ws_id in "${skip_ids[@]}"; do
      total_checks=$((total_checks + 1))
      local db_count
      db_count=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_workspace WHERE id='${ws_id}';" 2>/dev/null | tr -d '\r')
      local tuple_count
      tuple_count=$(echo "$(_read_kessel_workspace_tuples "${ws_id}")" | jq '.tuples | length' 2>/dev/null || echo "0")

      if [[ "${db_count}" != "0" && "${tuple_count}" -gt 0 ]]; then
        good "${ws_id}: DB=yes, Kessel=${tuple_count} (correctly unchanged)"
        pass_count=$((pass_count + 1))
      else
        bad "${ws_id}: unexpected (db=${db_count}, kessel=${tuple_count})"
        fail_count=$((fail_count + 1))
      fi
    done
  fi

  # Scenario 3: Corrective ADD — should now have tuples in Kessel restored
  if [[ "${#corrective_add_ids[@]}" -gt 0 ]]; then
    echo ""
    echo -e "--- Scenario 3: Corrective ADD (${#corrective_add_ids[@]} workspace(s)) ---"
    echo ""
    echo -e "  ${BOLD}Expected:${NC} present in DB ${GREEN}AND${NC} tuples restored in Kessel"
    echo ""
    for add_id in "${corrective_add_ids[@]}"; do
      total_checks=$((total_checks + 1))
      local db_count
      db_count=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_workspace WHERE id='${add_id}';" 2>/dev/null | tr -d '\r')
      local tuple_count
      tuple_count=$(echo "$(_read_kessel_workspace_tuples "${add_id}")" | jq '.tuples | length' 2>/dev/null || echo "0")

      if [[ "${db_count}" != "0" && "${tuple_count}" -gt 0 ]]; then
        good "${add_id}: DB=yes, Kessel=${tuple_count} (tuples restored)"
        pass_count=$((pass_count + 1))
      elif [[ "${db_count}" != "0" && "${tuple_count}" == "0" ]]; then
        bad "${add_id}: DB=yes, Kessel=0 (tuples NOT restored)"
        fail_count=$((fail_count + 1))
      else
        warn "${add_id}: unexpected (db=${db_count}, kessel=${tuple_count})"
        fail_count=$((fail_count + 1))
      fi
    done
  fi

  # Ghost workspace
  if [[ -n "${ghost_id}" ]]; then
    echo ""
    echo "--- Ghost workspace (not reconcilable) ---"
    echo ""
    echo -e "  ${BOLD}Expected:${NC} still in DB, still no Kessel tuples"
    echo ""
    local ghost_in_db
    ghost_in_db=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
      psql -U "${_db_user}" -d "${_db_name}" -t -A \
      -c "SELECT count(*) FROM management_workspace WHERE id='${ghost_id}';" 2>/dev/null | tr -d '\r')
    local ghost_tuple_count
    ghost_tuple_count=$(echo "$(_read_kessel_workspace_tuples "${ghost_id}")" | jq '.tuples | length' 2>/dev/null || echo "0")
    echo -e "  ${CYAN}[INFO]${NC} ${ghost_id}: db=${ghost_in_db}, kessel=${ghost_tuple_count}"
    echo "         (Event-based reconcile cannot fix this — expected)"
  fi

  # ----- Role Binding scenarios -----

  # RB Scenario 1: Corrective REMOVE — should be absent from DB and Kessel
  if [[ -n "${rb_remove_ids_csv}" ]]; then
    local rb_remove_ids
    IFS=',' read -ra rb_remove_ids <<< "${rb_remove_ids_csv}"
    echo ""
    echo -e "--- RB Scenario 1: Corrective REMOVE (${#rb_remove_ids[@]} role binding(s)) ---"
    echo ""
    echo -e "  ${BOLD}Expected:${NC} absent from DB ${GREEN}AND${NC} no tuples in Kessel"
    echo ""
    for rb_id in "${rb_remove_ids[@]}"; do
      total_checks=$((total_checks + 1))
      local rb_db_count
      rb_db_count=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_rolebinding WHERE uuid='${rb_id}';" 2>/dev/null | tr -d '\r')
      local rb_tuple_count
      rb_tuple_count=$(echo "$(_read_kessel_role_binding_tuples "${rb_id}")" | jq '.tuples | length' 2>/dev/null || echo "0")

      if [[ "${rb_db_count}" == "0" && "${rb_tuple_count}" == "0" ]]; then
        good "${rb_id}: DB=no, Kessel=0 (stale tuples removed)"
        pass_count=$((pass_count + 1))
      elif [[ "${rb_db_count}" == "0" && "${rb_tuple_count}" -gt 0 ]]; then
        bad "${rb_id}: DB=no, Kessel=${rb_tuple_count} (STILL STALE)"
        fail_count=$((fail_count + 1))
      else
        warn "${rb_id}: unexpected (db=${rb_db_count}, kessel=${rb_tuple_count})"
        fail_count=$((fail_count + 1))
      fi
    done
  fi

  # RB Scenario 2: Skip — should remain in sync
  if [[ -n "${rb_skip_ids_csv}" ]]; then
    local rb_skip_ids
    IFS=',' read -ra rb_skip_ids <<< "${rb_skip_ids_csv}"
    echo ""
    echo -e "--- RB Scenario 2: Skip / consistent (${#rb_skip_ids[@]} role binding(s)) ---"
    echo ""
    echo -e "  ${BOLD}Expected:${NC} present in DB ${GREEN}AND${NC} tuples in Kessel (unchanged)"
    echo ""
    for rb_id in "${rb_skip_ids[@]}"; do
      total_checks=$((total_checks + 1))
      local rb_db_count
      rb_db_count=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_rolebinding WHERE uuid='${rb_id}';" 2>/dev/null | tr -d '\r')
      local rb_tuple_count
      rb_tuple_count=$(echo "$(_read_kessel_role_binding_tuples "${rb_id}")" | jq '.tuples | length' 2>/dev/null || echo "0")

      if [[ "${rb_db_count}" != "0" && "${rb_tuple_count}" -gt 0 ]]; then
        good "${rb_id}: DB=yes, Kessel=${rb_tuple_count} (correctly unchanged)"
        pass_count=$((pass_count + 1))
      else
        bad "${rb_id}: unexpected (db=${rb_db_count}, kessel=${rb_tuple_count})"
        fail_count=$((fail_count + 1))
      fi
    done
  fi

  # RB Scenario 3: Corrective ADD — should have tuples restored
  if [[ "${#rb_add_ids[@]}" -gt 0 ]]; then
    echo ""
    echo -e "--- RB Scenario 3: Corrective ADD (${#rb_add_ids[@]} role binding(s)) ---"
    echo ""
    echo -e "  ${BOLD}Expected:${NC} present in DB ${GREEN}AND${NC} tuples restored in Kessel"
    echo ""
    for rb_id in "${rb_add_ids[@]}"; do
      total_checks=$((total_checks + 1))
      local rb_db_count
      rb_db_count=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_rolebinding WHERE uuid='${rb_id}';" 2>/dev/null | tr -d '\r')
      local rb_tuple_count
      rb_tuple_count=$(echo "$(_read_kessel_role_binding_tuples "${rb_id}")" | jq '.tuples | length' 2>/dev/null || echo "0")

      if [[ "${rb_db_count}" != "0" && "${rb_tuple_count}" -gt 0 ]]; then
        good "${rb_id}: DB=yes, Kessel=${rb_tuple_count} (tuples restored)"
        pass_count=$((pass_count + 1))
      elif [[ "${rb_db_count}" != "0" && "${rb_tuple_count}" == "0" ]]; then
        bad "${rb_id}: DB=yes, Kessel=0 (tuples NOT restored)"
        fail_count=$((fail_count + 1))
      else
        warn "${rb_id}: unexpected (db=${rb_db_count}, kessel=${rb_tuple_count})"
        fail_count=$((fail_count + 1))
      fi
    done
  fi

  # ----- Role (RoleV2) post-check scenarios -----
  local role_remove_csv="${DR_ROLE_CORRECTIVE_REMOVE_IDS:-}"
  local role_skip_csv="${DR_ROLE_SKIP_IDS:-}"
  local role_add_csv="${DR_ROLE_CORRECTIVE_ADD_IDS:-}"

  if [[ -n "${role_remove_csv}" ]]; then
    local role_remove_ids
    IFS=',' read -ra role_remove_ids <<< "${role_remove_csv}"
    echo ""
    echo -e "--- Role Scenario 1: Corrective REMOVE (${#role_remove_ids[@]} role(s)) ---"
    echo ""
    echo -e "  ${BOLD}Expected:${NC} absent from DB ${GREEN}AND${NC} no tuples in Kessel"
    echo ""
    for ruuid in "${role_remove_ids[@]}"; do
      total_checks=$((total_checks + 1))
      local r_db
      r_db=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_rolev2 WHERE uuid='${ruuid}';" 2>/dev/null | tr -d '\r')
      local r_tc
      r_tc=$(echo "$(_read_kessel_role_tuples "${ruuid}")" | jq '.tuples | length' 2>/dev/null || echo "0")
      if [[ "${r_db}" == "0" && "${r_tc}" == "0" ]]; then
        good "${ruuid}: DB=no, Kessel=0 (stale tuples removed)"
        pass_count=$((pass_count + 1))
      elif [[ "${r_db}" == "0" && "${r_tc}" -gt 0 ]]; then
        bad "${ruuid}: DB=no, Kessel=${r_tc} (STILL STALE)"
        fail_count=$((fail_count + 1))
      else
        warn "${ruuid}: unexpected (db=${r_db}, kessel=${r_tc})"
        fail_count=$((fail_count + 1))
      fi
    done
  fi

  if [[ -n "${role_skip_csv}" ]]; then
    local role_skip_ids
    IFS=',' read -ra role_skip_ids <<< "${role_skip_csv}"
    echo ""
    echo -e "--- Role Scenario 2: Skip / consistent (${#role_skip_ids[@]} role(s)) ---"
    echo ""
    echo -e "  ${BOLD}Expected:${NC} present in DB ${GREEN}AND${NC} tuples in Kessel (unchanged)"
    echo ""
    for ruuid in "${role_skip_ids[@]}"; do
      total_checks=$((total_checks + 1))
      local r_db
      r_db=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_rolev2 WHERE uuid='${ruuid}';" 2>/dev/null | tr -d '\r')
      local r_tc
      r_tc=$(echo "$(_read_kessel_role_tuples "${ruuid}")" | jq '.tuples | length' 2>/dev/null || echo "0")
      if [[ "${r_db}" != "0" && "${r_tc}" -gt 0 ]]; then
        good "${ruuid}: DB=yes, Kessel=${r_tc} (correctly unchanged)"
        pass_count=$((pass_count + 1))
      else
        bad "${ruuid}: unexpected (db=${r_db}, kessel=${r_tc})"
        fail_count=$((fail_count + 1))
      fi
    done
  fi

  if [[ -n "${role_add_csv}" ]]; then
    local role_add_ids
    IFS=',' read -ra role_add_ids <<< "${role_add_csv}"
    echo ""
    echo -e "--- Role Scenario 3: Corrective ADD (${#role_add_ids[@]} role(s)) ---"
    echo ""
    echo -e "  ${BOLD}Expected:${NC} present in DB ${GREEN}AND${NC} tuples restored in Kessel"
    echo ""
    for ruuid in "${role_add_ids[@]}"; do
      total_checks=$((total_checks + 1))
      local r_db
      r_db=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_rolev2 WHERE uuid='${ruuid}';" 2>/dev/null | tr -d '\r')
      local r_tc
      r_tc=$(echo "$(_read_kessel_role_tuples "${ruuid}")" | jq '.tuples | length' 2>/dev/null || echo "0")
      if [[ "${r_db}" != "0" && "${r_tc}" -gt 0 ]]; then
        good "${ruuid}: DB=yes, Kessel=${r_tc} (tuples restored)"
        pass_count=$((pass_count + 1))
      elif [[ "${r_db}" != "0" && "${r_tc}" == "0" ]]; then
        bad "${ruuid}: DB=yes, Kessel=0 (tuples NOT restored)"
        fail_count=$((fail_count + 1))
      else
        warn "${ruuid}: unexpected (db=${r_db}, kessel=${r_tc})"
        fail_count=$((fail_count + 1))
      fi
    done
  fi

  # ----- Group post-check scenarios -----
  local grp_remove_csv="${DR_GROUP_CORRECTIVE_REMOVE_IDS:-}"
  local grp_skip_csv="${DR_GROUP_SKIP_IDS:-}"
  local grp_add_csv="${DR_GROUP_CORRECTIVE_ADD_IDS:-}"

  if [[ -n "${grp_remove_csv}" ]]; then
    local grp_remove_ids
    IFS=',' read -ra grp_remove_ids <<< "${grp_remove_csv}"
    echo ""
    echo -e "--- Group Scenario 1: Corrective REMOVE (${#grp_remove_ids[@]} group(s)) ---"
    echo ""
    echo -e "  ${BOLD}Expected:${NC} absent from DB ${GREEN}AND${NC} no member tuples in Kessel"
    echo ""
    for guuid in "${grp_remove_ids[@]}"; do
      total_checks=$((total_checks + 1))
      local g_db
      g_db=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_group WHERE uuid='${guuid}';" 2>/dev/null | tr -d '\r')
      local g_tc
      g_tc=$(echo "$(_read_kessel_group_tuples "${guuid}")" | jq '.tuples | length' 2>/dev/null || echo "0")
      if [[ "${g_db}" == "0" && "${g_tc}" == "0" ]]; then
        good "${guuid}: DB=no, Kessel=0 (stale tuples removed)"
        pass_count=$((pass_count + 1))
      elif [[ "${g_db}" == "0" && "${g_tc}" -gt 0 ]]; then
        bad "${guuid}: DB=no, Kessel=${g_tc} (STILL STALE)"
        fail_count=$((fail_count + 1))
      else
        warn "${guuid}: unexpected (db=${g_db}, kessel=${g_tc})"
        fail_count=$((fail_count + 1))
      fi
    done
  fi

  if [[ -n "${grp_skip_csv}" ]]; then
    local grp_skip_ids
    IFS=',' read -ra grp_skip_ids <<< "${grp_skip_csv}"
    echo ""
    echo -e "--- Group Scenario 2: Skip / consistent (${#grp_skip_ids[@]} group(s)) ---"
    echo ""
    echo -e "  ${BOLD}Expected:${NC} present in DB ${GREEN}AND${NC} member tuples in Kessel (unchanged)"
    echo ""
    for guuid in "${grp_skip_ids[@]}"; do
      total_checks=$((total_checks + 1))
      local g_db
      g_db=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_group WHERE uuid='${guuid}';" 2>/dev/null | tr -d '\r')
      local g_tc
      g_tc=$(echo "$(_read_kessel_group_tuples "${guuid}")" | jq '.tuples | length' 2>/dev/null || echo "0")
      if [[ "${g_db}" != "0" && "${g_tc}" -gt 0 ]]; then
        good "${guuid}: DB=yes, Kessel=${g_tc} (correctly unchanged)"
        pass_count=$((pass_count + 1))
      else
        bad "${guuid}: unexpected (db=${g_db}, kessel=${g_tc})"
        fail_count=$((fail_count + 1))
      fi
    done
  fi

  if [[ -n "${grp_add_csv}" ]]; then
    local grp_add_ids
    IFS=',' read -ra grp_add_ids <<< "${grp_add_csv}"
    echo ""
    echo -e "--- Group Scenario 3: Corrective ADD (${#grp_add_ids[@]} group(s)) ---"
    echo ""
    echo -e "  ${BOLD}Expected:${NC} present in DB ${GREEN}AND${NC} member tuples restored in Kessel"
    echo ""
    for guuid in "${grp_add_ids[@]}"; do
      total_checks=$((total_checks + 1))
      local g_db
      g_db=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "SELECT count(*) FROM management_group WHERE uuid='${guuid}';" 2>/dev/null | tr -d '\r')
      local g_tc
      g_tc=$(echo "$(_read_kessel_group_tuples "${guuid}")" | jq '.tuples | length' 2>/dev/null || echo "0")
      if [[ "${g_db}" != "0" && "${g_tc}" -gt 0 ]]; then
        good "${guuid}: DB=yes, Kessel=${g_tc} (tuples restored)"
        pass_count=$((pass_count + 1))
      elif [[ "${g_db}" != "0" && "${g_tc}" == "0" ]]; then
        bad "${guuid}: DB=yes, Kessel=0 (tuples NOT restored)"
        fail_count=$((fail_count + 1))
      else
        warn "${guuid}: unexpected (db=${g_db}, kessel=${g_tc})"
        fail_count=$((fail_count + 1))
      fi
    done
  fi

  # Verdict
  echo ""
  echo -e "${BOLD}--- Result ---${NC}"
  echo ""
  echo "  Scenarios passed : ${pass_count}/${total_checks}"
  if [[ "${fail_count}" -gt 0 ]]; then
    echo -e "  Scenarios failed : ${RED}${fail_count}${NC}"
  fi
  echo ""

  if [[ "${fail_count}" == "0" ]]; then
    banner_ok "=================================================================="
    banner_ok "  DISASTER RECOVERY: ALL SCENARIOS PASSED"
    echo ""
    echo "  Workspaces:"
    echo "    Corrective REMOVE : stale tuples removed from Kessel"
    echo "    Skip              : consistent workspaces left unchanged"
    echo "    Corrective ADD    : missing tuples restored in Kessel"
    echo "  Role Bindings:"
    echo "    Corrective REMOVE : stale tuples removed from Kessel"
    echo "    Skip              : consistent bindings left unchanged"
    echo "    Corrective ADD    : missing tuples restored in Kessel"
    echo "  Roles:"
    echo "    Corrective REMOVE : stale tuples removed from Kessel"
    echo "    Skip              : consistent roles left unchanged"
    echo "    Corrective ADD    : missing tuples restored in Kessel"
    echo "  Groups:"
    echo "    Corrective REMOVE : stale member tuples removed from Kessel"
    echo "    Skip              : consistent groups left unchanged"
    echo "    Corrective ADD    : missing member tuples restored in Kessel"
    echo -e "  RBAC DB and Kessel Relations are ${GREEN}back in sync${NC}."
    banner_ok "=================================================================="
  else
    banner_fail "=================================================================="
    banner_fail "  DISASTER RECOVERY: INCOMPLETE"
    echo ""
    echo -e "  ${RED}${fail_count}${NC} scenario(s) did not produce the expected result."
    echo ""
    echo "  Possible causes:"
    echo "    - Reconciliation Celery task has not completed yet"
    echo "    - Debezium connector is not processing outbox events"
    echo "    - Kessel has not consumed the corrective events yet"
    echo ""
    echo "  Try: wait a few seconds and re-run"
    echo "    DR_STEP=post-check $0 --rbac-kessel"
    banner_fail "=================================================================="
  fi
}

dr_kessel() {
  local step="${DR_STEP:-all}"
  echo "=== RBAC <-> Kessel DR [disaster_recovery/reconcile/] step=${step} ==="
  echo ""

  # Pre-flight: verify required env vars before any step.
  if [[ "${DR_FAST}" != "true" ]]; then
    info "Checking DR env vars on pods..."
    _check_dr_enabled || return 1
    echo ""
  else
    info "Fast mode — skipping DR env var checks."
  fi

  case "$step" in
    setup)      dr_kessel_setup ;;
    simulate)   dr_kessel_simulate ;;
    pre-check)  dr_kessel_pre_check ;;
    dry-run)    DR_DRY_RUN=true dr_kessel_fix ;;
    fix)        dr_kessel_fix ;;
    post-check) dr_kessel_post_check ;;
    cleanup)    dr_kessel_cleanup ;;
    state)      dr_state_show ;;
    all)
      # Run all steps; on failure clean up partial test data so re-runs start fresh.
      local _dr_rc=0
      dr_kessel_setup      || _dr_rc=$?
      if [[ "${_dr_rc}" -ne 0 ]]; then
        echo ""
        err "Setup failed — cleaning up test data..."
        dr_kessel_cleanup
        return 1
      fi

      info "Waiting 10s for Debezium to publish all setup events to Kafka..."
      sleep 10

      dr_kessel_simulate   || _dr_rc=$?
      if [[ "${_dr_rc}" -ne 0 ]]; then
        echo ""
        err "Simulate failed — cleaning up test data..."
        dr_kessel_cleanup
        return 1
      fi

      if [[ "${DR_FAST}" != "true" ]]; then
        dr_kessel_pre_check  || _dr_rc=$?
        if [[ "${_dr_rc}" -ne 0 ]]; then
          echo ""
          err "Pre-check failed — cleaning up test data..."
          dr_kessel_cleanup
          return 1
        fi
      fi

      if [[ "${DR_NO_DRY_RUN}" != "true" ]]; then
        DR_DRY_RUN=true dr_kessel_fix || _dr_rc=$?
        if [[ "${_dr_rc}" -ne 0 ]]; then
          echo ""
          err "Dry run failed — cleaning up test data..."
          dr_kessel_cleanup
          return 1
        fi

        if [[ "${DR_FAST}" == "true" ]]; then
          info "Fast mode — auto-proceeding to real fix."
          _confirm="y"
        else
          echo ""
          read -r -p "Dry run complete. Proceed with real fix? [y/N] " _confirm
        fi
      else
        info "Skipping dry run — proceeding directly to real fix."
        _confirm="y"
      fi
      if [[ "${_confirm}" == "y" || "${_confirm}" == "Y" ]]; then
        DR_DRY_RUN=false dr_kessel_fix || _dr_rc=$?
        if [[ "${_dr_rc}" -ne 0 ]]; then
          echo ""
          err "Fix failed — cleaning up test data..."
          dr_kessel_cleanup
          return 1
        fi
      else
        info "Aborted after dry run — cleaning up test data..."
        dr_kessel_cleanup
        return 0
      fi

      dr_kessel_post_check
      dr_kessel_cleanup

      echo ""
      echo -e "${BOLD}${GREEN}==================================================================${NC}"
      echo -e "${BOLD}${GREEN}  RBAC <-> Kessel DR: ALL STEPS COMPLETED SUCCESSFULLY${NC}"
      echo -e "${BOLD}${GREEN}==================================================================${NC}"
      echo ""
      echo "  Steps executed: setup → simulate → fix → cleanup"
      if [[ "${DR_FAST}" == "true" ]]; then
        echo "  Mode: --fast (safety checks skipped)"
      fi
      if [[ "${DR_NO_DRY_RUN}" == "true" ]]; then
        echo "  Mode: --no-dry-run (dry run skipped)"
      fi
      if [[ "${DR_MINIMAL_DATA}" == "true" ]]; then
        echo "  Mode: --minimal-data (1 resource per scenario)"
      fi
      echo ""
      echo "  To verify results manually:"
      echo "    DR_STEP=post-check $0 --rbac-kessel"
      echo ""
      ;;
    *) err "Unknown step: ${step}. Valid: setup|simulate|pre-check|dry-run|fix|post-check|cleanup|state|all"; return 1 ;;
  esac
}

# ══════════════════════════════════════════════════════════════════════════════
# DR SIMULATION -- RBAC <-> HBI
# ══════════════════════════════════════════════════════════════════════════════
#
# Fix endpoint: POST /_private/api/disaster_recovery/workspaces/
# Body: { "restore_timestamp": "<ISO8601>", "buffer_minutes": 5, "dry_run": false }
# Requires: DR_WORKSPACE_RECONCILE_ENABLED=True in pod env
#
# The workspace recovery Celery task reads outbox.event.workspace Kafka events
# in [restore_timestamp - buffer_minutes, restore_timestamp], compares against
# RBAC DB, and writes corrective workspace events.

DR_HBI_ORG_ID="${DR_HBI_ORG_ID:-}"
DR_HBI_WORKSPACE_PREFIX="${DR_HBI_WORKSPACE_PREFIX:-dr-hbi}"
DR_BUFFER_MINUTES="${DR_BUFFER_MINUTES:-5}"

_hbi_require_org_id() {
  dr_state_load
  # Prefer explicit env var, then state file, then auto-resolve from DB.
  if [[ -z "${DR_HBI_ORG_ID:-}" ]]; then
    _auto_resolve_org_id || return 1
    DR_HBI_ORG_ID="${ORG_ID_RESOLVED}"
  fi
}

_hbi_create_workspace() {
  local ws_name="$1"
  local response http_code
  response=$(curl -s -w '\n%{http_code}' \
    -X POST \
    -u "${BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME}:${EPHEMERAL_PASSWORD}" \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"${ws_name}\"}" \
    "${BENTO_URL}/api/rbac/v2/workspaces/")
  http_code=$(echo "${response}" | tail -1)
  response=$(echo "${response}" | sed '$d')

  if [[ "${http_code}" -lt 200 || "${http_code}" -ge 300 ]]; then
    err "Failed to create workspace '${ws_name}' (HTTP ${http_code})"
    echo "  Response: ${response}" >&2
    return 1
  fi
  echo "${response}"
}

_hbi_delete_workspace_api() {
  local ws_id="$1"
  local del_http
  del_http=$(curl -s -o /dev/null -w '%{http_code}' \
    -X DELETE \
    -u "${BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME}:${EPHEMERAL_PASSWORD}" \
    "${BENTO_URL}/api/rbac/v2/workspaces/${ws_id}/")
  if [[ "${del_http}" -ge 200 && "${del_http}" -lt 300 ]] || [[ "${del_http}" == "204" ]]; then
    good "Deleted via API (HTTP ${del_http}): ${ws_id}"
  else
    err "Failed to delete workspace via API (HTTP ${del_http}): ${ws_id}"
    return 1
  fi
}

dr_hbi_setup() {
  # Three-scenario setup for workspace DR reconciliation:
  #
  # Truth table (from dr_recovery.py):
  #   create event + NOT in DB → corrective DELETE (orphaned downstream)
  #   create event + in DB     → SKIP (consistent)
  #   delete event + in DB     → corrective CREATE (missing notification)
  #   delete event + NOT in DB → SKIP (consistent)
  #
  # Scenario 1 — Corrective DELETE:
  #   Create ws, DB-delete in simulate → Kafka: create, DB: missing → corrective DELETE
  # Scenario 2 — Corrective CREATE:
  #   Create ws + API-delete (Kafka: delete), re-insert in simulate → corrective CREATE
  # Scenario 3 — SKIP:
  #   Create ws, leave in DB → Kafka: create, DB: exists → SKIP
  _hbi_require_org_id || return 1

  local run_tag
  run_tag=$(date +%s | tail -c 5)

  info "Bootstrapping tenant ${DR_HBI_ORG_ID}..."
  local bootstrap_resp
  bootstrap_resp=$(exec_private_curl POST /_private/api/utils/bootstrap_tenant/ \
    -H "Content-Type: application/json" \
    -d "{\"org_ids\": [\"${DR_HBI_ORG_ID}\"]}") || return 1
  echo "${bootstrap_resp}"

  echo ""
  echo -e "${BOLD}==================================================================${NC}"
  echo -e "${BOLD}  SETUP: 3-scenario workspace DR test${NC}"
  echo -e "${BOLD}==================================================================${NC}"
  echo ""
  echo "  Scenario 1 — Corrective DELETE (orphaned downstream):"
  echo "    Create workspace → DB-delete in simulate"
  echo "    Kafka has 'create' but DB has no workspace → reconciler writes DELETE"
  echo ""
  echo "  Scenario 2 — Corrective CREATE (missing notification):"
  echo "    Create workspace → API-delete now (generates 'delete' event)"
  echo "    Re-insert into DB in simulate → reconciler writes CREATE"
  echo ""
  echo "  Scenario 3 — SKIP (consistent):"
  echo "    Create workspace → leave in DB"
  echo "    Kafka has 'create' AND DB has workspace → no action needed"
  echo ""

  # --- Scenario 1: workspace for corrective DELETE ---
  local ws_del_name="${DR_HBI_WORKSPACE_PREFIX}-del-${run_tag}"
  info "Scenario 1: Creating workspace '${ws_del_name}' (will be DB-deleted in simulate)..."
  local ws_del_resp ws_del_id
  ws_del_resp=$(_hbi_create_workspace "${ws_del_name}") || return 1
  ws_del_id=$(echo "${ws_del_resp}" | jq -r '.id')
  good "Created: ${ws_del_name} (${ws_del_id})"

  # --- Scenario 3: workspace for SKIP ---
  local ws_skip_name="${DR_HBI_WORKSPACE_PREFIX}-skip-${run_tag}"
  info "Scenario 3: Creating workspace '${ws_skip_name}' (stays in DB — SKIP)..."
  local ws_skip_resp ws_skip_id
  ws_skip_resp=$(_hbi_create_workspace "${ws_skip_name}") || return 1
  ws_skip_id=$(echo "${ws_skip_resp}" | jq -r '.id')
  good "Created: ${ws_skip_name} (${ws_skip_id})"

  # --- Scenario 2: workspace for corrective CREATE ---
  local ws_add_name="${DR_HBI_WORKSPACE_PREFIX}-add-${run_tag}"
  info "Scenario 2: Creating workspace '${ws_add_name}' (will be API-deleted now)..."
  local ws_add_resp ws_add_id
  ws_add_resp=$(_hbi_create_workspace "${ws_add_name}") || return 1
  ws_add_id=$(echo "${ws_add_resp}" | jq -r '.id')
  good "Created: ${ws_add_name} (${ws_add_id})"

  info "Scenario 2: Deleting '${ws_add_name}' via API (generates 'delete' event)..."
  _hbi_delete_workspace_api "${ws_add_id}" || return 1

  # --- Save state ---
  dr_state_save DR_HBI_ORG_ID              "${DR_HBI_ORG_ID}"
  dr_state_save DR_HBI_WS_DEL_ID           "${ws_del_id}"
  dr_state_save DR_HBI_WS_DEL_NAME         "${ws_del_name}"
  dr_state_save DR_HBI_WS_SKIP_ID          "${ws_skip_id}"
  dr_state_save DR_HBI_WS_SKIP_NAME        "${ws_skip_name}"
  dr_state_save DR_HBI_WS_ADD_ID           "${ws_add_id}"
  dr_state_save DR_HBI_WS_ADD_NAME         "${ws_add_name}"

  echo ""
  echo -e "${BOLD}=== Setup complete ===${NC}"
  echo "  Corrective DELETE ws: ${ws_del_id} (${ws_del_name}) — in DB"
  echo "  Corrective CREATE ws: ${ws_add_id} (${ws_add_name}) — API-deleted, NOT in DB"
  echo "  SKIP ws:              ${ws_skip_id} (${ws_skip_name}) — in DB"
  info "Next: DR_STEP=simulate $0 --rbac-hbi"
}

dr_hbi_simulate() {
  # Simulate a DB restore that creates divergence across 3 scenarios:
  #   1. Corrective DELETE: DB-delete the workspace (Kafka has create, DB missing)
  #   2. Corrective CREATE: Re-insert the API-deleted workspace (Kafka has delete, DB exists)
  #   3. SKIP: Leave the workspace in DB (Kafka has create, DB exists)
  _hbi_require_org_id || return 1
  dr_state_load

  local ws_del_id="${DR_HBI_WS_DEL_ID:-}"
  local ws_add_id="${DR_HBI_WS_ADD_ID:-}"
  local ws_add_name="${DR_HBI_WS_ADD_NAME:-}"
  local ws_skip_id="${DR_HBI_WS_SKIP_ID:-}"
  if [[ -z "${ws_del_id}" || -z "${ws_add_id}" || -z "${ws_skip_id}" ]]; then
    err "Missing workspace IDs in state. Run setup first."
    return 1
  fi

  local restore_ts
  restore_ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  dr_state_save DR_RESTORE_TIMESTAMP "${restore_ts}"
  info "Restore timestamp: ${restore_ts}"

  _db_creds || return 1

  echo ""
  echo -e "${BOLD}==================================================================${NC}"
  echo -e "${BOLD}  SIMULATE: Creating 3-way divergence${NC}"
  echo -e "${BOLD}==================================================================${NC}"

  # Scenario 1: DB-delete the workspace → corrective DELETE
  echo ""
  echo -e "--- Scenario 1: Corrective DELETE (DB-delete ${ws_del_id}) ---"
  echo "  Kafka has 'create' event, but workspace will be missing from DB."
  echo "  Reconciler should write a corrective DELETE event."
  echo ""
  _db_query "DELETE FROM management_workspace WHERE id='${ws_del_id}' RETURNING id, name, type;"

  # Scenario 2: Re-insert the API-deleted workspace → corrective CREATE
  echo ""
  echo -e "--- Scenario 2: Corrective CREATE (re-insert ${ws_add_id}) ---"
  echo "  Kafka has 'delete' event (from API deletion in setup)."
  echo "  We re-insert into DB to simulate it being in the backup."
  echo "  Reconciler should write a corrective CREATE event."
  echo ""

  local tenant_id parent_id
  tenant_id=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
    psql -U "${_db_user}" -d "${_db_name}" -t -A \
    -c "SELECT id FROM api_tenant WHERE org_id='${DR_HBI_ORG_ID}';" 2>/dev/null | tr -d '\r')
  parent_id=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
    psql -U "${_db_user}" -d "${_db_name}" -t -A \
    -c "SELECT id FROM management_workspace WHERE tenant_id=${tenant_id} AND type='root' LIMIT 1;" 2>/dev/null | tr -d '\r')

  if [[ -n "${tenant_id}" && -n "${parent_id}" ]]; then
    PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
      psql -U "${_db_user}" -d "${_db_name}" -c \
      "INSERT INTO management_workspace (id, name, type, tenant_id, parent_id, created, modified) VALUES ('${ws_add_id}', '${ws_add_name}', 'standard', ${tenant_id}, '${parent_id}', NOW(), NOW()) ON CONFLICT DO NOTHING;" 2>/dev/null || true
    good "Re-inserted: ${ws_add_id} (simulates backup restore)"
  else
    err "Could not determine tenant_id/parent_id for re-insert."
    return 1
  fi

  # Scenario 3: SKIP — leave ws_skip in DB (nothing to do)
  echo ""
  echo -e "--- Scenario 3: SKIP (${ws_skip_id} stays in DB) ---"
  echo "  Kafka has 'create' event AND workspace exists in DB → consistent."
  echo "  Reconciler should SKIP this workspace."
  good "No action needed — workspace remains in DB."

  echo ""
  echo -e "${BOLD}=== Workspace state after simulated restore ===${NC}"
  _workspaces_for_org "${DR_HBI_ORG_ID}"

  echo ""
  echo -e "${BOLD}Expected reconciler results:${NC}"
  echo "  corrective_deletes: 1  (${ws_del_id})"
  echo "  corrective_creates: 1  (${ws_add_id})"
  echo "  skipped:            1  (${ws_skip_id})"
  info "Next: DR_STEP=fix $0 --rbac-hbi"
}

dr_hbi_pre_check() {
  _hbi_require_org_id || return 1
  dr_state_load
  _db_creds || return 1

  echo ""
  echo -e "${BOLD}=== Pre-check: Workspace state before reconciliation ===${NC}"
  _workspaces_for_org "${DR_HBI_ORG_ID}"

  echo ""
  echo -e "${BOLD}Expected state:${NC}"
  echo "  ${DR_HBI_WS_DEL_ID:-?} (${DR_HBI_WS_DEL_NAME:-?})  — ABSENT (DB-deleted → corrective DELETE)"
  echo "  ${DR_HBI_WS_ADD_ID:-?} (${DR_HBI_WS_ADD_NAME:-?})  — PRESENT (re-inserted → corrective CREATE)"
  echo "  ${DR_HBI_WS_SKIP_ID:-?} (${DR_HBI_WS_SKIP_NAME:-?}) — PRESENT (untouched → SKIP)"
  info "Next: DR_STEP=fix $0 --rbac-hbi"
}

dr_hbi_fix() {
  # POST /_private/api/disaster_recovery/workspaces/
  # Returns 202; Celery task reads workspace Kafka events in the restore window
  # and writes corrective events to restore the DB.
  _hbi_require_org_id || return 1
  dr_state_load

  local restore_ts="${DR_RESTORE_TIMESTAMP:-}"
  if [[ -z "$restore_ts" ]]; then
    err "DR_RESTORE_TIMESTAMP not set. Run simulate step first."
    return 1
  fi

  local body
  body=$(jq -nc \
    --arg  ts  "${restore_ts}" \
    --argjson buf "${DR_BUFFER_MINUTES}" \
    --argjson dry "${DR_DRY_RUN}" \
    '{restore_timestamp: $ts, buffer_minutes: $buf, dry_run: $dry}')

  info "POST /_private/api/disaster_recovery/workspaces/"
  info "  restore_timestamp : ${restore_ts}"
  info "  buffer_minutes    : ${DR_BUFFER_MINUTES}"
  info "  dry_run           : ${DR_DRY_RUN}"

  local response
  response=$(exec_private_curl POST /_private/api/disaster_recovery/workspaces/ \
    -H "Content-Type: application/json" -d "${body}") || return 1

  echo ""
  echo "=== Workspace recovery enqueued (202) ==="
  echo "$response" | jq .

  local task_id
  task_id=$(echo "$response" | jq -r '.task_id // empty')
  [[ -n "$task_id" ]] && dr_state_save DR_WORKSPACE_RECOVERY_TASK_ID "${task_id}"

  if [[ -n "${task_id}" ]]; then
    echo ""
    info "Waiting for workspace recovery task to complete..."
    local worker_pod
    worker_pod=$(get_pod_by_label "${RBAC_WORKER_POD_LABEL}")
    if [[ -z "${worker_pod}" ]]; then
      warn "No worker pod found — check logs manually."
      return 0
    fi
    local attempt=0 max_attempts=60 result_line=""
    while [[ "${attempt}" -lt "${max_attempts}" ]]; do
      result_line=$(oc logs "${worker_pod}" --tail=200 2>/dev/null \
        | grep "${task_id}" \
        | grep -E "succeeded|failed" \
        | tail -1 || true)
      if [[ -n "${result_line}" ]]; then
        break
      fi
      sleep 2
      attempt=$((attempt + 1))
      if (( attempt % 5 == 0 )); then
        echo -n "."
      fi
    done
    echo ""
    if [[ -z "${result_line}" ]]; then
      warn "Timed out after 120s. Check worker logs: oc logs ${worker_pod} | grep ${task_id}"
    elif echo "${result_line}" | grep -q "succeeded"; then
      good "Workspace recovery task completed successfully."
    else
      bad "Workspace recovery task failed. Check: oc logs ${worker_pod} | grep ${task_id}"
    fi
  fi
}

dr_hbi_manual_fix() {
  # Show the user the exact curl command to trigger workspace reconciliation,
  # then wait for them to run it manually.
  _hbi_require_org_id || return 1
  dr_state_load

  local restore_ts="${DR_RESTORE_TIMESTAMP:-}"
  if [[ -z "$restore_ts" ]]; then
    err "DR_RESTORE_TIMESTAMP not set. Run simulate step first."
    return 1
  fi

  local pod
  pod=$(get_pod_by_label "${RBAC_SERVICE_POD_LABEL}")
  if [[ -z "$pod" ]]; then
    err "No running RBAC service pod found (label: ${RBAC_SERVICE_POD_LABEL})"
    return 1
  fi

  local identity_header
  identity_header=$(build_internal_identity_header)

  local body
  body=$(jq -nc \
    --arg  ts  "${restore_ts}" \
    --argjson buf "${DR_BUFFER_MINUTES}" \
    '{restore_timestamp: $ts, buffer_minutes: $buf, dry_run: false}')

  echo ""
  echo -e "${BOLD}==================================================================${NC}"
  echo -e "${BOLD}  MANUAL RECONCILIATION${NC}"
  echo -e "${BOLD}==================================================================${NC}"
  echo ""
  echo "  Run this command to trigger workspace DR reconciliation:"
  echo ""
  echo -e "${CYAN}oc exec ${pod} -- curl -s -X POST \\\\${NC}"
  echo -e "${CYAN}  -H 'X-RH-Identity: ${identity_header}' \\\\${NC}"
  echo -e "${CYAN}  -H 'Content-Type: application/json' \\\\${NC}"
  echo -e "${CYAN}  -d '${body}' \\\\${NC}"
  echo -e "${CYAN}  http://localhost:8000/_private/api/disaster_recovery/workspaces/${NC}"
  echo ""
  echo "  Parameters:"
  echo "    restore_timestamp : ${restore_ts}"
  echo "    buffer_minutes    : ${DR_BUFFER_MINUTES}"
  echo "    dry_run           : false"
  echo ""

  read -r -p "Press Enter after you have run the command (or 'q' to quit)... " _wait
  if [[ "${_wait}" == "q" || "${_wait}" == "Q" ]]; then
    info "Aborted. Re-run with DR_STEP=manual-fix when ready."
    return 0
  fi

  echo ""
  info "Checking worker logs for task result..."
  local worker_pod
  worker_pod=$(get_pod_by_label "${RBAC_WORKER_POD_LABEL}")
  if [[ -z "${worker_pod}" ]]; then
    warn "No worker pod found — check logs manually."
    return 0
  fi

  local attempt=0 max_attempts=60 result_line=""
  while [[ "${attempt}" -lt "${max_attempts}" ]]; do
    result_line=$(oc logs "${worker_pod}" --tail=200 2>/dev/null \
      | grep -E "recover_workspace_events" \
      | grep -E "succeeded|failed" \
      | tail -1 || true)
    if [[ -n "${result_line}" ]]; then
      break
    fi
    sleep 2
    attempt=$((attempt + 1))
    if (( attempt % 5 == 0 )); then
      echo -n "."
    fi
  done
  echo ""
  if [[ -z "${result_line}" ]]; then
    warn "No task result found in logs after 120s."
    echo "  Check manually: oc logs ${worker_pod} | grep recover_workspace_events"
  elif echo "${result_line}" | grep -q "succeeded"; then
    good "Workspace recovery task completed successfully."
  else
    bad "Workspace recovery task may have failed. Check: oc logs ${worker_pod} | grep recover_workspace_events"
  fi
}

dr_hbi_post_check() {
  _hbi_require_org_id || return 1
  dr_state_load
  _db_creds || return 1

  echo ""
  echo -e "${BOLD}=== Post-check: Workspace state after reconciliation ===${NC}"
  _workspaces_for_org "${DR_HBI_ORG_ID}"

  echo ""
  echo -e "${BOLD}Expected results:${NC}"
  echo "  ${DR_HBI_WS_DEL_ID:-?}  — corrective DELETE event written (downstream told to remove)"
  echo "  ${DR_HBI_WS_ADD_ID:-?}  — corrective CREATE event written (downstream told to add)"
  echo "  ${DR_HBI_WS_SKIP_ID:-?} — SKIPPED (no corrective event needed)"
}

dr_hbi_cleanup() {
  # Remove all test data created by DR HBI steps and reset state.
  # Safe to call at any point — skips missing resources gracefully.
  dr_state_load

  echo ""
  echo -e "${BOLD}==================================================================${NC}"
  echo -e "${BOLD}  CLEANUP: Removing DR HBI test data${NC}"
  echo -e "${BOLD}==================================================================${NC}"
  echo ""

  local ws_del_id="${DR_HBI_WS_DEL_ID:-}"
  local ws_skip_id="${DR_HBI_WS_SKIP_ID:-}"
  local ws_add_id="${DR_HBI_WS_ADD_ID:-}"
  local cleaned=0

  # Delete any workspaces that may still exist in the DB (del + skip + add scenarios).
  local ws_ids_csv
  ws_ids_csv=$(printf '%s,' "${ws_del_id}" "${ws_skip_id}" "${ws_add_id}" | sed 's/,$//' | tr -s ',')
  ws_ids_csv="${ws_ids_csv%,}"

  if [[ -n "${ws_del_id}" || -n "${ws_skip_id}" || -n "${ws_add_id}" ]]; then
    _db_creds 2>/dev/null || {
      warn "Could not get DB credentials — skipping DB cleanup."
      echo ""
      echo "  State file still contains references. Clear manually:"
      echo "    rm -f ${DR_STATE_FILE}"
      return 0
    }

    for ws_id in "${ws_del_id}" "${ws_skip_id}" "${ws_add_id}"; do
      [[ -z "${ws_id}" ]] && continue
      local result
      result=$(PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
        psql -U "${_db_user}" -d "${_db_name}" -t -A \
        -c "DELETE FROM management_workspace WHERE id='${ws_id}' RETURNING id;" 2>/dev/null | tr -d '\r')
      if [[ -n "${result}" ]]; then
        good "Deleted workspace ${ws_id}"
        cleaned=$((cleaned + 1))
      else
        echo -e "  ${DIM}[----]${NC} ${ws_id} (not found, already removed)"
      fi
    done
  else
    echo -e "  ${DIM}No workspace IDs in state — nothing to clean from DB.${NC}"
  fi

  # Clear state file.
  if [[ -f "${DR_STATE_FILE}" ]]; then
    rm -f "${DR_STATE_FILE}"
    good "State file removed: ${DR_STATE_FILE}"
  fi

  echo ""
  echo -e "  Cleaned ${GREEN}${cleaned}${NC} workspace(s) from DB. State reset."
  echo "  Ready for a fresh run."
}

dr_hbi() {
  local step="${DR_STEP:-all}"
  echo "=== RBAC <-> HBI DR [disaster_recovery/workspaces/] step=${step} ==="
  echo ""

  # Pre-flight: verify required env vars (DR_WORKSPACE_RECONCILE_ENABLED) before any step.
  if [[ "${DR_FAST}" != "true" ]]; then
    info "Checking DR env vars on pods..."
    _check_dr_enabled || return 1
    echo ""
  else
    info "Fast mode — skipping DR env var checks."
  fi

  case "$step" in
    setup)      dr_hbi_setup ;;
    simulate)   dr_hbi_simulate ;;
    pre-check)  dr_hbi_pre_check ;;
    dry-run)    DR_DRY_RUN=true dr_hbi_fix ;;
    fix)        dr_hbi_fix ;;
    manual-fix) dr_hbi_manual_fix ;;
    post-check) dr_hbi_post_check ;;
    cleanup)    dr_hbi_cleanup ;;
    state)      dr_state_show ;;
    all)
      dr_hbi_setup
      dr_hbi_simulate
      if [[ "${DR_FAST}" != "true" ]]; then
        dr_hbi_pre_check
      fi
      if [[ "${DR_NO_DRY_RUN}" != "true" ]]; then
        DR_DRY_RUN=true dr_hbi_fix
        if [[ "${DR_FAST}" == "true" ]]; then
          info "Fast mode — auto-proceeding to real fix."
          _confirm="y"
        else
          echo ""
          read -r -p "Dry run complete. Proceed with real fix? [y/N] " _confirm
        fi
      else
        info "Skipping dry run — proceeding directly to real fix."
        _confirm="y"
      fi
      if [[ "${_confirm}" == "y" || "${_confirm}" == "Y" ]]; then
        DR_DRY_RUN=false dr_hbi_fix
      else
        info "Aborted after dry run. Re-run with DR_STEP=fix when ready."
        return 0
      fi
      dr_hbi_post_check

      echo ""
      echo -e "${BOLD}${GREEN}==================================================================${NC}"
      echo -e "${BOLD}${GREEN}  RBAC <-> HBI DR: ALL STEPS COMPLETED SUCCESSFULLY${NC}"
      echo -e "${BOLD}${GREEN}==================================================================${NC}"
      echo ""
      echo "  Steps executed: setup → simulate → fix → cleanup"
      echo "  Scenarios tested:"
      echo "    1. Corrective DELETE — create event + NOT in DB → DELETE written"
      echo "    2. Corrective CREATE — delete event + in DB    → CREATE written"
      echo "    3. SKIP             — create event + in DB     → no action"
      if [[ "${DR_FAST}" == "true" ]]; then
        echo "  Mode: --fast (safety checks skipped)"
      fi
      if [[ "${DR_NO_DRY_RUN}" == "true" ]]; then
        echo "  Mode: --no-dry-run (dry run skipped)"
      fi
      if [[ "${DR_MINIMAL_DATA}" == "true" ]]; then
        echo "  Mode: --minimal-data (1 resource per scenario)"
      fi
      echo ""
      dr_hbi_cleanup

      echo ""
      echo "  To verify results manually:"
      echo "    DR_STEP=post-check $0 --rbac-hbi"
      echo ""
      ;;
    *) err "Unknown step: ${step}. Valid: setup|simulate|pre-check|dry-run|fix|manual-fix|post-check|cleanup|state|all"; return 1 ;;
  esac
}

# ── Main ──────────────────────────────────────────────────────────────────────

usage() {
  cat <<EOF
RBAC Disaster Recovery Toolkit
Usage: $0 <command> [--fast] [args]

Flags:
  --fast                        Skip safety checks, sync waits, and auto-confirm prompts
  --no-dry-run                  Skip the dry-run phase, go straight to real fix
  --minimal-data                Use 1 resource per scenario instead of 2

DR Simulation commands:
  --rbac-kessel   RBAC <-> Kessel via POST /_private/api/disaster_recovery/reconcile/
  --rbac-hbi      RBAC <-> HBI    via POST /_private/api/disaster_recovery/workspaces/
  --dr-state      Show saved DR state file (timestamps, task IDs, workspace UUIDs)

  Six phases per scenario, run individually with DR_STEP=<phase>:
    DR_KESSEL_ORG_ID=<id> DR_STEP=setup      $0 --rbac-kessel
    DR_KESSEL_ORG_ID=<id> DR_STEP=simulate   $0 --rbac-kessel
    DR_KESSEL_ORG_ID=<id> DR_STEP=pre-check  $0 --rbac-kessel
    DR_KESSEL_ORG_ID=<id> DR_STEP=dry-run    $0 --rbac-kessel  # fix with dry_run=true
    DR_KESSEL_ORG_ID=<id> DR_STEP=fix        $0 --rbac-kessel  # real fix
    DR_KESSEL_ORG_ID=<id> DR_STEP=post-check $0 --rbac-kessel
    (same pattern for --rbac-hbi with DR_HBI_ORG_ID)

  'all' runs all phases and pauses for confirmation between dry-run and fix.

Utility commands:
  --workspaces                  List workspaces via public v2 API
  --replication                 Fetch replication data (pg slots, WAL LSN)
  --all                         Run --workspaces + --replication (default)
  --create-workspace NAME       Create a workspace via public v2 API
  --delete-workspace-db ID|NAME Delete a workspace directly from the DB pod (psql)
  --bootstrap-tenant ORG_ID..   Bootstrap one or more tenants (synchronous POST)
  --run-seeds                   Trigger run_seeds_in_worker Celery task, then tail worker logs
  --watch-worker                Follow Celery worker pod logs (Ctrl+C to stop)
  --watch-server                Follow RBAC service pod logs (Ctrl+C to stop)
  --debug-psk                   Show identity header used for internal auth
  --help                        Show this message

Environment overrides:
  DR_STEP                     Phase: setup|simulate|pre-check|fix|post-check|state|all
  DR_STATE_FILE               State file path  (default: /tmp/rbac-dr-state.env)
  DR_KESSEL_ORG_ID            Org ID for rbac-kessel scenario (auto-resolved if unset)
  DR_KESSEL_WORKSPACE_NAME    Test workspace name  (default: dr-test-kessel-ws)
  DR_BUFFER_SECONDS           Reconcile window in seconds  (default: 300)
  DR_HBI_ORG_ID               Org ID for rbac-hbi scenario (auto-resolved if unset)
  DR_HBI_WORKSPACE_PREFIX     Test workspace name prefix  (default: dr-hbi)
  DR_BUFFER_MINUTES           Workspace recovery window in minutes  (default: 5)
  DR_DRY_RUN                  true|false -- skip writes, return planned actions  (default: false)
  DR_FAST                     true|false -- skip safety checks, auto-confirm  (default: false)
  DR_NO_DRY_RUN               true|false -- skip dry-run, go straight to fix  (default: false)
  DR_MINIMAL_DATA              true|false -- 1 resource per scenario           (default: false)
  RBAC_DB_POD_LABEL           Pod selector for DB pod         (default: pod=rbac-db)
  RBAC_SERVICE_POD_LABEL      Pod selector for RBAC service   (default: pod=rbac-service)
  RBAC_WORKER_POD_LABEL       Pod selector for Celery worker  (default: pod=rbac-worker-service)
EOF
}

main() {
  check_deps

  # Strip --fast from any position in the argument list.
  local _args=()
  for _a in "$@"; do
    if [[ "${_a}" == "--fast" ]]; then
      DR_FAST=true
    elif [[ "${_a}" == "--no-dry-run" ]]; then
      DR_NO_DRY_RUN=true
    elif [[ "${_a}" == "--minimal-data" ]]; then
      DR_MINIMAL_DATA=true
    else
      _args+=("${_a}")
    fi
  done
  set -- "${_args[@]+"${_args[@]}"}"

  local cmd="${1:---all}"

  if [[ "$cmd" != "--help" && "$cmd" != "-h" ]]; then
    check_bonfire_env
    info "Namespace: ${EPHEMERAL_NAMESPACE}"
    info "Host:      ${EPHEMERAL_HOST_NAME}"
    [[ "${DR_FAST}" == "true" ]] && info "Fast mode: ON (skipping safety checks)"
    [[ "${DR_MINIMAL_DATA}" == "true" ]] && info "Minimal data: ON (1 per scenario)"
    echo ""
  fi

  case "$cmd" in
    --workspaces)           list_workspaces ;;
    --replication)          fetch_replication_data ;;
    --all|"")               list_workspaces; fetch_replication_data ;;
    --create-workspace)     shift; create_workspace "$@" ;;
    --delete-workspace-db)  shift; delete_workspace_db "$@" ;;
    --bootstrap-tenant)     shift; bootstrap_tenant "$@" ;;
    --run-seeds)            run_seeds ;;
    --debug-psk)            debug_psk ;;
    --watch-worker)         watch_worker ;;
    --watch-server)         watch_server ;;
    --rbac-kessel)          dr_kessel ;;
    --rbac-hbi)             dr_hbi ;;
    --dr-state)             dr_state_show ;;
    --help|-h)              usage ;;
    *) err "Unknown command: $cmd"; echo ""; usage; exit 1 ;;
  esac
}

main "$@"
