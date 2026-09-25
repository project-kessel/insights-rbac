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

from django.db import transaction
from management.atomic_transactions import atomic_block
from management.audit_log.model import AuditLog
from management.base_viewsets import BaseV2ViewSet
from management.group.v2_exceptions import GroupAlreadyExistsError, GroupHasRoleBindingsError, ProtectedGroupError
from management.group.v2_serializer import (
    GroupV2ListInputSerializer,
    GroupV2RequestSerializer,
    GroupV2ResponseSerializer,
)
from management.group.v2_service import GroupV2Service
from management.notifications.notification_handlers import group_obj_change_notification_handler
from management.permissions.group_v2_access import GroupV2KesselAccessPermission
from management.permissions.v2_edit_api_access import V2WriteRequiresWorkspacesEnabled
from management.utils import v2response_error_from_errors
from management.v2_mixins import AtomicOperationsMixin
from rest_framework import status
from rest_framework.response import Response

logger = logging.getLogger(__name__)

ALREADY_EXISTS_PROBLEM_TYPE = "http://project-kessel.org/problems/already-exists"


class GroupV2ViewSet(AtomicOperationsMixin, BaseV2ViewSet):
    """GroupV2 ViewSet."""

    permission_classes = (GroupV2KesselAccessPermission, V2WriteRequiresWorkspacesEnabled)
    serializer_class = GroupV2ResponseSerializer
    lookup_field = "uuid"
    http_method_names = ["get", "post", "put", "delete", "head", "options"]

    def get_queryset(self):
        """Return annotated groups for the requesting tenant."""
        return GroupV2Service(tenant=self.request.tenant).queryset()

    def get_serializer_class(self):
        """Return appropriate serializer based on action."""
        if self.action in ("create", "update"):
            return GroupV2RequestSerializer
        return GroupV2ResponseSerializer

    def list(self, request, *args, **kwargs):
        """Get a list of groups."""
        input_serializer = GroupV2ListInputSerializer(data=request.query_params)
        input_serializer.is_valid(raise_exception=True)

        queryset = GroupV2Service(tenant=request.tenant).list(input_serializer.validated_data)

        page = self.paginate_queryset(queryset)
        serializer = GroupV2ResponseSerializer(page, many=True)
        return self.get_paginated_response(serializer.data)

    def perform_atomic_create(self, request, *args, **kwargs):
        """Create a group and return the full response representation."""
        service = GroupV2Service(tenant=request.tenant)
        try:
            with atomic_block():
                serializer = self.get_serializer(data=request.data)
                serializer.is_valid(raise_exception=True)
                group = service.create(**serializer.validated_data)

                audit_log = AuditLog()
                audit_log.log_create_from_object(request=request, resource=AuditLog.GROUP_V2, object=group)
        except GroupAlreadyExistsError as e:
            return self._already_exists_response(e)

        self._log_success(request, "V2 Group created", "CREATE", group)
        self._send_notification(request, group, "created")

        # A brand-new group has no principals or role bindings yet, so the counts are deterministically
        # 0 -- set them directly instead of re-querying with count annotations.
        group.principal_count_annotation = 0
        group.role_count_annotation = 0
        return Response(GroupV2ResponseSerializer(group).data, status=status.HTTP_201_CREATED)

    def perform_atomic_update(self, request, *args, **kwargs):
        """Update a group and return the full response representation."""
        service = GroupV2Service(tenant=request.tenant)
        try:
            with atomic_block():
                instance = self.get_object()
                serializer = self.get_serializer(data=request.data)
                serializer.is_valid(raise_exception=True)

                # Log before saving so the audit entry can compare the request against the previous values.
                audit_log = AuditLog()
                audit_log.log_edit(request=request, resource=AuditLog.GROUP_V2, object=instance)

                group = service.update(instance, **serializer.validated_data)
        except GroupAlreadyExistsError as e:
            return self._already_exists_response(e)
        except ProtectedGroupError as e:
            return self._error_response(e, status.HTTP_400_BAD_REQUEST)

        self._log_success(request, "V2 Group updated", "UPDATE", group)
        self._send_notification(request, group, "updated")

        return Response(GroupV2ResponseSerializer(service.get(group)).data, status=status.HTTP_200_OK)

    def perform_atomic_destroy(self, request, *args, **kwargs):
        """Delete a group that is not protected and has no role bindings."""
        service = GroupV2Service(tenant=request.tenant)
        try:
            with atomic_block():
                group = self.get_object()

                # Log before deleting: Model.delete() clears the instance pk, which would otherwise
                # make AuditLog record resource_id=NULL.
                audit_log = AuditLog()
                audit_log.log_delete(request=request, resource=AuditLog.GROUP_V2, object=group)

                service.delete(group)
        except ProtectedGroupError as e:
            return self._error_response(e, status.HTTP_400_BAD_REQUEST)
        except GroupHasRoleBindingsError as e:
            return self._error_response(e, status.HTTP_409_CONFLICT)

        self._log_success(request, "V2 Group deleted", "DELETE", group)
        self._send_notification(request, group, "deleted")

        return Response(status=status.HTTP_204_NO_CONTENT)

    @staticmethod
    def _error_response(exc, status_code, problem_type=None):
        return Response(
            v2response_error_from_errors(
                errors=[{"detail": str(exc), "status": status_code}], exc=exc, problem_type=problem_type
            ),
            status=status_code,
        )

    def _already_exists_response(self, exc):
        return self._error_response(exc, status.HTTP_400_BAD_REQUEST, problem_type=ALREADY_EXISTS_PROBLEM_TYPE)

    @staticmethod
    def _log_success(request, message, action, group):
        # SEC-MON-REQ-1 compliance (EOI-1 pii_manipulation)
        logger.info(
            message,
            extra={
                "action": action,
                "resource_type": "group_v2",
                "resource_id": str(group.uuid),
                "outcome": "success",
                "org_id": getattr(request.user, "org_id", None),
                "username": getattr(request.user, "username", None),
            },
        )

    @staticmethod
    def _send_notification(request, group, operation):
        # The SERIALIZABLE transaction may still fail at commit and be retried, so defer the notification until the
        # commit succeeds. Notifications are best-effort and must never fail the request.
        def send():
            try:
                group_obj_change_notification_handler(request.user, group, operation)
            except Exception:
                logger.error(
                    "Failed to send notification for %s group: group uuid=%s, name=%r",
                    operation,
                    group.uuid,
                    group.name,
                    exc_info=True,
                )

        transaction.on_commit(send)
