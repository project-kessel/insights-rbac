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
"""View for RoleV2 management."""

import logging

from management.atomic_transactions import atomic_block
from management.audit_log.model import AuditLog
from management.base_viewsets import BaseV2ViewSet
from management.notifications.notification_handlers import custom_v2_role_obj_change_notification_handler
from management.permissions.role_v2_access import RoleV2KesselAccessPermission
from management.permissions.v2_edit_api_access import V2WriteRequiresWorkspacesEnabled
from management.role.v2_exceptions import CustomRoleRequiredError, RolesNotFoundError
from management.role.v2_model import RoleV2
from management.role.v2_serializer import (
    RoleV2BulkDeleteRequestSerializer,
    RoleV2ListSerializer,
    RoleV2RequestSerializer,
    RoleV2ResponseSerializer,
    validate_fields_parameter,
)
from management.role.v2_service import RoleV2Service
from management.role_binding.serializer import resolve_resource_identifiers
from management.utils import v2response_error_from_errors
from management.v2_mixins import AtomicOperationsMixin
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from api.common.pagination import V2CursorPagination


class RoleV2CursorPagination(V2CursorPagination):
    """Cursor pagination for roles."""

    ordering = "name"
    FIELD_MAPPING = {"name": "name", "last_modified": "modified"}


