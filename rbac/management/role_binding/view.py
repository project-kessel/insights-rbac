#
# Copyright 2025 Red Hat, Inc.
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
"""View for role binding management."""

import logging

from management.audit_log.model import AuditLog
from management.base_viewsets import BaseV2ViewSet
from management.group.model import Group
from management.permissions.role_binding_access import (
    RoleBindingKesselAccessPermission,
    RoleBindingSystemUserAccessPermission,
)
from management.permissions.v2_edit_api_access import V2WriteRequiresWorkspacesEnabled
from management.role.v2_model import RoleV2
from management.v2_mixins import AtomicOperationsMixin
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from api.common.pagination import V2CursorPagination
from .serializer import (
    BatchCreateRoleBindingRequestSerializer,
    BatchCreateRoleBindingResponseItemSerializer,
    RoleBindingInputSerializer,
    RoleBindingListInputSerializer,
    RoleBindingListOutputSerializer,
    RoleBindingOutputSerializer,
    UpdateRoleBindingRequestSerializer,
    UpdateRoleBindingResponseSerializer,
    resolve_resource_identifiers,
)
from .service import RoleBindingService

logger = logging.getLogger(__name__)


class _BindingWithRole:
    """Proxy that presents a RoleBinding with a different role.

    Used to expand platform-role bindings into per-child-role entries
    so the list endpoint returns concrete roles instead of abstract parents.
    """

    def __init__(self, binding, role):
        self._binding = binding
        self.role = role

    def __getattr__(self, name):
        return getattr(self._binding, name)


def _expand_platform_roles(bindings):
    """Replace platform-role bindings with one entry per child role.

    Non-platform bindings pass through unchanged.  Platform bindings are
    expanded: each child role produces a separate proxy entry that shares
    the original binding's subject/resource data but overrides the role.

    Note: this runs after pagination, so the actual number of items
    returned may slightly exceed the requested page size when platform
    roles are expanded into multiple children.  This is acceptable
    because platform-role bindings are few (only default bindings).
    """
    expanded = []
    for binding in bindings:
        if binding.role and binding.role.type == RoleV2.Types.PLATFORM:
            for child in binding.role.children.all():
                expanded.append(_BindingWithRole(binding, child))
        else:
            expanded.append(binding)
    return expanded


