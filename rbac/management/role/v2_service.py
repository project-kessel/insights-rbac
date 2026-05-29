#
# Copyright 2026 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Service for RoleV2 management."""

import builtins
import logging
import uuid
from typing import Iterable, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import QuerySet
from management.atomic_transactions import atomic
from management.exceptions import NotFoundError, RequiredFieldError
from management.permission.exceptions import InvalidPermissionDataError
from management.permission.model import PermissionValue
from management.permission.scope_service import (
    SCOPE_DISPLAY_NAME,
    Scope,
    default_implicit_resource_service,
    permission_scope_cache,
    resolve_workspace_scope,
    scopes_for_resource_type,
)
from management.permission.service import PermissionService
from management.relation_replicator.noop_replicator import NoopReplicator
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import (
    PartitionKey,
    RelationReplicator,
    ReplicationEvent,
    ReplicationEventType,
)
from management.role.v2_exceptions import (
    CustomRoleRequiredError,
    InvalidRolePermissionsError,
    PermissionsNotFoundError,
    RoleAlreadyExistsError,
    RoleDatabaseError,
    RolesNotFoundError,
)
from management.role.v2_model import CustomRoleV2, RoleV2
from management.role.v2_role_scope import v2_role_excluded_applications
from management.role_binding.model import RoleBinding
from management.utils import as_uuid

from api.models import Tenant

logger = logging.getLogger(__name__)


