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

"""View for cross access request."""

from typing import Callable, List, Optional

from django.db.models import Q
from django.utils import timezone
from django_filters import rest_framework as filters
from management.atomic_transactions import atomic
from management.filters import CommonFilters
from management.principal.proxy import PrincipalProxy
from management.relation_replicator.relation_replicator import ReplicationEventType
from management.utils import raise_validation_error, validate_and_get_key, validate_uuid
from rest_framework import mixins, viewsets
from rest_framework.filters import OrderingFilter

from api.cross_access.access_control import CrossAccountRequestAccessPermission
from api.cross_access.relation_api_dual_write_cross_access_handler import RelationApiDualWriteCrossAccessHandler
from api.cross_access.serializer import CrossAccountRequestDetailSerializer, CrossAccountRequestSerializer
from api.cross_access.util import create_cross_principal
from api.models import CrossAccountRequest, Tenant

QUERY_BY_KEY = "query_by"
ORG_ID = "target_org"
USER_ID = "user_id"
PARAMS_FOR_CREATION = ["target_org", "start_date", "end_date", "roles"]
VALID_QUERY_BY_KEY = [ORG_ID, USER_ID]

VALID_PATCH_FIELDS = ["start_date", "end_date", "roles", "status"]

PROXY = PrincipalProxy()


class CrossAccountRequestFilter(filters.FilterSet):
    """Filter for cross account request."""

    def org_id_filter(self, queryset, field, values):
        """Filter to lookup requests by target_org."""
        return CommonFilters.multiple_values_in(self, queryset, "target_org", values)

    def approved_filter(self, queryset, field, value):
        """Filter to lookup requests by status of approved."""
        if value:
            return queryset.filter(status="approved").filter(
                start_date__lt=timezone.now(), end_date__gt=timezone.now()
            )
        return queryset

    def status_filter(self, queryset, field, values):
        """Filter to lookup requests by status(es) in permissions."""
        statuses = values.split(",")
        query = Q()
        for status in statuses:
            query = query | Q(status__iexact=status)
        return queryset.distinct().filter(query)

    org_id = filters.CharFilter(field_name="target_org", method="org_id_filter")
    approved_only = filters.BooleanFilter(field_name="end_date", method="approved_filter")
    status = filters.CharFilter(field_name="status", method="status_filter")

    class Meta:
        model = CrossAccountRequest
        fields = ["org_id", "approved_only", "status"]


class CrossAccountRequestViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Cross Account Request view set.

    A viewset that provides default `create(), list(), and update()` actions.

    """

    permission_classes = (CrossAccountRequestAccessPermission,)
    filter_backends = (filters.DjangoFilterBackend, OrderingFilter)
    filterset_class = CrossAccountRequestFilter
    ordering_fields = ("request_id", "start_date", "end_date", "created", "modified", "status")

    def get_queryset(self):
        """Get query set based on the queryBy key word."""
        if self.request.method in ["PATCH", "PUT"]:
            return CrossAccountRequest.objects.all().select_for_update()

        if validate_and_get_key(self.request.query_params, QUERY_BY_KEY, VALID_QUERY_BY_KEY, ORG_ID) == ORG_ID:
            return CrossAccountRequest.objects.filter(target_org=self.request.user.org_id)

        return CrossAccountRequest.objects.filter(user_id=self.request.user.user_id)

    def get_serializer_class(self):
        """Get serializer based on route."""
        if self.request.path.endswith("cross-account-requests/") and self.request.method == "GET":
            return CrossAccountRequestSerializer
        return CrossAccountRequestDetailSerializer

    def get_serializer_context(self):
        """Get serializer context."""
        context = super().get_serializer_context()
        context["user"] = self.request.user
        return context

    def create(self, request, *args, **kwargs):
        """Create cross account requests for associate."""
        self.validate_and_format_input(request.data)
        return super().create(request=request, args=args, kwargs=kwargs)

    def list(self, request, *args, **kwargs):
        """List cross account requests for account/user_id."""
        result = super().list(request=request, args=args, kwargs=kwargs)
        # The approver's view requires requester's info such as first name, last name, email address.
        if validate_and_get_key(self.request.query_params, QUERY_BY_KEY, VALID_QUERY_BY_KEY, ORG_ID) == ORG_ID:
            return self.replace_user_id_with_info(result)
        return result

    # This can operate on V2 tenants and approves CARs which creates role bindings,
    # so we need SERIALIZABLE transaction to coordinate with role scope migrations.
    @atomic
    def partial_update(self, request, *args, **kwargs):
        """Patch a cross-account request. Target account admin use it to update status of the request."""
        return super().partial_update(request=request, *args, **kwargs)

    # This can operate on V2 tenants, so we need to use a SERIALIZABLE transaction here.
    @atomic
    def update(self, request, *args, **kwargs):
        """Update a cross-account request. TAM requestor use it to update their requesters."""
        validate_uuid(kwargs.get("pk"), "cross-account request uuid validation")
        return super().update(request=request, *args, **kwargs)

    def perform_update(self, serializer):
        """Update the cross account request and publish outbox when cross account request is approved."""
        current = serializer.instance
        request = self.request
        if serializer.partial and request.data.get("status"):
            self.update_status(current, request.data.get("status"))
        serializer.save()

    def retrieve(self, request, *args, **kwargs):
        """Retrieve cross account requests by request_id."""
        result = super().retrieve(request=request, args=args, kwargs=kwargs)

        if validate_and_get_key(self.request.query_params, QUERY_BY_KEY, VALID_QUERY_BY_KEY, ORG_ID) == ORG_ID:
            user_id = result.data.pop("user_id")
            principal = PROXY.request_filtered_principals(
                [user_id], org_id=None, options={"query_by": "user_id", "return_id": True}
            ).get("data")[0]

            # Replace the user_id with user's info
            result.data.update(
                {
                    "first_name": principal["first_name"],
                    "last_name": principal["last_name"],
                    "email": principal["email"],
                }
            )
        return result

    def replace_user_id_with_info(self, result):
        """Replace user id with user's info."""
        # Get principals through user_ids from BOP
        user_ids = [element["user_id"] for element in result.data["data"]]
        bop_resp = PROXY.request_filtered_principals(
            user_ids, org_id=None, options={"query_by": "user_id", "return_id": True}
        )

        # Make a mapping: user_id => principal
        principals = {
            str(principal["user_id"]): {
                "first_name": principal["first_name"],
                "last_name": principal["last_name"],
                "email": principal["email"],
            }
            for principal in bop_resp["data"]
        }

        # Replace the user_id with user's info
        for element in result.data["data"]:
            user_id = element["user_id"]
            requestor_info = principals.get(user_id)

            if requestor_info is not None:
                element["user_available"] = True

                del element["user_id"]
                element.update(requestor_info)
            else:
                element["user_available"] = False

        return result

    def validate_and_format_input(self, request_data):
        """Validate the create api input."""
        for field in PARAMS_FOR_CREATION:
            if not request_data.__contains__(field):
                raise_validation_error("cross-account-request", f"Field {field} must be specified.")

        target_org = request_data.get("target_org")
        if target_org == self.request.user.org_id:
            raise_validation_error(
                "cross-account-request", "Creating a cross access request for your own org id is not allowed."
            )

        try:
            Tenant.objects.get(org_id=target_org)
        except Tenant.DoesNotExist:
            raise raise_validation_error("cross-account-request", f"Org ID '{target_org}' does not exist.")

        request_data["user_id"] = self.request.user.user_id

    def _with_dual_write_handler(
        self,
        car: CrossAccountRequest,
        replication_event_type: ReplicationEventType,
        generate_relations: Optional[Callable[[RelationApiDualWriteCrossAccessHandler, List], None]] = None,
    ) -> None:
        """Use dual write handler."""
        cross_account_roles = car.roles.all()
        if any(True for _ in cross_account_roles):
            dual_write_handler = RelationApiDualWriteCrossAccessHandler(car, replication_event_type)

            if generate_relations and callable(generate_relations):
                generate_relations(dual_write_handler, cross_account_roles)

            dual_write_handler.replicate()

    def update_status(self, car, status):
        """Update the status of a cross-account-request."""
        if car.status == status:  # No operation needed
            return
        car.status = status
        if status == "approved":
            create_cross_principal(car.user_id, target_org=car.target_org)

            self._with_dual_write_handler(
                car,
                ReplicationEventType.APPROVE_CROSS_ACCOUNT_REQUEST,
                lambda dual_write_handler, cross_account_roles: dual_write_handler.generate_relations_to_add_roles(
                    cross_account_roles
                ),
            )
        elif status == "denied":
            self._with_dual_write_handler(
                car,
                ReplicationEventType.DENY_CROSS_ACCOUNT_REQUEST,
                lambda dual_write_handler, cross_account_roles: dual_write_handler.generate_relations_to_remove_roles(
                    cross_account_roles
                ),
            )

    def check_patch_permission(self, request, update_obj):
        """Check if user has right to patch cross access request."""
        if request.user.org_id == update_obj.target_org:
            """For approvers updating requests coming to them, only org admins
            may update status from pending/approved/denied to approved/denied.
            """
            if not request.user.admin:
                raise_validation_error("cross-account partial update", "Only org admins may update status.")
            if update_obj.status not in ["pending", "approved", "denied"]:
                raise_validation_error(
                    "cross-account partial update", "Only pending/approved/denied requests may be updated."
                )
            if request.data.get("status") not in ["approved", "denied"]:
                raise_validation_error(
                    "cross-account partial update", "Request status may only be updated to approved/denied."
                )
            if len(request.data.keys()) > 1 or next(iter(request.data)) != "status":
                raise_validation_error("cross-account partial update", "Only status may be updated.")
        elif request.user.user_id == update_obj.user_id:
            """For requestors updating their requests, the request status may
            only be updated from pending to cancelled.
            """
            if update_obj.status != "pending" or request.data.get("status") != "cancelled":
                raise_validation_error(
                    "cross-account partial update", "Request status may only be updated from pending to cancelled."
                )
            for field in request.data:
                if field not in VALID_PATCH_FIELDS:
                    raise_validation_error(
                        "cross-account partial update",
                        f"Field '{field}' is not supported. Please use one or more of: {VALID_PATCH_FIELDS}",
                    )
        else:
            raise_validation_error(
                "cross-account partial update", "User does not have permission to update the request."
            )

    def check_update_permission(self, request, update_obj):
        """Check if user has permission to update cross access request."""
        # Only requestors could update the cross access request.
        if request.user.user_id != update_obj.user_id:
            raise_validation_error("cross-account update", "Only the requestor may update the cross access request.")

        # Only pending request could be updated.
        if update_obj.status != "pending":
            raise_validation_error("cross-account update", "Only pending requests may be updated.")

        # Do not allow updating the status:
        if request.data.get("status") and str(request.data.get("status")) != "pending":
            raise_validation_error(
                "cross-account update",
                "The status may not be updated through PUT endpoint. "
                "Please use PATCH to update the status of the request.",
            )

        # Do not allow updating the target_org.
        if request.data.get("target_org") and str(request.data.get("target_org")) != update_obj.target_org:
            raise_validation_error("cross-account-update", "Target org must stay the same.")
