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

"""View for principal access."""

import logging

from django.db.models import Prefetch
from management.cache import AccessCache
from management.models import Access, ResourceDefinition
from management.permissions.v2_edit_api_access import is_v2_edit_enabled_for_request
from management.querysets import get_access_queryset
from management.role.serializer import AccessSerializer
from management.role.v2_role_scope import v2_role_excluded_applications
from management.utils import (
    APPLICATION_KEY,
    get_principal_from_request,
    validate_and_get_key,
    validate_key,
)
from prometheus_client import Counter
from rest_framework import status
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

v1_access_by_v2_org_total = Counter(
    "rbac_v1_access_v2_org_total",
    "Tracks v1 /access calls made by orgs that have been v2-enabled.",
    ["org_id", "application", "caller_type"],
)

ORDER_FIELD = "order_by"
VALID_ORDER_VALUES = ["application", "resource_type", "verb", "-application", "-resource_type", "-verb"]
STATUS_KEY = "status"
VALID_STATUS_VALUE = ["enabled", "disabled", "all"]


def _record_v2_org_v1_access_rejected(request):
    """Increment Prometheus counter and log context when a v2-enabled org's v1 /access call is rejected."""
    app_param = request.query_params.get(APPLICATION_KEY, "")
    caller_type = "service_account" if request.user.is_service_account else "user"
    v1_access_by_v2_org_total.labels(
        org_id=request.user.org_id,
        application=app_param,
        caller_type=caller_type,
    ).inc()
    logger.info(
        "V2 org called v1 /access/ endpoint: org_id=%s application=%s caller_type=%s "
        "user_id=%s client_id=%s is_org_admin=%s request_id=%s user_agent=%s result=rejected",
        request.user.org_id,
        app_param,
        caller_type,
        request.user.user_id,
        request.user.client_id if request.user.is_service_account else None,
        request.user.admin,
        getattr(request, "req_id", None),
        request.headers.get("user-agent"),
    )


def validate_v2_application_param(request):
    """For v2-enabled orgs, ensure all requested applications are in the migration exclude list.

    Returns None when the request is allowed, or a 400 Response when it must be rejected.
    """
    if not is_v2_edit_enabled_for_request(request):
        return None

    app_param = request.query_params.get(APPLICATION_KEY, "")
    if not app_param:
        return Response(
            status=status.HTTP_400_BAD_REQUEST,
            data={"detail": "V2 orgs must specify an application from the allowed list."},
        )

    allowed_apps = v2_role_excluded_applications()
    requested_apps = {a.strip() for a in app_param.split(",") if a.strip()}
    disallowed = requested_apps - allowed_apps
    if disallowed:
        return Response(
            status=status.HTTP_400_BAD_REQUEST,
            data={
                "detail": f"V2 orgs may only query applications in the allowed list. "
                f"Disallowed: {', '.join(sorted(disallowed))}"
            },
        )

    return None


