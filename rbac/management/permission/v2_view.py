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
"""View for PermissionV2 management."""

from django.db.models import Q
from django.db.models.functions import Collate
from django_filters import rest_framework as filters
from management.base_viewsets import BaseV2ViewSet
from management.filters import CommonFilters
from management.models import Permission
from management.permission.service import PermissionService
from management.permission.v2_serializer import PermissionV2ResponseSerializer, validate_fields_parameter
from management.permissions.permission_access import PermissionAccessPermission
from management.role.v2_role_scope import v2_role_excluded_applications
from management.utils import validate_and_get_key, validate_uuid
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError

VALID_BOOLEAN_PARAM_VALS = ["true", "false"]

# Maps API-facing field names (v2) to the underlying Permission model field names.
ORDER_FIELD_MAP = {
    "application": "application",
    "resource_type": "resource_type",
    "operation": "verb",
    "permission": "permission_collate",
}

OPTIONS_FIELD_MAP = {
    "application": "application",
    "resource_type": "resource_type",
    "operation": "verb",
    "permission": "permission",
}


class PermissionV2Filter(CommonFilters):
    """Filter for the PermissionV2 endpoint."""

    def exclude_globals_filter(self, queryset, field, value):
        """Filter to filter out global permissions from results."""
        query_field = validate_and_get_key(self.request.query_params, field, VALID_BOOLEAN_PARAM_VALS, "false")
        if query_field == "true":
            exclude_set = Q(application="*") | Q(resource_type="*") | Q(verb="*")
            return queryset.exclude(exclude_set)
        return queryset

    def exclude_roles_filter(self, queryset, field, value):
        """Filter to filter out permissions already included in the role(s) from results."""
        role_uuid_string = self.request.query_params.get(field)
        if role_uuid_string:
            role_uuids_list = role_uuid_string.split(",")
            for uuid in role_uuids_list:
                validate_uuid(uuid)
            return PermissionService().exclude_permissions_for_roles(queryset, role_uuids_list, self.request.tenant)
        return queryset

    def allowed_only_filter(self, queryset, field, value):
        """Filter to return only permissions from applications allowed for role creation."""
        query_field = validate_and_get_key(self.request.query_params, field, VALID_BOOLEAN_PARAM_VALS, "false")
        if query_field == "true":
            queryset = PermissionService().restrict_to_role_creation_allowed(queryset)
        return queryset

    application = filters.CharFilter(field_name="application", method="multiple_values_in")
    resource_type = filters.CharFilter(field_name="resource_type", method="multiple_values_in")
    operation = filters.CharFilter(field_name="verb", method="multiple_values_in")
    permission = filters.CharFilter(field_name="permission", lookup_expr="icontains")
    exclude_globals = filters.CharFilter(field_name="exclude_globals", method="exclude_globals_filter")
    exclude_roles = filters.CharFilter(field_name="exclude_roles", method="exclude_roles_filter")
    allowed_only = filters.CharFilter(field_name="allowed_only", method="allowed_only_filter")


class PermissionV2ViewSet(BaseV2ViewSet):
    """PermissionV2 ViewSet.

    A viewset that provides the `list()` action and an `options()` action for
    discovering distinct field values.

    No ``AccessFilterBackend`` is used because permission visibility is
    tenant-wide all-or-nothing; ``PermissionAccessPermission`` gates access
    at the endpoint level.
    """

    queryset = (
        Permission.objects.all().annotate(permission_collate=Collate("permission", "C")).order_by("permission_collate")
    )

    permission_classes = (PermissionAccessPermission,)
    filter_backends = (filters.DjangoFilterBackend,)
    filterset_class = PermissionV2Filter
    serializer_class = PermissionV2ResponseSerializer
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        """Scope permissions to the requesting tenant and exclude v2-role-scoped applications.

        Overrides ``BaseV2ViewSet.get_queryset()`` because ``Permission``
        lacks the ``name`` and ``modified`` fields used in the base ordering.
        """
        queryset = (
            Permission.objects.filter(tenant=self.request.tenant)
            .annotate(permission_collate=Collate("permission", "C"))
            .order_by("permission_collate")
        )

        excluded_apps = v2_role_excluded_applications()
        if excluded_apps:
            queryset = queryset.exclude(application__in=list(excluded_apps))

        return queryset

    def _get_ordering(self, request):
        """Resolve and validate the `order_by` query parameter into ORM field names."""
        order_param = request.query_params.get("order_by", "permission")
        requested_fields = [f.strip() for f in order_param.split(",") if f.strip()]
        if not requested_fields:
            requested_fields = ["permission"]

        ordering = []
        for requested_field in requested_fields:
            descending = requested_field.startswith("-")
            field_name = requested_field[1:] if descending else requested_field

            orm_field = ORDER_FIELD_MAP.get(field_name)
            if orm_field is None:
                raise ValidationError(
                    {
                        "order_by": (
                            f"Invalid ordering field '{requested_field}'. "
                            f"Valid fields: {', '.join(sorted(ORDER_FIELD_MAP))}"
                        )
                    }
                )
            ordering.append(f"-{orm_field}" if descending else orm_field)

        if "permission_collate" not in {field.lstrip("-") for field in ordering}:
            ordering.append("permission_collate")

        return ordering

    def get_serializer_context(self):
        """Add validated fields parameter to serializer context."""
        context = super().get_serializer_context()
        fields_param = self.request.query_params.get("fields", "").replace("\x00", "")
        context["fields"] = validate_fields_parameter(fields_param)
        return context

    def list(self, request, *args, **kwargs):
        """Obtain the list of permissions for the tenant."""
        queryset = self.filter_queryset(self.get_queryset())
        queryset = queryset.order_by(*self._get_ordering(request))

        if "requires" in self.get_serializer_context()["fields"]:
            queryset = queryset.prefetch_related("permissions")

        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page, many=True)
        return self.get_paginated_response(serializer.data)

    @action(detail=False)
    def options(self, request):
        """List distinct values for a single permission field."""
        field = request.query_params.get("field")
        if field not in OPTIONS_FIELD_MAP:
            raise ValidationError({"field": f"Must be one of: {', '.join(sorted(OPTIONS_FIELD_MAP))}."})
        orm_field = OPTIONS_FIELD_MAP[field]

        queryset = (
            self.filter_queryset(self.get_queryset())
            .order_by(orm_field)
            .distinct(orm_field)
            .values_list(orm_field, flat=True)
        )

        page = self.paginate_queryset(queryset)
        return self.get_paginated_response(page)
