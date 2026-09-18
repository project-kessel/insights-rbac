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

"""View for the Audit Log V2 API."""

from management.audit_log.model import AuditLog
from management.audit_log.v2_serializer import (
    AuditLogV2ListInputSerializer,
    AuditLogV2OutputSerializer,
    validate_fields_parameter,
)
from management.audit_log.v2_service import AuditLogV2Service
from management.base_viewsets import BaseV2ViewSet
from management.permissions.auditlog_v2_access import AuditLogV2KesselAccessPermission

from api.common.pagination import V2CursorPagination


class AuditLogV2CursorPagination(V2CursorPagination):
    """Cursor pagination for audit logs, newest entry first."""

    ordering = "-created"
    FIELD_MAPPING = {"created": "created"}


class AuditLogV2ViewSet(BaseV2ViewSet):
    """Read-only V2 ViewSet for audit logs.

    Only the list action is routed; individual entries are not addressable because
    audit log rows have no UUID identity of their own.
    """

    permission_classes = (AuditLogV2KesselAccessPermission,)
    queryset = AuditLog.objects.all()
    serializer_class = AuditLogV2OutputSerializer
    pagination_class = AuditLogV2CursorPagination
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        """Return the requesting tenant's audit log entries, newest first."""
        return AuditLogV2Service(tenant=self.request.tenant).base_queryset()

    def list(self, request, *args, **kwargs):
        """List the tenant's audit log entries with optional filtering."""
        input_serializer = AuditLogV2ListInputSerializer(data=request.query_params)
        input_serializer.is_valid(raise_exception=True)
        validated = input_serializer.validated_data

        fields = validate_fields_parameter(request.query_params.get("fields", "").replace("\x00", ""))

        service = AuditLogV2Service(tenant=request.tenant)
        queryset = service.list(validated)

        page = self.paginate_queryset(queryset)
        serializer = AuditLogV2OutputSerializer(page, many=True, context={"request": request, "fields": fields})
        return self.get_paginated_response(serializer.data)
