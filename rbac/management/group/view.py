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

"""View for group management."""

import logging
from typing import Iterable, List, Optional, Tuple
from uuid import UUID

import requests
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.db.models.aggregates import Count
from django.http import Http404
from django.utils.translation import gettext as _
from django_filters import rest_framework as filters
from management.authorization.scope_claims import ScopeClaims
from management.authorization.token_validator import ITSSOTokenValidator
from management.filters import CommonFilters
from management.group.definer import (
    _roles_by_query_or_ids,
    add_roles,
    remove_roles,
    set_system_flag_before_update,
)
from management.group.relation_api_dual_write_group_handler import (
    RelationApiDualWriteGroupHandler,
)
from management.group.serializer import (
    GroupInputSerializer,
    GroupPrincipalInputSerializer,
    GroupRoleSerializerIn,
    GroupRoleSerializerOut,
    GroupSerializer,
    RoleMinimumSerializer,
)
from management.models import AuditLog, Group, Role
from management.notifications.notification_handlers import (
    group_obj_change_notification_handler,
    group_principal_change_notification_handler,
)
from management.permissions import GroupAccessPermission
from management.permissions.v2_edit_api_access import is_v2_edit_enabled_for_request
from management.principal.it_service import ITService
from management.principal.model import Principal
from management.principal.proxy import PrincipalProxy
from management.principal.serializer import ServiceAccountSerializer
from management.principal.view import ADMIN_ONLY_KEY, USERNAME_ONLY_KEY, VALID_BOOLEAN_VALUE
from management.querysets import (
    get_group_queryset,
    get_role_queryset,
)
from management.relation_replicator.relation_replicator import ReplicationEventType
from management.role.view import RoleViewSet
from management.role_binding.service import RoleBindingService
from management.tenant_mapping.v2_activation import V1WriteBlockedError, assert_v1_write_allowed
from management.utils import validate_and_get_key, validate_group_name, validate_uuid
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter
from rest_framework.request import Request
from rest_framework.response import Response

from api.common.pagination import StandardResultsSetPagination
from api.models import Tenant, User
from .insufficient_privileges import InsufficientPrivilegesError
from .service_account_not_found_error import ServiceAccountNotFoundError
from ..principal.unexpected_status_code_from_it import UnexpectedStatusCodeFromITError

USERNAMES_KEY = "usernames"
SERVICE_ACCOUNTS_KEY = "service-accounts"
ROLES_KEY = "roles"
EXCLUDE_KEY = "exclude"
ORDERING_PARAM = "order_by"
NAME_KEY = "name"
PRINCIPAL_TYPE_KEY = "principal_type"
PRINCIPAL_USERNAME_KEY = "principal_username"
VALID_ROLE_ORDER_FIELDS = list(RoleViewSet.ordering_fields)
ROLE_DISCRIMINATOR_KEY = "role_discriminator"
SERVICE_ACCOUNT_CLIENT_IDS_KEY = "service_account_client_ids"
SERVICE_ACCOUNT_DESCRIPTION_KEY = "service_account_description"
SERVICE_ACCOUNT_NAME_KEY = "service_account_name"
SERVICE_ACCOUNT_USERNAME_FORMAT = "service-account-{clientId}"
VALID_EXCLUDE_VALUES = ["true", "false"]
VALID_GROUP_ROLE_FILTERS = [
    "role_name",
    "role_description",
    "role_display_name",
    "role_system",
]
VALID_GROUP_PRINCIPAL_FILTERS = ["principal_username"]
VALID_PRINCIPAL_ORDER_FIELDS = ["username"]
ALL_KEY = "all"
VALID_PRINCIPAL_TYPE_VALUE = [Principal.Types.SERVICE_ACCOUNT, Principal.Types.USER, ALL_KEY]
VALID_ROLE_ROLE_DISCRIMINATOR = ["all", "any"]
logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class GroupFilter(CommonFilters):
    """Filter for group."""

    def uuid_filter(self, queryset, field, values):
        """Filter for group uuid lookup."""
        uuids = values.split(",")
        for uuid in uuids:
            validate_uuid(uuid, "groups uuid filter")
        return CommonFilters.multiple_values_in(self, queryset, field, values)

    def roles_filter(self, queryset, field, values):
        """Filter for group to lookup list of role name."""
        if not values:
            key = "groups_filter"
            message = "No value of roles provided to filter groups!"
            error = {key: [_(message)]}
            raise serializers.ValidationError(error)
        roles_list = [value.lower() for value in values.split(",")]

        discriminator = validate_and_get_key(
            self.request.query_params,
            ROLE_DISCRIMINATOR_KEY,
            VALID_ROLE_ROLE_DISCRIMINATOR,
            "any",
        )

        if discriminator == "any":
            return queryset.filter(policies__roles__name__iregex=r"(" + "|".join(roles_list) + ")")

        for role_name in roles_list:
            queryset = queryset.filter(policies__roles__name__icontains=role_name)
        return queryset

    def principal_filter(self, queryset, field, values):
        """Filter for groups containing principals."""
        if not values:
            key = "groups_filter"
            message = "No principals provided to filter groups!"
            error = {key: [_(message)]}
            raise serializers.ValidationError(error)
        principals = [value.lower() for value in values.split(",")]

        for principal in principals:
            queryset = queryset.filter(principals__username__iexact=principal)

        return queryset

    name = filters.CharFilter(field_name="name", method="name_filter")
    role_names = filters.CharFilter(field_name="role_names", method="roles_filter")
    uuid = filters.CharFilter(field_name="uuid", method="uuid_filter")
    principals = filters.CharFilter(field_name="principals", method="principal_filter")
    system = filters.BooleanFilter(field_name="system")
    platform_default = filters.BooleanFilter(field_name="platform_default")
    admin_default = filters.BooleanFilter(field_name="admin_default")

    class Meta:
        model = Group
        fields = ["name", "role_names", "uuid", "principals"]


