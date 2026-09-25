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
"""Application service for Permission operations."""

from django.conf import settings
from management.models import Access, Role
from management.permission.model import Permission, PermissionValue


class PermissionService:
    """Application service for Permission operations."""

    def resolve(self, permission_data: list[dict]) -> list[Permission]:
        """Resolve permission dicts to Permission objects."""
        if not permission_data:
            return []

        permission_strings = [PermissionValue.from_v2_dict(perm_dict).v1_string() for perm_dict in permission_data]
        return list(Permission.objects.filter(permission__in=permission_strings))

    def restrict_to_role_creation_allowed(self, queryset):
        """Restrict a permission queryset to applications allowed for role creation."""
        return queryset.filter(application__in=settings.ROLE_CREATE_ALLOW_LIST)

    def exclude_permissions_for_roles(self, queryset, role_uuids, tenant):
        """Exclude permissions already assigned to the given role(s) within a tenant.

        Args:
            queryset: A Permission queryset to filter.
            role_uuids: List of validated role UUID strings.
            tenant: The tenant to scope role lookups to.

        Returns:
            The queryset with assigned permissions excluded.
        """
        roles = Role.objects.filter(uuid__in=role_uuids, tenant=tenant)
        permission_ids_to_exclude = Access.objects.filter(role__in=roles).values_list("permission_id", flat=True)
        return queryset.exclude(id__in=permission_ids_to_exclude)
