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

"""Handler for system defined group."""

import logging
from typing import Optional, Tuple, Union
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.db.models.query import QuerySet
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext as _
from management.group.model import Group
from management.group.platform import GlobalPolicyIdService
from management.group.relation_api_dual_write_group_handler import (
    RelationApiDualWriteGroupHandler,
)
from management.notifications.notification_handlers import (
    group_flag_change_notification_handler,
    group_role_change_notification_handler,
)
from management.policy.model import Policy
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import ReplicationEventType
from management.role.model import Role
from management.role_binding.service import RoleBindingService
from management.tenant_service.v2 import V2TenantBootstrapService, lock_tenant_for_bootstrap
from management.utils import clear_pk
from rest_framework import serializers

from api.models import Tenant

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


def seed_group() -> Tuple[Group, Group]:
    """Create or update default group."""
    try:
        public_tenant = Tenant.objects.get(tenant_name="public")
        with transaction.atomic():
            # 'Default access' group
            name = "Default access"
            group_description = (
                "This group contains the roles that all users inherit by default. "
                "Adding or removing roles in this group will affect permissions for all users in your organization."
            )

            group, _ = Group.objects.get_or_create(
                platform_default=True,
                defaults={"description": group_description, "name": name, "system": True},
                tenant=public_tenant,
            )

            platform_roles = Role.objects.filter(platform_default=True).public_tenant_only()
            update_group_roles(group, platform_roles, public_tenant)
            logger.info("Finished seeding default group %s.", name)

            # 'Default admin access' group
            admin_name = "Default admin access"
            admin_group_description = (
                "This group contains the roles that all org admin users inherit by default. "
                "Adding or removing roles in this group will affect permissions for all org admin users in your org."
            )
            admin_group, _ = Group.objects.get_or_create(
                admin_default=True,
                defaults={"description": admin_group_description, "name": admin_name, "system": True},
                tenant=public_tenant,
            )
            admin_roles = Role.objects.filter(admin_default=True).public_tenant_only()
            update_group_roles(admin_group, admin_roles, public_tenant)
            logger.info("Finished seeding default org admin group %s.", name)

        return group, admin_group
    finally:
        # Do this after updating the groups to ensure that any subsequent calls receive the correct value.
        GlobalPolicyIdService.clear_shared()


def set_system_flag_before_update(group: Group, tenant, user) -> Optional[Group]:
    """Update system flag on default groups."""
    if group.system:
        group = clone_default_group_in_public_schema(group, tenant)  # type: ignore
        group_flag_change_notification_handler(user, group)
    return group


def clone_default_group_in_public_schema(group, tenant) -> Optional[Group]:
    """Clone the default group for a tenant into the public schema."""
    bootstrapped_tenant = None
    if settings.V2_BOOTSTRAP_TENANT:
        tenant_bootstrap_service = V2TenantBootstrapService(OutboxReplicator())
        bootstrapped_tenant = tenant_bootstrap_service.bootstrap_tenant(tenant)

        # This prevents concurrent bootstrapping (which creating a custom default group would interfere with) and
        # deletion of an existing custom default group.
        lock = lock_tenant_for_bootstrap(tenant)

        if lock.custom_default_group is not None:
            raise ValueError(f"Cannot create custom default group when one already exists for tenant: {tenant}.")

        # Mapping is always present with V2
        assert lock.tenant_mapping is not None
        group_uuid = lock.tenant_mapping.default_group_uuid
    else:
        group_uuid = uuid4()

    public_tenant = Tenant.objects.get(tenant_name="public")
    tenant_default_policy = group.policies.get(system=True)
    group.name = "Custom default access"
    group.system = False
    group.tenant = tenant
    group.uuid = group_uuid
    clear_pk(group)
    clear_pk(tenant_default_policy)
    tenant_default_policy.uuid = uuid4()
    tenant_default_policy.name = "System Policy for Group {}".format(group.uuid)
    tenant_default_policy.tenant = tenant
    if Group.objects.filter(name=group.name, platform_default=group.platform_default, tenant=tenant):
        # TODO: returning none can break other code
        return None
    public_default_roles = Role.objects.filter(platform_default=True, tenant=public_tenant)

    group.save()
    tenant_default_policy.group = group
    tenant_default_policy.save()
    tenant_default_policy.roles.set(public_default_roles)

    if bootstrapped_tenant:
        dual_write_handler = RelationApiDualWriteGroupHandler(group, ReplicationEventType.CUSTOMIZE_DEFAULT_GROUP)
        dual_write_handler.generate_relations_reset_roles(
            public_default_roles, remove_default_access_from=bootstrapped_tenant.mapping
        )
        dual_write_handler.replicate()

        # Delete USER default role bindings since tenant now has custom default group
        role_binding_service = RoleBindingService(tenant=tenant)
        role_binding_service.delete_user_default_bindings()

    return group


