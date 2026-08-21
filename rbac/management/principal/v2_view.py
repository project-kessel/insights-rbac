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

"""View for Principal V2 management."""

from management.base_viewsets import BaseV2ViewSet
from management.permissions.principal_v2_access import PrincipalV2AccessPermission
from management.principal.model import Principal
from management.principal.v2_serializer import PrincipalV2ListInputSerializer, PrincipalV2OutputSerializer
from management.v2_filters import v2_name_filter


class PrincipalV2ViewSet(BaseV2ViewSet):
    """Read-only V2 ViewSet for principals."""

    permission_classes = (PrincipalV2AccessPermission,)
    queryset = Principal.objects.all()
    serializer_class = PrincipalV2OutputSerializer
    lookup_field = "uuid"
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        """Return non-cross-account principals for the requesting tenant."""
        return Principal.objects.filter(tenant=self.request.tenant, cross_account=False).order_by("username")

    def list(self, request, *args, **kwargs):
        """List principals with optional filtering."""
        input_serializer = PrincipalV2ListInputSerializer(data=request.query_params)
        input_serializer.is_valid(raise_exception=True)
        validated = input_serializer.validated_data

        queryset = self.get_queryset()

        principal_type = validated.get("type")
        if principal_type:
            queryset = queryset.filter(type=principal_type)

        username = validated.get("username")
        if username:
            queryset = v2_name_filter(queryset, username, field="username")

        order_by = validated.get("order_by")
        if order_by:
            queryset = queryset.order_by(order_by)

        page = self.paginate_queryset(queryset)
        serializer = PrincipalV2OutputSerializer(page, many=True)
        return self.get_paginated_response(serializer.data)
