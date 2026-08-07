#!/bin/bash
# RBAC Parity Check Toolkit
#
# Validates RBAC-Kessel data consistency on ephemeral clusters by exercising
# the on-demand parity check API endpoint (POST /_private/api/utils/kessel_parity_check/).
#
# The parity check verifies five sub-checks per org:
#   1. Workspace hierarchy   — parent-child workspace relations exist in Kessel
#   2. Custom roles           — role permission tuples exist in Kessel
#   3. Seeded role hierarchy  — platform role parent-child relations (global)
#   4. Bootstrap completeness — tenant/workspace/platform bindings
#   5. Group-principal        — group membership relations
#
# Two modes:
#   --check            Happy path: create data, wait for sync, run parity check, verify all pass
#   --check --failure  Failure path: same + introduce DB divergence, verify parity detects it
#
# Follows the same patterns as rbac-dr-toolkit.sh for infrastructure helpers.
#
# Prerequisites: oc (logged in), curl, jq
#
# Usage:
#   ./scripts/ephemeral/rbac-parity-toolkit.sh --check [--fast] [--minimal-data]
#   ./scripts/ephemeral/rbac-parity-toolkit.sh --check --failure [--fast] [--minimal-data]
#   PARITY_STEP=setup ./scripts/ephemeral/rbac-parity-toolkit.sh --check
#   PARITY_STEP=check ./scripts/ephemeral/rbac-parity-toolkit.sh --check

set -euo pipefail

# ── Environment setup ─────────────────────────────────────────────────────────

EPHEMERAL_NAMESPACE=$(oc project -q 2>/dev/null || true)
BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME=jdoe
EPHEMERAL_PASSWORD=$(oc get secret "env-${EPHEMERAL_NAMESPACE:-none}-keycloak" -o json 2>/dev/null \
  | jq -r '.data.defaultPassword // empty' | base64 -d 2>/dev/null || true)
EPHEMERAL_HOST_NAME=$(oc get frontendenvironment "env-${EPHEMERAL_NAMESPACE:-none}" -o json 2>/dev/null \
  | jq -r '.spec.hostname // empty' 2>/dev/null || true)
BENTO_URL=https://${EPHEMERAL_HOST_NAME}

# ── Config (overridable via env) ──────────────────────────────────────────────

RBAC_SERVICE_POD_LABEL="${RBAC_SERVICE_POD_LABEL:-pod=rbac-service}"
RBAC_WORKER_POD_LABEL="${RBAC_WORKER_POD_LABEL:-pod=rbac-worker-service}"
RBAC_DB_POD_LABEL="${RBAC_DB_POD_LABEL:-app=rbac,service=db}"

PARITY_ORG_ID="${PARITY_ORG_ID:-}"
PARITY_FAST="${PARITY_FAST:-false}"
PARITY_MINIMAL="${PARITY_MINIMAL:-false}"
PARITY_FAILURE="${PARITY_FAILURE:-false}"
PARITY_STEP="${PARITY_STEP:-all}"
PARITY_SYNC_WAIT="${PARITY_SYNC_WAIT:-30}"
PARITY_LOG_WAIT="${PARITY_LOG_WAIT:-500}"

# ── State file ────────────────────────────────────────────────────────────────

PARITY_STATE_FILE="${PARITY_STATE_FILE:-/tmp/rbac-parity-state.env}"

if [[ -f "${PARITY_STATE_FILE}" ]]; then
  _saved_ns=$(grep '^PARITY_STATE_NAMESPACE=' "${PARITY_STATE_FILE}" 2>/dev/null | cut -d= -f2) || true
  if [[ -n "${_saved_ns}" && "${_saved_ns}" != "${EPHEMERAL_NAMESPACE}" ]]; then
    echo "[WARN] Namespace changed (${_saved_ns} -> ${EPHEMERAL_NAMESPACE}) -- clearing stale state."
    rm -f "${PARITY_STATE_FILE}"
  fi
fi

state_save() {
  local key="$1" value="$2"
  if [[ -f "${PARITY_STATE_FILE}" ]]; then
    grep -v "^${key}=" "${PARITY_STATE_FILE}" > "${PARITY_STATE_FILE}.tmp" 2>/dev/null || true
    mv "${PARITY_STATE_FILE}.tmp" "${PARITY_STATE_FILE}"
  fi
  if ! grep -q '^PARITY_STATE_NAMESPACE=' "${PARITY_STATE_FILE}" 2>/dev/null; then
    echo "PARITY_STATE_NAMESPACE=${EPHEMERAL_NAMESPACE}" >> "${PARITY_STATE_FILE}"
  fi
  echo "${key}=${value}" >> "${PARITY_STATE_FILE}"
}