def add_roles(group, roles_or_role_ids, tenant, user=None):
    """Process list of roles and add them to the group."""
    roles = _roles_by_query_or_ids(roles_or_role_ids, tenant)
    group_name = group.name
    group, created = Group.objects.get_or_create(name=group_name, tenant=tenant)
    if created:
        logger.info(f"Created new group {group_name} for tenant {tenant.org_id}.")
    else:
        if tenant.tenant_name == "public":
            logger.info(f"Group {group_name} already exists for public tenant.")
        else:
            logger.info(f"Group {group_name} already exists for tenant {tenant.org_id}.")

    # check if role exists for the specific tenant
    for role in roles:
        role_object = get_object_or_404(Role, uuid=role.uuid)
        if role_object.tenant != tenant and role_object.tenant.tenant_name != "public":
            key = "roles"
            message = f"Role with id {role} does not exist."
            raise serializers.ValidationError({key: _(message)})

    system_policy_name = "System Policy for Group {}".format(group.uuid)
    system_policy, system_policy_created = Policy.objects.update_or_create(
        system=True, group=group, name=system_policy_name, defaults={"tenant": tenant}
    )

    if system_policy_created:
        logger.info(f"Created new system policy for tenant {tenant.org_id}.")

    system_roles = roles.filter(tenant=Tenant.objects.get(tenant_name="public"))

    # Custom roles are locked to prevent resources from being added/removed concurrently,
    # in the case that the Roles had _no_ resources specified to begin with.
    # This should not be necessary for system roles.
    custom_roles = roles.filter(tenant=tenant).select_for_update()

    added_roles: list[Role] = []

    for role in [*system_roles, *custom_roles]:
        # Only Organization administrators are allowed to add the role with RBAC permission
        # higher than "read" into a group.
        for access in role.access.all():
            if (
                access.permission_application() == "rbac"
                and access.permission.verb != "read"
                and user
                and not user.admin
            ):
                key = "add-roles"
                message = (
                    "Non org admin users are not allowed to add RBAC role with higher than 'read' permission "
                    "into groups."
                )
                raise serializers.ValidationError({key: _(message)})

        # Only add the role if it was not attached
        if system_policy.roles.filter(pk=role.pk).exists():
            logger.debug(
                "Skipped adding role to group: role_id=%s, group_id=%s (role already exists in group)",
                getattr(role, "pk", repr(role)),
                getattr(system_policy, "pk", repr(system_policy)),
            )
            continue

        system_policy.roles.add(role)
        group_role_change_notification_handler(user, group, role, "added")
        added_roles.append(role)

    if not added_roles:
        return
    if tenant.tenant_name != "public":
        dual_write_handler = RelationApiDualWriteGroupHandler(group, ReplicationEventType.ASSIGN_ROLE)
        dual_write_handler.generate_relations_reset_roles(added_roles)
        dual_write_handler.replicate()


def remove_roles(group, roles_or_role_ids, tenant, user=None):
    """Process list of roles and remove them from the group."""
    roles = _roles_by_query_or_ids(roles_or_role_ids, tenant)
    group = Group.objects.get(name=group.name, tenant=tenant)
    system_roles = roles.filter(tenant=Tenant.objects.get(tenant_name="public"))

    # Custom roles are locked to prevent resources from being added/removed concurrently,
    # in the case that the Roles had _no_ resources specified to begin with.
    # This should not be necessary for system roles.
    custom_roles = roles.filter(tenant=tenant).select_for_update()

    removed_roles: list[Role] = []

    for policy in group.policies.all():
        for role in [*system_roles, *custom_roles]:
            # Only remove the role if it was attached
            if policy.roles.filter(pk=role.pk).exists():
                policy.roles.remove(role)
                logger.info(f"Removing role {role} from group {group.name} for tenant {tenant.org_id}.")

                # Send notifications
                group_role_change_notification_handler(user, group, role, "removed")
                removed_roles.append(role)

    if tenant.tenant_name != "public":
        dual_write_handler = RelationApiDualWriteGroupHandler(group, ReplicationEventType.UNASSIGN_ROLE)
        dual_write_handler.generate_relations_to_remove_roles(removed_roles)
        dual_write_handler.replicate()


def update_group_roles(group, roleset, tenant):
    """Update group roles based on roleset."""
    # Add roles to group, which will only add roles in roleset but not in group.
    add_roles(group, roleset, tenant)

    # Remove roles not in roleset from group.
    role_ids = list(roleset.values_list("uuid", flat=True))
    roles_to_remove = group.roles().exclude(uuid__in=role_ids)
    if roles_to_remove.exists():
        remove_roles(group, roles_to_remove, tenant)


def _roles_by_query_or_ids(roles_or_role_ids: Union[QuerySet[Role], list[str]], tenant: Tenant) -> QuerySet[Role]:
    if not isinstance(roles_or_role_ids, QuerySet):
        # If given an iterable of UUIDs, get the corresponding objects
        filtered_roles = Role.objects.filter(uuid__in=roles_or_role_ids)
        if filtered_roles.count() == 0:
            raise Http404("This role is nonexistent/nonvalid and cannot be added to the group")
        return filtered_roles
    else:
        # Given a queryset, so because it may not be efficient (e.g. query on non indexed field)
        # keep prior behavior of querying once to get names, then use names (indexed) as base query
        # for further queries.
        # It MAY be faster to avoid this extra query, but this maintains prior behavior.
        role_names = list(roles_or_role_ids.values_list("name", flat=True))
        filtered_roles_name = Role.objects.filter(name__in=role_names, tenant_id=tenant.id)
        if not filtered_roles_name.exists():
            logger.warning("The roles may be nonexistent/nonvalid.")
        return filtered_roles_name
