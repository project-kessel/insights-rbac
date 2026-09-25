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

"""Service for the Audit Log V2 API."""

from django.db.models import QuerySet
from management.audit_log.model import AuditLog
from management.utils import filter_queryset_by_tenant
from management.v2_filters import v2_name_filter

from api.models import Tenant


class AuditLogV2Service:
    """
    Domain service for Audit Log V2 read operations.

    This service encapsulates the query behavior for audit log entries. The view
    layer stays responsible for request validation, pagination and serialization.
    """

    DEFAULT_ORDERING = "-created"

    def __init__(self, tenant: Tenant | None = None):
        """Initialize the service."""
        self.tenant = tenant

    def base_queryset(self) -> QuerySet:
        """Return the tenant's audit log entries, newest first."""
        return filter_queryset_by_tenant(AuditLog.objects.all(), self.tenant).order_by(self.DEFAULT_ORDERING)

    def list(self, params: dict) -> QuerySet:
        """Get a filtered list of audit log entries for the tenant.

        Args:
            params: Dictionary of validated query parameters (from input serializer)

        Returns:
            QuerySet of AuditLog entries

        Note:
            Ordering is handled by V2CursorPagination.get_ordering() to ensure
            cursor pagination works correctly with the requested order_by parameter.
        """
        queryset = self.base_queryset()

        principal_username = params.get("principal_username")
        if principal_username:
            queryset = v2_name_filter(queryset, principal_username, field="principal_username")

        resource_type = params.get("resource_type")
        if resource_type:
            queryset = queryset.filter(resource_type=resource_type)

        resource_id = params.get("resource_id")
        if resource_id:
            queryset = queryset.filter(resource_uuid=resource_id)

        action = params.get("action")
        if action:
            queryset = queryset.filter(action=action)

        created_after = params.get("created_after")
        if created_after:
            queryset = queryset.filter(created__gte=created_after)

        created_before = params.get("created_before")
        if created_before:
            queryset = queryset.filter(created__lte=created_before)

        return queryset
