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
"""View for GroupV2 management."""

import logging

from management.atomic_transactions import atomic_block
from management.audit_log.model import AuditLog
from management.base_viewsets import BaseV2ViewSet
from management.group.model import Group
from management.group.v2_filters import GroupV2AccessFilterBackend
from management.group.v2_serializer import (
    GroupV2AddPrincipalsSerializer,
    GroupV2ListSerializer,
    GroupV2RequestSerializer,
    GroupV2ResponseSerializer,
    PrincipalV2ResponseSerializer,
)
from management.group.v2_service import GroupV2Service
from management.permissions.group_v2_access import GroupV2KesselAccessPermission
from management.permissions.v2_edit_api_access import V2WriteRequiresWorkspacesEnabled
from management.utils import v2response_error_from_errors, validate_uuid
from management.v2_mixins import AtomicOperationsMixin
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from api.common.pagination import V2CursorPagination

logger = logging.getLogger(__name__)


class GroupV2CursorPagination(V2CursorPagination):
    """Cursor pagination for groups."""

    ordering = "name"
    FIELD_MAPPING = {
        "name": "name",
        "created": "created",
        "modified": "modified",
        "last_modified": "modified",
    }


class GroupV2ViewSet(AtomicOperationsMixin, BaseV2ViewSet):
    """GroupV2 ViewSet."""

    permission_classes = (
        GroupV2KesselAccessPermission,
        V2WriteRequiresWorkspacesEnabled,
    )
    filter_backends = (GroupV2AccessFilterBackend,)
    queryset = Group.objects.all()
    serializer_class = GroupV2ResponseSerializer
    pagination_class = GroupV2CursorPagination
    lookup_field = "uuid"
    http_method_names = ["get", "post", "put", "delete", "head", "options"]

    def _log_success(self, request, message, action, resource_id):
        logger.info(
            message,
            extra={
                "action": action,
                "resource_type": "group_v2",
                "resource_id": str(resource_id),
                "outcome": "success",
                "org_id": getattr(request.user, "org_id", None),
                "username": getattr(request.user, "username", None),
            },
        )

    def get_queryset(self):
        """Return groups for the requesting tenant."""
        return Group.objects.filter(tenant=self.request.tenant).order_by("name")

    def get_serializer_class(self):
        """Return appropriate serializer based on action."""
        if self.action in ("create", "update"):
            return GroupV2RequestSerializer
        if self.action == "add_principals":
            return GroupV2AddPrincipalsSerializer
        return GroupV2ResponseSerializer

    def list(self, request, *args, **kwargs):
        """Get a list of groups."""
        input_serializer = GroupV2ListSerializer(data=request.query_params)
        input_serializer.is_valid(raise_exception=True)
        validated_params = input_serializer.validated_data

        service = GroupV2Service(tenant=request.tenant)
        queryset = service.list(validated_params)

        page = self.paginate_queryset(queryset)
        serializer = GroupV2ResponseSerializer(page, many=True)
        return self.get_paginated_response(serializer.data)

    def perform_atomic_create(self, request, *args, **kwargs):
        """Create a group and return the full response representation."""
        with atomic_block():
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            service = GroupV2Service(tenant=request.tenant)
            try:
                group = service.create(
                    name=serializer.validated_data["name"],
                    description=serializer.validated_data.get("description", ""),
                )
            except ValueError as e:
                raise ValidationError({"name": str(e)})

            audit_log = AuditLog()
            audit_log.log_create_from_object(request=request, resource=AuditLog.GROUP_V2, object=group)

        self._log_success(request, "V2 Group created", "CREATE", group.uuid)

        response_serializer = GroupV2ResponseSerializer(group)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    def perform_atomic_update(self, request, *args, **kwargs):
        """Update a group and return the full response representation."""
        with atomic_block():
            instance = self.get_object()

            audit_log = AuditLog()
            audit_log.log_edit(request=request, resource=AuditLog.GROUP_V2, object=instance)

            service = GroupV2Service(tenant=request.tenant)
            try:
                serializer = self.get_serializer(instance, data=request.data)
                serializer.is_valid(raise_exception=True)

                group = service.update(
                    group=instance,
                    name=serializer.validated_data["name"],
                    description=serializer.validated_data.get("description", ""),
                )
            except PermissionError as e:
                raise PermissionDenied(str(e))
            except ValueError as e:
                raise ValidationError({"name": str(e)})

        self._log_success(request, "V2 Group updated", "UPDATE", group.uuid)

        response_serializer = GroupV2ResponseSerializer(group)
        return Response(response_serializer.data, status=status.HTTP_200_OK)

    def perform_atomic_destroy(self, request, *args, **kwargs):
        """Delete a group."""
        with atomic_block():
            instance = self.get_object()

            service = GroupV2Service(tenant=request.tenant)
            try:
                service.delete(instance)
            except PermissionError as e:
                raise PermissionDenied(str(e))
            except ValueError as e:
                return Response(
                    v2response_error_from_errors(
                        errors=[{"detail": str(e), "status": status.HTTP_400_BAD_REQUEST}],
                    ),
                    status=status.HTTP_400_BAD_REQUEST,
                )

            audit_log = AuditLog()
            audit_log.log_delete(request=request, resource=AuditLog.GROUP_V2, object=instance)

        self._log_success(request, "V2 Group deleted", "DELETE", instance.uuid)

        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get", "post"], url_path="principals")
    def add_principals(self, request, uuid=None):
        """Add principals to a group or list principals in a group."""
        if request.method == "GET":
            return self.list_principals(request, uuid=uuid)
        return self._atomic_action(self._perform_add_principals, "add_principals", request, uuid=uuid)

    def _perform_add_principals(self, request, uuid=None):
        """Core add principals logic."""
        group = self.get_object()

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        principal_ids = [str(p["id"]) for p in serializer.validated_data["principals"]]

        service = GroupV2Service(tenant=request.tenant)
        try:
            service.add_principals(group, principal_ids)
        except PermissionError as e:
            raise PermissionDenied(str(e))
        except ValueError as e:
            return Response(
                v2response_error_from_errors(
                    errors=[{"detail": str(e), "status": status.HTTP_400_BAD_REQUEST}],
                ),
                status=status.HTTP_400_BAD_REQUEST,
            )

        audit_log = AuditLog()
        audit_log.log_v2(
            request=request,
            resource_type=AuditLog.GROUP_V2,
            action=AuditLog.ADD,
            resource_uuid=group.uuid,
            description=f"Added {len(principal_ids)} principal(s) to group: {group.name}",
        )

        self._log_success(request, "Principals added to V2 Group", "UPDATE", group.uuid)

        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(
        detail=True,
        methods=["delete"],
        url_path="principals/(?P<principal_uuid>[^/.]+)",
    )
    def remove_principal(self, request, uuid=None, principal_uuid=None):
        """Remove a principal from a group."""
        return self._atomic_action(
            self._perform_remove_principal,
            "remove_principal",
            request,
            uuid=uuid,
            principal_uuid=principal_uuid,
        )

    def _perform_remove_principal(self, request, uuid=None, principal_uuid=None):
        """Core remove principal logic."""
        validate_uuid(principal_uuid, key="principal_uuid")
        group = self.get_object()

        service = GroupV2Service(tenant=request.tenant)
        try:
            service.remove_principal(group, principal_uuid)
        except PermissionError as e:
            raise PermissionDenied(str(e))
        except ValueError as e:
            return Response(
                v2response_error_from_errors(
                    errors=[{"detail": str(e), "status": status.HTTP_404_NOT_FOUND}],
                ),
                status=status.HTTP_404_NOT_FOUND,
            )

        audit_log = AuditLog()
        audit_log.log_v2(
            request=request,
            resource_type=AuditLog.GROUP_V2,
            action=AuditLog.REMOVE,
            resource_uuid=group.uuid,
            description=f"Removed principal {principal_uuid} from group: {group.name}",
        )

        self._log_success(request, "Principal removed from V2 Group", "UPDATE", group.uuid)

        return Response(status=status.HTTP_204_NO_CONTENT)

    def list_principals(self, request, uuid=None):
        """List principals in a group."""
        group = self.get_object()
        principals = group.principals.all().order_by("username")

        page = self.paginate_queryset(principals)
        serializer = PrincipalV2ResponseSerializer(page, many=True)
        return self.get_paginated_response(serializer.data)
