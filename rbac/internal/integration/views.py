#
# Copyright 2022 Red Hat, Inc.
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

"""Views for OCM/integrations API."""

import logging

from django_filters import rest_framework as filters
from feature_flags import FEATURE_FLAGS
from internal.integration.serializers import IntegrationRoleV2Serializer, TenantSerializer
from internal.integration.service import annotate_integration_roles, roles_for_group
from management.cache import TenantCache
from management.filters import CommonFilters
from management.group.view import GroupViewSet, VALID_ROLE_ORDER_FIELDS
from management.permissions.admin_access import AdminAccessPermission
from management.querysets import get_group_queryset
from management.role.view import RoleViewSet
from management.utils import clean_query_param, validate_and_get_key, validate_uuid
from rest_framework import mixins, viewsets

from api.models import Tenant

logger = logging.getLogger(__name__)
TENANTS = TenantCache()


class TenantFilter(CommonFilters):
    """Filter for tenant."""

    def modified_only_filter(self, queryset, field, modified_only):
        """Filter to return only modified tenants."""
        if modified_only:
            queryset = queryset.modified_only()
        return queryset

    modified_only = filters.BooleanFilter(field_name="modified_only", method="modified_only_filter")


class TenantViewSet(viewsets.GenericViewSet, mixins.ListModelMixin):
    """Tenant view set."""

    queryset = Tenant.objects.all()
    permission_classes = (AdminAccessPermission,)
    serializer_class = TenantSerializer
    filter_backends = (filters.DjangoFilterBackend,)
    filterset_class = TenantFilter

    def list(self, request, *args, **kwargs):
        """Tenant list."""
        return super().list(request=request, args=args, kwargs=kwargs)

    def groups(self, request, org_id):
        """Format and pass internal groups request to /groups/ API."""
        view = GroupViewSet.as_view({"get": "list"})
        return view(request._request)

    def roles(self, request, org_id):
        """Format and pass internal roles request to /roles/ API."""
        view = RoleViewSet.as_view({"get": "list"})
        return view(request._request)

    def groups_for_principal(self, request, org_id, principals):
        """Format and pass /principal/<username>/groups/ request to /groups/ API."""
        view = GroupViewSet.as_view({"get": "list"})
        return view(request._request, principals=principals)

    def roles_for_group(self, request, org_id, uuid):
        """Select the V1 or V2 data source for the OCM roles-for-group contract."""
        group_view = OCMGroupViewSet if FEATURE_FLAGS.is_ocm_v2_enabled(org_id) else GroupViewSet
        view = group_view.as_view({"get": "roles"})
        return view(request._request, uuid=uuid)

    def roles_for_group_principal(self, request, org_id, principals, uuid):
        """Pass internal /principal/<username>/groups/<uuid>/roles/ request to /groups/ API."""
        view = GroupViewSet.as_view({"get": "roles"})
        return view(request._request, uuid=uuid, principals=principals)

    def principals_for_group(self, request, org_id, uuid):
        """Pass internal /groups/<uuid>/principals/ request to /groups/ API."""
        view = GroupViewSet.as_view({"get": "principals"})
        return view(request._request, uuid=uuid)


class OCMGroupViewSet(GroupViewSet):
    """Keep V1 group lookup and pagination while reading OCM assignments from V2."""

    http_method_names = ["get", "head", "options"]

    def roles(self, request, uuid=None):
        """Return tenant-scoped V2 role bindings in the V1 integration format."""
        validate_uuid(uuid, "group uuid validation")
        group = self.get_object()
        exclude = validate_and_get_key(request.query_params, "exclude", ["true", "false"], "false")
        principal_groups = None
        if exclude == "true" and (
            request.query_params.get("username") or request.query_params.get("scope") == "principal"
        ):
            principal_groups = get_group_queryset(request)
        roles = roles_for_group(group, request.tenant, exclude=exclude == "true", principal_groups=principal_groups)

        roles = annotate_integration_roles(roles, request.tenant)
        role_filters = self.filters_from_params(
            ["role_name", "role_description", "role_display_name", "role_system"], "role", request
        )
        role_filters = {field: clean_query_param(value, field) or value for field, value in role_filters.items()}
        if external_tenant := request.query_params.get("role_external_tenant"):
            role_filters["v1_source__ext_relation__ext_tenant__name__iexact"] = (
                clean_query_param(external_tenant, "role_external_tenant") or external_tenant
            )
        roles = roles.filter(**role_filters)
        roles = self.order_queryset(roles, VALID_ROLE_ORDER_FIELDS, request.query_params.get("order_by", "name"))
        page = self.paginate_queryset(roles)
        return self.get_paginated_response(IntegrationRoleV2Serializer(page, many=True).data)
