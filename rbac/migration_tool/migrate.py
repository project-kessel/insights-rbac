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

import logging
from typing import Union

from django.db import transaction
from management.group.relation_api_dual_write_group_handler import RelationApiDualWriteGroupHandler
from management.models import Group
from management.principal.model import Principal
from management.relation_replicator.logging_replicator import LoggingReplicator
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import (
    RelationReplicator,
    ReplicationEventType,
)
from management.relation_replicator.relations_api_replicator import RelationsApiReplicator
from management.role.model import Role
from management.role.relation_api_dual_write_handler import RelationApiDualWriteHandler

from api.cross_access.relation_api_dual_write_cross_access_handler import RelationApiDualWriteCrossAccessHandler
from api.models import CrossAccountRequest, Tenant

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


def migrate_groups_for_tenant(tenant: Tenant, replicator: RelationReplicator):
    """Generate user relationships and system role assignments for groups in a tenant."""
    groups = tenant.group_set.only("pk").values("pk")
    for group in groups:
        # The migrator deals with concurrency control.
        # We need an atomic block because the select_for_update is used in the dual write handler,
        # and the group must be locked to add principals to the groups.
        # NOTE: The lock on the group is not necessary when adding system roles to the group,
        # as the binding mappings are locked during this process to ensure concurrency control.
        # Start of transaction for group operations
        with transaction.atomic():
            # Requery the group with a lock
            group = Group.objects.select_for_update().get(pk=group["pk"])
            principals: list[Principal] = []
            system_roles: list[Role] = []
            if not group.platform_default:
                principals = group.principals.all()
            if group.system is False and group.admin_default is False:
                system_roles = group.roles().public_tenant_only()
            if any(True for _ in system_roles) or any(True for _ in principals):
                dual_write_handler = RelationApiDualWriteGroupHandler(
                    group, ReplicationEventType.MIGRATE_TENANT_GROUPS, replicator=replicator
                )
                # this operation requires lock on group as well as in view,
                # more details in GroupViewSet#get_queryset method which is used to add principals.
                dual_write_handler.generate_relations_to_add_principals(principals)
                # lock on group is not required to add system role, only binding mappings which is included in
                # dual_write_handler
                # `reset_mapping_for_roles` is used because it is idempotent,
                # HOWEVER it is also potentially destructive. This is done because we are sure
                # there is currently only one source for roles to the same group and resource
                # which does not duplicate roles (group policy).
                dual_write_handler.generate_relations_reset_roles(system_roles)
                dual_write_handler.replicate()
        # End of transaction for group operations, locks are released


def migrate_roles_for_tenant(tenant, exclude_apps, replicator):
    """Migrate all roles for a given tenant."""
    roles = tenant.role_set.only("pk")
    if exclude_apps:
        roles = roles.exclude(access__permission__application__in=exclude_apps)
    role_pks = roles.values_list("pk", flat=True)
    for role in role_pks:
        # The migrator deals with concurrency control and roles needs to be locked.
        with transaction.atomic():
            # Requery and lock role
            role = Role.objects.select_for_update().get(pk=role)
            logger.info(f"Migrating role: {role.name} with UUID {role.uuid}.")
            dual_write_handler = RelationApiDualWriteHandler(
                role, ReplicationEventType.MIGRATE_CUSTOM_ROLE, replicator
            )
            dual_write_handler.prepare_for_update()
            dual_write_handler.replicate_new_or_updated_role(role)
        # End of transaction, locks on role is released.
        logger.info(f"Migration completed for role: {role.name} with UUID {role.uuid}.")

    logger.info(f"Migrated {roles.count()} roles for tenant: {tenant.org_id}")


def migrate_data_for_tenant(tenant: Tenant, exclude_apps: list, replicator: RelationReplicator, skip_roles: bool):
    """Migrate all data for a given tenant."""
    logger.info("Migrating relations of group and user.")

    migrate_groups_for_tenant(tenant, replicator)

    logger.info("Finished migrating relations of group and user.")

    if skip_roles:
        logger.info("Skipping migrating roles.")
    else:
        migrate_roles_for_tenant(tenant, exclude_apps, replicator)

    logger.info("Migrating relations of cross account requests.")
    migrate_cross_account_requests(tenant, replicator)
    logger.info("Finished relations of cross account requests.")


def migrate_cross_account_requests(tenant: Tenant, replicator: RelationReplicator):
    """Migrate approved account requests."""
    cross_account_requests = CrossAccountRequest.objects.filter(status="approved", target_org=tenant.org_id)
    for cross_account_request in cross_account_requests:
        # The migrator deals with concurrency control.
        # We need an atomic block because the select_for_update is used in the dual write handler,
        # and cross account request must be locked to add roles.
        # Start of transaction for approved cross account request and "add roles" operation
        with transaction.atomic():
            # Lock cross account request
            cross_account_request = CrossAccountRequest.objects.select_for_update().get(pk=cross_account_request.pk)
            cross_account_roles = cross_account_request.roles.all()
            if any(True for _ in cross_account_roles):
                dual_write_handler = RelationApiDualWriteCrossAccessHandler(
                    cross_account_request, ReplicationEventType.MIGRATE_CROSS_ACCOUNT_REQUEST, replicator
                )
                # This operation requires lock on cross account request as is done
                # in CrossAccountRequestViewSet#get_queryset
                # This also locks binding mapping if exists for passed system roles.
                dual_write_handler.generate_relations_to_add_roles(cross_account_request.roles.all())
                dual_write_handler.replicate()
        # End of transaction for approved cross account request and its add role operation
        # Locks on cross account request and eventually on default workspace are released.
        # Default workspace is locked when related binding mapping did not exist yet
        # (Considering the position of this algorithm,the binding mappings for system roles should already exist,
        # as they are tied to the system roles.)


def migrate_data(
    exclude_apps: list = [],
    orgs: list = [],
    write_relationships: Union[str, RelationReplicator] = "False",
    skip_roles: bool = False,
):
    """Migrate all data for all tenants."""
    count = 0
    tenants = Tenant.objects.filter(ready=True).exclude(tenant_name="public")
    replicator = _get_replicator(write_relationships)
    if orgs:
        tenants = tenants.filter(org_id__in=orgs)
    total = tenants.count()
    for tenant in tenants.iterator():
        if tenant.org_id is None:
            logger.warning(f"Not migrating tenant, no org id: pk={tenant.id}")
            continue
        else:
            logger.info(f"Migrating data for tenant: {tenant.org_id}")

        try:
            migrate_data_for_tenant(tenant, exclude_apps, replicator, skip_roles)
        except Exception as e:
            logger.error(f"Failed to migrate data for tenant: {tenant.org_id}. Error: {e}")
            raise e
        count += 1
        logger.info(f"Finished migrating data for tenant: {tenant.org_id}. {count} of {total} tenants completed")
    logger.info("Finished migrating data for all tenants")


def _get_replicator(write_relationships: Union[str, RelationReplicator]) -> RelationReplicator:
    if isinstance(write_relationships, RelationReplicator):
        return write_relationships

    option = write_relationships.lower()

    if option == "true" or option == "relations-api":
        return RelationsApiReplicator()

    if option == "outbox":
        return OutboxReplicator()

    return LoggingReplicator()