class GroupViewSet(
    mixins.CreateModelMixin,
    mixins.DestroyModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Group View.

    A viewset that provides default `create()`, `destroy`, `retrieve()`,
    and `list()` actions.

    """

    queryset = Group.objects.annotate(
        principalCount=Count("principals", filter=Q(principals__type=Principal.Types.USER), distinct=True),
        policyCount=Count("policies", distinct=True),
    )
    permission_classes = (GroupAccessPermission,)
    lookup_field = "uuid"
    filter_backends = (filters.DjangoFilterBackend, OrderingFilter)
    filterset_class = GroupFilter
    ordering_fields = ("name", "modified", "principalCount", "policyCount")
    ordering = ("name",)
    proxy = PrincipalProxy()

    def get_queryset(self):
        """Obtain queryset for requesting user based on access."""
        principals_method = self.action == "principals" and (self.request.method != "GET")
        destroy_method = self.action == "destroy"

        if principals_method or destroy_method:
            # In this case, the group must be locked to prevent principal changes during deletion.
            # If not locked, replication to relations may be out of sync due to phantom reads.
            # We have to modify the starting queryset to support locking because
            # FOR UPDATE statement cannot be used with GROUP BY statement
            # and by default this uses GROUP BY for counts.
            #
            # When removing a group, this must also be locked in the case where the removed group is a custom default
            # access group. See try_lock_tenants_for_bootstrap.
            group_query_set = get_group_queryset(
                self.request, self.args, self.kwargs, base_query=Group.objects.all()
            ).select_for_update()
        else:
            group_query_set = get_group_queryset(self.request, self.args, self.kwargs)
        return group_query_set

    def get_serializer_class(self):
        """Get serializer based on route."""
        if "principals" in self.request.path:
            return GroupPrincipalInputSerializer
        if ROLES_KEY in self.request.path.split("/") and self.request.method == "GET":
            return GroupRoleSerializerOut
        if ROLES_KEY in self.request.path.split("/"):
            return GroupRoleSerializerIn
        if self.request.method in ("POST", "PUT"):
            return GroupInputSerializer
        if self.request.path.endswith("groups/") and self.request.method == "GET":
            return GroupInputSerializer
        return GroupSerializer

    def protect_special_groups(self, action, group=None, additional=None):
        """
        Prevent modifications to protected groups.

        This method denies the specified action if the group belongs to certain protected categories,
        such as system groups or any additional conditionally protected groups.

        Args:
            action (str): The action being attempted (e.g., "update", "delete").
            group (Optional[object]): The group instance to check. If None, defaults to `self.get_object()`.
            additional (Optional[str]): An optional attribute name to check for additional protection.

        Raises:
            serializers.ValidationError: If the group has a protected attribute, preventing modification.
        """
        if group is None:
            group = self.get_object()
        attrs = ["system"]
        if additional:
            attrs.append(additional)
        for attr in attrs:
            if getattr(group, attr):
                key = "group"
                message = f"{action.upper()} cannot be performed on {attr} groups."
                error = {key: [_(message)]}
                raise serializers.ValidationError(error)

    def restrict_custom_default_group_renaming(self, request, group):
        """Restrict users from changing the name or description of the Custom default group."""
        invalid_parameters = ["name", "description"]
        if group.platform_default and request.method == "PUT":
            invalid_fields = [field for field in invalid_parameters if field in request.data]
            if invalid_fields:
                key = "detail"
                message = "Updating the name or description of 'Custom default group' is restricted"
                error = {key: (message)}
                raise serializers.ValidationError(error)

    def protect_default_admin_group_roles(self, group):
        """Disallow default admin access roles from being updated."""
        if group.admin_default:
            error = {"group": [_("Default admin access cannot be modified.")]}
            raise serializers.ValidationError(error)

    def protect_group_with_user_access_admin_role(self, roles, source_key):
        """Disallow group with 'User Access administrator' role from being updated."""
        # Only organization administrators are allowed to create, modify, or delete a group
        # with RBAC permission higher than "read".
        for role in roles:
            for access in role.access.all():
                if access.permission_application() == "rbac" and access.permission.verb != "read":
                    key = source_key
                    message = (
                        "Non org admin users are not allowed to create, modify, or delete a group with higher "
                        "than 'read' RBAC permission."
                    )
                    raise serializers.ValidationError({key: _(message)})

    def create(self, request, *args, **kwargs):
        """Create a group.

        @api {post} /api/v1/groups/   Create a group
        @apiName createGroup
        @apiGroup Group
        @apiVersion 1.0.0
        @apiDescription Create a Group

        @apiHeader {String} token User authorization token

        @apiParam (Request Body) {String} name Group name
        @apiParamExample {json} Request Body:
            {
                "name": "GroupA"
            }

        @apiSuccess {String} uuid Group unique identifier
        @apiSuccess {String} name Group name
        @apiSuccessExample {json} Success-Response:
            HTTP/1.1 201 CREATED
            {
                "uuid": "16fd2706-8baf-433b-82eb-8c7fada847da",
                "name": "GroupA",
                "principals": []
            }
        """
        validate_group_name(request.data.get("name"))
        try:
            create_group = super().create(request=request, args=args, kwargs=kwargs)
        except IntegrityError as e:
            if "unique constraint" in str(e.args):
                raise serializers.ValidationError(
                    {"group": f"A group with the name '{request.data.get('name')}' exists for this tenant"}
                )
            else:
                raise serializers.ValidationError(
                    {"group": "Unknown Integrity Error occurred while trying to add group for this tenant"}
                )

        if status.is_success(create_group.status_code):
            auditlog = AuditLog()
            auditlog.log_create(request, AuditLog.GROUP)

        return create_group

    def list(self, request, *args, **kwargs):
        """Obtain the list of groups for the tenant.

        @api {get} /api/v1/groups/   Obtain a list of groups
        @apiName getGroups
        @apiGroup Group
        @apiVersion 1.0.0
        @apiDescription Obtain a list of groups

        @apiHeader {String} token User authorization token

        @apiParam (Query) {String} name Filter by group name.
        @apiParam (Query) {array} uuid Filter by comma separated list of uuids
        @apiParam (Query) {Number} offset Parameter for selecting the start of data (default is 0).
        @apiParam (Query) {Number} limit Parameter for selecting the amount of data (default is 10).

        @apiSuccess {Object} meta The metadata for pagination.
        @apiSuccess {Object} links  The object containing links of results.
        @apiSuccess {Object[]} data  The array of results.

        @apiSuccessExample {json} Success-Response:
            HTTP/1.1 200 OK
            {
                'meta': {
                    'count': 2
                }
                'links': {
                    'first': /api/v1/groups/?offset=0&limit=10,
                    'next': None,
                    'previous': None,
                    'last': /api/v1/groups/?offset=0&limit=10
                },
                'data': [
                                {
                                    "uuid": "16fd2706-8baf-433b-82eb-8c7fada847da",
                                    "name": "GroupA"
                                },
                                {
                                    "uuid": "20ecdcd0-397c-4ede-8940-f3439bf40212",
                                    "name": "GroupB"
                                }
                            ]
            }

        """
        return super().list(request=request, args=args, kwargs=kwargs)

    def retrieve(self, request, *args, **kwargs):
        """Get a group.

        @api {get} /api/v1/groups/:uuid   Get a group
        @apiName getGroup
        @apiGroup Group
        @apiVersion 1.0.0
        @apiDescription Get a group

        @apiHeader {String} token User authorization token

        @apiParam (Path) {String} id Group unique identifier.

        @apiSuccess {String} uuid Group unique identifier
        @apiSuccess {String} name Group name
        @apiSuccess {Array} principals Array of principals
        @apiSuccess {Array} roles Array of roles
        @apiSuccessExample {json} Success-Response:
            HTTP/1.1 200 OK
            {
                "uuid": "16fd2706-8baf-433b-82eb-8c7fada847da",
                "name": "GroupA",
                "principals": [
                    { "username": "jsmith" }
                ],
                "roles": [
                    {
                        "name": "RoleA",
                        "uuid": "4df211e0-2d88-49a4-8802-728630224d15"
                    }
                ]
            }
        """
        validate_uuid(kwargs.get("uuid"), "group uuid validation")
        return super().retrieve(request=request, args=args, kwargs=kwargs)

    def destroy(self, request, *args, **kwargs):
        """Delete a group.

        @api {delete} /api/v1/groups/:uuid   Delete a group
        @apiName deleteGroup
        @apiGroup Group
        @apiVersion 1.0.0
        @apiDescription Delete a group

        @apiHeader {String} token User authorization token

        @apiParam (Path) {String} uuid Group unique identifier

        @apiSuccessExample {json} Success-Response:
            HTTP/1.1 204 NO CONTENT
        """
        validate_uuid(kwargs.get("uuid"), "group uuid validation")

        with transaction.atomic():
            self.protect_special_groups("delete")
            group = self.get_object()
            if not request.user.admin:
                self.protect_group_with_user_access_admin_role(group.roles_with_access(), "remove_group")

            # Capture these before deletion
            is_custom_default_group = group.platform_default
            group_tenant = group.tenant

            dual_write_handler = RelationApiDualWriteGroupHandler(group, ReplicationEventType.DELETE_GROUP)
            roles = Role.objects.filter(policies__group=group)
            if not group.platform_default and not group.principals.exists() and not roles.exists():
                expected_empty_relation_reason = (
                    f"No principal or role found for group({group.uuid}): '{group.name}'. "
                    "Assuming no current relations exist. "
                    f"event_type='{ReplicationEventType.DELETE_GROUP}'",
                )
                dual_write_handler.set_expected_empty_relation_reason(expected_empty_relation_reason)
            else:
                dual_write_handler.prepare_to_delete_group(roles)

            response = super().destroy(request=request, args=args, kwargs=kwargs)

            dual_write_handler.replicate()

            # Restore USER default role bindings if custom default group was deleted
            if is_custom_default_group:
                role_binding_service = RoleBindingService(tenant=group_tenant)
                role_binding_service.restore_user_default_bindings()

        if response.status_code == status.HTTP_204_NO_CONTENT:
            group_obj_change_notification_handler(request.user, group, "deleted")

            auditlog = AuditLog()
            auditlog.log_delete(request, AuditLog.GROUP, group)
        return response

    def update(self, request, *args, **kwargs):
        """Update a group.

        @api {put} /api/v1/groups/:uuid   Update a group
        @apiName updateGroup
        @apiGroup Group
        @apiVersion 1.0.0
        @apiDescription Update a group

        @apiHeader {String} token User authorization token

        @apiParam (Path) {String} id Group unique identifier

        @apiSuccess {String} uuid Group unique identifier
        @apiSuccess {String} name Group name
        @apiSuccessExample {json} Success-Response:
            HTTP/1.1 200 OK
            {
                "uuid": "16fd2706-8baf-433b-82eb-8c7fada847da",
                "name": "GroupA"
            }
        """
        validate_uuid(kwargs.get("uuid"), "group uuid validation")
        self.protect_special_groups("update")

        group = self.get_object()

        if not request.user.admin:
            self.protect_group_with_user_access_admin_role(group.roles_with_access(), "update_group")

        self.restrict_custom_default_group_renaming(request, group)

        update_group = super().update(request=request, args=args, kwargs=kwargs)

        if status.is_success(update_group.status_code):
            auditlog = AuditLog()
            auditlog.log_edit(request, AuditLog.GROUP, group)

        return update_group

    def validate_principals_in_proxy_request(self, principals, org_id=None):
        """Validate principals in proxy request."""
        users = [principal.get("username") for principal in principals]
        resp = self.proxy.request_filtered_principals(
            users, org_id=org_id, limit=len(users), options={"return_id": True}
        )
        if "errors" in resp:
            return resp
        if len(resp.get("data", [])) == 0:
            return {
                "status_code": status.HTTP_404_NOT_FOUND,
                "errors": [
                    {
                        "detail": "User(s) {} not found.".format(users),
                        "status": "404",
                        "source": "principals",
                    }
                ],
            }
        return resp

    def add_users(self, group, principals_from_response, org_id=None):
        """Add principals to the group."""
        tenant = self.request.tenant
        new_principals = []
        for item in principals_from_response:
            # cross-account request principals won't be in the resp from BOP since they don't exist
            username = item["username"]
            try:
                principal = Principal.objects.get(username__iexact=username, tenant=tenant)
                if principal.user_id is None and "user_id" in item:
                    # Some lazily created Principals may not have user_id.
                    user_id = item["user_id"]
                    principal.user_id = user_id
                    principal.save()
            except Principal.DoesNotExist:
                principal = Principal.objects.create(username=username, tenant=tenant, user_id=item["user_id"])
                logger.info("Created new principal %s for org_id %s.", username, org_id)
            group.principals.add(principal)
            new_principals.append(principal)
            group_principal_change_notification_handler(self.request.user, group, username, "added")
        return group, new_principals

    def ensure_id_for_service_accounts_exists(
        self,
        user: User,
        service_accounts: Iterable[dict],
    ):
        """Validate service account in IT Service and populate user IDs if needed."""
        # Fetch all the user's service accounts from IT. If we are on a development or testing environment, we might
        # want to skip calling IT
        it_service = ITService()
        if not settings.IT_BYPASS_IT_CALLS:
            it_service_accounts = it_service.request_service_accounts(bearer_token=user.bearer_token)

            # Organize them by their client ID.
            it_service_accounts_by_client_ids: dict[str, dict] = {}
            for it_sa in it_service_accounts:
                it_service_accounts_by_client_ids[it_sa["clientId"]] = it_sa

            # Make sure that the service accounts the user specified are visible by them.
            invalid_service_accounts: set = set()
            for specified_sa in service_accounts:
                client_id = specified_sa["clientId"]
                it_sa = it_service_accounts_by_client_ids.get(client_id)
                if it_sa is None:
                    invalid_service_accounts.add(client_id)
                elif "userId" in it_sa:
                    # Service may not be returning userId's yet.
                    specified_sa["userId"] = it_sa["userId"]

            # If we have any invalid service accounts, notify the user.
            if len(invalid_service_accounts) > 0:
                raise ServiceAccountNotFoundError(f"Service account(s) {invalid_service_accounts} not found.")

    def add_service_accounts(
        self,
        group: Group,
        service_accounts: Iterable[dict],
        org_id: str = "",
    ) -> Tuple[Group, List[Principal]]:
        """Add service accounts to the group."""
        # Get the tenant in order to fetch or store the service account in the database.
        tenant: Tenant = self.request.tenant
        new_service_accounts = []
        # Fetch the service account from our database to add it to the group. If it doesn't exist, we create
        # it.
        for specified_sa in service_accounts:
            client_id = specified_sa["clientId"]
            user_id = specified_sa.get("userId")
            try:
                principal = Principal.objects.get(
                    username__iexact=SERVICE_ACCOUNT_USERNAME_FORMAT.format(clientId=client_id),
                    tenant=tenant,
                )
                if principal.user_id is None and user_id is not None:
                    # May happen in case principal is lazily created without user ID.
                    principal.user_id = user_id
                    principal.save()
            except Principal.DoesNotExist:
                principal = Principal.objects.create(
                    username=SERVICE_ACCOUNT_USERNAME_FORMAT.format(clientId=client_id),
                    user_id=user_id,
                    service_account_id=client_id,
                    type=Principal.Types.SERVICE_ACCOUNT,
                    tenant=tenant,
                )

                logger.info("Created new service account %s for org_id %s.", client_id, org_id)

            group.principals.add(principal)
            new_service_accounts.append(principal)
            group_principal_change_notification_handler(
                self.request.user,
                group,
                SERVICE_ACCOUNT_USERNAME_FORMAT.format(clientId=client_id),
                "added",
            )

        return group, new_service_accounts

    def remove_users(self, group, principals, org_id=None):
        """Process list of principals and remove them from the group."""
        req_id = getattr(self.request, "req_id", None)
        log_prefix = f"[Request_id:{req_id}]"
        logger.info(f"{log_prefix} remove_principals({principals}),Group:{group.name},OrgId:{org_id}")

        tenant = Tenant.objects.get(org_id=org_id)

        valid_principals = Principal.objects.filter(
            group=group, tenant=tenant, type=Principal.Types.USER, username__in=principals
        )
        valid_usernames = valid_principals.values_list("username", flat=True)
        usernames_diff = set(principals) - set(valid_usernames)
        if usernames_diff:
            logger.info(f"Principals {usernames_diff} not found for org id {org_id}.")
            return {
                "status_code": status.HTTP_404_NOT_FOUND,
                "errors": [
                    {
                        "detail": f"User(s) {usernames_diff} not found in the group '{group.name}'.",
                        "status": status.HTTP_404_NOT_FOUND,
                        "source": "principals",
                    }
                ],
            }, []

        principals_to_remove = []
        for principal in valid_principals:
            group.principals.remove(principal)
            principals_to_remove.append(principal)

        logger.info(f"[Request_id:{req_id}] {valid_usernames} removed from group {group.name} for org id {org_id}.")
        for username in principals:
            group_principal_change_notification_handler(self.request.user, group, username, "removed")
        return group, principals_to_remove

    @action(detail=True, methods=["get", "post", "delete"])
    def principals(self, request: Request, uuid: Optional[UUID] = None):
        """Alias for individual methods based on the HTTP method."""
        if request.method == "GET":
            return self._list_principals_in_group(request, uuid)
        elif request.method == "POST":
            return self._add_principal_into_group(request, uuid)
        elif request.method == "DELETE":
            return self._remove_principal_from_group(request, uuid)
        return Response({"error": "Method not allowed"}, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def _list_principals_in_group(self, request: Request, uuid: Optional[UUID] = None):
        """List principals in a group."""
        """
        @api {get} /api/v1/groups/:uuid/principals/    Get principals for a group
        @apiName getPrincipals
        @apiGroup Group
        @apiVersion 1.0.0
        @apiDescription Get principals for a group

        @apiHeader {String} token User authorization token

        @apiParam (Path) {String} id Group unique identifier
        @apiParam (Query) {String} principal_type Parameter for selecting the type of principal to be returned (either
                                                  "service-account" or "user").

        @apiSuccess {String} uuid Group unique identifier
        @apiSuccess {String} name Group name
        @apiSuccess {Array} principals Array of principals
        @apiSuccessExample {json} Success-Response:
            HTTP/1.1 200 OK
            {
                "principals": [
                    { "username": "jsmith" }
                ]
            }
        """
        validate_uuid(uuid, "group uuid validation")
        group = self.get_object()

        # Check if the request comes with a bunch of service account client IDs that we need to check. Since this
        # query parameter is incompatible with any other query parameter, we make the checks first. That way if any
        # other query parameter was specified, we simply return early.
        if SERVICE_ACCOUNT_CLIENT_IDS_KEY in request.query_params:
            return self.get_sa_group_membership(group, request)

        # Get the "order_by" query parameter.
        all_valid_fields = VALID_PRINCIPAL_ORDER_FIELDS + ["-" + field for field in VALID_PRINCIPAL_ORDER_FIELDS]
        sort_order = None
        if request.query_params.get(ORDERING_PARAM):
            sort_field = validate_and_get_key(request.query_params, ORDERING_PARAM, all_valid_fields, "username")
            sort_order = "des" if sort_field == "-username" else "asc"

        # Get the "username_only" query parameter.
        username_only = validate_and_get_key(
            request.query_params,
            USERNAME_ONLY_KEY,
            VALID_BOOLEAN_VALUE,
            default_value="false",
            required=False,
        )

        # Build the 'options' dict.
        options: dict = {"sort_order": sort_order, "username_only": username_only}

        # Attempt validating and obtaining the "principal_type" query parameter.
        # It is important because we need to call BOP for the users, and IT for the service accounts.
        principalType = validate_and_get_key(
            request.query_params,
            PRINCIPAL_TYPE_KEY,
            VALID_PRINCIPAL_TYPE_VALUE,
            default_value=Principal.Types.USER,
            required=False,
        )

        # Store the principal type in the 'options' dict.
        options[PRINCIPAL_TYPE_KEY] = principalType

        if principalType == Principal.Types.SERVICE_ACCOUNT:
            response = self._list_service_accounts_in_group(request, group, options)

        elif principalType == Principal.Types.USER:
            response = self._list_user_based_principals_in_group(request, group, options)

        elif principalType == ALL_KEY:
            response = self._list_both_principal_types_in_group(request, group, options)

        return response

    def _add_principal_into_group(self, request: Request, uuid: Optional[UUID] = None):
        """Add principals into a group."""
        """
        @api {post} /api/v1/groups/:uuid/principals/   Add principals to a group
        @apiName addPrincipals
        @apiGroup Group
        @apiVersion 1.0.0
        @apiDescription Add principals to a group

        @apiHeader {String} token User authorization token

        @apiParam (Path) {String} id Group unique identifier

        @apiParam (Request Body) {String} username Principal username
        @apiParamExample {json} Request Body:
            {
                "principals": [
                    {
                        "username": "jsmith"
                    },
                    {
                        "username": "ksmith"
                    }
                ]
            }

        @apiSuccess {String} uuid Group unique identifier
        @apiSuccess {String} name Group name
        @apiSuccess {Array} principals Array of principals
        @apiSuccessExample {json} Success-Response:
            HTTP/1.1 200 OK
            {
                "uuid": "16fd2706-8baf-433b-82eb-8c7fada847da",
                "name": "GroupA",
                "principals": [
                    { "username": "jsmith" }
                ]
            }
        """
        validate_uuid(uuid, "group uuid validation")
        org_id = self.request.user.org_id

        serializer = GroupPrincipalInputSerializer(data=request.data)

        # Serialize the payload and validate that it is correct.
        user_specified_principals = []
        if serializer.is_valid(raise_exception=True):
            user_specified_principals = serializer.data.pop("principals")

        # Extract the principals and the service accounts from the user's payload.
        principals = []
        service_accounts = []
        for specified_principal in user_specified_principals:
            if ("type" in specified_principal) and (specified_principal["type"] == Principal.Types.SERVICE_ACCOUNT):
                service_accounts.append(specified_principal)
            else:
                principals.append(specified_principal)

        with transaction.atomic():
            group = self.get_object()
            self.protect_special_groups("add principals", group, additional="platform_default")

            if not request.user.admin:
                self.protect_group_with_user_access_admin_role(group.roles_with_access(), "add principals")

            # Process the service accounts and add them to the group.
            if len(service_accounts) > 0:
                token_validator = ITSSOTokenValidator()
                request.user.bearer_token = token_validator.validate_token(
                    request=request,
                    additional_scopes_to_validate=set[ScopeClaims]([ScopeClaims.SERVICE_ACCOUNTS_CLAIM]),
                )
                try:
                    self.ensure_id_for_service_accounts_exists(user=request.user, service_accounts=service_accounts)
                except InsufficientPrivilegesError as ipe:
                    return Response(
                        status=status.HTTP_403_FORBIDDEN,
                        data={
                            "errors": [
                                {
                                    "detail": str(ipe),
                                    "status": str(status.HTTP_403_FORBIDDEN),
                                    "source": "groups",
                                }
                            ]
                        },
                    )
                except ServiceAccountNotFoundError as err:
                    return Response(
                        status=status.HTTP_404_NOT_FOUND,
                        data={
                            "errors": [
                                {
                                    "detail": str(err),
                                    "source": "groups",
                                    "status": str(status.HTTP_404_NOT_FOUND),
                                }
                            ]
                        },
                    )

            # Process user principals and add them to the group.
            principals_from_response = []
            if len(principals) > 0:
                proxy_response = self.validate_principals_in_proxy_request(principals, org_id=org_id)
                if len(proxy_response.get("data", [])) > 0:
                    principals_from_response = proxy_response.get("data", [])
                if isinstance(proxy_response, dict) and "errors" in proxy_response:
                    return Response(status=proxy_response["status_code"], data=proxy_response["errors"])

            new_service_accounts = []
            if len(service_accounts) > 0:
                group, new_service_accounts = self.add_service_accounts(
                    group=group,
                    service_accounts=service_accounts,
                    org_id=org_id,
                )
                for sa in new_service_accounts:
                    auditlog = AuditLog()
                    auditlog.log_group_assignment(
                        request,
                        AuditLog.GROUP,
                        group,
                        sa,
                        Principal.Types.SERVICE_ACCOUNT,
                    )
            new_users = []
            if len(principals) > 0:
                group, new_users = self.add_users(group, principals_from_response, org_id=org_id)
                for user in new_users:
                    auditlog = AuditLog()
                    auditlog.log_group_assignment(
                        request,
                        AuditLog.GROUP,
                        group,
                        user,
                        Principal.Types.USER,
                    )

            dual_write_handler = RelationApiDualWriteGroupHandler(group, ReplicationEventType.ADD_PRINCIPALS_TO_GROUP)
            dual_write_handler.replicate_new_principals(new_users + new_service_accounts)
        # Serialize the group...
        output = GroupSerializer(group)
        response = Response(status=status.HTTP_200_OK, data=output.data)

        return response

    def _remove_principal_from_group(self, request: Request, uuid: Optional[UUID] = None):
        """Remove principals from a group."""
        """
        @api {delete} /api/v1/groups/:uuid/principals/   Remove principals from group
        @apiName removePrincipals
        @apiGroup Group
        @apiVersion 1.0.0
        @apiDescription Remove principals from a group

        @apiHeader {String} token User authorization token

        @apiParam (Path) {String} id Group unique identifier

        @apiParam (Query) {String} usernames List of comma separated principal usernames

        @apiSuccessExample {json} Success-Response:
            HTTP/1.1 204 NO CONTENT
        """
        validate_uuid(uuid, "group uuid validation")
        org_id = self.request.user.org_id

        with transaction.atomic():
            group = self.get_object()

            self.protect_special_groups("remove principals", additional="platform_default")

            if not request.user.admin:
                self.protect_group_with_user_access_admin_role(group.roles_with_access(), "remove_principals")

            if SERVICE_ACCOUNTS_KEY not in request.query_params and USERNAMES_KEY not in request.query_params:
                key = "detail"
                message = "Query parameter {} or {} is required.".format(SERVICE_ACCOUNTS_KEY, USERNAMES_KEY)
                raise serializers.ValidationError({key: _(message)})

            service_accounts_to_remove = []
            # Remove the service accounts from the group.
            if SERVICE_ACCOUNTS_KEY in request.query_params:
                service_accounts_parameter = request.query_params.get(SERVICE_ACCOUNTS_KEY, "")
                service_accounts = [
                    service_account.strip() for service_account in service_accounts_parameter.split(",")
                ]

                service_accounts_to_remove = self.remove_service_accounts(
                    user=request.user,
                    service_accounts=service_accounts,
                    group=group,
                    org_id=org_id,
                )
                # Save the information to audit logs
                for service_account_info in service_accounts_to_remove:
                    auditlog = AuditLog()
                    auditlog.log_group_remove(
                        request,
                        AuditLog.GROUP,
                        group,
                        service_account_info,
                        Principal.Types.SERVICE_ACCOUNT,
                    )
                # Create a default and successful response object. If no user principals are to be removed below,
                # this response will be returned. Else, it will be overridden with whichever response the user
                # removal generates.
                response = Response(status=status.HTTP_204_NO_CONTENT)

            users_to_remove = []
            # Remove the users from the group too.
            if USERNAMES_KEY in request.query_params:
                username = request.query_params.get(USERNAMES_KEY, "")
                principals = [name.strip() for name in username.split(",")]
                resp, users_to_remove = self.remove_users(group, principals, org_id=org_id)
                if isinstance(resp, dict) and "errors" in resp:
                    return Response(status=resp.get("status_code"), data={"errors": resp.get("errors")})

                # Save the information to audit logs
                for users_info in users_to_remove:
                    auditlog = AuditLog()
                    auditlog.log_group_remove(
                        request,
                        AuditLog.GROUP,
                        group,
                        users_info,
                        Principal.Types.USER,
                    )
                response = Response(status=status.HTTP_204_NO_CONTENT)

            dual_write_handler = RelationApiDualWriteGroupHandler(
                group,
                ReplicationEventType.REMOVE_PRINCIPALS_FROM_GROUP,
            )
            dual_write_handler.replicate_removed_principals(users_to_remove + service_accounts_to_remove)

        return response

    @staticmethod
    def get_sa_group_membership(group, request):
        """Process the request with the SERVICE_ACCOUNT_CLIENT_IDS_KEY query param."""
        # pagination is ignored in this case
        for query_param in request.query_params:
            if query_param not in [
                SERVICE_ACCOUNT_CLIENT_IDS_KEY,
                "limit",
                "offset",
            ]:
                return Response(
                    status=status.HTTP_400_BAD_REQUEST,
                    data={
                        "errors": [
                            {
                                "detail": f"The '{SERVICE_ACCOUNT_CLIENT_IDS_KEY}' parameter is incompatible with "
                                f"any other query parameter. Please, use it alone",
                                "source": "groups",
                                "status": str(status.HTTP_400_BAD_REQUEST),
                            }
                        ]
                    },
                )

        # Check that the specified query parameter is not empty.
        service_account_client_ids_raw = request.query_params.get(SERVICE_ACCOUNT_CLIENT_IDS_KEY).strip()
        if not service_account_client_ids_raw:
            return Response(
                status=status.HTTP_400_BAD_REQUEST,
                data={
                    "errors": [
                        {
                            "detail": "Not a single client ID was specified for the client IDs filter",
                            "source": "groups",
                            "status": str(status.HTTP_400_BAD_REQUEST),
                        }
                    ]
                },
            )

        # Turn the received and comma separated client IDs into a manageable set.
        received_client_ids: set[str] = set(service_account_client_ids_raw.split(","))

        # Validate that the provided strings are actually UUIDs.
        for rci in received_client_ids:
            try:
                UUID(rci)
            except ValueError:
                return Response(
                    status=status.HTTP_400_BAD_REQUEST,
                    data={
                        "errors": [
                            {
                                "detail": f"The specified client ID '{rci}' is not a valid UUID",
                                "source": "groups",
                                "status": str(status.HTTP_400_BAD_REQUEST),
                            }
                        ]
                    },
                )

        # Generate the report of which of the tenant's service accounts are in a group, and which
        # ones are available to be added to the given group.
        it_service = ITService()
        result: dict = it_service.generate_service_accounts_report_in_group(
            group=group, client_ids=received_client_ids
        )

        # Prettify the output payload and return it.
        return Response(
            status=status.HTTP_200_OK,
            data={
                "meta": {"count": len(result)},
                "links": {},
                "data": result,
            },
        )

    def _list_service_accounts_in_group(self, request, group, options):
        """List service account based principals in the group."""
        # Get the service account's description and name filters, and the principal's username filter too.
        # Finally, get the limit and offset parameters.
        options[SERVICE_ACCOUNT_DESCRIPTION_KEY] = request.query_params.get(SERVICE_ACCOUNT_DESCRIPTION_KEY)
        options[SERVICE_ACCOUNT_NAME_KEY] = request.query_params.get(SERVICE_ACCOUNT_NAME_KEY)

        # Get the "principal username" parameter.
        options[PRINCIPAL_USERNAME_KEY] = request.query_params.get(PRINCIPAL_USERNAME_KEY)

        # Validate the token only if username_only is false (default value)
        if options["username_only"] == "false":
            token_validator = ITSSOTokenValidator()
            request.user.bearer_token = token_validator.validate_token(
                request=request,
                additional_scopes_to_validate=set[ScopeClaims]([ScopeClaims.SERVICE_ACCOUNTS_CLAIM]),
            )
        # Fetch the group's service accounts.
        it_service = ITService()
        try:
            service_accounts = it_service.get_service_accounts_group(group=group, user=request.user, options=options)
        except (
            requests.exceptions.ConnectionError,
            UnexpectedStatusCodeFromITError,
        ):
            return Response(
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                data={
                    "errors": [
                        {
                            "detail": "Unexpected internal error.",
                            "source": "principals",
                            "status": str(status.HTTP_500_INTERNAL_SERVER_ERROR),
                        }
                    ]
                },
            )

        if options["username_only"] == "true":
            resp = Response(status=200, data=service_accounts)
            page = self.paginate_queryset(resp.data)
            return self.get_paginated_response(page)

        # Prettify the output payload and return it.
        page = self.paginate_queryset(service_accounts)
        serializer = ServiceAccountSerializer(page, many=True)

        return self.get_paginated_response(serializer.data)

    def _list_user_based_principals_in_group(self, request, group, options):
        """List user based principals in the group."""
        principals_from_params = self.filtered_principals(group, request)
        username_list = [principal.username for principal in principals_from_params]

        admin_only = validate_and_get_key(request.query_params, ADMIN_ONLY_KEY, VALID_BOOLEAN_VALUE, False, False)
        if admin_only == "true":
            options[ADMIN_ONLY_KEY] = True

        proxy = PrincipalProxy()
        org_id = self.request.user.org_id
        resp = proxy.request_filtered_principals(username_list, org_id=org_id, options=options)
        if isinstance(resp, dict) and "errors" in resp:
            return Response(status=resp.get("status_code"), data=resp.get("errors"))

        # For 'principal_type=all' we need to return before pagination is applied
        if options["principal_type"] == ALL_KEY:
            return resp

        page = self.paginate_queryset(resp.get("data"))
        return self.get_paginated_response(page)

    def _list_both_principal_types_in_group(self, request, group, options):
        """
        List both principal types (user based, service account based) in the group.

        First we try to list service account based principals and then user based principals.
        For the second query we need to calculate new limit and offset.
        Example:
            the group contains 3 SA + 4 U, limit = 2, offset = 0
            pagination:
            page 1 -> 2 SA
            page 2 -> 1 SA + 1 U
            page 3 -> 2 U
            page 4 -> 1 U
        """
        paginator = StandardResultsSetPagination()
        paginator.paginate_queryset([], request)
        limit = paginator.limit
        offset = paginator.offset

        # Get Service Account based principals
        response_sa = self._list_service_accounts_in_group(request, group, options)
        if response_sa.status_code != status.HTTP_200_OK:
            return response_sa

        # Calculate new limit and offset for the user based principals query
        sa_count_total = int(response_sa.data.get("meta").get("count"))
        sa_count = len(response_sa.data.get("data", []))
        remaining_limit = limit - sa_count
        if remaining_limit == 0:
            new_limit = 1
            new_offset = 0
        elif remaining_limit > 0:
            if offset >= sa_count_total:
                new_limit = limit
                new_offset = offset - sa_count_total
            else:
                new_limit = remaining_limit
                new_offset = 0

        # Get User based principals
        response_user = self._list_user_based_principals_in_group(request, group, options)
        if response_user.get("status_code") != status.HTTP_200_OK:
            return response_user

        # Calculate the total count and save it for pagination
        user_count_total = len(response_user.get("data"))
        self.paginator.count = sa_count_total + user_count_total

        # Put together the final response
        response_data = {}

        if response_sa.data.get("data", []):
            response_data["serviceAccounts"] = response_sa.data.get("data")

        if response_user.get("data", []) and remaining_limit:
            response_data["users"] = response_user.get("data")[new_offset : new_offset + new_limit]  # noqa: E203

        return self.get_paginated_response(response_data)

    @action(detail=True, methods=["get", "post", "delete"])
    def roles(self, request, uuid=None, principals=None):
        """Get, add or remove roles from a group."""
        """
        @api {get} /api/v1/groups/:uuid/roles/   Get roles for a group
        @apiName getRoles
        @apiGroup Group
        @apiVersion 1.0.0
        @apiDescription Get roles for a group

        @apiHeader {String} token User authorization token

        @apiParam (Path) {String} id Group unique identifier.

        @apiParam (Query) {String} order_by Determine ordering of returned roles.

        @apiSuccess {Array} data Array of roles
        @apiSuccessExample {json} Success-Response:
            HTTP/1.1 200 OK
            {
                "data": [
                    {
                        "name": "RoleA",
                        "uuid": "4df211e0-2d88-49a4-8802-728630224d15",
                        "description": "RoleA Description",
                        "policyCount: 0,
                        "applications": [],
                        "system": false,
                        "platform_default": false
                    }
                ]
            }
        """
        """
        @api {post} /api/v1/groups/:uuid/roles/   Add roles to a group
        @apiName addRoles
        @apiGroup Group
        @apiVersion 1.0.0
        @apiDescription Add roles to a group
        @apiHeader {String} token User authorization token
        @apiParam (Path) {String} id Group unique identifier
        @apiParam (Request Body) {Array} roles Array of role UUIDs
        @apiParamExample {json} Request Body:
            {
                "roles": [
                    "4df211e0-2d88-49a4-8802-728630224d15"
                ]
            }
        @apiSuccess {String} uuid Group unique identifier
        @apiSuccess {String} name Group name
        @apiSuccess {Array} roles Array of roles
        @apiSuccessExample {json} Success-Response:
            HTTP/1.1 200 OK
            {
                "data": [
                    {
                        "name": "RoleA",
                        "uuid": "4df211e0-2d88-49a4-8802-728630224d15",
                        "description": "RoleA Description",
                        "policyCount: 0,
                        "applications": [],
                        "system": false,
                        "platform_default": false
                    }
                ]
            }
        """
        """
        @api {delete} /api/v1/groups/:uuid/roles/   Remove roles from group
        @apiName removeRoles
        @apiGroup Group
        @apiVersion 1.0.0
        @apiDescription Remove roles from a group

        @apiHeader {String} token User authorization token

        @apiParam (Path) {String} id Group unique identifier

        @apiParam (Query) {String} roles List of comma separated role UUIDs

        @apiSuccessExample {json} Success-Response:
            HTTP/1.1 204 NO CONTENT
        """
        roles = []
        validate_uuid(uuid, "group uuid validation")
        group = self.get_object()
        v2_write_msg = (
            "V1 role-to-group assignment operations are not allowed "
            "for orgs using workspaces. Use v2 role bindings instead."
        )
        if request.method in ("POST", "DELETE") and is_v2_edit_enabled_for_request(request):
            return Response(
                status=status.HTTP_403_FORBIDDEN,
                data={"errors": [{"detail": v2_write_msg}]},
            )
        if request.method == "POST":
            self.protect_default_admin_group_roles(group)

            if not request.user.admin:
                self.protect_group_with_user_access_admin_role(group.roles_with_access(), "add_role")

            serializer = GroupRoleSerializerIn(data=request.data)
            if serializer.is_valid(raise_exception=True):
                roles = request.data.pop(ROLES_KEY, [])

            try:
                original_group_pk = group.pk
                with transaction.atomic():
                    assert_v1_write_allowed(request.tenant)
                    group = set_system_flag_before_update(group, request.tenant, request.user)
                    add_roles(group, roles, request.tenant, user=request.user)
            except V1WriteBlockedError:
                return Response(
                    status=status.HTTP_403_FORBIDDEN,
                    data={"errors": [{"detail": v2_write_msg}]},
                )

            response_data = GroupRoleSerializerIn(group)
            response = Response(status=status.HTTP_200_OK, data=response_data.data)
            if status.is_success(response.status_code):
                if group is not None and group.pk != original_group_pk:
                    auditlog = AuditLog()
                    auditlog.log_create_from_object(request, AuditLog.GROUP, group)

                for role in response_data.data["data"]:
                    auditlog = AuditLog()
                    auditlog.log_group_assignment(
                        request,
                        AuditLog.GROUP,
                        group,
                        role,
                        AuditLog.ROLE,
                    )

        elif request.method == "GET":
            serialized_roles = self.obtain_roles(request, group)
            page = self.paginate_queryset(serialized_roles)
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        else:
            self.protect_default_admin_group_roles(group)

            if not request.user.admin:
                self.protect_group_with_user_access_admin_role(group.roles_with_access(), "remove_role")

            if ROLES_KEY not in request.query_params:
                key = "detail"
                message = "Query parameter {} is required.".format(ROLES_KEY)
                raise serializers.ValidationError({key: _(message)})

            role_ids = request.query_params.get(ROLES_KEY, "").split(",")
            serializer = GroupRoleSerializerIn(data={"roles": role_ids})
            if serializer.is_valid(raise_exception=True):
                original_group_pk = group.pk
                try:
                    with transaction.atomic():
                        assert_v1_write_allowed(request.tenant)
                        group = set_system_flag_before_update(group, request.tenant, request.user)
                        remove_roles(group, role_ids, request.tenant, request.user)
                except V1WriteBlockedError:
                    return Response(
                        status=status.HTTP_403_FORBIDDEN,
                        data={"errors": [{"detail": v2_write_msg}]},
                    )

                # Log custom default group creation if a new group was actually cloned
                if group is not None and group.pk != original_group_pk:
                    auditlog = AuditLog()
                    auditlog.log_create_from_object(request, AuditLog.GROUP, group)

                # Save the information to audit logs
                roles = _roles_by_query_or_ids(role_ids, request.tenant)
                for role_info in roles:
                    auditlog = AuditLog()
                    auditlog.log_group_remove(
                        request,
                        AuditLog.GROUP,
                        group,
                        role_info,
                        AuditLog.ROLE,
                    )
            response = Response(status=status.HTTP_204_NO_CONTENT)

            return response

        return Response(status=status.HTTP_200_OK, data=response_data.data)

    def order_queryset(self, queryset, valid_fields, order_field):
        """Return queryset ordered according to order_by query param."""
        all_valid_fields = valid_fields + ["-" + field for field in valid_fields]
        if order_field in all_valid_fields:
            return queryset.order_by(order_field)
        else:
            key = "detail"
            message = f"{order_field} is not a valid ordering field. Valid values are {all_valid_fields}"
            raise serializers.ValidationError({key: _(message)})

    def filtered_roles(self, roles, request):
        """Return filtered roles for group from query params."""
        role_filters = self.filters_from_params(VALID_GROUP_ROLE_FILTERS, "role", request)
        role_filters = self.add_role_external_tenant_filter(role_filters, request)
        return roles.filter(**role_filters)

    def add_role_external_tenant_filter(self, role_filters, request):
        """Add role external tenant filter if param is on the request."""
        role_external_tenant = request.query_params.get("role_external_tenant")
        if role_external_tenant:
            role_filters["ext_relation__ext_tenant__name__iexact"] = role_external_tenant
        return role_filters

    def filtered_principals(self, group, request):
        """Return filtered user principals for group from query params."""
        principal_filters = self.filters_from_params(VALID_GROUP_PRINCIPAL_FILTERS, "principal", request)
        # Make sure we only return users.
        return group.principals.filter(**principal_filters).filter(type=Principal.Types.USER)

    def filters_from_params(self, valid_filters, model_name, request):
        """Build filters from group params."""
        filters = {}
        for param_name, param_value in request.query_params.items():
            if param_name in valid_filters:
                attr_filter_name = param_name.replace(f"{model_name}_", "")
                filters[f"{attr_filter_name}__icontains"] = param_value
        return filters

    def obtain_roles(self, request, group):
        """Obtain roles based on request, supports exclusion."""
        exclude = validate_and_get_key(request.query_params, EXCLUDE_KEY, VALID_EXCLUDE_VALUES, "false")

        roles = group.roles_with_access() if exclude == "false" else self.obtain_roles_with_exclusion(request, group)
        filtered_roles = self.filtered_roles(roles, request)
        annotated_roles = filtered_roles.annotate(policyCount=Count("policies", distinct=True))

        order_field = request.query_params.get(ORDERING_PARAM, NAME_KEY)
        ordered_roles = self.order_queryset(annotated_roles, VALID_ROLE_ORDER_FIELDS, order_field)
        return [RoleMinimumSerializer(role).data for role in ordered_roles]

    def obtain_roles_with_exclusion(self, request, group):
        """Obtain the queryset for roles based on scope."""
        # Get roles in principal or account scope
        roles = get_role_queryset(request)

        # Exclude the roles in the group
        roles_for_group = group.roles().values("uuid")
        return roles.exclude(uuid__in=roles_for_group)

    def remove_service_accounts(self, user: User, group: Group, service_accounts: Iterable[str], org_id: str = ""):
        """Remove the given service accounts from the tenant."""
        # Log our intention.
        request_id = getattr(self.request, "req_id", None)
        logger.info(
            f"[Request_id: {request_id}] remove_service_accounts({service_accounts}),"
            f"Group:{group.name},OrgId:{org_id},Acct:{user.account}"
        )

        # Fetch the tenant from the database.
        tenant = Tenant.objects.get(org_id=org_id)

        # Get the group's service accounts that match the service accounts that the user specified.
        valid_service_accounts = Principal.objects.filter(
            group=group,
            tenant=tenant,
            type=Principal.Types.SERVICE_ACCOUNT,
            service_account_id__in=service_accounts,
        )

        # Collect the service account IDs the user specified.
        valid_service_account_ids = valid_service_accounts.values_list("service_account_id", flat=True)

        # If there is a difference in the sets, then we know that the user specified service accounts
        # that did not exist in the database.
        service_account_ids_diff = set(service_accounts).difference(valid_service_account_ids)

        if service_account_ids_diff:
            logger.info(f"Service accounts {service_account_ids_diff} not found for org id {org_id}.")

            raise Http404(f"Service account(s) {service_account_ids_diff} not found in the group '{group.name}'")

        removed_service_accounts = []
        # Remove service accounts from the group.
        for service_account in valid_service_accounts:
            group.principals.remove(service_account)
            removed_service_accounts.append(service_account)

        logger.info(
            f"[Request_id:{request_id}] {valid_service_account_ids} "
            f"removed from group {group.name} for org id {org_id}."
        )
        for username in service_accounts:
            group_principal_change_notification_handler(self.request.user, group, username, "removed")

        return removed_service_accounts
