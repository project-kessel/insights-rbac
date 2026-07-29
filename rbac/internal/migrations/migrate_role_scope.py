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

import dataclasses
import itertools
import logging
from typing import Optional

from django.conf import settings

from management.atomic_transactions import atomic_block, atomic_with_retry
from management.group.model import Group
from management.permission.scope_service import ImplicitResourceService
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import RelationReplicator
from management.role.model import Role, RoleScopeState
from migration_tool.migrate_binding_scope import migrate_car_bindings, migrate_system_role_bindings_for_group

from api.cross_access.model import CrossAccountRequest

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class _CheckResult:
    can_migrate: bool
    scope_state: Optional[RoleScopeState]

    def __post_init__(self):
        if self.can_migrate != (self.scope_state is not None):
            raise TypeError(f"Expected a scope state to be present iff can_migrate is true.")

    @classmethod
    def failed(cls):
        return _CheckResult(can_migrate=False, scope_state=None)


def _check_migration(
    role: Role, resource_service: ImplicitResourceService, expected_version: Optional[int]
) -> _CheckResult:
    """
    Return whether we can continue to migrate the scope for the provided role.

    This should be run in the same SERIALIZABLE transaction as the actual migration (otherwise, the results of the
    check will be out of date).
    """
    # Re-fetch the role to operate on its current state. When this function runs inside
    # the SERIALIZABLE atomic_block(), this refetch is the authoritative concurrency check.
    role = Role.objects.filter(pk=role.pk).first()

    if role is None:
        logger.info(f"System role concurrently deleted; not updating binding scopes.")
        return _CheckResult.failed()

    if not role.system:
        raise AssertionError(f"System role became a non-system role: {role.name!r}")

    scope_state: RoleScopeState = RoleScopeState.objects.filter(role=role).first()
    expected_scopes = resource_service.binding_scopes_for_role(role)

    if scope_state is None:
        logger.warning(
            f"Not migrating binding scopes for system role {role.name!r} without a RoleScopeState. "
            f"If migration is necessary, ensure that seeding has run for the role."
        )

        return _CheckResult.failed()

    if set(scope_state.computed_scopes) != set(expected_scopes):
        logger.warning(
            f"Not migrating binding scopes for system role {role.name!r}; either it has changed "
            f"concurrently or the current scope settings do not match those used to compute the most recent "
            f"scopes."
        )

        return _CheckResult.failed()

    if scope_state.migrated:
        logger.info(f"Not migrating binding scopes for system role {role.name!r} already marked as migrated.")
        return _CheckResult.failed()

    if expected_version is not None:
        if scope_state.version != expected_version:
            logger.info(
                f"Not migrating binding scopes for system role {role.name!r} whose scope state version has changed."
            )

            return _CheckResult.failed()

    return _CheckResult(can_migrate=True, scope_state=scope_state)


@dataclasses.dataclass(frozen=True)
class _MigrateContext:
    """
    A bundle of all of the resources required to migrate a role's scope.

    This isn't a particularly meaningful class, but it's clearer than passing around everything separately,
    and it allows us to do type checking in exactly one place.
    """

    role: Role
    replicator: RelationReplicator
    resource_service: ImplicitResourceService
    expected_state_version: int

    def __post_init__(self):
        if not self.role.system:
            raise ValueError("Expected system role")

        if not isinstance(self.expected_state_version, int):
            raise TypeError(f"Expected expected_state_version to be an int, but got: {self.expected_state_version!r}")

    def assert_can_migrate(self):
        """
        Raise an exception if the role to migrate has changed concurrently such that it can no longer be migrated.

        This must be called at the start of each transaction in order to re-establish the invariants that permit us
        to run the migration. Each such transaction must also be SERIALIZABLE.
        """
        if not _check_migration(
            role=self.role, resource_service=self.resource_service, expected_version=self.expected_state_version
        ).can_migrate:
            raise RuntimeError(f"Cannot continue migrating changed role {self.role.name!r}")


