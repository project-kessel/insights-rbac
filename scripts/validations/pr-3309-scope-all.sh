#!/usr/bin/env bash
# Validate PR #3309 Scope.ALL behavior against the local Podman full stack.
#
# Usage:
#   scripts/validations/pr-3309-scope-all.sh
#
# The local Podman stack must already be running. The requests under test are
# V2 requests only. The local compose configuration intentionally replaces
# every identity header with its fixed developer identity (org_id=11111,
# user_id=51736777). The local Kessel schema grants RBAC role permissions to
# its development-principal wildcard, so the script adds a uniquely named,
# short-lived Kessel role and binds it to that developer principal. Every added
# Kessel tuple is removed when the script finishes.
#
# What PR #3309 changes:
#   Scope.ALL is a permission scope that has no physical resource of its own.
#   ALL-scoped permissions must therefore not create an `all` resource or
#   binding. They may coexist with one concrete scope, and ALL-only roles fall
#   back to the default workspace when a binding resource is required.
#
# Scenario inputs and expected outputs:
#   1. Create an isolated V2 workspace in the local developer tenant.
#   2. Obtain that tenant's default workspace.
#   3. Create an ALL + TENANT role: it must remain tenant-scoped.
#   4. Create an ALL-only role: it must fall back to default-workspace scope.
#   5. Attempt ROOT + TENANT + ALL: it must be rejected as a concrete conflict.
#   6. Confirm the V2 role filters classify the two accepted roles correctly.
#   7. Bind the ALL-only role and confirm the V2 response never exposes `all`
#      as a real bindable resource.
set -euo pipefail

API_URL="${API_URL:-http://localhost:9080}"
API_PREFIX="${API_PATH_PREFIX:-/api/rbac}"
RUN_ID="$(date +%s)"
ORG_ID="11111"
ACCOUNT_NUMBER="10001"
USER_ID="51736777"
USERNAME="user_dev"
NAME_SUFFIX="${ORG_ID}-${RUN_ID}"
WORKSPACE_NAME="pr3309-scope-all-workspace-${NAME_SUFFIX}"
ALL_PLUS_TENANT_ROLE="pr3309-all-plus-tenant-${NAME_SUFFIX}"
ALL_ONLY_ROLE="pr3309-all-only-${NAME_SUFFIX}"
CONCRETE_CONFLICT_ROLE="pr3309-concrete-conflict-${NAME_SUFFIX}"
LOCAL_KESSEL_ACCESS_ROLE="pr3309-validator-access-${NAME_SUFFIX}"
LOCAL_KESSEL_ACCESS_BINDING="pr3309-validator-access-binding-${NAME_SUFFIX}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SPICEDB_ENV_FILE="${SPICEDB_ENV_FILE:-${REPO_ROOT}/.local-deps/inventory-api/development/full-kessel/.env}"
SPICEDB_ENDPOINT="${SPICEDB_ENDPOINT:-localhost:50051}"
KESSEL_PRINCIPAL_DOMAIN="${KESSEL_PRINCIPAL_DOMAIN:-redhat}"
CONTAINER_RUNTIME="${CONTAINER_RUNTIME:-podman}"
RBAC_SERVER_CONTAINER="${RBAC_SERVER_CONTAINER:-full-kessel-rbac-server-1}"
IDENTITY_HEADER=""
BODY_FILE=""
SPICEDB_TOKEN="${SPICEDB_TOKEN:-}"
CREATED_RELATIONSHIPS=()
V2_WRITE_ENABLED_BY_VALIDATOR=false
V2_OPT_IN_ENABLED_BY_VALIDATOR=false

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

scenario() {
  local number="$1"
  local title="$2"
  local input="$3"
  local expected="$4"
  local reason="$5"

  printf '\nSCENARIO %s: %s\n' "$number" "$title"
  printf '  Input:    %s\n' "$input"
  printf '  Expected: %s\n' "$expected"
  printf '  Why:      %s\n' "$reason"
}

