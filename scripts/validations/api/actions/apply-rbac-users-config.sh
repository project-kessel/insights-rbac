#!/usr/bin/env bash
# Apply a declarative local RBAC users/groups/roles fixture.
#
# Usage:
#   scripts/validations/api/actions/apply-rbac-users-config.sh
#   scripts/validations/api/actions/apply-rbac-users-config.sh --file fixture.yaml
#   scripts/validations/api/actions/apply-rbac-users-config.sh --dry-run
#   scripts/validations/api/actions/apply-rbac-users-config.sh --file temporary.yaml --delete

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/rbac-users.yaml"
RBAC_SERVER_CONTAINER="${RBAC_SERVER_CONTAINER:-full-kessel-rbac-server-1}"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

show_help() {
  cat <<'EOF'
Usage: apply-rbac-users-config.sh [options]

Apply a YAML fixture to the running local RBAC container. The fixture can
bootstrap tenants, create users/principals, create groups and memberships,
create V2 custom roles, and create V2 role bindings for users or groups.

Options:
  --file PATH       YAML fixture (default: actions/rbac-users.yaml)
  --dry-run         validate and print the normalized fixture without applying
  --delete          remove users declared by a temporary fixture
  --help            show this help

Environment:
  RBAC_SERVER_CONTAINER  RBAC container name
                         (default: full-kessel-rbac-server-1)
  CONTAINER_RUNTIME      docker or podman (auto-detected)

The operation is intended for local development only. It writes RBAC data and
replicates resulting relations through the normal local outbox path.

Deletion requires each tenant in the fixture to set temporary: true. It
removes the declared users through the normal user-disable/bootstrap path.
EOF
}

DRY_RUN=false
DELETE_CONFIG=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --file|-f)
      shift
      [[ $# -gt 0 ]] || die "--file requires a path"
      CONFIG_FILE="$1"
      ;;
    --dry-run)
      DRY_RUN=true
      ;;
    --delete)
      DELETE_CONFIG=true
      ;;
    --help|-h)
      show_help
      exit 0
      ;;
    *)
      die "unknown option '$1' (use --help for usage)"
      ;;
  esac
  shift
done

[[ "$DRY_RUN" == true && "$DELETE_CONFIG" == true ]] && die "--dry-run and --delete cannot be combined"

[[ -f "$CONFIG_FILE" ]] || die "fixture does not exist: $CONFIG_FILE"

CONFIG_JSON="$({
  python3 - "$CONFIG_FILE" <<'PY'
import json
import pathlib
import sys

import yaml

path = pathlib.Path(sys.argv[1])
with path.open() as stream:
    config = yaml.safe_load(stream)

if not isinstance(config, dict):
    raise SystemExit("fixture root must be a YAML mapping")
if config.get("version") != 1:
    raise SystemExit("fixture version must be 1")
if not isinstance(config.get("tenants"), list) or not config["tenants"]:
    raise SystemExit("fixture must contain a non-empty tenants list")

print(json.dumps(config))
PY
})" || die "could not parse YAML fixture"

if [[ "$DELETE_CONFIG" == true ]]; then
  python3 - "$CONFIG_FILE" <<'PY'
import pathlib
import sys

import yaml

path = pathlib.Path(sys.argv[1])
with path.open() as stream:
    config = yaml.safe_load(stream)

for tenant in config.get("tenants", []):
    if tenant.get("temporary") is not True:
        raise SystemExit(
            f"refusing deletion for {tenant.get('org_id')}: set temporary: true in the fixture"
        )
PY
fi

if [[ "$DRY_RUN" == true ]]; then
  python3 - "$CONFIG_FILE" <<'PY'
import pathlib
import sys

import yaml

path = pathlib.Path(sys.argv[1])
with path.open() as stream:
    config = yaml.safe_load(stream)

for tenant in config.get("tenants", []):
    print(
        "org={org} users={users} groups={groups} roles={roles} bindings={bindings}".format(
            org=tenant.get("org_id"),
            users=len(tenant.get("users", [])),
            groups=len(tenant.get("groups", [])),
            roles=len(tenant.get("roles", [])),
            bindings=len(tenant.get("role_bindings", [])),
        )
    )
PY
  exit 0
fi

detect_runtime() {
  if [[ -n "${CONTAINER_RUNTIME:-}" ]]; then
    return
  fi

  if command -v docker >/dev/null 2>&1 && docker container inspect "$RBAC_SERVER_CONTAINER" >/dev/null 2>&1; then
    CONTAINER_RUNTIME=docker
  elif command -v podman >/dev/null 2>&1 && podman container inspect "$RBAC_SERVER_CONTAINER" >/dev/null 2>&1; then
    CONTAINER_RUNTIME=podman
  else
    die "RBAC container '$RBAC_SERVER_CONTAINER' is not available. Start the local full stack first."
  fi
}