class AccessView(APIView):
    """Obtain principal access list."""

    """
    @api {get} /api/v1/access/   Obtain principal access list
    @apiName getPrincipalAccess
    @apiGroup Access
    @apiVersion 1.0.0
    @apiDescription Obtain principal access list

    @apiHeader {String} token User authorization token

    @apiParam (Query) {String} application Application name
    @apiParam (Query) {Number} offset Parameter for selecting the start of data (default is 0).
    @apiParam (Query) {Number} limit Parameter for selecting the amount of data (default is 10).

    @apiSuccess {Object} meta The metadata for pagination.
    @apiSuccess {Object} links  The object containing links of results.
    @apiSuccess {Object[]} data  The array of results.
    @apiSuccessExample {json} Success-Response:
        HTTP/1.1 20O OK
        {
            'meta': {
                'count': 1
            }
            'links': {
                'first': /api/v1/access/?offset=0&limit=10&application=app,
                'next': None,
                'previous': None,
                'last': /api/v1/groups/?offset=0&limit=10&application=app
            },
            "data": [
                {
                    "permission": "app:*:read",
                    "resourceDefinitions": [
                        {
                            "attributeFilter": {
                                "key": "app.attribute.condition",
                                "value": "value1",
                                "operation": "equal"
                            }
                        }
                    ]
                }
            ]
        }
    """

    serializer_class = AccessSerializer
    pagination_class = api_settings.DEFAULT_PAGINATION_CLASS
    # Read-only endpoint: no DRF permission gate (auth/enforcement via middleware & query logic).
    permission_classes = ()

    def get_access_queryset_unique_by_column(self, *columns):
        """Define the access query set with DISTINCT ON clause to get unique records."""
        access_queryset = get_access_queryset(self.request)
        return access_queryset.distinct(*columns).order_by(*columns)

    def get_queryset(self, ordering):
        """Define the query set."""
        unique_columns = ["permission_id", "resourceDefinitions__attributeFilter"]
        distinct_queryset = self.get_access_queryset_unique_by_column(*unique_columns)
        access_queryset = (
            Access.objects.filter(id__in=distinct_queryset)
            .select_related("permission")
            .prefetch_related(
                Prefetch(
                    "resourceDefinitions", queryset=ResourceDefinition.objects.select_related("access__permission")
                )
            )
        )

        if ordering:
            if ordering[0] == "-":
                order_sign = "-"
                field = ordering[1:]
            else:
                order_sign = ""
                field = ordering
            return access_queryset.order_by(f"{order_sign}permission__{field}")
        return access_queryset

    def get(self, request):
        """Provide access data for principal."""
        # Parameter extraction and validation
        try:
            sub_key, ordering = self.validate_and_get_param(request)
            validate_key(request.query_params, STATUS_KEY, VALID_STATUS_VALUE, "enabled")
        except ValueError as e:
            return Response(status=status.HTTP_400_BAD_REQUEST, data=e)

        v2_error = validate_v2_application_param(request)
        if v2_error is not None:
            _record_v2_org_v1_access_rejected(request)
            return v2_error

        principal = get_principal_from_request(request)
        cache = AccessCache(request.tenant.org_id)
        access_policy = cache.get_policy(principal.uuid, sub_key)
        if access_policy is None:
            queryset = self.get_queryset(ordering)
            access_policy = self.serializer_class(
                queryset, many=True, context={"request": request, "for_access": True}
            ).data
            # Filter out None values (blocked permissions for v1 API)
            access_policy = [item for item in access_policy if item is not None]
            cache.save_policy(principal.uuid, sub_key, access_policy)

        page = self.paginate_queryset(access_policy)
        response = Response({"data": access_policy}) if page is None else self.get_paginated_response(page)

        return response

    @property
    def paginator(self):
        """Return the paginator instance associated with the view, or `None`."""
        if not hasattr(self, "_paginator"):
            self._paginator = self.pagination_class()
            self._paginator.max_limit = None
        return self._paginator

    def paginate_queryset(self, queryset):
        """Return a single page of results, or `None` if pagination is disabled."""
        if self.paginator is None:
            return None
        if "limit" not in self.request.query_params:
            self.paginator.default_limit = len(queryset)
        return self.paginator.paginate_queryset(queryset, self.request, view=self)

    def get_paginated_response(self, data):
        """Return a paginated style `Response` object for the given output data."""
        assert self.paginator is not None
        return self.paginator.get_paginated_response(data)

    def validate_and_get_param(self, request):
        """Validate input parameters and get ordering and sub_key."""
        params = request.query_params
        app = params.get(APPLICATION_KEY)
        sub_key = app
        ordering = validate_and_get_key(params, ORDER_FIELD, VALID_ORDER_VALUES, required=False)
        if ordering:
            sub_key = f"{app}&order:{ordering}"
        # Include is_org_admin in the cache sub_key so that a flag change
        # triggers a cache miss and returns up-to-date permissions.
        is_org_admin = bool(request.user.admin)
        sub_key = f"{sub_key}&is_org_admin:{is_org_admin}"
        return sub_key, ordering