usage() {
  cat <<'EOF'
Usage: pr-3309-scope-all.sh

The local Podman stack must already be running.

Run with no setup:
  scripts/validations/pr-3309-scope-all.sh

The local compose configuration replaces every identity header with its fixed
developer identity: org_id=11111, user_id=51736777. The script creates uniquely
named test resources in that tenant. Its local Kessel schema permits RBAC test
grants only to the development-principal wildcard, so it creates one uniquely
named, short-lived authorization role and binding, then deletes every related
tuple on exit.
This scaffolding is necessary because V2 role writes require Kessel
authorization. It is not part of the feature being tested. All API requests
asserted by scenarios 1-7 use /v2 endpoints.

The temporary Kessel relationships are deleted on exit. The isolated V2
workspace, roles, and binding are intentionally retained so their data remains
available for local inspection.

Environment:
  API_URL     RBAC API URL (default: http://localhost:9080).
  SPICEDB_ENV_FILE
              File holding SPICEDB_GRPC_PRESHARED_KEY for the local stack.
  RBAC_SERVER_CONTAINER
              Local RBAC server container (default: full-kessel-rbac-server-1).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "This validator takes no deployment argument; start the stack first."
      ;;
  esac
done

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

touch_relationship() {
  local resource="$1"
  local relation="$2"
  local subject="$3"

  ZED_TOKEN="$SPICEDB_TOKEN" ZED_ENDPOINT="$SPICEDB_ENDPOINT" ZED_INSECURE=true \
    zed relationship touch "$resource" "$relation" "$subject" >/dev/null
  CREATED_RELATIONSHIPS+=("${resource}|${relation}|${subject}")
}

load_spicedb_token() {
  if [[ -z "$SPICEDB_TOKEN" ]]; then
    [[ -f "$SPICEDB_ENV_FILE" ]] || die "SpiceDB token file not found: ${SPICEDB_ENV_FILE}"
    SPICEDB_TOKEN=$(awk -F= '$1 == "SPICEDB_GRPC_PRESHARED_KEY" {print substr($0, index($0, "=") + 1); exit}' "$SPICEDB_ENV_FILE")
  fi
  [[ -n "$SPICEDB_TOKEN" ]] || die "SPICEDB_GRPC_PRESHARED_KEY is missing from ${SPICEDB_ENV_FILE}"
}

require_local_principal() {
  "$CONTAINER_RUNTIME" exec \
    -e VALIDATION_ORG_ID="$ORG_ID" \
    -e VALIDATION_USER_ID="$USER_ID" \
    "$RBAC_SERVER_CONTAINER" \
    python /opt/rbac/rbac/manage.py shell -c '
import os
import sys
from api.models import Tenant
from management.principal.model import Principal

tenant = Tenant.objects.get(org_id=os.environ["VALIDATION_ORG_ID"])
if not Principal.objects.filter(tenant=tenant, user_id=os.environ["VALIDATION_USER_ID"]).exists():
    sys.exit("The local developer principal is missing; restart the local stack.")
' >/dev/null
}

local_mapping_field_is_set() {
  local field_name="$1"

  "$CONTAINER_RUNTIME" exec \
    -e VALIDATION_ORG_ID="$ORG_ID" \
    -e VALIDATION_FIELD_NAME="$field_name" \
    "$RBAC_SERVER_CONTAINER" \
    python /opt/rbac/rbac/manage.py shell -c '
import os
import sys
from api.models import Tenant
from management.tenant_mapping.model import TenantMapping

tenant = Tenant.objects.get(org_id=os.environ["VALIDATION_ORG_ID"])
mapping = TenantMapping.objects.get(tenant=tenant)
sys.exit(0 if getattr(mapping, os.environ["VALIDATION_FIELD_NAME"]) is not None else 1)
' >/dev/null 2>&1
}

enable_local_v2_writes() {
  if local_mapping_field_is_set v2_write_activated_at; then
    return
  fi

  if ! local_mapping_field_is_set v2_opted_in_at; then
    V2_OPT_IN_ENABLED_BY_VALIDATOR=true
  fi

  "$CONTAINER_RUNTIME" exec \
    -e VALIDATION_ORG_ID="$ORG_ID" \
    "$RBAC_SERVER_CONTAINER" \
    python /opt/rbac/rbac/manage.py shell -c '
import os
from django.utils import timezone
from api.models import Tenant
from management.tenant_mapping.model import TenantMapping

tenant = Tenant.objects.get(org_id=os.environ["VALIDATION_ORG_ID"])
mapping = TenantMapping.objects.get(tenant=tenant)
now = timezone.now()
fields = []
if mapping.v2_opted_in_at is None:
    mapping.v2_opted_in_at = now
    fields.append("v2_opted_in_at")
mapping.v2_write_activated_at = now
fields.append("v2_write_activated_at")
mapping.save(update_fields=fields)
' >/dev/null
  V2_WRITE_ENABLED_BY_VALIDATOR=true
}

restore_local_v2_writes() {
  [[ "$V2_WRITE_ENABLED_BY_VALIDATOR" == true ]] || return

  "$CONTAINER_RUNTIME" exec \
    -e VALIDATION_ORG_ID="$ORG_ID" \
    -e VALIDATION_RESTORE_OPT_IN="$V2_OPT_IN_ENABLED_BY_VALIDATOR" \
    "$RBAC_SERVER_CONTAINER" \
    python /opt/rbac/rbac/manage.py shell -c '
import os
from api.models import Tenant
from management.tenant_mapping.model import TenantMapping

tenant = Tenant.objects.get(org_id=os.environ["VALIDATION_ORG_ID"])
mapping = TenantMapping.objects.get(tenant=tenant)
mapping.v2_write_activated_at = None
fields = ["v2_write_activated_at"]
if os.environ["VALIDATION_RESTORE_OPT_IN"] == "true":
    mapping.v2_opted_in_at = None
    fields.append("v2_opted_in_at")
mapping.save(update_fields=fields)
' >/dev/null 2>&1 || true
}

provision_local_authorization() {
  local tenant_resource="rbac/tenant:${KESSEL_PRINCIPAL_DOMAIN}/${ORG_ID}"
  local principal_resource="rbac/principal:${KESSEL_PRINCIPAL_DOMAIN}/${USER_ID}"
  local all_principals_resource="rbac/principal:*"
  local access_role_resource="rbac/role:${LOCAL_KESSEL_ACCESS_ROLE}"
  local access_binding_resource="rbac/role_binding:${LOCAL_KESSEL_ACCESS_BINDING}"
  local workspace_resource="rbac/workspace:${default_workspace_id}"

  printf '\nLOCAL TEST HARNESS: authorizing the local V2 developer caller\n'
  printf '  Grants the local development-principal wildcard minimum temporary Kessel access.\n'
  printf '  Temporarily enables V2 writes and adds minimum Kessel access; both are restored on exit.\n'

  require_local_principal
  enable_local_v2_writes
  touch_relationship "$access_role_resource" t_rbac_roles_read "$all_principals_resource"
  touch_relationship "$access_role_resource" t_rbac_roles_write "$all_principals_resource"
  touch_relationship "$access_role_resource" t_rbac_role_binding_grant "$all_principals_resource"
  touch_relationship "$access_role_resource" t_rbac_role_binding_view "$all_principals_resource"
  touch_relationship "$access_binding_resource" t_role "$access_role_resource"
  touch_relationship "$access_binding_resource" t_subject "$principal_resource"
  touch_relationship "$tenant_resource" t_binding "$access_binding_resource"
  touch_relationship "$workspace_resource" t_binding "$access_binding_resource"
}

cleanup() {
  local relationship resource relation subject
  if [[ -n "${CREATED_RELATIONSHIPS[*]:-}" ]]; then
    for relationship in "${CREATED_RELATIONSHIPS[@]}"; do
      IFS='|' read -r resource relation subject <<< "$relationship"
      ZED_TOKEN="$SPICEDB_TOKEN" ZED_ENDPOINT="$SPICEDB_ENDPOINT" ZED_INSECURE=true \
        zed relationship delete "$resource" "$relation" "$subject" >/dev/null 2>&1 || true
    done
  fi
  restore_local_v2_writes
  [[ -z "$BODY_FILE" ]] || rm -f "$BODY_FILE"
}
trap cleanup EXIT

make_identity_header() {
  local identity
  identity=$(printf '%s' "{\"identity\":{\"account_number\":\"${ACCOUNT_NUMBER}\",\"org_id\":\"${ORG_ID}\",\"type\":\"User\",\"user\":{\"username\":\"${USERNAME}\",\"email\":\"${USERNAME}@example.com\",\"is_org_admin\":true,\"user_id\":\"${USER_ID}\"}}}" | base64 | tr -d '\n')
  IDENTITY_HEADER="$identity"
}

api_call() {
  local expected="$1"
  local method="$2"
  local path="$3"
  local payload="${4:-}"
  local status

  [[ -z "$BODY_FILE" ]] || rm -f "$BODY_FILE"
  BODY_FILE="$(mktemp "${TMPDIR:-/tmp}/pr-3309-response.XXXXXX")"
  if [[ -n "$payload" ]]; then
    status=$(curl -sS -o "$BODY_FILE" -w '%{http_code}' -X "$method" \
      -H "Content-Type: application/json" -H "x-rh-identity: $IDENTITY_HEADER" \
      --data "$payload" "${API_URL}${API_PREFIX}${path}")
  else
    status=$(curl -sS -o "$BODY_FILE" -w '%{http_code}' -X "$method" \
      -H "x-rh-identity: $IDENTITY_HEADER" "${API_URL}${API_PREFIX}${path}")
  fi

  if [[ "$status" != "$expected" ]]; then
    printf 'FAIL %s %s: expected HTTP %s, got %s\n' "$method" "$path" "$expected" "$status" >&2
    cat "$BODY_FILE" >&2
    return 1
  fi
  printf 'PASS %s %s -> HTTP %s\n' "$method" "$path" "$status"
}

json_value() {
  jq -er "$1" "$BODY_FILE"
}

assert_role_filter() {
  local role_name="$1"
  local resource_type="$2"
  local expected_count="$3"
  local count

  api_call 200 GET "/v2/roles/?name=${role_name}&resource_type=${resource_type}"
  count=$(jq --arg name "$role_name" '[.data[]? | select(.name == $name)] | length' "$BODY_FILE")
  [[ "$count" == "$expected_count" ]] || die "Expected ${role_name} count ${expected_count} for ${resource_type}, got ${count}."
  if [[ "$expected_count" == 0 ]]; then
    printf 'PASS role %s is not returned for resource_type=%s\n' "$role_name" "$resource_type"
  else
    printf 'PASS role %s is returned for resource_type=%s\n' "$role_name" "$resource_type"
  fi
}

main() {
  require_cmd awk
  require_cmd base64
  require_cmd curl
  require_cmd jq
  require_cmd zed
  require_cmd "$CONTAINER_RUNTIME"
  load_spicedb_token
  make_identity_header

  printf 'PR #3309 V2 Scope.ALL validation\n'
  printf 'Waiting for RBAC API at %s...\n' "${API_URL}${API_PREFIX}/v2/workspaces/"
  for _ in $(seq 1 90); do
    if curl -sS -o /dev/null "${API_URL}/metrics"; then
      break
    fi
    sleep 2
  done
  curl -sS -o /dev/null "${API_URL}/metrics" || die "RBAC API did not become ready."

  scenario 1 \
    'Create an isolated V2 workspace' \
    "POST /v2/workspaces/ {name: ${WORKSPACE_NAME}}" \
    'HTTP 201; RBAC creates one standard workspace under the local default workspace' \
    'Confirms V2 workspace mutation works and keeps this run identifiable in the shared local tenant.'
  api_call 201 POST "/v2/workspaces/" "{\"name\":\"${WORKSPACE_NAME}\"}"

  scenario 2 \
    'Find the default V2 workspace' \
    'GET /v2/workspaces/?type=default' \
    'HTTP 200 with a workspace id' \
    'The id is the concrete workspace used when an ALL-only role is bound.'
  api_call 200 GET "/v2/workspaces/?type=default"
  default_workspace_id=$(json_value '.data[0].id')
  provision_local_authorization

  scenario 3 \
    'Create ALL + TENANT role' \
    'POST /v2/roles/ with rbac:principal:read + rbac:role_binding:view' \
    'HTTP 201; role is returned only for resource_type=tenant' \
    'ALL must coexist with one concrete TENANT scope without becoming a separate resource.'
  all_plus_tenant="{\"name\":\"${ALL_PLUS_TENANT_ROLE}\",\"description\":\"PR 3309 validation\",\"permissions\":[{\"application\":\"rbac\",\"resource_type\":\"principal\",\"operation\":\"read\"},{\"application\":\"rbac\",\"resource_type\":\"role_binding\",\"operation\":\"view\"}]}"
  api_call 201 POST "/v2/roles/" "$all_plus_tenant"
  all_plus_tenant_id=$(json_value '.id')

  scenario 4 \
    'Create ALL-only role' \
    'POST /v2/roles/ with rbac:role_binding:view' \
    'HTTP 201; role is returned only for resource_type=workspace' \
    'Scope.ALL has no physical resource, so the role must fall back to DEFAULT for binding.'
  all_only="{\"name\":\"${ALL_ONLY_ROLE}\",\"description\":\"PR 3309 validation\",\"permissions\":[{\"application\":\"rbac\",\"resource_type\":\"role_binding\",\"operation\":\"view\"}]}"
  api_call 201 POST "/v2/roles/" "$all_only"
  all_only_id=$(json_value '.id')

  scenario 5 \
    'Reject two concrete scopes even when ALL is present' \
    'POST /v2/roles/ with advisor:*:read + rbac:principal:read + rbac:role_binding:view' \
    'HTTP 400' \
    'ALL must not mask the ROOT-versus-TENANT conflict.'
  concrete_conflict="{\"name\":\"${CONCRETE_CONFLICT_ROLE}\",\"description\":\"PR 3309 validation\",\"permissions\":[{\"application\":\"advisor\",\"resource_type\":\"*\",\"operation\":\"read\"},{\"application\":\"rbac\",\"resource_type\":\"principal\",\"operation\":\"read\"},{\"application\":\"rbac\",\"resource_type\":\"role_binding\",\"operation\":\"view\"}]}"
  api_call 400 POST "/v2/roles/" "$concrete_conflict"

  scenario 6 \
    'Verify V2 role resource classification' \
    "GET /v2/roles/?name=...&resource_type=tenant/workspace" \
    'ALL + TENANT => tenant; ALL-only => workspace' \
    'Confirms Scope.ALL is used for classification but never exposed as a bindable resource.'
  assert_role_filter "$ALL_PLUS_TENANT_ROLE" tenant 1
  assert_role_filter "$ALL_PLUS_TENANT_ROLE" workspace 0
  assert_role_filter "$ALL_ONLY_ROLE" workspace 1
  assert_role_filter "$ALL_ONLY_ROLE" tenant 0

  scenario 7 \
    'Bind the ALL-only role to a V2 user' \
    'POST /v2/role-bindings:batchCreate/ on the default workspace' \
    'HTTP 201; GET returns resource.type=workspace and never all' \
    'Verifies the final resource mapping used by V2 role bindings.'
  api_call 200 GET "/v2/principals/?username=${USERNAME}"
  principal_id=$(jq -er --arg user_id "$USER_ID" '[.data[]? | select(.user_id == $user_id)][0].uuid' "$BODY_FILE") \
    || die "V2 principal ${USER_ID} was not found."

  binding=$(jq -cn \
    --arg workspace_id "$default_workspace_id" \
    --arg principal_id "$principal_id" \
    --arg role_id "$all_only_id" \
    '{requests: [{resource: {id: $workspace_id, type: "workspace"}, subject: {id: $principal_id, type: "user"}, role: {id: $role_id}}]}')
  api_call 201 POST "/v2/role-bindings:batchCreate/" "$binding"
  api_call 200 GET "/v2/role-bindings/?role_id=${all_only_id}&resource_id=${default_workspace_id}&resource_type=workspace&fields=resource(id,type),role(id)"
  jq -e --arg role "$all_only_id" '[.data[]? | select(.role.id == $role and .resource.type == "workspace")] | length > 0' "$BODY_FILE" >/dev/null \
    || die "Created role binding was not returned for the default workspace."
  jq -e '[.data[]?.resource.type] | all(. != "all")' "$BODY_FILE" >/dev/null \
    || die "An invalid Scope.ALL bindable resource was returned."

  printf '\nPR #3309 local validation passed.\n'
}

main "$@"