detect_runtime

printf '%s\n' "$CONFIG_JSON" | "$CONTAINER_RUNTIME" exec -i \
  -e "RBAC_USERS_DELETE=$DELETE_CONFIG" "$RBAC_SERVER_CONTAINER" \
  python /opt/rbac/rbac/manage.py shell -c '
import json
import os
import sys
from contextlib import nullcontext

from django.db import transaction
from api.models import Tenant, User
from management.group.model import Group
from management.inventory_replicator.outbox_replicator import OutboxReplicator
from management.principal.model import Principal
from management.role.v2_model import RoleV2
from management.role.v2_service import RoleV2Service
from management.role_binding.service import CreateBindingRequest, RoleBindingService
from management.tenant_mapping.model import TenantMapping
from management.tenant_service.v2 import V2TenantBootstrapService
from management.tenant_service.tenant_service import BootstrappedTenant
from management.workspace.model import Workspace


config = json.load(sys.stdin)
replicator = OutboxReplicator()
bootstrap_service = V2TenantBootstrapService(replicator=replicator)


def value_list(raw):
    return raw if isinstance(raw, list) else []


def resolve_workspace_id(tenant, resource_id):
    aliases = {
        "default": Workspace.objects.default(tenant=tenant).id,
        "default_workspace": Workspace.objects.default(tenant=tenant).id,
        "root": Workspace.objects.root(tenant=tenant).id,
        "root_workspace": Workspace.objects.root(tenant=tenant).id,
    }
    return str(aliases.get(str(resource_id), resource_id))


def resolve_resource(tenant, resource):
    if not isinstance(resource, dict):
        raise ValueError("role binding resource must be a mapping")
    resource_type = str(resource.get("type", ""))
    resource_id = resource.get("id")
    if not resource_type or resource_id is None:
        raise ValueError("role binding resource requires type and id")
    if resource_type == "workspace":
        resource_id = resolve_workspace_id(tenant, resource_id)
    elif resource_type == "tenant" and str(resource_id) in ("current", "tenant"):
        resource_id = tenant.tenant_resource_id()
    return resource_type, str(resource_id)


def ensure_tenant(tenant_config):
    org_id = str(tenant_config["org_id"])
    account_id = tenant_config.get("account_id")
    tenant = Tenant.objects.filter(org_id=org_id).first()
    if tenant is None:
        if tenant_config.get("bootstrap", True) is False:
            raise ValueError(f"Tenant {org_id} does not exist and bootstrap is disabled")
        bootstrapped = bootstrap_service.new_bootstrapped_tenant(org_id, account_id)
        return bootstrapped.tenant, bootstrapped

    if account_id and tenant.account_id != str(account_id):
        tenant.account_id = str(account_id)
        tenant.save(update_fields=["account_id"])
    mapping = TenantMapping.objects.filter(tenant=tenant).first()
    if mapping is None and tenant_config.get("bootstrap", True):
        with transaction.atomic():
            bootstrapped = bootstrap_service.bootstrap_tenant(tenant)
        return tenant, bootstrapped
    return tenant, BootstrappedTenant(tenant=tenant, mapping=mapping)


def delete_temporary_users(tenant_config):
    org_id = str(tenant_config.get("org_id"))
    if tenant_config.get("temporary") is not True:
        raise ValueError(f"Tenant {org_id} is not marked temporary")
    tenant = Tenant.objects.filter(org_id=org_id).first()
    if tenant is None:
        print(f"Already absent org={org_id}")
        return
    mapping = TenantMapping.objects.filter(tenant=tenant).first()
    bootstrapped_tenant = BootstrappedTenant(tenant=tenant, mapping=mapping)
    role_names = [str(role_config["name"]) for role_config in value_list(tenant_config.get("roles"))]
    roles = list(
        RoleV2.objects.filter(tenant=tenant, name__in=role_names, type=RoleV2.Types.CUSTOM)
    )
    if roles:
        RoleV2Service(tenant=tenant, replicator=replicator).bulk_delete(
            [str(role.uuid) for role in roles], from_tenant=tenant
        )
    users = value_list(tenant_config.get("users"))
    for user_config in users:
        username = str(user_config["username"])
        user = User(
            username=username,
            user_id=str(user_config.get("user_id") or f"local-{username}"),
            org_id=org_id,
            account=str(tenant.account_id or ""),
            is_active=False,
        )
        bootstrap_service.update_user(user, upsert=False, bootstrapped_tenant=bootstrapped_tenant)
    print(f"Deleted temporary users org={org_id} users={len(users)}")