class RoleV2Service:
    """
    Domain service for RoleV2 operations.

    This service encapsulates business logic for role management.

    Raises domain-specific exceptions that should be caught and converted
    to HTTP-level errors by the view layer.
    """

    DEFAULT_LIST_FIELDS = {"id", "name", "description", "last_modified"}
    DEFAULT_RETRIEVE_FIELDS = {"id", "name", "description", "permissions", "last_modified"}

    def __init__(
        self,
        tenant: Tenant | None = None,
        replicator: RelationReplicator | None = None,
    ):
        """Initialize the service."""
        self.tenant = tenant
        self.permission_service = PermissionService()
        if settings.REPLICATION_TO_RELATION_ENABLED:
            self._replicator = replicator if replicator is not None else OutboxReplicator()
        else:
            self._replicator = NoopReplicator()

    def _validate_and_resolve_permissions(self, permission_data: list[dict]) -> list:
        """
        Validate permissions and resolve permission objects.

        Returns list of Permission objects.
        Raises domain exceptions for validation failures.
        """
        if not permission_data:
            raise RequiredFieldError("permissions")

        try:
            permissions = self.permission_service.resolve(permission_data)
            requested = {PermissionValue.from_v2_dict(p).v1_string() for p in permission_data}
        except InvalidPermissionDataError as e:
            raise InvalidRolePermissionsError(str(e))

        found = {p.permission for p in permissions}
        not_found = requested - found
        if not_found:
            raise PermissionsNotFoundError(list(not_found))

        excluded_apps = v2_role_excluded_applications()
        if excluded_apps:
            bad_apps = sorted({p.application for p in permissions if p.application in excluded_apps})
            if bad_apps:
                raise InvalidRolePermissionsError(
                    "Permissions from migration-excluded applications cannot be used in V2 custom roles: "
                    + ", ".join(bad_apps)
                )

        perms_by_scope: dict[Scope, list[str]] = {}
        for p in permissions:
            scope = default_implicit_resource_service.scope_for_permission(p.permission)
            perms_by_scope.setdefault(scope, []).append(p.permission)
        if len(perms_by_scope) > 1:
            details = "; ".join(
                f"{SCOPE_DISPLAY_NAME[scope]}: {', '.join(sorted(perms))}"
                for scope, perms in sorted(perms_by_scope.items())
            )
            raise InvalidRolePermissionsError(
                f"All permissions in a role must belong to the same scope. Found: {details}"
            )

        return permissions

    @atomic
    def create(
        self,
        name: str,
        description: str,
        permission_data: list[dict],
        tenant: Tenant,
    ) -> CustomRoleV2:
        """Create a new custom role with the given attributes."""
        permissions = self._validate_and_resolve_permissions(permission_data)

        try:
            role = CustomRoleV2(
                name=name,
                description=description,
                tenant=tenant,
            )
            role.save()
            role.permissions.set(permissions)

            tuples_to_add = RoleV2.tuples_for_create(role=role, cached_permissions=permissions)

            self._replicator.replicate(
                ReplicationEvent(
                    event_type=ReplicationEventType.CREATE_CUSTOM_ROLE,
                    info={"role_uuid": str(role.uuid), "org_id": str(tenant.org_id)},
                    partition_key=PartitionKey.byEnvironment(),
                    add=tuples_to_add,
                )
            )

            logger.info(
                "Created custom role '%s' (uuid=%s) with %d permissions for tenant %s",
                role.name,
                role.uuid,
                len(permissions),
                tenant.org_id,
            )

            # Attach resolved permissions to avoid extra query in serializer
            # It solves stale prefetch cache issue, do not remove it
            role._resolved_permissions = permissions

            return role

        except ValidationError as e:
            error_msg = str(e)
            if "name" in error_msg.lower() and "already exists" in error_msg.lower():
                raise RoleAlreadyExistsError(name)
            raise
        except IntegrityError as e:
            error_msg = str(e)
            if "unique role v2 name per tenant" in error_msg.lower() or "unique" in error_msg.lower():
                raise RoleAlreadyExistsError(name)
            logger.exception("Database error creating role '%s'", name)
            raise RoleDatabaseError()

    @atomic
    def update(
        self,
        role_uuid: str,
        name: str,
        description: str,
        permission_data: list[dict],
        tenant: Tenant,
    ) -> CustomRoleV2:
        """Update an existing custom role with the given attributes."""
        permissions = self._validate_and_resolve_permissions(permission_data)

        try:
            # Lock the role for update to prevent concurrent modifications
            # Prefetch permissions to ensure they're loaded for the old state snapshot
            role = (
                CustomRoleV2.objects.filter(uuid=role_uuid, tenant=tenant)
                .select_for_update()
                .prefetch_related("permissions")
                .first()
            )
            if not role:
                raise NotFoundError("role", role_uuid)

            # Capture current state before update for outbox replication
            # The permissions are already loaded from prefetch_related above
            old_permissions = list(role.permissions.all())

            role.update(name, description)
            role.save()
            role.permissions.set(permissions)

            tuples_to_add, tuples_to_remove = RoleV2.tuples_for_update(
                role, old_permissions=old_permissions, new_permissions=permissions
            )

            self._replicator.replicate(
                ReplicationEvent(
                    event_type=ReplicationEventType.UPDATE_CUSTOM_ROLE,
                    info={"role_uuid": str(role.uuid), "org_id": str(tenant.org_id)},
                    partition_key=PartitionKey.byEnvironment(),
                    add=tuples_to_add,
                    remove=tuples_to_remove,
                )
            )
            logger.info(
                "Updated custom role '%s' (uuid=%s) with %d permissions for tenant %s",
                role.name,
                role.uuid,
                len(permissions),
                tenant.org_id,
            )

            # Attach resolved permissions to avoid extra query in serializer
            role._resolved_permissions = permissions

            return role

        except ValidationError as e:
            error_msg = str(e)
            if "name" in error_msg.lower() and "already exists" in error_msg.lower():
                raise RoleAlreadyExistsError(name)
            raise
        except IntegrityError as e:
            error_msg = str(e)
            if "unique role v2 name per tenant" in error_msg.lower() or "unique" in error_msg.lower():
                raise RoleAlreadyExistsError(name)
            logger.exception("Database error updating role '%s'", name)
            raise RoleDatabaseError()

    def list(self, params: dict) -> QuerySet:
        """Get a list of roles for the tenant, including seeded roles from the public tenant."""
        fields = params.get("fields")
        queryset = RoleV2.objects.for_tenant(self.tenant).assignable().excluding_out_of_scope_v2_roles()
        if fields:
            queryset = queryset.with_fields(fields)

        resource_type = params.get("resource_type")
        if resource_type:
            queryset = self._filter_by_resource_type(queryset, resource_type, resource_id=params.get("resource_id"))

        name = params.get("name")
        if name:
            queryset = queryset.named(name)

        permission = params.get("permission")
        if permission:
            permission_values = [p.strip() for p in permission.split(",") if p.strip()]
            if permission_values:
                queryset = queryset.filter(permissions__permission__in=permission_values).distinct()

        return queryset

    def _filter_by_resource_type(
        self, queryset: QuerySet, resource_type: str, resource_id: Optional[uuid.UUID] = None
    ) -> QuerySet:
        """Filter roles to those whose highest permission scope maps to resource_type.

        For ``resource_type=workspace``:
        - With no ``resource_id``: DEFAULT-scoped roles (both explicit-DEFAULT
          and workspace-granular, plus permissionless).
        - With ``resource_id`` the root workspace UUID: only ROOT-scoped roles.
        - With ``resource_id`` the default workspace UUID: explicit-DEFAULT +
          workspace-granular + permissionless roles.
        - With ``resource_id`` a standard workspace UUID: only workspace-granular
          roles (fallback-DEFAULT).  Explicit-DEFAULT and permissionless roles
          are excluded because they represent grants that no service checks at
          standard workspace level.

        Uses DB-level filtering with cached Permission-ID-to-Scope mappings to
        avoid loading all roles and their permissions into Python memory.
        """
        matching_scopes = scopes_for_resource_type(resource_type)
        if not matching_scopes:
            return queryset.none()

        is_standard_workspace = False
        if resource_type == "workspace":
            if resource_id is not None:
                assert self.tenant is not None
                result = resolve_workspace_scope(str(resource_id), self.tenant)
                if result is None:
                    return queryset.none()
                resolved, is_standard_workspace = result
                matching_scopes = {resolved}
            else:
                matching_scopes = {Scope.DEFAULT}

        higher_non_matching = {s for s in (set(Scope) - matching_scopes) if s > max(matching_scopes)}

        if Scope.DEFAULT not in matching_scopes:
            matching_ids = permission_scope_cache.ids_for_scopes(matching_scopes)
            queryset = queryset.filter(permissions__id__in=matching_ids).distinct()

        if higher_non_matching:
            higher_ids = permission_scope_cache.ids_for_scopes(higher_non_matching)
            if higher_ids:
                queryset = queryset.exclude(permissions__id__in=higher_ids)

        if is_standard_workspace:
            explicit_default_ids = permission_scope_cache.explicit_default_ids
            if explicit_default_ids:
                queryset = queryset.exclude(permissions__id__in=explicit_default_ids)
            queryset = queryset.filter(permissions__isnull=False).distinct()

        return queryset

    @atomic
    def bulk_delete(
        self, ids: Iterable[str | uuid.UUID], from_tenant: Optional[Tenant] = None
    ) -> builtins.list[CustomRoleV2]:
        """
        Delete custom roles with the provided UUIDs.

        Returns the roles found before removal.
        """
        # Normalize UUIDs. These should have already been validated.
        ids = {as_uuid(id) for id in ids}

        query = RoleV2.objects.filter(uuid__in=ids)

        if from_tenant is not None:
            query = query.filter(tenant=from_tenant)

        roles: list[RoleV2] = list(query)

        if len(roles) != len(ids):
            raise RolesNotFoundError(ids.difference(r.uuid for r in roles))

        non_custom_roles = [r for r in roles if r.type != RoleV2.Types.CUSTOM]

        if non_custom_roles:
            error_info = ", ".join(f"{str(r.uuid)} ({r.name!r})" for r in non_custom_roles)
            raise CustomRoleRequiredError(f"Only custom roles can be deleted, but got the following: {error_info}")

        relations_to_remove = []
        binding_pks_to_remove = []

        # We must still explicitly lock the roles to prevent conflicts with old dual-write code that does not
        # necessarily use SERIALIZABLE transactions.
        roles_to_remove = list(
            CustomRoleV2.objects.filter(pk__in=(r.pk for r in roles))
            .select_related("tenant")
            .prefetch_related("permissions")
            .select_for_update(of=["self"])
        )

        for role_binding in (
            RoleBinding.objects.filter(role__in=roles)
            .prefetch_related("role", "group_entries", "principal_entries")
            .iterator(chunk_size=1000)
        ):
            relations_to_remove.extend(role_binding.all_tuples())
            binding_pks_to_remove.append(role_binding.pk)

        for role in roles_to_remove:
            relations_to_remove.extend(RoleV2.tuples_for_delete(role=role))

        self._replicator.replicate(
            ReplicationEvent(
                event_type=ReplicationEventType.DELETE_CUSTOM_ROLE,
                info={"role_uuids": [str(r.uuid) for r in roles_to_remove]},
                partition_key=PartitionKey.byEnvironment(),
                add=[],
                remove=relations_to_remove,
            )
        )

        RoleBinding.objects.filter(pk__in=binding_pks_to_remove).delete()
        CustomRoleV2.objects.filter(pk__in=(r.pk for r in roles_to_remove)).delete()

        return roles_to_remove
