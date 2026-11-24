#!/usr/bin/env bash
# Basic V2 API lifecycle validation for the local Docker/Podman full stack.
#
# Usage:
#   scripts/validations/api/v2-crud.sh
#
# The local full stack must already be running. This script loads a unique
# temporary principal through the declarative local users fixture, creates
# uniquely named test resources in org_id=11111, exercises every V2 API route,
# and removes the principal and resources it creates.
#
# V2 route coverage:
#   workspaces: GET, POST, query POST, detail GET/PATCH/PUT/DELETE, move POST
#   roles:      GET/POST, detail GET/PUT, batchDelete POST
#   bindings:   GET, batchCreate POST, by-subject GET/PUT
#   principals: GET collection and detail (the endpoint is read-only)
#
# The local stack protects role and role-binding writes through Kessel and
# disables V2 writes by default. The script temporarily enables the local V2
# tenant mapping and adds a short-lived Kessel authorization graph. These
# changes are restored when it exits, including after a failed assertion.
#
# Read-only scenarios print the existing direct tuples for their resource.
# Each mutation waits for and prints the exact tuple it must create, update,
# or remove; a passing API status alone is not sufficient.
set -euo pipefail

API_URL="${API_URL:-http://localhost:9080}"
API_PREFIX="${API_PATH_PREFIX:-/api/rbac}"
RUN_ID="$(date +%s)-$$"
ORG_ID="11111"
ACCOUNT_NUMBER="10001"
USER_ID="v2-crud-user-${RUN_ID}"
USERNAME="v2-crud-user-${RUN_ID}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
SPICEDB_ENV_FILE="${SPICEDB_ENV_FILE:-${FULL_KESSEL_COMPOSE_DIR:-${REPO_ROOT}/.local-deps/inventory-api/development/full-kessel}/.env}"
SPICEDB_ENDPOINT="${SPICEDB_ENDPOINT:-localhost:50051}"
SPICEDB_CONTAINER_ENDPOINT="${SPICEDB_CONTAINER_ENDPOINT:-spicedb:50051}"
KESSEL_PRINCIPAL_DOMAIN="${KESSEL_PRINCIPAL_DOMAIN:-redhat}"
RBAC_SERVER_CONTAINER="${RBAC_SERVER_CONTAINER:-full-kessel-rbac-server-1}"
if [[ -n "${CONTAINER_RUNTIME:-}" ]]; then
  : # explicit override — keep it
elif docker container inspect "$RBAC_SERVER_CONTAINER" &>/dev/null 2>&1; then
  CONTAINER_RUNTIME="docker"
elif podman container inspect "$RBAC_SERVER_CONTAINER" &>/dev/null 2>&1; then
  CONTAINER_RUNTIME="podman"
elif command -v docker &>/dev/null; then
  CONTAINER_RUNTIME="docker"
elif command -v podman &>/dev/null; then
  CONTAINER_RUNTIME="podman"
else
  die "Neither docker nor podman found. Install a container runtime."
fi
COLOR="${COLOR:-auto}"

if [[ -z "${NO_COLOR+x}" && ( "$COLOR" == always || ( "$COLOR" == auto && -t 1 ) ) ]]; then
  RESET=$'\033[0m'
  BOLD=$'\033[1m'
  DIM=$'\033[2m'
  CYAN=$'\033[36m'
  GREEN=$'\033[32m'
  YELLOW=$'\033[33m'
  RED=$'\033[31m'
else
  RESET=""
  BOLD=""
  DIM=""
  CYAN=""
  GREEN=""
  YELLOW=""
  RED=""
fi

NAME_SUFFIX="v2-crud-${RUN_ID}"
PARENT_WORKSPACE_NAME="validation-parent-${NAME_SUFFIX}"
CHILD_WORKSPACE_NAME="validation-child-${NAME_SUFFIX}"
UPDATED_WORKSPACE_NAME="validation-child-updated-${NAME_SUFFIX}"
ROLE_NAME="validation-role-${NAME_SUFFIX}"
UPDATED_ROLE_NAME="validation-role-updated-${NAME_SUFFIX}"
LOCAL_KESSEL_ACCESS_ROLE="validation-access-role-${NAME_SUFFIX}"
LOCAL_KESSEL_ACCESS_BINDING="validation-access-binding-${NAME_SUFFIX}"
LOCAL_CONFIG_ACCESS_ROLE="validation-fixture-access-role-${NAME_SUFFIX}"

IDENTITY_HEADER=""
BODY_FILE=""
SPICEDB_TOKEN="${SPICEDB_TOKEN:-}"
CREATED_RELATIONSHIPS=()
V2_WRITE_ENABLED_BY_VALIDATOR=false
V2_OPT_IN_ENABLED_BY_VALIDATOR=false
DEFAULT_WORKSPACE_ID=""
PARENT_WORKSPACE_ID=""
CHILD_WORKSPACE_ID=""
ROLE_ID=""
PRINCIPAL_ID=""
BINDING_ID=""
CLEANUP_FAILURES=0
VALIDATION_STATUS=0
TEMP_FIXTURE=""
TEMP_FIXTURE_APPLIED=false
TEMP_FIXTURE_ACCESS_ROLE_ID=""
TEMP_FIXTURE_ACCESS_BINDING_ID=""