def ensure_user(tenant, user_config, bootstrapped_tenant):
    username = str(user_config["username"])
    user_id = str(user_config.get("user_id") or f"local-{username}")
    user = User(
        username=username,
        user_id=user_id,
        org_id=str(tenant.org_id),
        account=str(tenant.account_id or ""),
        admin=bool(user_config.get("admin", False)),
        is_active=True,
    )
    bootstrap_service.update_user(user, upsert=True, bootstrapped_tenant=bootstrapped_tenant)
    return Principal.objects.get(username=username.lower(), tenant=tenant)


def ensure_group(tenant, group_config):
    name = str(group_config["name"])
    group, created = Group.objects.get_or_create(
        tenant=tenant,
        name=name,
        defaults={"description": group_config.get("description")},
    )
    description = group_config.get("description")
    if description is not None and group.description != description:
        group.description = description
        group.save(update_fields=["description"])
    return group, created


def ensure_role(tenant, role_config, role_service):
    name = str(role_config["name"])
    permissions = value_list(role_config.get("permissions"))
    role = RoleV2.objects.filter(tenant=tenant, name=name).first()
    description = role_config.get("description") or ""
    if role is None:
        return role_service.create(name, description, permissions, tenant), True
    if role.type != RoleV2.Types.CUSTOM:
        raise ValueError(f"Role {name} exists but is not a custom V2 role")
    return role_service.update(str(role.uuid), name, description, permissions, tenant), False


for tenant_config in config["tenants"]:
    if os.environ.get("RBAC_USERS_DELETE") == "true":
        delete_temporary_users(tenant_config)
        continue
    with nullcontext():
        tenant, bootstrapped = ensure_tenant(tenant_config)
        principals = {}
        for user_config in value_list(tenant_config.get("users")):
            principal = ensure_user(tenant, user_config, bootstrapped)
            principals[str(user_config["username"]).lower()] = principal

        groups = {}
        for group_config in value_list(tenant_config.get("groups")):
            group, _ = ensure_group(tenant, group_config)
            groups[str(group_config["name"])] = group

        for user_config in value_list(tenant_config.get("users")):
            principal = principals[str(user_config["username"]).lower()]
            for group_name in value_list(user_config.get("groups")):
                group = groups.get(str(group_name))
                if group is None:
                    raise ValueError(f"Unknown group {group_name} for user {principal.username}")
                group.principals.add(principal)

        for group_config in value_list(tenant_config.get("groups")):
            group = groups[str(group_config["name"])]
            for username in value_list(group_config.get("members")):
                principal = principals.get(str(username).lower())
                if principal is None:
                    raise ValueError(f"Unknown user {username} for group {group.name}")
                group.principals.add(principal)

        role_service = RoleV2Service(tenant=tenant, replicator=replicator)
        roles = {}
        for role_config in value_list(tenant_config.get("roles")):
            role, _ = ensure_role(tenant, role_config, role_service)
            roles[str(role_config["name"])] = role

        binding_specs = list(value_list(tenant_config.get("role_bindings")))
        for user_config in value_list(tenant_config.get("users")):
            for binding in value_list(user_config.get("role_bindings")):
                binding = dict(binding)
                binding["subjects"] = [{"type": "user", "name": user_config["username"]}]
                binding_specs.append(binding)
        for group_config in value_list(tenant_config.get("groups")):
            for binding in value_list(group_config.get("role_bindings")):
                binding = dict(binding)
                binding["subjects"] = [{"type": "group", "name": group_config["name"]}]
                binding_specs.append(binding)

        requests = []
        for binding in binding_specs:
            role_name = str(binding["role"])
            role = roles.get(role_name)
            if role is None:
                raise ValueError(f"Unknown role {role_name}")
            resource_type, resource_id = resolve_resource(tenant, binding["resource"])
            for subject in value_list(binding.get("subjects")):
                subject_type = str(subject["type"])
                subject_name = str(subject["name"])
                if subject_type == "user":
                    subject_obj = principals.get(subject_name.lower())
                elif subject_type == "group":
                    subject_obj = groups.get(subject_name)
                else:
                    raise ValueError(f"Unsupported binding subject type {subject_type}")
                if subject_obj is None:
                    raise ValueError(f"Unknown {subject_type} subject {subject_name}")
                requests.append(
                    CreateBindingRequest(
                        role_id=str(role.uuid),
                        resource_type=resource_type,
                        resource_id=resource_id,
                        subject_type=subject_type,
                        subject_id=str(subject_obj.uuid),
                    )
                )

        if requests:
            RoleBindingService(tenant=tenant, replicator=replicator).batch_create(requests)

        print(
            "Applied org={org} users={users} groups={groups} roles={roles} bindings={bindings}".format(
                org=tenant.org_id,
                users=len(principals),
                groups=len(groups),
                roles=len(roles),
                bindings=len(requests),
            )
        )
'