state_load() {
  if [[ -f "${PARITY_STATE_FILE}" ]]; then
    if grep -qvE '^[A-Z_]+=.*$|^$' "${PARITY_STATE_FILE}" 2>/dev/null; then
      err "State file contains invalid entries -- refusing to load: ${PARITY_STATE_FILE}"
      return 1
    fi
    while IFS='=' read -r key value; do
      [[ -z "${key}" ]] && continue
      printf -v "${key}" '%s' "${value}"
      export "${key}"
    done < "${PARITY_STATE_FILE}"
  fi
}

state_show() {
  echo ""
  echo "=== Parity State (${PARITY_STATE_FILE}) ==="
  if [[ -f "${PARITY_STATE_FILE}" ]]; then cat "${PARITY_STATE_FILE}"; else echo "(empty)"; fi
}

# ── Colors ────────────────────────────────────────────────────────────────────

if [[ -t 1 ]]; then
  RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'
  CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
else
  RED='' GREEN='' YELLOW='' CYAN='' BOLD='' NC=''
fi

# ── Helpers ───────────────────────────────────────────────────────────────────

err()  { echo -e "${RED}[ERROR]${NC} $*" >&2; }
info() { echo -e "${CYAN}[INFO]${NC}  $*" >&2; }
good() { echo -e "  ${GREEN}[GOOD]${NC} $*"; }
warn() { echo -e "  ${YELLOW}[WARN]${NC} $*"; }

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
    exit 1
  fi
  if ! oc get namespace "${EPHEMERAL_NAMESPACE}" &>/dev/null; then
    err "Namespace '${EPHEMERAL_NAMESPACE}' does not exist. Has the bonfire environment expired?"
    exit 1
  fi
  if ! oc get frontendenvironment "env-${EPHEMERAL_NAMESPACE}" &>/dev/null; then
    err "No bonfire environment found in namespace '${EPHEMERAL_NAMESPACE}'."
    exit 1
  fi
  local pod_count
  pod_count=$(oc get pods -l "${RBAC_SERVICE_POD_LABEL}" --field-selector=status.phase=Running \
              -o name 2>/dev/null | wc -l | tr -d ' ')
  if [[ "${pod_count}" -eq 0 ]]; then
    err "No running RBAC service pods found (label: ${RBAC_SERVICE_POD_LABEL})."
    exit 1
  fi
}

get_pod_by_label() {
  local label="$1"
  oc get pods -l "${label}" \
    --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null
}

# ── DB helpers ────────────────────────────────────────────────────────────────

