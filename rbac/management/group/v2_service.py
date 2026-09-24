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

import logging
from typing import Optional

from django.db import IntegrityError, transaction
from django.db.models import Count, F, ProtectedError, Q, QuerySet
from management.group.model import Group
from management.group.relation_api_dual_write_group_handler import RelationApiDualWriteGroupHandler
from management.group.v2_exceptions import GroupAlreadyExistsError, GroupHasRoleBindingsError, ProtectedGroupError
from management.principal.model import Principal
from management.relation_replicator.relation_replicator import ReplicationEventType
from management.role.model import Role
from management.v2_filters import v2_name_filter

from api.models import Tenant

logger = logging.getLogger(__name__)


class GroupV2Service:
    """Service for V2 group operations."""

    UNIQUE_NAME_CONSTRAINT = "unique group name per tenant"
    DEFAULT_ORDER_BY = "name"
    ORDER_BY_FIELD_MAPPING = {
        "name": "name",
        "modified": "modified",
        "principal_count": "principal_count_annotation",
        "role_count": "role_count_annotation",
    }
    PROTECTED_FLAGS_FOR_UPDATE = ("system",)
    PROTECTED_FLAGS_FOR_DELETE = ("system", "platform_default", "admin_default")

    def __init__(self, tenant: Tenant):
        """Initialize service with tenant context."""
        self.tenant = tenant

    def queryset(self) -> QuerySet:
        """Return the tenant's groups annotated with principal and role counts."""
        return Group.objects.filter(tenant=self.tenant).annotate(
            principal_count_annotation=Count(
                "principals", filter=Q(principals__type=Principal.Types.USER), distinct=True
            ),
            role_count_annotation=Count(
                "role_binding_entries__binding__role",
                filter=Q(role_binding_entries__binding__tenant=F("tenant")),
                distinct=True,
            ),
        )

    def list(self, params: dict) -> QuerySet:
        """List groups with optional filtering and ordering."""
        queryset = self.queryset()

        name = params.get("name")
        if name:
            queryset = v2_name_filter(queryset, name, field="name")

        uuids = params.get("uuid")
        if uuids:
            queryset = queryset.filter(uuid__in=uuids)

        for flag in ("system", "platform_default", "admin_default"):
            value = params.get(flag)
            if value is not None:
                queryset = queryset.filter(**{flag: value})

        return queryset.order_by(*self._ordering(params.get("order_by") or self.DEFAULT_ORDER_BY))

    def get(self, group: Group) -> Group:
        """Return the given group re-fetched with count annotations."""
        return self.queryset().get(pk=group.pk)

    def create(self, name: str, description: Optional[str] = None) -> Group:
        """Create a new group."""
        try:
            with transaction.atomic():
                group = Group.objects.create(name=name, description=description, tenant=self.tenant)
        except IntegrityError as e:
            if self._is_unique_name_violation(e):
                raise GroupAlreadyExistsError(name)
            raise
        return group

    def update(self, group: Group, name: str, description: Optional[str] = None) -> Group:
        """Update the name and description of a group."""
        self._check_not_protected(group, self.PROTECTED_FLAGS_FOR_UPDATE, "updated")

        group.name = name
        group.description = description
        try:
            with transaction.atomic():
                group.save()
        except IntegrityError as e:
            if self._is_unique_name_violation(e):
                raise GroupAlreadyExistsError(name)
            raise
        return group

    def delete(self, group: Group) -> None:
        """Delete a group and replicate the removal of its membership relations."""
        self._check_not_protected(group, self.PROTECTED_FLAGS_FOR_DELETE, "deleted")

        # Count both v2 role bindings (RoleBindingGroup) and legacy v1 role assignments
        # (Policy). role_binding_entries alone misses tenants still on v1-era Policy/
        # BindingMapping assignments, which would otherwise pass this guard and leave
        # orphaned SpiceDB tuples behind after the group is deleted.
        binding_count = group.role_binding_entries.count()
        legacy_role_count = Role.objects.filter(policies__group=group).count()
        if binding_count or legacy_role_count:
            raise GroupHasRoleBindingsError(binding_count + legacy_role_count)

        # Capture members before deletion; the M2M rows are removed along with the group.
        principals = list(group.principals.all())
        # Construct before delete(): Django clears the instance pk on delete(), and while
        # this handler currently only reads group.tenant_id/uuid (unaffected), constructing
        # it up front matches the V1 destroy() ordering and avoids relying on that detail.
        dual_write_handler = RelationApiDualWriteGroupHandler(group, ReplicationEventType.DELETE_GROUP)
        try:
            group.delete()
        except ProtectedError as e:
            raise GroupHasRoleBindingsError(len(e.protected_objects))

        dual_write_handler.replicate_removed_principals(principals)

    def _ordering(self, order_by: str) -> tuple[str, ...]:
        """Translate an API order_by value into ORM ordering, with a stable name/uuid tiebreaker."""
        descending = order_by.startswith("-")
        field = self.ORDER_BY_FIELD_MAPPING[order_by.lstrip("-")]
        primary = f"-{field}" if descending else field
        if field == "name":
            return (primary, "uuid")
        return (primary, "name", "uuid")

    @staticmethod
    def _check_not_protected(group: Group, flags: tuple[str, ...], action: str) -> None:
        for flag in flags:
            if getattr(group, flag):
                raise ProtectedGroupError(action, flag)

    @classmethod
    def _is_unique_name_violation(cls, error: IntegrityError) -> bool:
        """Check whether an IntegrityError was raised by the unique group name constraint.

        Inspects the underlying psycopg2 diagnostics (constraint_name) instead of matching the
        free-text error message, which is brittle across driver/locale differences.
        """
        diag = getattr(getattr(error, "__cause__", None), "diag", None)
        constraint_name = getattr(diag, "constraint_name", None)
        if constraint_name is not None:
            return constraint_name == cls.UNIQUE_NAME_CONSTRAINT
        return cls.UNIQUE_NAME_CONSTRAINT in str(error)
