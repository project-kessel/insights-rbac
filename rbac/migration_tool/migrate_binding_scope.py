"""
Copyright 2025 Red Hat, Inc.

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

import logging
from typing import Optional

from django.conf import settings
from django.db import transaction
from internal.pg_notify_wait import NotifyCoordinatedReplicator
from management.atomic_transactions import atomic as atomic_serializable
from management.group.model import Group
from management.group.relation_api_dual_write_group_handler import RelationApiDualWriteGroupHandler
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import RelationReplicator, ReplicationEventType
from management.role.model import Role
from management.role.relation_api_dual_write_handler import RelationApiDualWriteHandler
from management.tenant_mapping.v2_activation import (
    TenantVersion,
    lock_tenant_version,
)

from api.cross_access.model import CrossAccountRequest
from api.cross_access.relation_api_dual_write_cross_access_handler import RelationApiDualWriteCrossAccessHandler
from api.models import Tenant

logger = logging.getLogger(__name__)


@transaction.atomic
def migrate_custom_role_bindings(raw_role: Role, replicator: RelationReplicator) -> int:
    """
    Migrate all bindings for a custom role to the correct scope.

    Uses RelationApiDualWriteHandler to delete old bindings and create new ones at correct scope.
    Groups and users assigned to the role are preserved.

    Args:
        raw_role: Custom role to migrate bindings for (must not be system role)
        replicator: Replicator to use for relation updates

    Returns: Number of bindings migrated (1 if migrated, 0 if no change)
    """
    role: Optional[Role] = (
        Role.objects.select_for_update(of=["self"]).select_related("tenant").filter(pk=raw_role.pk).first()
    )

    if role is None:
        logger.warning(f"Role vanished before it could be migrated: pk={raw_role.pk!r}")
        return 0

    tenant_version = lock_tenant_version(role.tenant)

    if tenant_version != TenantVersion.VERSION_1:
        logger.info(f"Skipping role with pk={role.pk!r}; tenant (pk={role.tenant.pk!r}) has migrated to V2")
        return 0

    if role.system:
        raise ValueError(f"migrate_custom_role_bindings called with system role {role.uuid}")

    logger.info(f"Migrating role bindings for custom role: pk={role.pk!r}")

    # Use RelationApiDualWriteHandler
    dual_write = RelationApiDualWriteHandler(role, ReplicationEventType.MIGRATE_BINDING_SCOPE, replicator=replicator)

    if not dual_write.replication_enabled():
        raise AssertionError("Expected replication to be enabled.")

    # Prepare for update: captures current bindings
    dual_write.prepare_for_update()

    # Recreate bindings at correct scope
    dual_write.replicate_new_or_updated_role(role)

    logger.info(f"Migrated custom role {role.uuid} ({role.name}) bindings to correct scope")
    return 1


@transaction.atomic
def migrate_system_role_bindings_for_group(raw_group: Group, replicator: RelationReplicator) -> int:
    """
    Migrate system role bindings for a group to correct scope.

    Uses RelationApiDualWriteGroupHandler to rebind system roles at correct scope,
    then removes the group from any wrong-scoped bindings.

    Args:
        raw_group: Group with system role assignments
        replicator: Replicator to use for relation updates

    Returns: Number of bindings cleaned up
    """
    group: Optional[Group] = (
        Group.objects.select_for_update(of=["self"]).select_related("tenant").filter(pk=raw_group.pk).first()
    )

    if group is None:
        logger.warning(f"Group vanished before it could be migrated: pk={raw_group.pk!r}")
        return 0

    tenant_version = lock_tenant_version(group.tenant)

    if tenant_version != TenantVersion.VERSION_1:
        logger.info(f"Skipping group with pk={group.pk!r}; tenant (pk={group.tenant.pk!r}) has migrated to V2")
        return 0

    logger.info(f"Migrating system role binding scopes for group: pk={group.pk}, uuid={str(group.uuid)}")

    # Get system roles assigned to this group
    system_roles = list(Role.objects.filter(policies__group=group, system=True).distinct())

    if not system_roles:
        logger.info(f"Found no system roles for group: pk={group.pk}")
        return 0

    logger.info(
        f"Found system roles for group with pk={group.pk}: "
        + ", ".join(f"{r.pk!r} ({r.name!r})" for r in system_roles)
    )

    # Use group handler to bind/rebind system roles at correct scope
    dual_write_handler = RelationApiDualWriteGroupHandler(
        group, ReplicationEventType.MIGRATE_BINDING_SCOPE, replicator=replicator
    )

    if not dual_write_handler.replication_enabled():
        raise AssertionError("Expected replication to be enabled.")

    # First remove existing bindings (including wrong-scoped ones) before re-adding correct assignments.
    dual_write_handler.generate_relations_to_remove_roles(system_roles)
    bindings_cleaned = len(dual_write_handler.relations_to_remove)
    dual_write_handler.replicate()

    # Rehydrate handler to reset relation buffers before creating correct bindings.
    dual_write_handler = RelationApiDualWriteGroupHandler(
        group, ReplicationEventType.MIGRATE_BINDING_SCOPE, replicator=replicator
    )
    dual_write_handler.generate_relations_reset_roles(system_roles)
    dual_write_handler.replicate()

    logger.info(f"Processed {bindings_cleaned} binding/group relation removals for group {group.uuid} ({group.name})")
    return bindings_cleaned


# This function can operate on V2 tenants, so we need to use a SERIALIZABLE transaction here.
@atomic_serializable
def migrate_car_bindings(raw_car: CrossAccountRequest, replicator: RelationReplicator):
    """Migrate cross-account request bindings to split mixed scope roles."""
    car: Optional[CrossAccountRequest] = CrossAccountRequest.objects.filter(pk=raw_car.pk).select_for_update().first()

    if car is None:
        logger.warning(f"Cross-account request vanished before it could be migrated: pk={raw_car.pk!r}")
        return 0

    if car.status != "approved":
        return 0

    # We do not need to check for V1-writability here. RelationApiDualWriteCrossAccessHandler will handle V2 tenants.

    logger.info(f"Migrating roles for cross-account request: pk={car.pk!r}")

    roles = list(car.roles.all())

    if len(roles) == 0:
        logger.info(f"Found no roles for cross-account request, pk={car.pk!r}")
        return 0

    logger.info(
        f"Found roles for cross-account request with pk={car.pk!r}: "
        + ", ".join(f"{r.pk!r} ({r.name!r})" for r in roles)
    )

    dual_write_handler = RelationApiDualWriteCrossAccessHandler(
        cross_account_request=car,
        event_type=ReplicationEventType.MIGRATE_BINDING_SCOPE,
        replicator=replicator,
    )

    if not dual_write_handler.replication_enabled():
        raise AssertionError("Expected replication to be enabled.")

    dual_write_handler.generate_relations_to_remove_roles(roles)
    bindings_cleaned = len(dual_write_handler.relations_to_remove)
    dual_write_handler.replicate()

    dual_write_handler = RelationApiDualWriteCrossAccessHandler(
        cross_account_request=car,
        event_type=ReplicationEventType.MIGRATE_BINDING_SCOPE,
        replicator=replicator,
    )

    dual_write_handler.generate_relations_to_add_roles(roles)
    dual_write_handler.replicate()

    logger.info(f"Processed {bindings_cleaned} binding/relation removals for CAR {str(car.request_id)}")
    return bindings_cleaned


def _do_migrate_all_group_custom_roles(
    replicator: RelationReplicator = OutboxReplicator(),
    tenant: Optional[Tenant] = None,
) -> tuple[int, int]:
    # Include all custom roles with access (permissions), even if they have no policies (groups) assigned yet.
    # This ensures v2 roles and bindings are created at the correct scope for roles that were created
    # before replication was enabled or before scope configuration was corrected.
    # Roles without access won't have any v2 models created (nothing to migrate).

    custom_roles = Role.objects.filter(system=False).distinct().order_by("pk")
    if tenant:
        custom_roles = custom_roles.filter(tenant=tenant)

    total_custom_roles = custom_roles.count()
    logger.info(f"Found {total_custom_roles} custom roles to migrate")

    custom_roles_checked = 0
    custom_roles_migrated = 0

    for raw_role in custom_roles.iterator():
        custom_roles_checked += 1

        try:
            migrated = migrate_custom_role_bindings(raw_role, replicator)
            custom_roles_migrated += migrated
        except Exception as e:
            logger.error(f"Failed to migrate custom role {raw_role.uuid}: {e}", exc_info=True)
            continue

        if custom_roles_checked % 10 == 0 or custom_roles_checked == total_custom_roles:
            logger.info(
                f"Progress (custom roles): processed {custom_roles_checked}/{total_custom_roles}, "
                f"migrated {custom_roles_migrated}"
            )

    logger.info(f"Completed custom role migration: {custom_roles_checked} checked, {custom_roles_migrated} migrated")

    return custom_roles_checked, custom_roles_migrated


def _do_migrate_all_group_system_roles(
    replicator: RelationReplicator = OutboxReplicator(),
    tenant: Optional[Tenant] = None,
) -> tuple[int, int]:
    # Get all groups that have system roles
    groups_with_system_roles = (
        Group.objects.filter(policies__roles__system=True)
        .exclude(tenant__tenant_name="public")
        .distinct()
        .order_by("pk")
    )
    if tenant:
        groups_with_system_roles = groups_with_system_roles.filter(tenant=tenant)

    total_groups = groups_with_system_roles.count()
    logger.info(f"Found {total_groups} groups with system roles to migrate")

    groups_checked = 0
    groups_migrated = 0

    for raw_group in groups_with_system_roles.iterator():
        groups_checked += 1

        try:
            migrated = migrate_system_role_bindings_for_group(raw_group, replicator)
            if migrated > 0:
                groups_migrated += 1
        except Exception as e:
            logger.error(f"Failed to migrate system roles for group {raw_group.uuid}: {e}", exc_info=True)
            continue

        if groups_checked % 10 == 0 or groups_checked == total_groups:
            logger.info(f"Progress (groups): processed {groups_checked}/{total_groups}, migrated {groups_migrated}")

    logger.info(
        f"Completed system role migration via groups: {groups_checked} groups checked, {groups_migrated} migrated"
    )

    return groups_checked, groups_migrated


def _do_migrate_all_cars(
    replicator: RelationReplicator = OutboxReplicator(),
    tenant: Optional[Tenant] = None,
) -> tuple[int, int]:
    cars = CrossAccountRequest.objects.filter(status="approved")

    if tenant:
        cars = cars.filter(target_org=tenant.org_id)

    total_cars = cars.count()
    logger.info(f"Found {total_cars} cross-account requests to migrate.")

    cars_checked = 0
    cars_migrated = 0

    for raw_car in cars.iterator():
        cars_checked += 1

        try:
            migrated = migrate_car_bindings(raw_car, replicator=replicator)

            if migrated > 0:
                cars_migrated += 1
        except Exception as e:
            logger.error(
                f"Failed to migrate roles for cross-account request with pk={raw_car.pk!r}: {e}", exc_info=True
            )
            continue

        if cars_checked % 10 == 0 or cars_checked == total_cars:
            logger.info(f"Progress (CARs): processed {cars_checked}/{total_cars}, migrated {cars_migrated}")

    logger.info(f"Completed cross-account request migration: {cars_checked} CARs checked, {cars_migrated} migrated")

    return cars_checked, cars_migrated


custom_role_source = "custom_roles"
system_role_source = "system_roles"
car_source = "cars"

_all_sources = {custom_role_source, system_role_source, car_source}


def migrate_all_role_bindings(
    replicator: RelationReplicator | None = None,
    tenant: Optional[Tenant] = None,
    sources: Optional[set[str]] = None,
):
    """
    Migrate all role bindings to correct scope.

    - Custom roles: Migrated individually using RelationApiDualWriteHandler
    - System roles: Migrated per group using RelationApiDualWriteGroupHandler

    Args:
        replicator: Replicator to use for relation updates. Defaults to OutboxReplicator.
        tenant: Optional tenant to filter roles and groups. If None, migrates all tenants.
        sources: a subset of "custom_roles", "system_roles", and "cars" to migrate (defaulting to everything)

    Returns: Tuple of (items_checked, items_migrated)
    """
    if replicator is None:
        replicator = NotifyCoordinatedReplicator(
            OutboxReplicator(),
            event_type=ReplicationEventType.MIGRATE_BINDING_SCOPE,
        )

    if tenant:
        if tenant.tenant_name == "public":
            raise ValueError("Cannot migrate binding scope for the public tenant.")

        if tenant.org_id is None:
            raise ValueError("Cannot migrate binding scope for a tenant without an org_id.")

    # We check for instance equality here to mirror RelationApiDualWriteSubjectHandler.replication_enabled.
    if settings.REPLICATION_TO_RELATION_ENABLED is not True:
        raise RuntimeError("Replication must be enabled while migrating binding scope.")

    if sources is None:
        sources = _all_sources

    sources = set(sources)

    if not sources.issubset(_all_sources):
        invalid = sources - _all_sources
        raise ValueError(f"Unexpected sources: {invalid!r}")

    tenant_info = f" for tenant {tenant.org_id}" if tenant else ""
    logger.info(f"Starting binding scope migration{tenant_info} with the following sources: {sources}")
    logger.info(f"ROOT_SCOPE_PERMISSIONS: {settings.ROOT_SCOPE_PERMISSIONS}")
    logger.info(f"TENANT_SCOPE_PERMISSIONS: {settings.TENANT_SCOPE_PERMISSIONS}")
    logger.info(f"DEFAULT_SCOPE_PERMISSIONS: {settings.DEFAULT_SCOPE_PERMISSIONS}")

    if custom_role_source in sources:
        custom_roles_checked, custom_roles_migrated = _do_migrate_all_group_custom_roles(
            replicator=replicator, tenant=tenant
        )
    else:
        custom_roles_checked = 0
        custom_roles_migrated = 0

    if system_role_source in sources:
        groups_checked, groups_migrated = _do_migrate_all_group_system_roles(replicator=replicator, tenant=tenant)
    else:
        groups_checked = 0
        groups_migrated = 0

    if car_source in sources:
        cars_checked, cars_migrated = _do_migrate_all_cars(replicator=replicator, tenant=tenant)
    else:
        cars_checked = 0
        cars_migrated = 0

    total_checked = custom_roles_checked + groups_checked + cars_checked
    total_migrated = custom_roles_migrated + groups_migrated + cars_migrated

    logger.info(
        f"Completed binding scope migration{tenant_info}: "
        f"{custom_roles_migrated} custom roles + {groups_migrated} groups + {cars_migrated} CARs migrated"
    )

    return total_checked, total_migrated
