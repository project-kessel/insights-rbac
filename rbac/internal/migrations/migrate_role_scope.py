#
# Copyright 2019 Red Hat, Inc.
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

"""Handler for system defined roles."""

import logging
from typing import Optional

from management.atomic_transactions import atomic_with_retry
from management.group.model import Group
from management.permission.scope_service import ImplicitResourceService
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import RelationReplicator
from management.role.model import Role, RoleScopeState
from migration_tool.migrate_binding_scope import migrate_car_bindings, migrate_system_role_bindings_for_group

from api.cross_access.model import CrossAccountRequest

logger = logging.getLogger(__name__)


@atomic_with_retry(retries=5)
def migrate_role_scope_if_changed(v1_role: Role, replicator: Optional[RelationReplicator] = None):
    """
    Log scope change and trigger binding migration if scope has changed.

    Whether a migration needs to be performed is determined based on the role's RoleScopeState. A migration will not be
    performed if: (a) the role no longer exists or (b) the scope we would migrate to (as determined with the provided
    ImplicitResourceService) would not match the expected scopes in the RoleScopeState (likely because the
    RoleScopeState was updated by an instance with different scope settings).
    """
    if replicator is None:
        replicator = OutboxReplicator()

    # We do not need to lock to prevent updates from concurrent seeding, since both this and seeding are in
    # SERIALIZABLE transactions.
    v1_role = Role.objects.filter(pk=v1_role.pk).first()

    if v1_role is None:
        logger.info(f"System role {v1_role.name!r} concurrently deleted; not updating binding scopes.")
        return

    if not v1_role.system:
        raise ValueError(f"Expected system role, but got pk={v1_role.pk!r}")

    resource_service = ImplicitResourceService.from_settings()

    scope_state: RoleScopeState = RoleScopeState.objects.filter(role=v1_role).first()
    expected_scopes = resource_service.binding_scopes_for_role(v1_role)

    if scope_state is not None:
        if set(scope_state.computed_scopes) != set(expected_scopes):
            logger.warning(
                f"Not migrating binding scopes for system role {v1_role.name!r}; either it has changed "
                f"concurrently or the current scope settings do not match those used to compute the most recent "
                f"scopes."
            )

            return

        if scope_state.migrated:
            logger.info(f"Not migrating binding scopes for system role {v1_role.name!r} already marked as migrated.")
            return

        logger.info(f"Migrating binding scopes for system role {v1_role.name!r} to scopes: {expected_scopes}.")
    else:
        logger.info(
            f"No scope state exists for system role {v1_role.name!r}; migrating binding scopes and "
            f"creating initial scope state."
        )

        scope_state = RoleScopeState.objects.create(
            role=v1_role,
            version=0,
            computed_scopes=list(expected_scopes),
            migrated=False,
        )

    _migrate_bindings_for_scope_change(v1_role, replicator)

    # RoleScopeState is only updated in SERIALIZABLE transactions, so the state cannot have changed out from under us.
    scope_state.migrated = True
    scope_state.save(force_update=True, update_fields=["migrated"])


def _migrate_bindings_for_scope_change(v1_role: Role, replicator: RelationReplicator):
    """
    Migrate bindings for a system role when its scope has changed during seeding.

    This functions in the same way as the regular binding scope migration (see
    rbac/migration_tool/migrate_binding_scope.py), except that it only migrates groups and CARs with the provided
    role.

    Args:
        v1_role: The V1 system role whose scope has changed
    """
    if not v1_role.system:
        raise ValueError("Expected system role")

    # Find all groups (non-public tenant) that have this system role assigned
    groups_with_role = Group.objects.filter(policies__roles=v1_role).exclude(tenant__tenant_name="public").distinct()

    # Find all approved CARs that have this system role
    cars_with_role = CrossAccountRequest.objects.filter(roles=v1_role, status="approved")

    groups = list(groups_with_role)
    cars = list(cars_with_role)

    if not groups and not cars:
        logger.info("No groups or CARs found with role %s, skipping binding migration", v1_role.name)
        return

    migrated_groups = 0
    migrated_cars = 0

    # Migrate group bindings
    for group in groups:
        try:
            # Use the existing migration function to migrate bindings for this group
            # This will update all system role bindings for the group to the correct scope
            result = migrate_system_role_bindings_for_group(group, replicator)
            if result > 0:
                migrated_groups += 1
                logger.debug(
                    "Migrated bindings for group %s (uuid=%s) with system role %s",
                    group.name,
                    group.uuid,
                    v1_role.name,
                )
        except Exception:
            logger.error(
                "Failed to migrate bindings for group %s with role %s",
                group.uuid,
                v1_role.name,
                exc_info=True,
            )

    # Migrate CAR bindings
    for car in cars:
        try:
            result = migrate_car_bindings(car, replicator)
            if result > 0:
                migrated_cars += 1
                logger.debug(
                    "Migrated bindings for CAR %s with system role %s",
                    car.request_id,
                    v1_role.name,
                )
        except Exception:
            logger.error(
                "Failed to migrate bindings for CAR %s with role %s",
                car.request_id,
                v1_role.name,
                exc_info=True,
            )

    logger.info(
        "Completed binding migration for system role %s: %d groups and %d CARs migrated successfully",
        v1_role.name,
        migrated_groups,
        migrated_cars,
    )