class RoleV2ViewSet(AtomicOperationsMixin, BaseV2ViewSet):
    """RoleV2 ViewSet."""

    permission_classes = (RoleV2KesselAccessPermission, V2WriteRequiresWorkspacesEnabled)
    queryset = RoleV2.objects.exclude(type=RoleV2.Types.PLATFORM)
    serializer_class = RoleV2ResponseSerializer
    pagination_class = RoleV2CursorPagination
    lookup_field = "uuid"
    http_method_names = ["get", "post", "put", "head", "options"]

    # Default fields for create/update operations (per API spec)
    DEFAULT_CREATE_UPDATE_FIELDS = {"id", "name", "description", "permissions", "last_modified"}

    def get_queryset(self):
        """Return assignable roles for the requesting tenant. Restricts creates to custom roles."""
        if self.action == "retrieve":
            fields = RoleV2Service.DEFAULT_RETRIEVE_FIELDS
        else:
            fields = self.DEFAULT_CREATE_UPDATE_FIELDS
        base_qs = RoleV2.objects.for_tenant(self.request.tenant).assignable().with_fields(fields)

        if self.action in ("list", "retrieve"):
            return base_qs.excluding_out_of_scope_v2_roles()
        if self.action in ("update", "bulk_destroy"):
            return base_qs
        return base_qs.filter(type=RoleV2.Types.CUSTOM)

    def get_serializer_context(self):
        """Add validated fields parameter to serializer context."""
        context = super().get_serializer_context()

        fields_param = self.request.query_params.get("fields", "").replace("\x00", "")

        if self.action == "retrieve":
            # Lenient validation for read operations (silently filters invalid fields per AIP-161)
            context["fields"] = validate_fields_parameter(fields_param, RoleV2Service.DEFAULT_RETRIEVE_FIELDS)
        elif self.action in ("create", "update"):
            # Strict validation for write operations (errors on invalid fields per AIP-161)
            # Cache the result to avoid validating twice
            if not hasattr(self.request, "_validated_fields"):
                try:
                    self.request._validated_fields = validate_fields_parameter(
                        fields_param, self.DEFAULT_CREATE_UPDATE_FIELDS, strict=True
                    )
                except ValidationError as e:
                    # Re-raise with field attribution for proper error formatting
                    raise ValidationError({"fields": e.detail})
            context["fields"] = self.request._validated_fields
        return context

    def get_serializer_class(self):
        """Return appropriate serializer based on action."""
        if self.action in ("create", "update"):
            return RoleV2RequestSerializer
        if self.action == "bulk_destroy":
            return RoleV2BulkDeleteRequestSerializer
        return RoleV2ResponseSerializer

    def list(self, request, *args, **kwargs):
        """Get a list of roles."""
        input_serializer = RoleV2ListSerializer(data=request.query_params, context={"request": request})
        input_serializer.is_valid(raise_exception=True)
        validated_params = input_serializer.validated_data

        if validated_params.get("resource_tenant_org_id"):
            resource_type, resource_id = resolve_resource_identifiers(validated_params)
            validated_params = {
                **{k: v for k, v in validated_params.items() if k != "resource_tenant_org_id"},
                "resource_id": resource_id,
                "resource_type": resource_type,
            }

        service = RoleV2Service(tenant=request.tenant)
        queryset = service.list(validated_params)

        context = {
            "request": request,
            "fields": validated_params.get("fields"),
        }

        page = self.paginate_queryset(queryset)
        serializer = RoleV2ResponseSerializer(page, many=True, context=context)
        return self.get_paginated_response(serializer.data)

    def perform_atomic_create(self, request, *args, **kwargs):
        """Create a role and return the full response representation."""
        with atomic_block():
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            role = serializer.save()

            audit_log = AuditLog()
            audit_log.log_create(request=request, resource=AuditLog.ROLE_V2)

        try:
            custom_v2_role_obj_change_notification_handler(role, "created", request.user)
        except ValueError:
            raise
        except Exception:
            logging.error(
                f"Failed to send notification for created role: role pk={role.pk!r}, name={role.name!r}",
                exc_info=True,
            )

        # Build response with field selection (from context) and permission ordering
        input_permissions = request.data.get("permissions", [])
        context = self.get_serializer_context()
        context["input_permissions"] = input_permissions
        response_serializer = RoleV2ResponseSerializer(role, context=context)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    def perform_atomic_update(self, request, *args, **kwargs):
        """Update a role and return the full response representation."""
        with atomic_block():
            instance = self.get_object()

            if instance.type != RoleV2.Types.CUSTOM:
                raise ValidationError({"uuid": "System roles may not be updated."})

            audit_log = AuditLog()
            audit_log.log_edit(request=request, resource=AuditLog.ROLE_V2, object=instance)

            serializer = self.get_serializer(instance, data=request.data)
            serializer.is_valid(raise_exception=True)
            role = serializer.save()

        try:
            custom_v2_role_obj_change_notification_handler(role, "updated", request.user)
        except ValueError:
            raise
        except Exception:
            logging.error(
                f"Failed to send notification for updated role: role pk={role.pk!r}, name={role.name!r}",
                exc_info=True,
            )

        # Build response with field selection (from context) and permission ordering
        input_permissions = request.data.get("permissions", [])
        context = self.get_serializer_context()
        context["input_permissions"] = input_permissions
        response_serializer = RoleV2ResponseSerializer(role, context=context)
        return Response(response_serializer.data, status=status.HTTP_200_OK)

    def bulk_destroy(self, request, *args, **kwargs):
        """Delete multiple roles atomically."""
        return self._atomic_action(self._perform_bulk_destroy, "bulk_destroy", request, *args, **kwargs)

    def _perform_bulk_destroy(self, request, *args, **kwargs):
        """Core bulk destroy logic."""
        service = RoleV2Service(tenant=request.tenant)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        ids = set(serializer.validated_data["ids"])

        if ids:
            non_custom = (
                RoleV2.objects.for_tenant(request.tenant)
                .assignable()
                .filter(uuid__in=ids)
                .exclude(type=RoleV2.Types.CUSTOM)
            )
            if non_custom.exists():
                return Response(
                    v2response_error_from_errors(
                        errors=[
                            {
                                "detail": "System roles may not be deleted.",
                                "status": status.HTTP_400_BAD_REQUEST,
                                "source": "ids",
                            }
                        ],
                    ),
                    status=status.HTTP_400_BAD_REQUEST,
                )

        try:
            removed_roles = service.bulk_delete(ids, from_tenant=self.request.tenant)

            for role in removed_roles:
                audit_log = AuditLog()
                audit_log.log_delete(request=request, resource=AuditLog.ROLE_V2, object=role)
        except RolesNotFoundError as e:
            return Response(
                v2response_error_from_errors(
                    errors=[{"detail": str(e), "status": status.HTTP_404_NOT_FOUND, "source": "ids"}], exc=e
                ),
                status=status.HTTP_404_NOT_FOUND,
            )
        except CustomRoleRequiredError as e:
            return Response(
                v2response_error_from_errors(
                    errors=[
                        {
                            "detail": str(e),
                            "status": status.HTTP_400_BAD_REQUEST,
                            "source": "ids",
                        }
                    ],
                    exc=e,
                ),
                status=status.HTTP_400_BAD_REQUEST,
            )

        # We don't allow a revert here because sending a notification in the middle could fail. This would make it
        # unclear what to do with all of the earlier notifications that have already been sent.
        for role in removed_roles:
            try:
                custom_v2_role_obj_change_notification_handler(role, "deleted", request.user)
            except Exception:
                logging.error(
                    f"Failed to send notification for deleted role: role pk={role.pk!r}, name={role.name!r}",
                    exc_info=True,
                )

        return Response(status=status.HTTP_204_NO_CONTENT)