_db_creds() {
  local svc_pod
  svc_pod=$(get_pod_by_label "${RBAC_SERVICE_POD_LABEL}")
  if [[ -z "$svc_pod" ]]; then
    err "No running RBAC service pod found"
    return 1
  fi

  _db_user=$(oc exec "${svc_pod}" -- sh -c 'echo "$DATABASE_USER"' 2>/dev/null | tr -d '\r')
  _db_password=$(oc exec "${svc_pod}" -- sh -c 'echo "$DATABASE_PASSWORD"' 2>/dev/null | tr -d '\r')
  _db_name=$(oc exec "${svc_pod}" -- sh -c 'echo "$DATABASE_NAME"' 2>/dev/null | tr -d '\r')
  local db_host
  db_host=$(oc exec "${svc_pod}" -- sh -c 'echo "${DATABASE_SERVICE_HOST:-$DATABASE_HOST}"' 2>/dev/null | tr -d '\r')

  if [[ -z "${_db_user}" || -z "${_db_password}" || -z "${_db_name}" ]]; then
    local cdapp_json
    cdapp_json=$(oc exec "${svc_pod}" -- cat /cdapp/cdappconfig.json 2>/dev/null || true)
    if [[ -n "${cdapp_json}" ]]; then
      _db_user="${_db_user:-$(echo "${cdapp_json}" | jq -r '.database.username // empty')}"
      _db_password="${_db_password:-$(echo "${cdapp_json}" | jq -r '.database.password // empty')}"
      _db_name="${_db_name:-$(echo "${cdapp_json}" | jq -r '.database.name // empty')}"
    fi
  fi

  _db_user="${_db_user:-rbac}"; _db_name="${_db_name:-rbac}"

  _db_pod=$(get_pod_by_label "${RBAC_DB_POD_LABEL}" 2>/dev/null)

  if [[ -z "${_db_pod}" && -n "${db_host}" ]]; then
    local svc_name="${db_host%%.*}"
    local selector
    selector=$(oc get svc "${svc_name}" -o jsonpath='{.spec.selector}' 2>/dev/null \
      | jq -r 'to_entries | map("\(.key)=\(.value)") | join(",")' 2>/dev/null)
    if [[ -n "$selector" ]]; then
      _db_pod=$(oc get pods -l "${selector}" --field-selector=status.phase=Running \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    fi
  fi

  if [[ -z "${_db_pod}" ]]; then
    err "No DB pod found"
    return 1
  fi
}

_db_query() {
  PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
    psql -U "${_db_user}" -d "${_db_name}" -t -A -c "$1" 2>/dev/null | tr -d '\r'
}

_db_query_parameterized() {
  local var_binding="$1" sql="$2"
  PGPASSWORD="${_db_password}" oc exec "${_db_pod}" -- \
    psql -U "${_db_user}" -d "${_db_name}" -v "${var_binding}" -t -A -c "${sql}" 2>/dev/null | tr -d '\r'
}

# ── Internal API helper ──────────────────────────────────────────────────────

build_internal_identity_header() {
  local identity_json
  identity_json=$(jq -nc \
    --arg email "admin@redhat.com" \
    --arg org_id "${PARITY_ORG_ID}" \
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

exec_private_curl() {
  local method="$1" path="$2"; shift 2
  local pod
  pod=$(get_pod_by_label "${RBAC_SERVICE_POD_LABEL}")
  if [[ -z "$pod" ]]; then
    err "No running RBAC service pod found"
    return 1
  fi
  local url="http://localhost:8000${path}"
  local identity_header
  identity_header=$(build_internal_identity_header)
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
    err "Response: ${body}"
    return 1
  fi
  echo "${body}"
}

# ── Org-admin API helper (calls pod directly, bypasses Kessel) ────────────────

build_org_admin_identity_header() {
  local identity_json
  identity_json=$(jq -nc \
    --arg org_id "${PARITY_ORG_ID}" \
    --arg username "${BENTO_BASIC_AUTH_CONSOLE_DOT_USERNAME:-jdoe}" \
    '{
      identity: {
        type: "User",
        user: { username: $username, email: ($username + "@redhat.com"), is_org_admin: true },
        account_number: "000000",
        org_id: $org_id,
        internal: { org_id: $org_id }
      }
    }')
  echo -n "${identity_json}" | base64
}

exec_api_curl() {
  local method="$1" path="$2"; shift 2
  local pod
  pod=$(get_pod_by_label "${RBAC_SERVICE_POD_LABEL}")
  if [[ -z "$pod" ]]; then
    err "No running RBAC service pod found"
    return 1
  fi
  local url="http://localhost:8000${path}"
  local identity_header
  identity_header=$(build_org_admin_identity_header)
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
    err "API call failed: ${method} ${path} -> HTTP ${http_code}"
    err "Response: ${body}"
    return 1
  fi
  echo "${body}"
}

# ── Org ID resolution ─────────────────────────────────────────────────────────

resolve_org_id() {
  if [[ -n "${PARITY_ORG_ID}" ]]; then return 0; fi

  state_load
  if [[ -n "${PARITY_ORG_ID:-}" ]]; then
    _db_creds || return 1
    local chk
    chk=$(_db_query "SELECT 1 FROM api_tenant WHERE org_id='${PARITY_ORG_ID}';")
    if [[ -n "${chk}" ]]; then return 0; fi
    warn "Stale org_id=${PARITY_ORG_ID} from state file -- re-resolving..."
    PARITY_ORG_ID=""
  fi

  info "Auto-resolving org_id from DB..."
  _db_creds || return 1

  PARITY_ORG_ID=$(_db_query "SELECT org_id FROM api_tenant
    WHERE tenant_name <> 'public' AND ready = true AND org_id IS NOT NULL
    LIMIT 1;")

  if [[ -z "${PARITY_ORG_ID}" ]]; then
    PARITY_ORG_ID=$(_db_query "SELECT org_id FROM api_tenant
      WHERE tenant_name <> 'public' AND org_id IS NOT NULL
      LIMIT 1;")
  fi

  if [[ -z "${PARITY_ORG_ID}" ]]; then
    info "No tenant found -- seeding one from namespace..."
    _db_query "INSERT INTO api_tenant (tenant_name, org_id, ready)
      VALUES ('${EPHEMERAL_NAMESPACE}', '${EPHEMERAL_NAMESPACE}', false)
      ON CONFLICT (org_id) DO NOTHING;" >/dev/null

    PARITY_ORG_ID=$(_db_query "SELECT org_id FROM api_tenant
      WHERE tenant_name <> 'public' AND org_id IS NOT NULL
      LIMIT 1;")
  fi

  if [[ -z "${PARITY_ORG_ID}" ]]; then
    err "Could not resolve org_id"
    return 1
  fi

  state_save PARITY_ORG_ID "${PARITY_ORG_ID}"
  good "Resolved org_id: ${PARITY_ORG_ID}"
}

# ── Bootstrap tenant ──────────────────────────────────────────────────────────

ensure_bootstrapped() {
  info "Ensuring tenant ${PARITY_ORG_ID} is bootstrapped..."

  local ready
  ready=$(_db_query "SELECT ready FROM api_tenant WHERE org_id='${PARITY_ORG_ID}';")
  if [[ "${ready}" == "t" ]]; then
    good "Tenant already bootstrapped"
    return 0
  fi

  info "Bootstrapping tenant via internal API..."
  local body
  body=$(jq -nc --arg org "${PARITY_ORG_ID}" '{"org_ids": [$org]}')
  exec_private_curl POST "/_private/api/utils/bootstrap_tenant/" \
    -H "Content-Type: application/json" -d "${body}" >/dev/null || {
    err "Bootstrap failed"
    return 1
  }

  local attempts=0
  while [[ "${attempts}" -lt 30 ]]; do
    ready=$(_db_query "SELECT ready FROM api_tenant WHERE org_id='${PARITY_ORG_ID}';")
    [[ "${ready}" == "t" ]] && break
    sleep 2
    attempts=$((attempts + 1))
  done

  if [[ "${ready}" != "t" ]]; then
    err "Tenant did not become ready after bootstrap"
    return 1
  fi
  good "Tenant bootstrapped successfully"
}

# ── Activate V2 writes ───────────────────────────────────────────────────────

activate_v2_writes() {
  info "Activating V2 writes for tenant..."
  local tenant_id
  tenant_id=$(_db_query "SELECT id FROM api_tenant WHERE org_id='${PARITY_ORG_ID}';")
  if [[ -z "${tenant_id}" ]]; then
    err "Cannot find tenant for org_id=${PARITY_ORG_ID}"
    return 1
  fi
  _db_query "UPDATE management_tenantmapping
    SET v2_write_activated_at = NOW()
    WHERE tenant_id = ${tenant_id} AND v2_write_activated_at IS NULL;" >/dev/null 2>&1 || true
  local check
  check=$(_db_query "SELECT v2_write_activated_at IS NOT NULL FROM management_tenantmapping WHERE tenant_id = ${tenant_id};")
  if [[ "${check}" == "t" ]]; then
    good "V2 writes activated"
  else
    warn "Could not verify V2 activation -- role creation may fail"
  fi
}

# ══════════════════════════════════════════════════════════════════════════════
# PHASE: SETUP -- Create test data covering all 5 parity sub-checks
# ══════════════════════════════════════════════════════════════════════════════

phase_setup() {
  local run_tag
  run_tag=$(date +%s | tail -c 6)

  echo ""
  echo -e "${BOLD}=== Phase: SETUP ===${NC}"
  echo ""
  echo "  Creating test data via Django ORM (bypasses V2 API permission checks)."
  echo "  Run tag: ${run_tag}"
  echo ""

  resolve_org_id || return 1
  _db_creds || return 1
  ensure_bootstrapped || return 1
  activate_v2_writes || return 1

  state_save PARITY_RUN_TAG "${run_tag}"

  local svc_pod
  svc_pod=$(get_pod_by_label "${RBAC_SERVICE_POD_LABEL}")
  if [[ -z "${svc_pod}" ]]; then
    err "No running RBAC service pod found"
    return 1
  fi

  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  info "Copying parity_data_setup.py to pod ${svc_pod}..."
  oc cp "${script_dir}/parity_data_setup.py" "${svc_pod}:/tmp/parity_data_setup.py" || {
    err "Failed to copy setup script to pod"
    return 1
  }

  local setup_cmd="cd /opt/rbac/rbac && DJANGO_SETTINGS_MODULE=rbac.settings /opt/rbac/.venv/bin/python /tmp/parity_data_setup.py setup --org-id=${PARITY_ORG_ID} --run-tag=${run_tag}"
  [[ "${PARITY_MINIMAL}" == "true" ]] && setup_cmd="${setup_cmd} --minimal"

  info "Running ORM-based data setup on pod..."
  local raw_output
  raw_output=$(oc exec "${svc_pod}" -- sh -c "${setup_cmd}" 2>&1)

  local setup_output
  setup_output=$(echo "${raw_output}" | grep -v '^Defaulted' | tail -1)

  local setup_error
  setup_error=$(echo "${setup_output}" | jq -r '.error // empty' 2>/dev/null)
  if [[ -n "${setup_error}" ]]; then
    err "Setup script failed: ${setup_error}"
    err "Full output: ${raw_output}"
    return 1
  fi

  local ws_ids_csv role_uuid group_ids_csv test_username binding_count role_perm_count
  ws_ids_csv=$(echo "${setup_output}" | jq -r '.workspace_ids | join(",")')
  role_uuid=$(echo "${setup_output}" | jq -r '.role_uuid')
  group_ids_csv=$(echo "${setup_output}" | jq -r '.group_ids | join(",")')
  test_username=$(echo "${setup_output}" | jq -r '.test_username')
  binding_count=$(echo "${setup_output}" | jq -r '.binding_count')
  role_perm_count=$(echo "${setup_output}" | jq -r '.role_perm_count')

  if [[ -z "${ws_ids_csv}" || "${ws_ids_csv}" == "null" ]]; then
    err "Setup script returned no workspace IDs"
    err "Output: ${raw_output}"
    return 1
  fi

  state_save PARITY_WORKSPACE_IDS "${ws_ids_csv}"
  state_save PARITY_ROLE_UUID "${role_uuid}"
  state_save PARITY_GROUP_IDS "${group_ids_csv}"
  state_save PARITY_TEST_USERNAME "${test_username}"

  local ws_count group_count
  ws_count=$(echo "${ws_ids_csv}" | tr ',' '\n' | wc -l | tr -d ' ')
  group_count=$(echo "${group_ids_csv}" | tr ',' '\n' | wc -l | tr -d ' ')

  echo ""
  echo -e "${BOLD}=== Setup Summary ===${NC}"
  echo ""
  good "Workspaces:  ${ws_count} created (${ws_ids_csv})"
  good "Role:        parity-role-${run_tag} (${role_uuid}) with ${role_perm_count} permission(s)"
  good "Groups:      ${group_count} with principal '${test_username}'"
  good "Bindings:    ${binding_count} (role -> group -> workspace)"
  echo "  Bootstrap:   pre-existing"
  echo "  Seeded roles: pre-existing"
  echo ""
  good "Setup complete. All 5 parity sub-check categories have test data."
}

# ══════════════════════════════════════════════════════════════════════════════
# PHASE: WAIT -- Let Debezium/Kafka replication sync to Kessel
# ══════════════════════════════════════════════════════════════════════════════

phase_wait() {
  echo ""
  echo -e "${BOLD}=== Phase: WAIT ===${NC}"
  echo ""

  if [[ "${PARITY_FAST}" == "true" ]]; then
    info "Fast mode -- skipping sync wait."
    return 0
  fi

  echo "  Waiting ${PARITY_SYNC_WAIT}s for Debezium/Kafka to replicate data to Kessel."
  echo "  Override with PARITY_SYNC_WAIT=<seconds>"
  echo ""

  local elapsed=0
  while [[ "${elapsed}" -lt "${PARITY_SYNC_WAIT}" ]]; do
    local remaining=$((PARITY_SYNC_WAIT - elapsed))
    printf "\r  Waiting... %ds remaining  " "${remaining}"
    sleep 5
    elapsed=$((elapsed + 5))
  done
  printf "\r  Wait complete.                      \n"
  echo ""
  good "Sync wait finished."
}

# ══════════════════════════════════════════════════════════════════════════════
# PHASE: BREAK -- Introduce divergence for failure path
# ══════════════════════════════════════════════════════════════════════════════

phase_break() {
  echo ""
  echo -e "${BOLD}=== Phase: BREAK (Failure Path) ===${NC}"
  echo ""
  echo "  Introducing RBAC DB <-> Kessel divergence by deleting a workspace"
  echo "  directly from the database (bypassing dual-write to Kessel)."
  echo "  Kessel will still have the relation, but RBAC DB won't."
  echo ""

  state_load
  _db_creds || return 1

  local ws_ids_csv="${PARITY_WORKSPACE_IDS:-}"
  if [[ -z "${ws_ids_csv}" ]]; then
    err "No workspace IDs in state file. Run setup first."
    return 1
  fi

  # Pick the first workspace to delete from DB.
  local target_ws_id="${ws_ids_csv%%,*}"
  info "Deleting workspace ${target_ws_id} directly from DB..."

  # First remove any role bindings referencing this workspace.
  local rb_deleted
  rb_deleted=$(_db_query_parameterized "ws_id=${target_ws_id}" "DELETE FROM management_rolebinding
    WHERE resource_id = :'ws_id'
    RETURNING uuid;")
  if [[ -n "${rb_deleted}" ]]; then
    info "  Removed role bindings referencing workspace: ${rb_deleted}"
  fi

  # Delete the workspace itself.
  local ws_deleted
  ws_deleted=$(_db_query_parameterized "ws_id=${target_ws_id}" "DELETE FROM management_workspace
    WHERE id = :'ws_id'
    RETURNING id, name;")

  if [[ -n "${ws_deleted}" ]]; then
    good "Deleted workspace from DB: ${ws_deleted}"
    state_save PARITY_BROKEN_WORKSPACE_ID "${target_ws_id}"

    # Update workspace IDs in state to exclude the broken one (prevents cleanup errors).
    local remaining_ids
    remaining_ids=$(echo "${ws_ids_csv}" | tr ',' '\n' | grep -v "^${target_ws_id}$" | paste -sd, -)
    state_save PARITY_WORKSPACE_IDS "${remaining_ids}"
  else
    warn "Workspace ${target_ws_id} not found in DB (already deleted?)"
  fi

  echo ""
  echo "  Kessel still has the workspace relation, but RBAC DB does not."
  echo "  The parity check should detect this as a workspace hierarchy failure."
  echo ""
}

# ══════════════════════════════════════════════════════════════════════════════
# PHASE: CHECK -- Call the parity check API endpoint
# ══════════════════════════════════════════════════════════════════════════════

phase_check() {
  echo ""
  echo -e "${BOLD}=== Phase: CHECK ===${NC}"
  echo ""

  state_load
  if [[ -z "${PARITY_ORG_ID:-}" ]]; then
    resolve_org_id || return 1
  fi

  echo "  Org ID: ${PARITY_ORG_ID}"
  echo ""

  # Try the HTTP endpoint first; fall back to direct Celery invocation if 404.
  local body
  body=$(jq -nc --arg org "${PARITY_ORG_ID}" '{"org_ids": [$org]}')

  local response
  if response=$(exec_private_curl POST "/_private/api/utils/kessel_parity_check/" \
    -H "Content-Type: application/json" -d "${body}" 2>/dev/null); then
    local task_id
    task_id=$(echo "${response}" | jq -r '.task_id // empty')
    if [[ -n "${task_id}" ]]; then
      state_save PARITY_TASK_ID "${task_id}"
      good "Parity check queued via API: task_id=${task_id}"
    else
      good "Parity check queued via API"
    fi
  else
    info "API endpoint not available -- queuing Celery task via service pod."
    local svc_pod
    svc_pod=$(get_pod_by_label "${RBAC_SERVICE_POD_LABEL}")
    if [[ -z "${svc_pod}" ]]; then
      err "No running service pod found"
      return 1
    fi
    local task_output
    task_output=$(oc exec "${svc_pod}" -- sh -c "
cd /opt/rbac/rbac && DJANGO_SETTINGS_MODULE=rbac.settings \
/opt/rbac/.venv/bin/python -c \"
import django; django.setup()
from rbac.celery import app
result = app.send_task('management.tasks.run_kessel_parity_checks_in_worker', kwargs={'org_ids': ['${PARITY_ORG_ID}']})
print(result.id)
\"" 2>&1)
    local task_id
    task_id=$(echo "${task_output}" | grep -v '^Defaulted' | tail -1)
    if [[ -n "${task_id}" ]]; then
      state_save PARITY_TASK_ID "${task_id}"
      good "Parity check queued via Celery: task_id=${task_id}"
    else
      warn "Task queued but could not capture task_id"
      echo "  Output: ${task_output}"
    fi
  fi

  echo ""
  info "Results will appear in the Celery worker logs (or direct output above)."
}

# ══════════════════════════════════════════════════════════════════════════════
# PHASE: RESULTS -- Tail worker logs and parse parity check output
# ══════════════════════════════════════════════════════════════════════════════

phase_results() {
  echo ""
  echo -e "${BOLD}=== Phase: RESULTS ===${NC}"
  echo ""

  local worker_pod
  worker_pod=$(get_pod_by_label "${RBAC_WORKER_POD_LABEL}")
  if [[ -z "${worker_pod}" ]]; then
    err "No running worker pod found (label: ${RBAC_WORKER_POD_LABEL})"
    echo ""
    echo "  Try manually: oc logs -l ${RBAC_WORKER_POD_LABEL} --tail=200 | grep -i parity"
    return 1
  fi

  info "Worker pod: ${worker_pod}"
  echo "  Searching worker logs (last ${PARITY_LOG_WAIT} lines) for parity results..."
  echo "  (Override with PARITY_LOG_WAIT=<lines>)"
  echo ""

  # Use --tail instead of --since to survive pod restarts.
  local logs
  logs=$(oc logs "${worker_pod}" --tail="${PARITY_LOG_WAIT}" 2>/dev/null || true)

  if [[ -z "${logs}" ]]; then
    warn "No logs found."
    echo "  The task may still be running. Try:"
    echo "    oc logs ${worker_pod} -f | grep -i parity"
    return 1
  fi

  # Extract parity-related lines.
  # The task logs individual lines like:
  #   "Starting Kessel parity checks for 1 org(s): ['12345']"
  #   "Parity check PASSED for tenant 12345"
  #   "workspace_check_passed: false"
  # And may also log the full result dict as JSON.
  local parity_lines
  parity_lines=$(echo "${logs}" | grep -iE "parity|PASSED|FAILED|workspace_check|custom_role_check|bootstrap_check|group_principal|seeded_role|Checking.*workspace|Checked.*role|Checked.*group" || true)

  if [[ -z "${parity_lines}" ]]; then
    warn "No parity check output found yet."
    echo ""
    echo "  The task may still be processing. Try increasing the wait:"
    echo "    PARITY_LOG_WAIT=1000 PARITY_STEP=results $0 --check"
    echo ""
    echo "  Or watch logs live:"
    echo "    oc logs ${worker_pod} -f | grep -i parity"
    if [[ -f "$(dirname "$0")/parse-dr-logs.sh" ]]; then
      echo "    ./scripts/ephemeral/parse-dr-logs.sh --pods rbac-worker"
    fi
    return 1
  fi

  echo -e "${BOLD}--- Parity Check Results ---${NC}"
  echo ""

  # Check for pass/fail.
  local passed=true
  if echo "${parity_lines}" | grep -qi "FAILED"; then
    passed=false
  fi

  # Display the parity log lines with highlighting.
  while IFS= read -r line; do
    if echo "${line}" | grep -qi "PASSED"; then
      echo -e "  ${GREEN}${line}${NC}"
    elif echo "${line}" | grep -qi "FAILED\|: false\|_passed.*false"; then
      echo -e "  ${RED}${line}${NC}"
    else
      echo "  ${line}"
    fi
  done <<< "${parity_lines}"

  echo ""

  # Try to extract the full JSON result from the logs (Celery task return value).
  local json_result
  json_result=$(echo "${logs}" | grep 'passed_tenants' | sed 's/.*\({.*}\)/\1/' | jq -R 'fromjson? // empty' 2>/dev/null | tail -1 || true)

  if [[ -n "${json_result}" ]]; then
    echo -e "${BOLD}--- Parsed Result Summary ---${NC}"
    echo ""
    echo "${json_result}" | jq '.' 2>/dev/null || echo "${json_result}"
    echo ""
  fi

  # Final verdict.
  if [[ "${PARITY_FAILURE}" == "true" ]]; then
    if [[ "${passed}" == "false" ]]; then
      echo -e "${GREEN}${BOLD}EXPECTED RESULT: Parity check correctly detected divergence.${NC}"
    else
      echo -e "${RED}${BOLD}UNEXPECTED: Parity check passed but divergence was introduced.${NC}"
      echo "  Either the break phase didn't work, or the check ran before break took effect."
    fi
  else
    if [[ "${passed}" == "true" ]]; then
      echo -e "${GREEN}${BOLD}RESULT: All parity checks PASSED -- RBAC and Kessel are in sync.${NC}"
    else
      echo -e "${RED}${BOLD}RESULT: Parity check FAILED -- divergence detected.${NC}"
      echo "  Review the log output above for details on which sub-check failed."
    fi
  fi
}

# ══════════════════════════════════════════════════════════════════════════════
# PHASE: CLEANUP -- Remove test data
# ══════════════════════════════════════════════════════════════════════════════

phase_cleanup() {
  echo ""
  echo -e "${BOLD}=== Phase: CLEANUP ===${NC}"
  echo ""

  state_load

  local svc_pod
  svc_pod=$(get_pod_by_label "${RBAC_SERVICE_POD_LABEL}")
  if [[ -z "${svc_pod}" ]]; then
    err "No running RBAC service pod found"
    return 1
  fi

  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  oc cp "${script_dir}/parity_data_setup.py" "${svc_pod}:/tmp/parity_data_setup.py" 2>/dev/null || true

  local cleanup_cmd="cd /opt/rbac/rbac && DJANGO_SETTINGS_MODULE=rbac.settings /opt/rbac/.venv/bin/python /tmp/parity_data_setup.py cleanup --org-id=${PARITY_ORG_ID:-}"

  local ws_ids="${PARITY_WORKSPACE_IDS:-}"
  local role_uuid="${PARITY_ROLE_UUID:-}"
  local group_ids="${PARITY_GROUP_IDS:-}"

  [[ -n "${ws_ids}" ]] && cleanup_cmd="${cleanup_cmd} --workspace-ids=${ws_ids}"
  [[ -n "${role_uuid}" ]] && cleanup_cmd="${cleanup_cmd} --role-uuid=${role_uuid}"
  [[ -n "${group_ids}" ]] && cleanup_cmd="${cleanup_cmd} --group-ids=${group_ids}"

  if [[ -z "${ws_ids}" && -z "${role_uuid}" && -z "${group_ids}" ]]; then
    info "No resources to clean up (state file empty or missing)."
    rm -f "${PARITY_STATE_FILE}"
    return 0
  fi

  info "Running ORM-based cleanup on pod..."
  local raw_output
  raw_output=$(oc exec "${svc_pod}" -- sh -c "${cleanup_cmd}" 2>&1)

  local cleanup_output
  cleanup_output=$(echo "${raw_output}" | grep -v '^Defaulted' | tail -1)

  local cleaned
  cleaned=$(echo "${cleanup_output}" | jq -r '.cleaned // 0' 2>/dev/null)

  rm -f "${PARITY_STATE_FILE}"

  echo ""
  good "Cleanup complete. Removed ${cleaned} resource(s)."
}

# ══════════════════════════════════════════════════════════════════════════════
# ORCHESTRATOR -- Run phases in sequence
# ══════════════════════════════════════════════════════════════════════════════

run_parity_check() {
  local step="${PARITY_STEP}"

  case "${step}" in
    setup)     phase_setup ;;
    wait)      phase_wait ;;
    break)     phase_break ;;
    check)     phase_check ;;
    results)   phase_results ;;
    cleanup)   phase_cleanup ;;
    state)     state_show ;;
    all)
      phase_setup || return 1

      phase_wait || return 1

      if [[ "${PARITY_FAILURE}" == "true" ]]; then
        phase_break || return 1
      fi

      phase_check || return 1

      echo ""
      info "Waiting 15s for the Celery task to complete..."
      sleep 15

      phase_results || true

      if [[ "${PARITY_FAST}" != "true" ]]; then
        echo ""
        read -r -p "Run cleanup to remove test data? [Y/n] " confirm
        if [[ "${confirm}" != "n" && "${confirm}" != "N" ]]; then
          phase_cleanup
        else
          info "Skipping cleanup. State saved at ${PARITY_STATE_FILE}"
          info "Run cleanup later: PARITY_STEP=cleanup $0 --check"
        fi
      else
        phase_cleanup
      fi
      ;;
    *)
      err "Unknown step: ${step}"
      err "Valid: setup|wait|break|check|results|cleanup|state|all"
      return 1
      ;;
  esac
}

