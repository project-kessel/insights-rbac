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

"""Filters for Audit Log."""

from django_filters import rest_framework as filters
from management.filters import CommonFilters
from management.models import AuditLog


class AuditLogFilter(CommonFilters):
    """Filter for audit log."""

    def principal_username_filter(self, queryset, field, value):
        """Filter to lookup principal username, partial or exact."""
        return self.name_filter(queryset, field, value, "principal_username")

    principal_username = filters.CharFilter(field_name="principal_username", method="principal_username_filter")
    resource_type = filters.MultipleChoiceFilter(
        field_name="resource_type",
        choices=AuditLog.RESOURCE_CHOICES,
    )
    action = filters.MultipleChoiceFilter(
        field_name="action",
        choices=AuditLog.ACTION_CHOICES,
    )
    created_after = filters.IsoDateTimeFilter(field_name="created", lookup_expr="gte")
    created_before = filters.IsoDateTimeFilter(field_name="created", lookup_expr="lte")

    class Meta:
        model = AuditLog
        fields = ["principal_username", "resource_type", "action", "created_after", "created_before"]