def migrate_role_scope_if_changed(v1_role: Role, replicator: Optional[RelationReplicator] = None):
    """
    Log scope change and trigger binding migration if scope has changed.

    Whether a migration needs to be performed is determined based on the role's RoleScopeState. A migration will not be
    performed if: (a) the role no longer exists or (b) the scope we would migrate to (as determined with the provided
    ImplicitResourceService) would not match the expected scopes in the RoleScopeState (likely because the
    RoleScopeState was updated by an instance with different scope settings).
    """
    if not settings.REPLICATION_TO_RELATION_ENABLED:
        logger.info("Not migrating role scope when replication is disabled.")
        return

    if replicator is None:
        replicator = OutboxReplicator()

    # Early exit optimization outside the transaction. The authoritative concurrency
    # check happens inside atomic_block() below via _check_migration().
    v1_role = Role.objects.filter(pk=v1_role.pk).first()

    if v1_role is None:
        logger.info("System role concurrently deleted; not updating binding scopes.")
        return

    if not v1_role.system:
        raise ValueError(f"Expected system role, but got pk={v1_role.pk!r}")

    resource_service = ImplicitResourceService.from_settings()

    with atomic_block():
        initial_check = _check_migration(role=v1_role, resource_service=resource_service, expected_version=None)

        if not initial_check.can_migrate:
            logger.info(f"Determined that we cannot migrate role {v1_role.name} early; not migrating.")
            return

    # Convince mypy.
    assert initial_check.scope_state is not None

    logger.info(
        f"Migrating binding scopes for system role {v1_role.name!r} "
        f"(scopes: {initial_check.scope_state.computed_scopes}, state version: {initial_check.scope_state.version})."
    )

    context = _MigrateContext(
        role=v1_role,
        replicator=replicator,
        resource_service=resource_service,
        expected_state_version=initial_check.scope_state.version,
    )

    try:
        _migrate_bindings_for_scope_change(context)
    except Exception:
        # We exit here and leave migrated unchanged, thus ensuring that we will try again if the migration is re-run
        # (unless migrated is set to true by another instance of the job).
        logger.error(f"Failed to migrate bindings for role {v1_role.name!r}", exc_info=True)
        return

    # If it's still the case that nothing has changed out from under us, we can update the scope state to show that
    # the role was migrated to the relevant scopes.
    with atomic_block():
        final_check = _check_migration(
            role=v1_role, resource_service=resource_service, expected_version=initial_check.scope_state.version
        )

        if final_check.can_migrate:
            # Convince mypy.
            assert final_check.scope_state is not None

            final_check.scope_state.migrated = True
            final_check.scope_state.save(force_update=True, update_fields=["migrated"])

            logger.info(f"Successfully migrated binding scopes for system role {v1_role.name!r}.")
        else:
            logger.info(f"Aborted migrating scopes for system role {v1_role.name!r} immediately before committing.")


def _migrate_bindings_for_scope_change(context: _MigrateContext):
    """
    Migrate bindings for a system role when its scope has changed during seeding.

    This functions in the same way as the regular binding scope migration (see
    rbac/migration_tool/migrate_binding_scope.py), except that it only migrates groups and CARs with the provided
    role.
    """
    role = context.role

    # Find all groups and CARs that have this system role assigned. (This does not need to be in a SERIALIZABLE
    # transaction, since the migration will always leave the group/CAR in a valid state, regardless of if it still
    # has the role. We assume all roles assigned after this point will have the correct scope, so we don't need to
    # worry about including them.)
    groups = list(Group.objects.filter(policies__roles=role).exclude(tenant__tenant_name="public").distinct())
    cars = list(CrossAccountRequest.objects.filter(roles=role, status="approved"))

    if not groups and not cars:
        logger.info("No groups or CARs found with role %s, skipping binding migration", role.name)
        return

    migrated_groups = 0
    migrated_cars = 0

    for group_batch in itertools.batched(groups, 20):
        migrated_groups += _migrate_group_batch(context=context, groups=list(group_batch))

    for car_batch in itertools.batched(cars, 20):
        migrated_cars += _migrate_car_batch(context=context, cars=list(car_batch))

    logger.info(
        "Completed binding migration for system role %s: %d groups and %d CARs migrated successfully",
        role.name,
        migrated_groups,
        migrated_cars,
    )


@atomic_with_retry(retries=5)
def _migrate_group_batch(context: _MigrateContext, groups: list[Group]) -> int:
    context.assert_can_migrate()

    migrated_groups = 0

    for group in groups:
        result = migrate_system_role_bindings_for_group(group, context.replicator)

        if result > 0:
            migrated_groups += 1
            logger.debug(
                "Migrated bindings for group %s (uuid=%s) with system role %s",
                group.name,
                group.uuid,
                context.role.name,
            )

    return migrated_groups


@atomic_with_retry(retries=5)
def _migrate_car_batch(context: _MigrateContext, cars: list[CrossAccountRequest]) -> int:
    context.assert_can_migrate()

    migrated_cars = 0

    for car in cars:
        result = migrate_car_bindings(car, context.replicator)

        if result > 0:
            migrated_cars += 1
            logger.debug(
                "Migrated bindings for CAR %s with system role %s",
                car.request_id,
                context.role.name,
            )

    return migrated_cars
