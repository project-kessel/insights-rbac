#
# Copyright 2019 Red Hat, Inc.
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

"""View for permission management."""

from django.conf import settings
from django.db.models import Q
from django.db.models.functions import Collate
from django_filters import rest_framework as filters
from management.filters import CommonFilters
from management.models import Access, Permission, Role
from management.permission.serializer import PermissionSerializer
from management.permissions.permission_access import PermissionAccessPermission
from management.permissions.v2_edit_api_access import is_v2_edit_enabled_for_request
from management.role.v2_role_scope import v2_role_excluded_applications
from management.utils import (
    api_path_prefix,
    validate_and_get_key,
    validate_uuid,
)
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter

PERMISSION_FIELD_KEYS = {"application", "resource_type", "verb"}
VALID_BOOLEAN_PARAM_VALS = ["true", "false"]
QUERY_FIELD = "field"


class PermissionFilter(CommonFilters):
    """Filter for role."""

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
            roles = Role.objects.filter(uuid__in=role_uuids_list)
            permission_ids_to_exclude = Access.objects.filter(role__in=roles).values_list("permission_id", flat=True)
            return queryset.exclude(id__in=permission_ids_to_exclude)
        return queryset

    def allowed_only_filter(self, queryset, field, value):
        """Filter to return only permissions from roles in the ROLE_CREATE_ALLOW_LIST."""
        query_field = validate_and_get_key(self.request.query_params, field, VALID_BOOLEAN_PARAM_VALS, "false")
        if query_field == "true":
            queryset = queryset.filter(application__in=settings.ROLE_CREATE_ALLOW_LIST)
        return queryset

    application = filters.CharFilter(field_name="application", method="multiple_values_in")
    resource_type = filters.CharFilter(field_name="resource_type", method="multiple_values_in")
    verb = filters.CharFilter(field_name="verb", method="multiple_values_in")
    permission = filters.CharFilter(field_name="permission", lookup_expr="icontains")
    exclude_globals = filters.CharFilter(field_name="exclude_globals", method="exclude_globals_filter")
    exclude_roles = filters.CharFilter(field_name="exclude_roles", method="exclude_roles_filter")
    allowed_only = filters.CharFilter(field_name="allowed_only", method="allowed_only_filter")


class PermissionViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Permission View.

    A viewset that provides the default `list()` action.

    """

    queryset = (
        Permission.objects.all().annotate(permission_collate=Collate("permission", "C")).order_by("permission_collate")
    )

    permission_classes = (PermissionAccessPermission,)
    filter_backends = (filters.DjangoFilterBackend, OrderingFilter)
    serializer_class = PermissionSerializer
    filterset_class = PermissionFilter
    ordering_fields = (
        "application",
        "resource_type",
        "verb",
    )
    ordering = ("permission_collate",)

    def get_queryset(self):
        """Override to filter out blocked permissions for v1 API and scope for v2 tenants."""
        queryset = super().get_queryset()

        if self.request.path.startswith(f"/{api_path_prefix()}v1/"):
            blocked_permissions = settings.V1_ROLE_PERMISSION_BLOCK_LIST
            if blocked_permissions:
                queryset = queryset.exclude(permission__in=blocked_permissions)

        if is_v2_edit_enabled_for_request(self.request):
            excluded_apps = v2_role_excluded_applications()
            if excluded_apps:
                queryset = queryset.exclude(application__in=list(excluded_apps))

        return queryset

    def list(self, request, *args, **kwargs):
        """Obtain the list of permissions for the tenant.

        @api {get} /api/v1/permissions/   Obtain a list of permissions
        @apiName getPermissions
        @apiGroup Permission
        @apiVersion 1.0.0
        @apiDescription Obtain a list of permissions

        @apiHeader {String} token User authorization token

        @apiParam (Query) {String} application Filter by permission name.
        @apiParam (Query) {String} resource_type Filter by permission name.
        @apiParam (Query) {String} verb Filter by permission name.
        @apiParam (Query) {Number} offset Parameter for selecting the start of data (default is 0).
        @apiParam (Query) {Number} limit Parameter for selecting the amount of data (default is 10).

        @apiSuccess {Object} meta The metadata for pagination.
        @apiSuccess {Object} links  The object containing links of results.
        @apiSuccess {Object[]} data  The array of results.

        @apiSuccessExample {json} Success-Response:
            HTTP/1.1 200 OK
        {
          "meta": {
            "count": 2
          },
          "links": {
            "first": "/api/v1/permissions/?offset=0&limit=10",
            "next": null,
            "previous": null,
            "last": "/api/v1/permissions/?offset=0&limit=10"
          },
          "data": [
            {
              "application": "rbac",
              "resource_type": "*",
              "verb": "read",
              "permission": "rbac:*:read"
            }
          ]
        }
        """
        if request.query_params.get("order_by") == "-permission":
            self.queryset = self.queryset.order_by("-permission_collate")
            self.ordering = "-permission_collate"
        return super().list(request=request, args=args, kwargs=kwargs)

    @action(detail=False)
    def options(self, request):
        """Get options of applications."""
        """
        @api {get} /api/v1/permissions/options/   Get option of permission
        @apiName getPermissionOption
        @apiGroup Permission
        @apiVersion 1.0.0
        @apiDescription Get option of permission

        @apiHeader {String} token User authorization token

        @apiParam (Query) {String} field The field to return.

        @apiParam (Query) {String} application Filter by permission name.
        @apiParam (Query) {String} resource_type Filter by resource_type.
        @apiParam (Query) {String} verb Filter by verb.

        @apiSuccess {Object[]} data  The array of results.

            HTTP/1.1 200 OK
        {
          "data": [
            catalog, approval
          ]
        }
        """
        filters = {}
        query_field = validate_and_get_key(request.query_params, QUERY_FIELD, PERMISSION_FIELD_KEYS, None)

        for key in PERMISSION_FIELD_KEYS:
            context = request.query_params.get(key)
            if context:
                filters[f"{key}__in"] = context.split(",")
        query_set = (
            self.filter_queryset(self.get_queryset())
            .order_by(query_field)
            .distinct(query_field)
            .filter(**filters)
            .values_list(query_field, flat=True)
        )

        if "limit" not in self.request.query_params:
            self.paginator.default_limit = self.paginator.max_limit
        page = self.paginate_queryset(query_set)
        return self.get_paginated_response(page)