die() {
  printf '%s✘ ERROR%s %s\n' "$RED" "$RESET" "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: v2-crud.sh

Run this after `make docker-local-full-up rbac=local rbac-config=upstream` or
`make docker-local-full-up rbac=<github-pr-url>`.

No existing user setup is required. The script creates a temporary user in
org_id=11111 through actions/apply-rbac-users-config.sh, temporarily enables
V2 writes, and removes the user, Kessel permissions, and test API resources
before it exits.

Environment:
  API_URL                   RBAC API URL (default: http://localhost:9080)
  API_PATH_PREFIX           Path prefix for RBAC API routes (default: /api/rbac)
  SPICEDB_ENV_FILE          Local Kessel .env file containing its PSK
                            (default: <repo>/.local-deps/inventory-api/development/full-kessel/.env)
  SPICEDB_ENDPOINT             Host SpiceDB gRPC endpoint (default: localhost:50051)
  SPICEDB_CONTAINER_ENDPOINT   SpiceDB endpoint from the Kessel network (default: spicedb:50051)
  SPICEDB_TOKEN             SpiceDB pre-shared key; overrides SPICEDB_ENV_FILE when set
  ZED_IMAGE                 Container image used when host zed is unavailable
                            (default: docker.io/authzed/zed:latest)
  KESSEL_NETWORK_NAME       Docker/Podman network for containerized zed (default: kessel)
  KESSEL_PRINCIPAL_DOMAIN   Domain prefix for Kessel principals (default: redhat)
  RBAC_SERVER_CONTAINER     RBAC server container (default: full-kessel-rbac-server-1)
  CONTAINER_RUNTIME         docker or podman (auto-detected from RBAC_SERVER_CONTAINER)
  COLOR                     auto (default), always, or never; NO_COLOR also disables color
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "This validator takes no arguments."
      ;;
  esac
done

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

scenario() {
  local number="$1"
  local title="$2"
  local request="$3"
  local expected="$4"

  printf '\n%s━━ SCENARIO %s ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n' "$CYAN$BOLD" "$number" "$RESET"
  printf '%s%s%s\n' "$BOLD" "$title" "$RESET"
  printf '  %sRequest%s   %s\n' "$DIM" "$RESET" "$request"
  printf '  %sExpected%s  %s\n' "$DIM" "$RESET" "$expected"
}

print_tuples() {
  local output="$1"
  local tuple

  while IFS= read -r tuple; do
    [[ -z "$tuple" ]] || printf '    %s%s%s\n' "$DIM" "$tuple" "$RESET"
  done <<< "$output"
}

make_identity_header() {
  IDENTITY_HEADER=$(printf '%s' "{\"identity\":{\"account_number\":\"${ACCOUNT_NUMBER}\",\"org_id\":\"${ORG_ID}\",\"type\":\"User\",\"user\":{\"username\":\"${USERNAME}\",\"email\":\"${USERNAME}@example.com\",\"is_org_admin\":true,\"user_id\":\"${USER_ID}\"}}}" | base64 | tr -d '\n')
}

load_temporary_user_fixture() {
  TEMP_FIXTURE="$(mktemp "${TMPDIR:-/tmp}/v2-crud-users.XXXXXX").yaml"
  {
    printf 'version: 1\n'
    printf 'tenants:\n'
    printf '  - org_id: %s\n' "$ORG_ID"
    printf '    account_id: "%s"\n' "$ACCOUNT_NUMBER"
    printf '    bootstrap: true\n'
    printf '    temporary: true\n'
    printf '    users:\n'
    printf '      - username: %s\n' "$USERNAME"
    printf '        user_id: %s\n' "$USER_ID"
    printf '        admin: true\n'
    printf '    roles:\n'
    printf '      - name: %s\n' "$LOCAL_CONFIG_ACCESS_ROLE"
    printf '        description: temporary CRUD validator access\n'
    printf '        permissions:\n'
    printf '          - application: rbac\n'
    printf '            resource_type: "*"\n'
    printf '            operation: "*"\n'
    printf '    role_bindings:\n'
    printf '      - role: %s\n' "$LOCAL_CONFIG_ACCESS_ROLE"
    printf '        resource:\n'
    printf '          type: tenant\n'
    printf '          id: current\n'
    printf '        subjects:\n'
    printf '          - type: user\n'
    printf '            name: %s\n' "$USERNAME"
  } > "$TEMP_FIXTURE"

  TEMP_FIXTURE_APPLIED=true
  "$SCRIPT_DIR/actions/apply-rbac-users-config.sh" --file "$TEMP_FIXTURE" \
    || die "Could not create the temporary V2 CRUD user."

  TEMP_FIXTURE_ACCESS_ROLE_ID=$(
    "$CONTAINER_RUNTIME" exec \
      -e VALIDATION_ORG_ID="$ORG_ID" \
      -e VALIDATION_ROLE_NAME="$LOCAL_CONFIG_ACCESS_ROLE" \
      "$RBAC_SERVER_CONTAINER" \
      python /opt/rbac/rbac/manage.py shell -c '
import os
from api.models import Tenant
from management.role.v2_model import RoleV2

tenant = Tenant.objects.get(org_id=os.environ["VALIDATION_ORG_ID"])
print(RoleV2.objects.get(tenant=tenant, name=os.environ["VALIDATION_ROLE_NAME"]).uuid)
' | tail -1
  )
  TEMP_FIXTURE_ACCESS_BINDING_ID=$(
    "$CONTAINER_RUNTIME" exec \
      -e VALIDATION_ORG_ID="$ORG_ID" \
      -e VALIDATION_ROLE_ID="$TEMP_FIXTURE_ACCESS_ROLE_ID" \
      -e VALIDATION_USER_ID="$USER_ID" \
      "$RBAC_SERVER_CONTAINER" \
      python /opt/rbac/rbac/manage.py shell -c '
import os
from api.models import Tenant
from management.role_binding.model import RoleBinding

tenant = Tenant.objects.get(org_id=os.environ["VALIDATION_ORG_ID"])
binding = RoleBinding.objects.get(
    tenant=tenant,
    role__uuid=os.environ["VALIDATION_ROLE_ID"],
    principal_entries__principal__user_id=os.environ["VALIDATION_USER_ID"],
)
print(binding.uuid)
' | tail -1
  )

  wait_for_relationship 'temporary fixture role is owned by the tenant' \
    "rbac/role:${TEMP_FIXTURE_ACCESS_ROLE_ID}" t_owner "rbac/tenant:${KESSEL_PRINCIPAL_DOMAIN}/${ORG_ID}"
  wait_for_relationship 'temporary fixture binding is attached to the tenant' \
    "rbac/tenant:${KESSEL_PRINCIPAL_DOMAIN}/${ORG_ID}" t_binding "rbac/role_binding:${TEMP_FIXTURE_ACCESS_BINDING_ID}"
  wait_for_relationship 'temporary fixture binding points to the role' \
    "rbac/role_binding:${TEMP_FIXTURE_ACCESS_BINDING_ID}" t_role "rbac/role:${TEMP_FIXTURE_ACCESS_ROLE_ID}"
  wait_for_relationship 'temporary fixture binding points to the user' \
    "rbac/role_binding:${TEMP_FIXTURE_ACCESS_BINDING_ID}" t_subject "rbac/principal:${KESSEL_PRINCIPAL_DOMAIN}/${USER_ID}"
}

delete_temporary_user_fixture() {
  [[ "$TEMP_FIXTURE_APPLIED" == true ]] || return

  if ! "$SCRIPT_DIR/actions/apply-rbac-users-config.sh" --file "$TEMP_FIXTURE" --delete; then
    printf '%s⚠ cleanup: failed to delete temporary V2 CRUD user%s\n' "$YELLOW" "$RESET" >&2
    ((++CLEANUP_FAILURES))
  fi
  TEMP_FIXTURE_APPLIED=false
  TEMP_FIXTURE_ACCESS_ROLE_ID=""
  TEMP_FIXTURE_ACCESS_BINDING_ID=""
  [[ -z "$TEMP_FIXTURE" ]] || rm -f "$TEMP_FIXTURE"
  TEMP_FIXTURE=""
}

api_call() {
  local expected="$1"
  local method="$2"
  local path="$3"
  local payload="${4:-}"
  local status

  [[ -z "$BODY_FILE" ]] || rm -f "$BODY_FILE"
  BODY_FILE="$(mktemp "${TMPDIR:-/tmp}/v2-crud-response.XXXXXX")"
  if [[ -n "$payload" ]]; then
    status=$(curl -sS -o "$BODY_FILE" -w '%{http_code}' -X "$method" \
      -H 'Content-Type: application/json' -H "x-rh-identity: $IDENTITY_HEADER" \
      --data "$payload" "${API_URL}${API_PREFIX}${path}")
  else
    status=$(curl -sS -o "$BODY_FILE" -w '%{http_code}' -X "$method" \
      -H "x-rh-identity: $IDENTITY_HEADER" "${API_URL}${API_PREFIX}${path}")
  fi

  if [[ "$status" != "$expected" ]]; then
    printf '  %s✘ FAIL%s %-6s %s  %sexpected HTTP %s, got %s%s\n' \
      "$RED$BOLD" "$RESET" "$method" "$path" "$RED" "$expected" "$status" "$RESET" >&2
    cat "$BODY_FILE" >&2
    return 1
  fi
  printf '  %s✔ PASS%s %-6s %s  %s→ HTTP %s%s\n' \
    "$GREEN$BOLD" "$RESET" "$method" "$path" "$DIM" "$status" "$RESET"
}

json_value() {
  jq -er "$1" "$BODY_FILE"
}

load_spicedb_token() {
  if [[ -z "$SPICEDB_TOKEN" ]]; then
    [[ -f "$SPICEDB_ENV_FILE" ]] || die "SpiceDB token file not found: ${SPICEDB_ENV_FILE}"
    SPICEDB_TOKEN=$(awk -F= '$1 == "SPICEDB_GRPC_PRESHARED_KEY" {print substr($0, index($0, "=") + 1); exit}' "$SPICEDB_ENV_FILE")
  fi
  [[ -n "$SPICEDB_TOKEN" ]] || die "SPICEDB_GRPC_PRESHARED_KEY is missing from ${SPICEDB_ENV_FILE}"
}

zed_cli() {
  if command -v zed &>/dev/null; then
    ZED_TOKEN="$SPICEDB_TOKEN" ZED_ENDPOINT="$SPICEDB_ENDPOINT" ZED_INSECURE=true zed "$@"
  else
    "$CONTAINER_RUNTIME" run --rm --network "${KESSEL_NETWORK_NAME:-kessel}" \
      -e "ZED_TOKEN=$SPICEDB_TOKEN" -e "ZED_ENDPOINT=$SPICEDB_CONTAINER_ENDPOINT" -e ZED_INSECURE=true \
      "${ZED_IMAGE:-docker.io/authzed/zed:latest}" "$@"
  fi
}

touch_relationship() {
  local resource="$1"
  local relation="$2"
  local subject="$3"

  zed_cli relationship touch "$resource" "$relation" "$subject" >/dev/null
  CREATED_RELATIONSHIPS+=("${resource}|${relation}|${subject}")
}

read_relationship() {
  local resource="$1"
  local relation="${2:-}"
  local subject="${3:-}"
  local args=("$resource")

  [[ -z "$relation" ]] || args+=("$relation")
  [[ -z "$subject" ]] || args+=("$subject")
  zed_cli relationship read "${args[@]}"
}

display_relationships() {
  local label="$1"
  local resource="$2"
  local output=""

  output=$(read_relationship "$resource" 2>/dev/null) || die "Could not read Kessel tuples for ${resource}."
  printf '  %s⌘ Kessel tuples%s  %s\n' "$YELLOW$BOLD" "$RESET" "$label"
  if [[ -n "$output" ]]; then
    print_tuples "$output"
  else
    printf '    %s(no direct tuples)%s\n' "$DIM" "$RESET"
  fi
}

wait_for_relationship() {
  local label="$1"
  local resource="$2"
  local relation="$3"
  local subject="${4:-}"
  local output=""

  for _ in $(seq 1 30); do
    if output=$(read_relationship "$resource" "$relation" "$subject" 2>/dev/null) && [[ -n "$output" ]]; then
      printf '  %s✔ Kessel relation%s  %s\n' "$GREEN$BOLD" "$RESET" "$label"
      print_tuples "$output"
      return
    fi
    sleep 1
  done

  die "Kessel relation was not replicated: ${label} (${resource}#${relation}@${subject})"
}

wait_for_relationship_removal() {
  local label="$1"
  local resource="$2"
  local relation="$3"
  local subject="$4"
  local output=""

  for _ in $(seq 1 30); do
    if output=$(read_relationship "$resource" "$relation" "$subject" 2>/dev/null) && [[ -z "$output" ]]; then
      printf '  %s✔ Kessel removal%s   %s\n' "$GREEN$BOLD" "$RESET" "$label"
      return
    fi
    sleep 1
  done

  printf '%sKessel relation still exists after delete:%s %s\n' "$RED" "$RESET" "$label" >&2
  print_tuples "$output" >&2
  die "Kessel relationship removal was not replicated."
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
sys.exit(0 if Principal.objects.filter(tenant=tenant, user_id=os.environ["VALIDATION_USER_ID"]).exists() else 1)
' >/dev/null
}

lookup_role_binding_id() {
  "$CONTAINER_RUNTIME" exec \
    -e VALIDATION_ROLE_ID="$ROLE_ID" \
    -e VALIDATION_WORKSPACE_ID="$DEFAULT_WORKSPACE_ID" \
    -e VALIDATION_PRINCIPAL_ID="$PRINCIPAL_ID" \
    "$RBAC_SERVER_CONTAINER" \
    python /opt/rbac/rbac/manage.py shell -c '
import os
from management.role_binding.model import RoleBinding

binding = RoleBinding.objects.get(
    role__uuid=os.environ["VALIDATION_ROLE_ID"],
    resource_type="workspace",
    resource_id=os.environ["VALIDATION_WORKSPACE_ID"],
    principal_entries__principal__uuid=os.environ["VALIDATION_PRINCIPAL_ID"],
)
print(binding.uuid)
' | tail -1
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

  "$CONTAINER_RUNTIME" exec -e VALIDATION_ORG_ID="$ORG_ID" "$RBAC_SERVER_CONTAINER" \
    python /opt/rbac/rbac/manage.py shell -c '
import os
from django.utils import timezone
from api.models import Tenant
from management.tenant_mapping.model import TenantMapping

mapping = TenantMapping.objects.get(tenant=Tenant.objects.get(org_id=os.environ["VALIDATION_ORG_ID"]))
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

mapping = TenantMapping.objects.get(tenant=Tenant.objects.get(org_id=os.environ["VALIDATION_ORG_ID"]))
mapping.v2_write_activated_at = None
fields = ["v2_write_activated_at"]
if os.environ["VALIDATION_RESTORE_OPT_IN"] == "true":
    mapping.v2_opted_in_at = None
    fields.append("v2_opted_in_at")
mapping.save(update_fields=fields)
' >/dev/null 2>&1 || { printf '%s⚠ cleanup: failed to restore V2 write settings%s\n' "$YELLOW" "$RESET" >&2; ((++CLEANUP_FAILURES)); }
}

provision_local_authorization() {
  local tenant_resource="rbac/tenant:${KESSEL_PRINCIPAL_DOMAIN}/${ORG_ID}"
  local principal_resource="rbac/principal:${KESSEL_PRINCIPAL_DOMAIN}/${USER_ID}"
  local all_principals_resource="rbac/principal:*"
  local access_role_resource="rbac/role:${LOCAL_KESSEL_ACCESS_ROLE}"
  local access_binding_resource="rbac/role_binding:${LOCAL_KESSEL_ACCESS_BINDING}"
  local workspace_resource="rbac/workspace:${DEFAULT_WORKSPACE_ID}"

  printf '\n%s━━ LOCAL TEST HARNESS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n' "$YELLOW$BOLD" "$RESET"
  printf '%sEnabling temporary V2 writes and Kessel access (removed at exit).%s\n' "$DIM" "$RESET"
  require_local_principal
  enable_local_v2_writes
  touch_relationship "$access_role_resource" t_rbac_roles_read "$all_principals_resource"
  touch_relationship "$access_role_resource" t_rbac_roles_write "$all_principals_resource"
  touch_relationship "$access_role_resource" t_rbac_role_binding_grant "$all_principals_resource"
  touch_relationship "$access_role_resource" t_rbac_role_binding_revoke "$all_principals_resource"
  touch_relationship "$access_role_resource" t_rbac_role_binding_view "$all_principals_resource"
  # Scope.ALL is represented by the global all_all_all permission in Kessel.
  # There is no t_rbac_workspace_all relation in the local schema; granting
  # this existing relation covers the workspace CRUD permissions exercised by
  # this validator without changing the deployed schema.
  touch_relationship "$access_role_resource" t_all_all_all "$all_principals_resource"
  touch_relationship "$access_binding_resource" t_role "$access_role_resource"
  touch_relationship "$access_binding_resource" t_subject "$principal_resource"
  touch_relationship "$tenant_resource" t_binding "$access_binding_resource"
  touch_relationship "$workspace_resource" t_binding "$access_binding_resource"

  wait_for_temporary_admin_group_membership
}

wait_for_temporary_admin_group_membership() {
  local group_uuid
  group_uuid=$(
    "$CONTAINER_RUNTIME" exec -e VALIDATION_ORG_ID="$ORG_ID" "$RBAC_SERVER_CONTAINER" \
      python /opt/rbac/rbac/manage.py shell -c '
import os
from api.models import Tenant
from management.tenant_mapping.model import TenantMapping

tenant = Tenant.objects.get(org_id=os.environ["VALIDATION_ORG_ID"])
print(TenantMapping.objects.get(tenant=tenant).default_admin_group_uuid)
' | tail -1
  )
  wait_for_relationship 'temporary user is in the default admin group' \
    "rbac/group:${group_uuid}" t_member "rbac/principal:${KESSEL_PRINCIPAL_DOMAIN}/${USER_ID}"
}

cleanup_api_resources() {
  local payload

  if [[ -n "$ROLE_ID" && -n "$PRINCIPAL_ID" && -n "$DEFAULT_WORKSPACE_ID" ]]; then
    curl -sS -o /dev/null -X PUT -H 'Content-Type: application/json' -H "x-rh-identity: $IDENTITY_HEADER" \
      --data '{"roles":[]}' \
      "${API_URL}${API_PREFIX}/v2/role-bindings/by-subject/?resource_id=${DEFAULT_WORKSPACE_ID}&resource_type=workspace&subject_id=${PRINCIPAL_ID}&subject_type=user" \
      || { printf '%s⚠ cleanup: failed to remove role bindings%s\n' "$YELLOW" "$RESET" >&2; ((++CLEANUP_FAILURES)); }
  fi
  if [[ -n "$ROLE_ID" ]]; then
    payload=$(jq -cn --arg role_id "$ROLE_ID" '{ids: [$role_id]}')
    curl -sS -o /dev/null -X POST -H 'Content-Type: application/json' -H "x-rh-identity: $IDENTITY_HEADER" \
      --data "$payload" "${API_URL}${API_PREFIX}/v2/roles:batchDelete/" \
      || { printf '%s⚠ cleanup: failed to delete role %s%s\n' "$YELLOW" "$ROLE_ID" "$RESET" >&2; ((++CLEANUP_FAILURES)); }
  fi
  if [[ -n "$CHILD_WORKSPACE_ID" ]]; then
    curl -sS -o /dev/null -X DELETE -H "x-rh-identity: $IDENTITY_HEADER" \
      "${API_URL}${API_PREFIX}/v2/workspaces/${CHILD_WORKSPACE_ID}/" \
      || { printf '%s⚠ cleanup: failed to delete child workspace %s%s\n' "$YELLOW" "$CHILD_WORKSPACE_ID" "$RESET" >&2; ((++CLEANUP_FAILURES)); }
  fi
  if [[ -n "$PARENT_WORKSPACE_ID" ]]; then
    curl -sS -o /dev/null -X DELETE -H "x-rh-identity: $IDENTITY_HEADER" \
      "${API_URL}${API_PREFIX}/v2/workspaces/${PARENT_WORKSPACE_ID}/" \
      || { printf '%s⚠ cleanup: failed to delete parent workspace %s%s\n' "$YELLOW" "$PARENT_WORKSPACE_ID" "$RESET" >&2; ((++CLEANUP_FAILURES)); }
  fi
}

cleanup() {
  local relationship resource relation subject
  VALIDATION_STATUS=$?
  # Continue cleanup even if one API/Kessel operation fails. In particular,
  # do not leave the temporary YAML fixture user behind after a passing run.
  set +e
  cleanup_api_resources
  if [[ -n "${CREATED_RELATIONSHIPS[*]:-}" ]]; then
    for relationship in "${CREATED_RELATIONSHIPS[@]}"; do
      IFS='|' read -r resource relation subject <<< "$relationship"
      zed_cli relationship delete "$resource" "$relation" "$subject" >/dev/null 2>&1 \
        || { printf '%s⚠ cleanup: failed to delete Kessel tuple %s#%s@%s%s\n' "$YELLOW" "$resource" "$relation" "$subject" "$RESET" >&2; ((++CLEANUP_FAILURES)); }
    done
  fi
  restore_local_v2_writes
  delete_temporary_user_fixture
  [[ -z "$BODY_FILE" ]] || rm -f "$BODY_FILE"
  if [[ "$VALIDATION_STATUS" -ne 0 ]]; then
    exit "$VALIDATION_STATUS"
  elif [[ "$CLEANUP_FAILURES" -gt 0 ]]; then
    printf '\n%s⚠ Validation passed but %d cleanup operation(s) failed — temporary state may remain.%s\n' \
      "$YELLOW$BOLD" "$CLEANUP_FAILURES" "$RESET" >&2
    exit 2
  fi
}
trap cleanup EXIT

main() {
  require_cmd awk
  require_cmd base64
  require_cmd curl
  require_cmd jq
  require_cmd "$CONTAINER_RUNTIME"
  load_spicedb_token
  load_temporary_user_fixture
  make_identity_header

  printf '%s╭──────────────────────────────────────────────────────────────╮%s\n' "$CYAN$BOLD" "$RESET"
  printf '%s│                 LOCAL V2 API CRUD VALIDATION                  │%s\n' "$CYAN$BOLD" "$RESET"
  printf '%s╰──────────────────────────────────────────────────────────────╯%s\n' "$CYAN$BOLD" "$RESET"
  printf 'Identity: temporary fixture user=%s user_id=%s org_id=%s\n' "$USERNAME" "$USER_ID" "$ORG_ID"
  printf '%sWaiting for RBAC API%s  %s\n' "$DIM" "$RESET" "${API_URL}${API_PREFIX}/v2/workspaces/"
  for _ in $(seq 1 90); do
    if curl -sS -o /dev/null "${API_URL}/metrics" 2>/dev/null; then
      break
    fi
    sleep 2
  done
  curl -sS -o /dev/null "${API_URL}/metrics" 2>/dev/null || die 'RBAC API did not become ready.'

  scenario 1 'List the default workspace' 'GET /v2/workspaces/?type=default' 'HTTP 200 with a workspace id'
  api_call 200 GET '/v2/workspaces/?type=default'
  DEFAULT_WORKSPACE_ID=$(json_value '.data[0].id')
  display_relationships 'the read-only default workspace' "rbac/workspace:${DEFAULT_WORKSPACE_ID}"
  provision_local_authorization

  scenario 2 'List and read V2 principals' 'GET /v2/principals/ and GET /v2/principals/{id}/' 'HTTP 200 for both read-only routes'
  api_call 200 GET "/v2/principals/?username=${USERNAME}"
  PRINCIPAL_ID=$(jq -er --arg user_id "$USER_ID" '[.data[] | select(.user_id == $user_id)][0].uuid' "$BODY_FILE")
  api_call 200 GET "/v2/principals/${PRINCIPAL_ID}/"
  wait_for_relationship 'local test authorization is attached to the principal' \
    "rbac/role_binding:${LOCAL_KESSEL_ACCESS_BINDING}" t_subject "rbac/principal:${KESSEL_PRINCIPAL_DOMAIN}/${USER_ID}"

  scenario 3 'Create and read workspaces' 'POST /v2/workspaces/ twice, then GET each detail route' 'HTTP 201 and HTTP 200'
  api_call 201 POST '/v2/workspaces/' "{\"name\":\"${PARENT_WORKSPACE_NAME}\"}"
  PARENT_WORKSPACE_ID=$(json_value '.id')
  api_call 201 POST '/v2/workspaces/' "{\"name\":\"${CHILD_WORKSPACE_NAME}\",\"parent_id\":\"${PARENT_WORKSPACE_ID}\"}"
  CHILD_WORKSPACE_ID=$(json_value '.id')
  api_call 200 GET "/v2/workspaces/${CHILD_WORKSPACE_ID}/"
  wait_for_relationship 'parent workspace is attached to the default workspace' \
    "rbac/workspace:${PARENT_WORKSPACE_ID}" t_parent "rbac/workspace:${DEFAULT_WORKSPACE_ID}"
  wait_for_relationship 'child workspace is attached to the parent workspace' \
    "rbac/workspace:${CHILD_WORKSPACE_ID}" t_parent "rbac/workspace:${PARENT_WORKSPACE_ID}"

  scenario 4 'Query, PATCH, PUT, and move a workspace' 'POST query, PATCH detail, PUT detail, POST move' 'HTTP 200 for every route'
  api_call 200 POST '/v2/workspaces/query/' "{\"ids\":[\"${CHILD_WORKSPACE_ID}\"]}"
  jq -e --arg id "$CHILD_WORKSPACE_ID" '[.data[] | select(.id == $id)] | length == 1' "$BODY_FILE" >/dev/null \
    || die 'Workspace query did not return the created child workspace.'
  api_call 200 PATCH "/v2/workspaces/${CHILD_WORKSPACE_ID}/" '{"description":"patched by v2-crud validation"}'
  api_call 200 PUT "/v2/workspaces/${CHILD_WORKSPACE_ID}/" "{\"name\":\"${UPDATED_WORKSPACE_NAME}\",\"description\":\"updated by v2-crud validation\",\"parent_id\":\"${PARENT_WORKSPACE_ID}\"}"
  api_call 200 POST "/v2/workspaces/${CHILD_WORKSPACE_ID}/move/" "{\"parent_id\":\"${DEFAULT_WORKSPACE_ID}\"}"
  wait_for_relationship 'moved child workspace is attached to the default workspace' \
    "rbac/workspace:${CHILD_WORKSPACE_ID}" t_parent "rbac/workspace:${DEFAULT_WORKSPACE_ID}"
  wait_for_relationship_removal 'moved child workspace is detached from the previous parent' \
    "rbac/workspace:${CHILD_WORKSPACE_ID}" t_parent "rbac/workspace:${PARENT_WORKSPACE_ID}"

  scenario 5 'Create, list, and read a V2 role' 'POST /v2/roles/, GET collection, GET detail' 'HTTP 201 then HTTP 200'
  api_call 201 POST '/v2/roles/' "{\"name\":\"${ROLE_NAME}\",\"description\":\"created by v2-crud validation\",\"permissions\":[{\"application\":\"inventory\",\"resource_type\":\"hosts\",\"operation\":\"read\"}]}"
  ROLE_ID=$(json_value '.id')
  api_call 200 GET "/v2/roles/?name=${ROLE_NAME}"
  jq -e --arg id "$ROLE_ID" '[.data[] | select(.id == $id)] | length == 1' "$BODY_FILE" >/dev/null \
    || die 'Role list did not return the created role.'
  api_call 200 GET "/v2/roles/${ROLE_ID}/"
  wait_for_relationship 'role is owned by this tenant' \
    "rbac/role:${ROLE_ID}" t_owner "rbac/tenant:${KESSEL_PRINCIPAL_DOMAIN}/${ORG_ID}"
  wait_for_relationship 'role grants inventory hosts read' \
    "rbac/role:${ROLE_ID}" t_inventory_hosts_read 'rbac/principal:*'

  scenario 6 'Update the V2 role' 'PUT /v2/roles/{id}/' 'HTTP 200 with the updated name'
  api_call 200 PUT "/v2/roles/${ROLE_ID}/" "{\"name\":\"${UPDATED_ROLE_NAME}\",\"description\":\"updated by v2-crud validation\",\"permissions\":[{\"application\":\"inventory\",\"resource_type\":\"hosts\",\"operation\":\"read\"}]}"
  [[ "$(json_value '.name')" == "$UPDATED_ROLE_NAME" ]] || die 'Role update did not return the updated name.'
  wait_for_relationship 'updated role keeps its tenant owner' \
    "rbac/role:${ROLE_ID}" t_owner "rbac/tenant:${KESSEL_PRINCIPAL_DOMAIN}/${ORG_ID}"
  wait_for_relationship 'updated role keeps its inventory permission' \
    "rbac/role:${ROLE_ID}" t_inventory_hosts_read 'rbac/principal:*'

  scenario 7 'Create and list a role binding' 'POST batchCreate, then GET /v2/role-bindings/' 'HTTP 201 then HTTP 200'
  binding=$(jq -cn \
    --arg workspace_id "$DEFAULT_WORKSPACE_ID" \
    --arg principal_id "$PRINCIPAL_ID" \
    --arg role_id "$ROLE_ID" \
    '{requests: [{resource: {id: $workspace_id, type: "workspace"}, subject: {id: $principal_id, type: "user"}, role: {id: $role_id}}]}')
  api_call 201 POST '/v2/role-bindings:batchCreate/' "$binding"
  BINDING_ID=$(lookup_role_binding_id)
  api_call 200 GET "/v2/role-bindings/?role_id=${ROLE_ID}&resource_id=${DEFAULT_WORKSPACE_ID}&resource_type=workspace&fields=resource(id,type),role(id)"
  jq -e --arg role_id "$ROLE_ID" '[.data[] | select(.role.id == $role_id and .resource.type == "workspace")] | length > 0' "$BODY_FILE" >/dev/null \
    || die 'Role-binding list did not return the created binding.'
  wait_for_relationship 'workspace points to the role binding' \
    "rbac/workspace:${DEFAULT_WORKSPACE_ID}" t_binding "rbac/role_binding:${BINDING_ID}"
  wait_for_relationship 'role binding points to the role' \
    "rbac/role_binding:${BINDING_ID}" t_role "rbac/role:${ROLE_ID}"
  wait_for_relationship 'role binding points to the principal' \
    "rbac/role_binding:${BINDING_ID}" t_subject "rbac/principal:${KESSEL_PRINCIPAL_DOMAIN}/${USER_ID}"

  scenario 8 'Read and replace bindings by subject' 'GET then PUT /v2/role-bindings/by-subject/' 'HTTP 200 for both routes'
  by_subject_path="/v2/role-bindings/by-subject/?resource_id=${DEFAULT_WORKSPACE_ID}&resource_type=workspace&subject_id=${PRINCIPAL_ID}&subject_type=user"
  api_call 200 GET "$by_subject_path"
  api_call 200 PUT "$by_subject_path" "{\"roles\":[{\"id\":\"${ROLE_ID}\"}]}"
  wait_for_relationship 'by-subject update keeps the workspace binding' \
    "rbac/workspace:${DEFAULT_WORKSPACE_ID}" t_binding "rbac/role_binding:${BINDING_ID}"
  wait_for_relationship 'by-subject update keeps the role subject' \
    "rbac/role_binding:${BINDING_ID}" t_subject "rbac/principal:${KESSEL_PRINCIPAL_DOMAIN}/${USER_ID}"

  scenario 9 'Delete the binding, role, and workspaces' 'PUT roles=[], POST roles:batchDelete, DELETE workspace details' 'HTTP 200, 204, and 204'
  api_call 200 PUT "$by_subject_path" '{"roles":[]}'
  wait_for_relationship_removal 'role binding no longer points to the principal' \
    "rbac/role_binding:${BINDING_ID}" t_subject "rbac/principal:${KESSEL_PRINCIPAL_DOMAIN}/${USER_ID}"
  api_call 204 POST '/v2/roles:batchDelete/' "{\"ids\":[\"${ROLE_ID}\"]}"
  wait_for_relationship_removal 'workspace no longer points to the deleted role binding' \
    "rbac/workspace:${DEFAULT_WORKSPACE_ID}" t_binding "rbac/role_binding:${BINDING_ID}"
  wait_for_relationship_removal 'deleted role has no tenant-owner tuple' \
    "rbac/role:${ROLE_ID}" t_owner "rbac/tenant:${KESSEL_PRINCIPAL_DOMAIN}/${ORG_ID}"
  ROLE_ID=""
  api_call 204 DELETE "/v2/workspaces/${CHILD_WORKSPACE_ID}/"
  wait_for_relationship_removal 'deleted child workspace has no parent tuple' \
    "rbac/workspace:${CHILD_WORKSPACE_ID}" t_parent "rbac/workspace:${DEFAULT_WORKSPACE_ID}"
  CHILD_WORKSPACE_ID=""
  api_call 204 DELETE "/v2/workspaces/${PARENT_WORKSPACE_ID}/"
  wait_for_relationship_removal 'deleted parent workspace has no parent tuple' \
    "rbac/workspace:${PARENT_WORKSPACE_ID}" t_parent "rbac/workspace:${DEFAULT_WORKSPACE_ID}"
  PARENT_WORKSPACE_ID=""

  printf '\n%s✔ Local V2 API CRUD validation passed.%s\n' "$GREEN$BOLD" "$RESET"
}

main "$@"
