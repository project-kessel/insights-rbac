"""
Copyright 2019 Red Hat, Inc.

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as
published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import dataclasses
import logging
from collections.abc import Callable
from typing import Any, Iterable, Optional, Tuple, Union

import uuid_utils.compat as uuid
from django.db.models import F
from feature_flags import FEATURE_FLAGS
from management.models import BindingMapping, Workspace
from management.permission.model import Permission
from management.permission.scope_service import ImplicitResourceService, Scope
from management.role.model import Role
from management.role.v2_model import CustomRoleV2, RoleV2
from management.role_binding.model import RoleBinding, RoleBindingGroup
from migration_tool.ingest import add_element
from migration_tool.models import (
    V2boundresource,
    V2role,
    V2rolebinding,
    cleanNameForV2SchemaCompatibility,
)

logger = logging.getLogger(__name__)

_PermissionGroupings = dict[V2boundresource, set[Permission]]

ScopeBoundResourceResolver = Callable[[Scope], V2boundresource]


def constant_bound_resource(resource: V2boundresource) -> ScopeBoundResourceResolver:
    """Return a resolver that always returns the same bound resource."""
    return lambda _scope: resource


def with_workspace_scope_inheritance(resource_map: dict[Scope, V2boundresource]) -> dict[Scope, V2boundresource]:
    """Return a copy of ``resource_map`` with DEFAULT aliased to ROOT when needed.

    Workspace bindings at ROOT cover the default workspace via parent inheritance,
    so callers building a map from ``binding_scopes_for_role`` should apply this
    before resolving per-permission scopes that may include DEFAULT.
    """
    result = dict(resource_map)
    if Scope.ROOT in result and Scope.DEFAULT not in result:
        result[Scope.DEFAULT] = result[Scope.ROOT]
    return result


def bound_resource_resolver_from_map(resource_map: dict[Scope, V2boundresource]) -> ScopeBoundResourceResolver:
    """Return a resolver that maps scope to resource.

    ``resource_map`` must contain an entry for every scope returned by
    ``scope_for_permission`` for permissions being migrated. Use
    ``with_workspace_scope_inheritance`` when building from binding scopes.
    """
    return lambda scope: resource_map[scope]


def add_system_role(system_roles, role: V2role):
    """Add a system role to the system role map."""
    system_roles[frozenset(role.permissions)] = role


def inventory_to_workspace(v2_perm):
    """Convert inventory permissions to workspace permissions."""
    if v2_perm == "inventory_groups_read":
        return "workspace_read"
    elif v2_perm == "inventory_groups_write":
        return "workspace_write"
    elif v2_perm == "inventory_groups_all":
        return "workspace_all"
    return v2_perm


class SystemRole:
    """A system role."""

    SYSTEM_ROLES: dict[frozenset[str], V2role] = {}

    @classmethod
    def get_system_roles(cls):
        """Get the system roles, if empty, set them."""
        if not cls.SYSTEM_ROLES:
            cls.set_system_roles()
        return cls.SYSTEM_ROLES

    @classmethod
    def set_system_roles(cls):
        """Set the system roles."""
        for role in Role.objects.public_tenant_only():
            # Skip roles such as OCM since they don't have permission
            if role.external_role_id():
                continue
            cls.set_system_role(role)

    @classmethod
    def set_system_role(cls, role):
        """Set the system role."""
        permission_list = list()
        for access in role.access.all():
            v2_perm = cleanNameForV2SchemaCompatibility(access.permission.permission)
            v2_perm = inventory_to_workspace(v2_perm)
            permission_list.append(v2_perm)
        add_system_role(cls.SYSTEM_ROLES, V2role(str(role.uuid), True, frozenset(permission_list)))


@dataclasses.dataclass(frozen=True)
class MigrateCustomRoleResult:
    """The models resulting from migrating a V1 custom role to V2."""

    v2_roles: tuple[CustomRoleV2, ...]
    binding_mappings: tuple[BindingMapping, ...]
    role_bindings: tuple[RoleBinding, ...]

    def __post_init__(self):
        """Check that this object is in a valid state."""
        if len(self.binding_mappings) != len(self.role_bindings):
            raise ValueError("BindingMappings and RoleBindings must be one-to-one")

        if {str(r.uuid) for r in self.role_bindings} != {m.mappings["id"] for m in self.binding_mappings}:
            raise ValueError("BindingMapping and RoleBinding UUIDs must match")

        if not {r.id for r in self.v2_roles}.issuperset(b.role_id for b in self.role_bindings):
            raise ValueError("All V2 roles referenced by RoleBindings must be included in v2_roles")


def v1_role_to_v2_bindings(
    v1_role: Role,
    resource_for_scope: ScopeBoundResourceResolver,
    existing_role_bindings: Iterable[BindingMapping],
    existing_v2_roles: Iterable[CustomRoleV2],
) -> MigrateCustomRoleResult:
    """Convert a V1 role to a set of V2 role bindings.

    ``resource_for_scope`` maps each permission's implicit scope (from the scope
    service) to the V2boundresource for permissions without explicit resource
    definitions.
    """
    from internal.utils import (
        get_or_create_ungrouped_workspace,
        get_workspace_ids_from_resource_definition,
    )

    _scope_service = ImplicitResourceService.from_settings()

    def _resolve_default(permission: Permission) -> V2boundresource:
        scope = _scope_service.scope_for_permission(permission.permission)
        return resource_for_scope(scope)

    perm_groupings: _PermissionGroupings = {}

    # Group V2 permissions by target resource
    for access in v1_role.access.all():
        permission: Permission = access.permission

        default = True
        for resource_def in access.resourceDefinitions.all():
            default = False
            attri_filter = resource_def.attributeFilter

            # Deal with some malformed data in db
            if attri_filter["operation"] == "in":
                if not isinstance(attri_filter["value"], list):
                    # Override operation as "equal" if value is not a list
                    attri_filter["operation"] = "equal"
                elif attri_filter["value"] == []:
                    # Skip empty values
                    continue

            resource_type = attribute_key_to_v2_related_resource_type(attri_filter["key"])

            if resource_type is None:
                # Resource type not mapped to v2
                continue

            # validate permission was not added to workspace out of users org for v1 (RHCLOUD-35481)
            if resource_type == ("rbac", "workspace"):
                requested_workspace_ids = set(get_workspace_ids_from_resource_definition(attri_filter))

                if len(requested_workspace_ids) > 0:
                    actual_workspace_ids = set(
                        w.id for w in Workspace.objects.filter(tenant=v1_role.tenant, id__in=requested_workspace_ids)
                    )

                    if requested_workspace_ids != actual_workspace_ids:
                        logger.info(
                            f"Skipping migrating permission '{permission}' from v1 role '{v1_role.name}'; "
                            f"it was added to workspaces outside of users org: "
                            f"{requested_workspace_ids - actual_workspace_ids}"
                        )

                        continue

            for resource_id in values_from_attribute_filter(attri_filter):
                if resource_id is None:
                    if resource_type != ("rbac", "workspace"):
                        raise ValueError(f"Resource ID is None for {resource_def}")
                    if FEATURE_FLAGS.is_remove_null_value_enabled:
                        ungrouped_ws = get_or_create_ungrouped_workspace(v1_role.tenant)
                        resource_id = str(ungrouped_ws.id)
                    else:
                        continue
                elif resource_id == "":
                    continue
                add_element(perm_groupings, V2boundresource(resource_type, resource_id), permission, collection=set)
        if default:
            add_element(
                perm_groupings,
                _resolve_default(permission),
                permission,
                collection=set,
            )

    # Project permission sets to roles per set of resources
    return permission_groupings_to_v2_role_bindings(
        perm_groupings,
        v1_role,
        existing_mappings=existing_role_bindings,
        existing_v2_roles=existing_v2_roles,
    )


def _target_role_uuid_for(
    existing_binding: Optional[V2rolebinding],
    latest_roles: Iterable[RoleV2],
) -> uuid.UUID:
    """Return the UUID to use for a new V2 role given an existing V2rolebinding and the set of V2 roles already used."""
    if existing_binding is not None:
        existing_uuid = existing_binding.role.id
        uuid_already_used = any(str(r.uuid) == existing_uuid for r in latest_roles)

        if not uuid_already_used:
            return uuid.UUID(existing_uuid)

    # Return a new UUID, since we can't use the existing one.
    return uuid.uuid7()


def _v2_custom_role_from_v1(v1_role: Role, disambiguator: int, target_uuid: uuid.UUID):
    # This guarantees that we do not accidentally move V2 roles between different V1 sources. If the UUID
    # already exists with a different V1 source, then this will attempt to create a new V2 role with an
    # existing UUID, which will fail due to the conflict.
    #
    # If we do not have an existing UUID to reuse, then is guaranteed to create a new role because we've just
    # generated a new random UUID.
    new_role, _ = CustomRoleV2.objects.update_or_create(
        uuid=target_uuid,
        tenant=v1_role.tenant,
        v1_source=v1_role,
        defaults=dict(
            name=f"{v1_role.display_name} ({disambiguator})",
            description=v1_role.description,
        ),
    )

    return new_role


def _get_or_create_binding(
    v1_role: Role,
    v2_role: RoleV2,
    resource: V2boundresource,
    existing_mapping: Optional[BindingMapping],
    group_uuids: list[str],
) -> tuple[BindingMapping, RoleBinding]:
    if v2_role.v1_source != v1_role:
        raise ValueError("Expected V2 role to have the provided V1 role as its source.")

    # Satisfy mypy.
    role_binding: RoleBinding

    if existing_mapping is not None:
        existing_binding_value = existing_mapping.get_role_binding()

        role_binding, _ = RoleBinding.objects.update_or_create(
            tenant=v1_role.tenant,
            uuid=existing_binding_value.id,
            defaults=dict(
                role=v2_role,
                resource_type=resource.resource_type[1],
                resource_id=resource.resource_id,
            ),
        )

        existing_mapping.update_mappings_from_role_binding(
            role_binding.as_migration_value(force_group_uuids=group_uuids)
        )
        existing_mapping.v2_role = v2_role

        return existing_mapping, role_binding
    else:
        # No existing binding for this resource, have to create one
        role_binding = RoleBinding.objects.create(
            tenant=v1_role.tenant,
            role=v2_role,
            resource_type=resource.resource_type[1],
            resource_id=resource.resource_id,
        )

        new_mapping = BindingMapping.for_role_binding(
            role_binding=role_binding.as_migration_value(force_group_uuids=group_uuids),
            v1_role=v1_role,
            v2_role=v2_role,
        )

        return new_mapping, role_binding


def permission_groupings_to_v2_role_bindings(
    perm_groupings: _PermissionGroupings,
    v1_role: Role,
    existing_mappings: Iterable[BindingMapping],
    existing_v2_roles: Iterable[CustomRoleV2],
) -> MigrateCustomRoleResult:
    """Determine updated role bindings based on latest resource-permission state and current role bindings."""
    existing_mappings = list(existing_mappings)
    existing_v2_roles = list(existing_v2_roles)

    # TODO: this is broken for system roles, need to have Tenant or Policies provided
    # so that we don't look up Policies across all Tenants!
    if v1_role.system:
        raise ValueError("System roles are not supported.")

    if not all(r.v1_source == v1_role for r in existing_v2_roles):
        raise ValueError(f"All provided V2 roles ({existing_v2_roles}) must have v1_role ({v1_role}) as a source.")

    if not all(r.type == RoleV2.Types.CUSTOM for r in existing_v2_roles):
        raise ValueError(f"All provided V2 roles ({existing_v2_roles}) must be CUSTOM roles.")

    requested_workspace_ids = set(
        r.resource_id for r in perm_groupings.keys() if r.resource_type == ("rbac", "workspace")
    )

    if len(requested_workspace_ids) > 0:
        # Ensure the workspaces we want exist and lock them to prevent them from being concurrently deleted.
        locked_workspace_ids = set(
            str(id)
            for id in Workspace.objects.select_for_update()
            .filter(tenant=v1_role.tenant, id__in=requested_workspace_ids)
            .values_list("id", flat=True)
        )

        if requested_workspace_ids != locked_workspace_ids:
            raise ValueError(
                f"Could not migrate role referencing nonexistent workspaces: "
                f"{requested_workspace_ids - locked_workspace_ids}"
            )

    existing_mappings_by_resource = {mapping.get_role_binding().resource: mapping for mapping in existing_mappings}

    latest_groups = frozenset(policy.group for policy in v1_role.policies.all())
    latest_group_uuids = frozenset(str(g.uuid) for g in latest_groups)

    latest_roles_by_permissions: dict[frozenset[Permission], CustomRoleV2] = {}
    latest_role_bindings: list[RoleBinding] = []
    latest_binding_mappings: list[BindingMapping] = []

    # Randomize any existing role names to ensure that we don't end up with any conflicts.
    CustomRoleV2.objects.filter(pk__in=[r.pk for r in existing_v2_roles]).update(name=F("uuid"))

    for resource, raw_expected_permissions in perm_groupings.items():
        expected_permissions = frozenset(raw_expected_permissions)
        existing_mapping = existing_mappings_by_resource.get(resource)

        # Try to find an existing role with the permissions we want.
        new_role = latest_roles_by_permissions.get(expected_permissions)

        if new_role is None:
            existing_binding_value = existing_mapping.get_role_binding() if existing_mapping is not None else None

            new_role = _v2_custom_role_from_v1(
                v1_role=v1_role,
                disambiguator=len(latest_roles_by_permissions) + 1,
                target_uuid=_target_role_uuid_for(
                    existing_binding=existing_binding_value,
                    latest_roles=latest_roles_by_permissions.values(),
                ),
            )

            new_role.permissions.set(expected_permissions)
            latest_roles_by_permissions[expected_permissions] = new_role

        # We should now have a role that we've either reused or created.
        assert new_role is not None

        new_binding_mapping, new_role_binding = _get_or_create_binding(
            v1_role=v1_role,
            v2_role=new_role,
            resource=resource,
            existing_mapping=existing_mapping,
            group_uuids=list(latest_group_uuids),
        )

        latest_binding_mappings.append(new_binding_mapping)
        latest_role_bindings.append(new_role_binding)

    # Ensure that the RoleBindings we're returning have the correct set of groups.
    latest_binding_groups = [RoleBindingGroup(binding=b, group=g) for b in latest_role_bindings for g in latest_groups]

    RoleBindingGroup.objects.filter(binding__in=latest_role_bindings).delete()
    RoleBindingGroup.objects.bulk_create(latest_binding_groups)

    return MigrateCustomRoleResult(
        v2_roles=tuple(latest_roles_by_permissions.values()),
        binding_mappings=tuple(latest_binding_mappings),
        role_bindings=tuple(latest_role_bindings),
    )


def values_from_attribute_filter(attribute_filter: dict[str, Any]) -> list[str]:
    """Split a resource definition into a list of resource IDs."""
    op: str = attribute_filter["operation"]
    resource_id: Union[list[str], str] = attribute_filter["value"]

    if isinstance(resource_id, list):
        return resource_id

    return resource_id.split(",") if op == "in" else [resource_id]


# Maintained for compatibility.
def v1_perm_to_v2_perm(v1_permission: Permission):
    """Convert a V1 permission to a V2 permission."""
    return v1_permission.v2_string()


V2_RESOURCE_BY_ATTRIBUTE = {"group.id": ("rbac", "workspace")}


def attribute_key_to_v2_related_resource_type(resourceType: str) -> Optional[Tuple[str, str]]:
    """Convert a V1 resource type to a V2 resource type."""
    if resourceType in V2_RESOURCE_BY_ATTRIBUTE:
        return V2_RESOURCE_BY_ATTRIBUTE[resourceType]
    return None
