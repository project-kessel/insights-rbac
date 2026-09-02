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
"""Service layer for GroupV2."""

from __future__ import annotations

import logging
from typing import List

from django.db import IntegrityError
from django.db.models import Count, ProtectedError
from management.group.model import Group
from management.group.relation_api_dual_write_group_handler import (
    RelationApiDualWriteGroupHandler,
)
from management.principal.model import Principal
from management.relation_replicator.relation_replicator import ReplicationEventType
from management.v2_filters import v2_name_filter

from api.models import Tenant

logger = logging.getLogger(__name__)


class GroupV2Service:
    """Service for V2 group operations."""

    def __init__(self, tenant: Tenant):
        """Initialize service with tenant context."""
        self.tenant = tenant

    @staticmethod
    def _check_not_system_group(group: Group, action: str):
        if group.system or group.admin_default or group.platform_default:
            raise PermissionError(f"System groups cannot be {action}.")

    def list(self, validated_params: dict):
        """List groups with optional filtering and default ordering."""
        queryset = (
            Group.objects.filter(tenant=self.tenant)
            .annotate(principal_count_annotation=Count("principals"))
            .order_by("name")
        )

        name = validated_params.get("name")
        if name:
            queryset = v2_name_filter(queryset, name, field="name")

        return queryset

    def create(self, name: str, description: str = "") -> Group:
        """Create a new group."""
        try:
            group = Group.objects.create(
                name=name,
                description=description,
                tenant=self.tenant,
            )
        except IntegrityError as e:
            if "unique group name per tenant" in str(e):
                raise ValueError(f"Group with name '{name}' already exists in this organization.")
            raise

        return group

    def update(self, group: Group, name: str, description: str) -> Group:
        """Update an existing group."""
        self._check_not_system_group(group, "modified")

        try:
            group.name = name
            group.description = description
            group.save()
        except IntegrityError as e:
            if "unique group name per tenant" in str(e):
                raise ValueError(f"Group with name '{name}' already exists in this organization.")
            raise

        return group

    def delete(self, group: Group):
        """Delete a group."""
        self._check_not_system_group(group, "deleted")

        try:
            group.delete()
        except ProtectedError as e:
            protected_objects = e.protected_objects
            binding_count = len(protected_objects)
            raise ValueError(
                f"Cannot delete group because it is referenced by {binding_count} active role binding(s). "
                f"Remove the group from all role bindings before deletion."
            )

    def add_principals(self, group: Group, principal_ids: List[str]):
        """Add principals to a group."""
        self._check_not_system_group(group, "modified")

        principal_ids = list(dict.fromkeys(principal_ids))

        principals = list(
            Principal.objects.filter(
                uuid__in=principal_ids,
                tenant=self.tenant,
            )
        )

        if len(principals) != len(principal_ids):
            found_ids = {str(p.uuid) for p in principals}
            missing_ids = set(principal_ids) - found_ids
            raise ValueError(f"Principal(s) not found: {', '.join(missing_ids)}")

        group.principals.add(*principals)

        handler = RelationApiDualWriteGroupHandler(
            group=group,
            event_type=ReplicationEventType.ADD_PRINCIPALS_TO_GROUP,
        )
        handler.replicate_new_principals(list(principals))

    def remove_principal(self, group: Group, principal_id: str):
        """Remove a principal from a group."""
        self._check_not_system_group(group, "modified")

        try:
            principal = Principal.objects.get(
                uuid=principal_id,
                tenant=self.tenant,
            )
        except Principal.DoesNotExist:
            raise ValueError(f"Principal not found: {principal_id}")

        if not group.principals.filter(pk=principal.pk).exists():
            raise ValueError(f"Principal {principal_id} is not a member of this group.")

        group.principals.remove(principal)

        handler = RelationApiDualWriteGroupHandler(
            group=group,
            event_type=ReplicationEventType.REMOVE_PRINCIPALS_FROM_GROUP,
        )
        handler.replicate_removed_principals([principal])