# ══════════════════════════════════════════════════════════════════════════════
# UTILITY COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

watch_worker() {
  local pod
  pod=$(get_pod_by_label "${RBAC_WORKER_POD_LABEL}")
  if [[ -z "${pod}" ]]; then
    err "No running worker pod found"
    return 1
  fi
  info "Following logs: ${pod}  (Ctrl+C to stop)"
  oc logs "${pod}" -f
}

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

usage() {
  cat <<EOF
RBAC Parity Check Toolkit
Usage: $0 <command> [flags]

Commands:
  --check               Run parity check (happy path by default)
  --check --failure     Run parity check with injected divergence
  --state               Show saved state
  --watch-worker        Follow Celery worker logs (Ctrl+C to stop)
  --help                Show this message

Flags:
  --fast                Skip sync wait, auto-confirm cleanup
  --minimal-data        Use minimal test data (1 of each resource)

Step-by-step execution (set PARITY_STEP env var):
  PARITY_STEP=setup     $0 --check      # Create test data
  PARITY_STEP=wait      $0 --check      # Wait for replication
  PARITY_STEP=break     $0 --check      # Introduce divergence (failure path)
  PARITY_STEP=check     $0 --check      # Call parity check API
  PARITY_STEP=results   $0 --check      # Parse worker logs
  PARITY_STEP=cleanup   $0 --check      # Remove test data

Environment variables:
  PARITY_ORG_ID         Org ID to check (auto-resolved if unset)
  PARITY_STEP           Phase to run: setup|wait|break|check|results|cleanup|state|all
  PARITY_SYNC_WAIT      Seconds to wait for replication (default: 30)
  PARITY_LOG_WAIT       Lines of logs to search for results (default: 500)
  PARITY_STATE_FILE     State file path (default: /tmp/rbac-parity-state.env)
  PARITY_FAST           Skip waits and auto-confirm (default: false)
  PARITY_MINIMAL        Minimal test data (default: false)
EOF
}

main() {
  check_deps

  local _args=()
  for _a in "$@"; do
    case "${_a}" in
      --fast)          PARITY_FAST=true ;;
      --minimal-data)  PARITY_MINIMAL=true ;;
      --failure)       PARITY_FAILURE=true ;;
      *)               _args+=("${_a}") ;;
    esac
  done
  set -- "${_args[@]+"${_args[@]}"}"

  local cmd="${1:---help}"

  if [[ "$cmd" != "--help" && "$cmd" != "-h" ]]; then
    check_bonfire_env
    info "Namespace: ${EPHEMERAL_NAMESPACE}"
    info "Host:      ${EPHEMERAL_HOST_NAME}"
    [[ "${PARITY_FAST}" == "true" ]] && info "Fast mode: ON"
    [[ "${PARITY_MINIMAL}" == "true" ]] && info "Minimal data: ON"
    [[ "${PARITY_FAILURE}" == "true" ]] && info "Failure mode: ON"
    echo ""
  fi

  case "$cmd" in
    --check)          run_parity_check ;;
    --state)          state_show ;;
    --watch-worker)   watch_worker ;;
    --help|-h)        usage ;;
    *)                err "Unknown command: $cmd"; echo ""; usage; exit 1 ;;
  esac
}

main "$@"
