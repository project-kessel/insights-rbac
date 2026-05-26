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
"""QuerySet for RoleV2 lookups."""

from django.db import models
from django.db.models import Count, Exists, IntegerField, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce
from management.v2_filters import v2_name_filter


class RoleV2QuerySet(models.QuerySet):
    """Custom QuerySet for RoleV2 with domain-aware query methods."""

    def assignable(self):
        """Filter to roles that can be assigned to bindings.

        Only custom and seeded roles can be directly assigned.
        Platform roles are internal aggregations and cannot be assigned directly.
        """
        # Import here to avoid circular import (RoleV2 -> queryset -> RoleV2).
        from management.role.v2_model import RoleV2

        return self.exclude(type=RoleV2.Types.PLATFORM)

    def for_tenant(self, tenant):
        """Return roles scoped to the given tenant, including seeded roles from the public tenant."""
        from api.models import Tenant

        public_tenant = Tenant._get_public_tenant()
        return self.filter(Q(tenant=tenant) | Q(tenant=public_tenant))

    def with_fields(self, fields):
        """Apply field-driven eager loading so the serializer never triggers lazy loads.

        Args:
            fields: Set of response field names that drive select_related,
                    prefetch_related, and annotations.
        """
        from management.role.v2_model import RoleV2

        qs = self
        if "org_id" in fields:
            qs = qs.select_related("tenant")
        if "permissions_count" in fields:
            through = RoleV2.permissions.through
            qs = qs.annotate(
                permissions_count_annotation=Coalesce(
                    Subquery(
                        through.objects.filter(rolev2_id=OuterRef("pk"))
                        .values("rolev2_id")
                        .annotate(cnt=Count("permission_id"))
                        .values("cnt"),
                        output_field=IntegerField(),
                    ),
                    Value(0),
                )
            )
        if "permissions" in fields:
            qs = qs.prefetch_related("permissions")
        return qs

    def named(self, name: str) -> "RoleV2QuerySet":
        """Filter to roles matching a name, with '*' glob support."""
        return v2_name_filter(self, name)

    def excluding_out_of_scope_v2_roles(self):
        """Exclude roles that include any permission from a migration-excluded application.

        Uses cached permission PKs; see ``v2_role_excluded_application_permission_ids_cache``.

        Implemented with ``EXISTS`` on the M2M through table so PostgreSQL does not need a
        wide join + ``DISTINCT`` (which can dominate latency on list/cursor queries).
        """
        from management.role.v2_model import RoleV2
        from management.role.v2_role_scope import v2_role_excluded_application_permission_ids_cache

        perm_ids = v2_role_excluded_application_permission_ids_cache.permission_ids()
        if not perm_ids:
            return self
        through = RoleV2.permissions.through
        has_excluded_permission = Exists(
            through.objects.filter(
                rolev2_id=OuterRef("pk"),
                permission_id__in=perm_ids,
            )
        )
        return self.filter(~has_excluded_permission)