class RoleBindingViewSet(AtomicOperationsMixin, BaseV2ViewSet):
    """Role Binding ViewSet.

    Provides access to role bindings with support for listing and updating.

    Query Parameters (by-subject endpoint):
        Required:
            - resource_id: Filter by resource ID
            - resource_type: Filter by resource type

        Optional:
            - subject_type: Filter by subject type (e.g., 'group')
            - subject_id: Filter by subject ID
            - fields: Control which fields are included in the response
            - order_by: Sort by specified field(s), prefix with '-' for descending
    """

    _SUBJECT_NAME_ATTR = {"group": "name", "user": "username"}

    serializer_class = RoleBindingListOutputSerializer
    permission_classes = (
        RoleBindingSystemUserAccessPermission,
        RoleBindingKesselAccessPermission,
        V2WriteRequiresWorkspacesEnabled,
    )
    pagination_class = V2CursorPagination

    def _log_audit(self, request, action, description):
        """Create an audit log entry for a role binding operation."""
        audit_log = AuditLog()
        audit_log.log_v2(
            request=request,
            resource_type=AuditLog.ROLE_BINDING,
            action=action,
            resource_uuid=None,
            description=description,
        )

    @staticmethod
    def _get_subject_name(subject, subject_type):
        attr = RoleBindingViewSet._SUBJECT_NAME_ATTR.get(subject_type, "name")
        return getattr(subject, attr, str(subject))

    def get_queryset(self):
        """Return an empty queryset to satisfy DRF's contract.

        The list and by_subject actions build their own querysets.
        """
        return Group.objects.none()

    def get_serializer_class(self):
        """Get serializer class based on action."""
        if self.action == "by_subject":
            return RoleBindingOutputSerializer
        return RoleBindingListOutputSerializer

    def list(self, request, *args, **kwargs):
        """Get a list of role bindings.

        Optional query parameters:
            - role_id: Filter by role ID (UUID)
            - resource_id: Filter by resource ID (must be used with resource_type for inherited bindings)
            - resource_type: Filter by resource type (must be used with resource_id for inherited bindings)
            - subject_type: Filter by subject type (e.g., 'group')
            - subject_id: Filter by subject ID (UUID)
            - granted_subject_type: Filter by effective grant subject type ('user', 'group', or 'principal')
            - granted_subject_id: Required for 'user'/'group' (principal UUID, user_id, or group UUID)
            - granted_subject.principal.user_id: Required for 'principal' (external user ID)
            - fields: Control which fields are included in the response
            - order_by: Sort by specified field(s), prefix with '-' for descending
            - exclude_sources: 'none' (default) shows all, 'indirect' hides inherited, 'direct' hides direct
        """
        input_serializer = RoleBindingListInputSerializer(data=request.query_params)
        input_serializer.is_valid(raise_exception=True)
        validated_params = input_serializer.validated_data

        # Convert resource_tenant_org_id to resource_id before passing to service
        resource_id = validated_params.get("resource_id")
        resource_type = validated_params.get("resource_type")
        if validated_params.get("resource_tenant_org_id"):
            resource_type, resource_id = resolve_resource_identifiers(validated_params)
            validated_params = {**validated_params, "resource_id": resource_id, "resource_type": resource_type}

        service = RoleBindingService(tenant=request.tenant)
        queryset = service.get_role_bindings_for_list(validated_params)

        granted_subject_type = validated_params.get("granted_subject_type")
        granted_subject_id = validated_params.get("granted_subject_id")
        granted_subject_principal_user_id = validated_params.get("granted_subject_principal_user_id")
        if granted_subject_type:
            queryset = queryset.for_granted_subject(
                granted_subject_type,
                granted_subject_id=granted_subject_id,
                granted_subject_principal_user_id=granted_subject_principal_user_id,
            )

        field_selection = validated_params.get("fields")
        if field_selection is not None:
            needs_resource_names = "name" in field_selection.get_nested(
                "resource"
            ) or "name" in field_selection.get_nested("sources")
            if needs_resource_names:
                queryset = queryset.with_resource_names()

        page = self.paginate_queryset(queryset)
        page = _expand_platform_roles(page)

        # Build context for output serializer
        context = {
            "request": request,
            "field_selection": field_selection,
            "queried_resource_id": str(resource_id) if resource_id else None,
            "queried_resource_type": resource_type,
            "service": service,
        }

        serializer = self.get_serializer(page, many=True, context=context)
        return self.get_paginated_response(serializer.data)

    def batch_create(self, request, *args, **kwargs):
        """Grant access to a resource to a set of subjects with a set of roles."""
        return self._atomic_action(self._perform_batch_create, "batch_create", request, *args, **kwargs)

    def _perform_batch_create(self, request, *args, **kwargs):
        """Core batch create logic."""
        data = {**request.data, "fields": request.query_params.get("fields", "")}
        serializer = BatchCreateRoleBindingRequestSerializer(data=data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        created_bindings = serializer.save()

        subjects = {(b["subject_type"], str(b["subject"].uuid)) for b in created_bindings}
        resources = {(b["resource_type"], b["resource_id"]) for b in created_bindings}
        self._log_audit(
            request,
            AuditLog.CREATE,
            f"Created {len(created_bindings)} role binding(s) for {len(subjects)} subject(s)"
            f" on {len(resources)} resource(s)",
        )

        fields = serializer.validated_data.get("fields")
        response_serializer = BatchCreateRoleBindingResponseItemSerializer(
            created_bindings, many=True, context={"field_selection": fields}
        )
        return Response({"role_bindings": response_serializer.data}, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get", "put"], url_path="by-subject")
    def by_subject(self, request, *args, **kwargs):
        """Handle role bindings by subject.

        GET: List role bindings grouped by subject.
        PUT: Update role bindings for a specific subject on a resource.
        """
        if request.method == "PUT":
            return self._update_by_subject(request)
        return self._list_by_subject(request)

    def _list_by_subject(self, request):
        """List role bindings grouped by subject.

        Required query parameters (one of):
            - resource_id + resource_type: Filter by resource
            - resource.tenant.org_id: Filter by tenant org ID (shorthand)

        Optional query parameters:
            - subject_type: Filter by subject type (e.g., 'group')
            - subject_id: Filter by subject ID (UUID)
            - fields: Control which fields are included in the response
            - order_by: Sort by specified field(s), prefix with '-' for descending
        """
        # Validate and parse query parameters using input serializer
        input_serializer = RoleBindingInputSerializer(data=request.query_params)
        input_serializer.is_valid(raise_exception=True)
        validated_params = input_serializer.validated_data

        if validated_params.get("resource_tenant_org_id"):
            resource_type, resource_id = resolve_resource_identifiers(validated_params)
            validated_params = {**validated_params, "resource_id": resource_id, "resource_type": resource_type}

        service = RoleBindingService(tenant=request.tenant)

        # Get role bindings queryset using validated parameters
        queryset = service.get_role_bindings_by_subject(validated_params)

        # Build context for output serializer
        context = {
            "request": request,
            **service.build_context(validated_params),
        }

        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page, many=True, context=context)
        return self.get_paginated_response(serializer.data)

    def _update_by_subject(self, request):
        """Update role bindings for a specific subject on a resource."""
        return self._atomic_action(self._perform_update_by_subject, "update_by_subject", request)

    def _perform_update_by_subject(self, request):
        """Core update-by-subject logic."""
        data = {**request.query_params.dict(), **request.data}

        serializer = UpdateRoleBindingRequestSerializer(data=data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        result = serializer.save()

        if result.custom_default_group_created is not None:
            audit_log = AuditLog()
            audit_log.log_create_from_object(request, AuditLog.GROUP, result.custom_default_group_created)

        subject_name = self._get_subject_name(result.subject, result.subject_type)
        resource_label = result.resource_name or result.resource_id
        self._log_audit(
            request,
            AuditLog.EDIT,
            f"Updated role bindings for {result.subject_type} '{subject_name}'"
            f" on {result.resource_type} '{resource_label}': {len(result.roles)} role(s) assigned",
        )

        response_context = {
            "request": request,
            "field_selection": serializer.validated_data.get("fields"),
        }
        response_serializer = UpdateRoleBindingResponseSerializer(result, context=response_context)
        return Response(response_serializer.data, status=status.HTTP_200_OK)
