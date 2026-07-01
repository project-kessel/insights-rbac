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
"""View for Workspace management."""

import logging
import uuid

import pgtransaction
from django.core.exceptions import ValidationError
from django.db import OperationalError, transaction
from django_filters import rest_framework as filters
from management.atomic_transactions import atomic_with_retry
from management.audit_log.model import AuditLog
from management.base_viewsets import BaseV2ViewSet
from management.cache import WORKSPACE_CACHE
from management.filters import ValidatedOrderingFilter
from management.permissions.workspace_access import WorkspaceAccessPermission
from management.utils import validate_and_get_key
from management.workspace.filters import WorkspaceAccessFilterBackend, WorkspaceObjectAccessMixin
from management.workspace.service import WorkspaceService
from psycopg2.errors import DeadlockDetected, SerializationFailure
from rest_framework import serializers, status
from rest_framework.decorators import action
from rest_framework.permissions import SAFE_METHODS
from rest_framework.request import Request
from rest_framework.response import Response

from api.common.pagination import V2ResultsSetPagination
from .model import Workspace
from .serializer import WorkspaceListInputSerializer, WorkspaceSerializer, WorkspaceWithAncestrySerializer
from ..utils import flatten_validation_error, validate_uuid

INCLUDE_ANCESTRY_KEY = "include_ancestry"
VALID_BOOLEAN_VALUES = ["true", "false"]

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


def _workspace_retrieve_cache_key(pk: str, include_ancestry: str = "false") -> str:
    """Build response cache key for a workspace retrieve.

    Includes include_ancestry so that requests with/without ancestry data
    are cached independently (different serializers produce different payloads).
    """
    return f"retrieve::{pk}::ancestry={include_ancestry}"


def _workspace_list_cache_key(ws_type: str, offset: str, limit: str, order_by: str = "name") -> str:
    """Build response cache key for a workspace list filtered by built-in type.

    Includes order_by for correctness even though built-in type filters (root, default)
    currently return at most one workspace per tenant, making ordering irrelevant in practice.
    """
    return f"list::{ws_type}::{offset}::{limit}::{order_by}"


def _get_cached_response(org_id: str, key: str):
    """Return a cached workspace API response or None."""
    return WORKSPACE_CACHE.get_response(org_id, key)


def _set_cached_response(org_id: str, key: str, data: dict):
    """Write a workspace API response to cache."""
    WORKSPACE_CACHE.cache_response(org_id, key, data)


def is_cacheable_builtin_type(validated_params):
    """Check if a workspace list request filters for exactly one built-in type with no other filters.

    Returns the workspace type string ('root' or 'default') if cacheable, None otherwise.
    """
    if validated_params.get("name") or validated_params.get("parent_id") or validated_params.get("ids"):
        return None

    type_filter = validated_params.get("type")
    if type_filter and len(type_filter) == 1:
        ws_type = type_filter[0]
        if ws_type in (Workspace.Types.ROOT, Workspace.Types.DEFAULT):
            return ws_type

    return None


class WorkspacePagination(V2ResultsSetPagination):
    """Custom pagination for Workspace API with higher max_limit."""

    # 3000 - max limit of count of workspaces per org id - UI needs to list all workspaces for main page
    max_limit = 3000


class WorkspaceViewSet(WorkspaceObjectAccessMixin, BaseV2ViewSet):
    """Workspace View.

    A viewset that provides default `create()`, `destroy` and `retrieve()`.

    Access control is handled by:
    - WorkspaceAccessPermission: Coarse-grained endpoint access
    - WorkspaceAccessFilterBackend: Queryset filtering via Kessel Inventory API
    - WorkspaceObjectAccessMixin: 404 for inaccessible workspaces (no existence leak)
    """

    permission_classes = (WorkspaceAccessPermission,)
    queryset = Workspace.objects.annotate()
    serializer_class = WorkspaceSerializer
    pagination_class = WorkspacePagination
    ordering_fields = ("name", "created", "modified", "type")
    ordering = ("name",)
    # WorkspaceAccessFilterBackend must be first to filter by access before other filters
    filter_backends = (WorkspaceAccessFilterBackend, filters.DjangoFilterBackend, ValidatedOrderingFilter)

    def __init__(self, **kwargs):
        """Init viewset."""
        super().__init__(**kwargs)
        self._service = WorkspaceService()

    def get_serializer_class(self):
        """Get serializer class based on route."""
        if self.action == "retrieve":
            include_ancestry = validate_and_get_key(
                self.request.query_params, INCLUDE_ANCESTRY_KEY, VALID_BOOLEAN_VALUES, "false"
            )
            if include_ancestry == "true":
                return WorkspaceWithAncestrySerializer
        return super().get_serializer_class()

    def get_queryset(self):
        """Get queryset override."""
        if self.request.method not in SAFE_METHODS:
            return super().get_queryset().select_for_update()
        return super().get_queryset()

    def _log_audit(self, request, action, workspace, description):
        """Create an audit log entry for a workspace operation."""
        audit_log = AuditLog()
        audit_log.log_v2(
            request=request,
            resource_type=AuditLog.WORKSPACE,
            action=action,
            resource_uuid=workspace.id,
            description=description,
        )

    def get_object(self):
        """Get the object, validating the UUID first."""
        pk = self.kwargs.get("pk")
        if pk is not None:
            validate_uuid(pk, "workspace uuid validation")
        return super().get_object()

    @pgtransaction.atomic(isolation_level=pgtransaction.SERIALIZABLE, retry=3)
    def _create_atomic(self, request, *args, **kwargs):
        """
        Create a workspace atomically with SERIALIZABLE isolation level and automatic retries.

        The SERIALIZABLE isolation level ensures the highest data consistency by preventing
        concurrent transactions from interfering with each other. However, when conflicts occur
        (e.g., two transactions trying to create workspaces simultaneously), PostgreSQL raises
        SerializationFailure. The retry=3 parameter automatically retries the transaction up to
        3 times when SerializationFailure or DeadlockDetected errors occur. This is expected
        behavior and retrying usually succeeds as concurrent transactions complete.
        """
        tenant = request.tenant
        parent_id = request.data.get("parent_id")

        if parent_id and tenant:
            if not Workspace.objects.filter(id=parent_id, tenant=tenant).exists():
                raise serializers.ValidationError(
                    {"parent_id": f"Parent workspace '{parent_id}' doesn't exist in tenant"}
                )
        return super().create(request=request, args=args, kwargs=kwargs)

    def perform_create(self, serializer):
        """Create workspace and log audit entry."""
        super().perform_create(serializer)
        workspace = serializer.instance
        self._log_audit(self.request, AuditLog.CREATE, workspace, f"Created workspace: {workspace.name}")

    def create(self, request, *args, **kwargs):
        """Create a Workspace."""
        try:
            return self._create_atomic(request, *args, **kwargs)
        except TimeoutError as e:
            logger.exception("TimeoutError in workspace creation operation")
            return Response(
                {"detail": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        except OperationalError as e:
            response = self._handle_operational_error(e, "creation")
            if response is not None:
                return response
            raise
        except ValidationError as e:
            message = ""
            for field, error_message in flatten_validation_error(e):
                if "unique_workspace_name_per_parent" in error_message:
                    message = "A workspace with the same name already exists under the parent."
                    break
                if "__all__" in field:
                    message = error_message
                    break
            if message:
                raise serializers.ValidationError(message)
            raise

    @staticmethod
    def _get_org_id(request):
        """Resolve org_id from the request tenant."""
        tenant = getattr(request, "tenant", None)
        return getattr(tenant, "org_id", None) if tenant else None

    def retrieve(self, request, *args, **kwargs):
        """Get a workspace, with response caching for built-in workspaces.

        Access control note: The response cache is only populated for root and default
        workspace types, which are universally visible to all users within a tenant.
        WorkspaceAccessPermission (endpoint-level) still runs in dispatch() before this
        method is called. The queryset-level WorkspaceAccessFilterBackend is intentionally
        bypassed on cache hit because root/default workspaces are never access-restricted
        within a tenant — every authenticated user in the org can read them.
        """
        org_id = self._get_org_id(request)
        pk = kwargs.get("pk")
        include_ancestry = validate_and_get_key(
            request.query_params, INCLUDE_ANCESTRY_KEY, VALID_BOOLEAN_VALUES, "false"
        )

        if org_id and pk:
            key = _workspace_retrieve_cache_key(pk, include_ancestry)
            cached = _get_cached_response(org_id, key)
            if cached is not None:
                return Response(cached)

        response = super().retrieve(request, *args, **kwargs)

        if org_id and pk and response.status_code == 200:
            ws_type = response.data.get("type")
            if ws_type in (Workspace.Types.ROOT, Workspace.Types.DEFAULT):
                _set_cached_response(org_id, _workspace_retrieve_cache_key(pk, include_ancestry), response.data)

        return response

    def list(self, request, *args, **kwargs):
        """Get a list of workspaces.

        Access filtering is handled by WorkspaceAccessFilterBackend.
        Ordering is handled by OrderingFilter (supports ?order_by=name or ?order_by=-name).
        Domain filters (type, name, parent_id, ids) are validated by
        WorkspaceListInputSerializer and applied by WorkspaceService.list().

        Responses for built-in workspace types (root, default) are cached when requested
        as a single type filter with no other filters. The cache key includes pagination
        parameters (offset, limit) and order_by so different pages/orderings are cached
        independently.

        Access control note: The response cache is only populated for root and default
        workspace types, which are universally visible to all users within a tenant.
        WorkspaceAccessPermission (endpoint-level) still runs in dispatch(). The
        queryset-level WorkspaceAccessFilterBackend is intentionally bypassed on cache hit
        because root/default workspaces are never access-restricted within a tenant.
        """
        input_serializer = WorkspaceListInputSerializer(data=request.query_params)
        input_serializer.is_valid(raise_exception=True)
        validated_params = input_serializer.validated_data

        org_id = self._get_org_id(request)
        cacheable_type = is_cacheable_builtin_type(validated_params)

        cache_key = None
        if org_id and cacheable_type:
            offset = request.query_params.get("offset", "0")
            limit = request.query_params.get("limit", "10")
            order_by = request.query_params.get("order_by", "name")
            cache_key = _workspace_list_cache_key(cacheable_type, offset, limit, order_by)
            cached = _get_cached_response(org_id, cache_key)
            if cached is not None:
                return Response(cached)

        queryset = self.filter_queryset(self.get_queryset())
        queryset = self._service.list(queryset, validated_params)

        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page, many=True)
        response = self.get_paginated_response(serializer.data)

        if cache_key:
            _set_cached_response(org_id, cache_key, response.data)

        return response

    @atomic_with_retry(retries=3)
    def destroy(self, request, *args, **kwargs):
        """
        Destroy the instance.

        Overridden only to add transaction.
        """
        return super().destroy(request, *args, **kwargs)

    def perform_destroy(self, instance):
        """Delegate to service for destroy logic and log audit entry."""
        self._service.destroy(instance)
        self._log_audit(self.request, AuditLog.DELETE, instance, f"Deleted workspace: {instance.name}")

    def perform_update(self, serializer):
        """Update workspace and log audit entry.

        Invalidates workspace cache for built-in types (default) to prevent
        serving stale data. ROOT workspaces are blocked at the service layer,
        but we guard both types defensively.
        """
        instance = serializer.instance
        audit_log = AuditLog()
        description = audit_log.find_edited_field(
            AuditLog.WORKSPACE, f"workspace {instance.name}", self.request, instance
        )
        super().perform_update(serializer)
        self._log_audit(self.request, AuditLog.EDIT, instance, description)

        if instance.type in (Workspace.Types.ROOT, Workspace.Types.DEFAULT):
            org_id = self._get_org_id(self.request)
            if org_id:
                WORKSPACE_CACHE.delete_workspaces_for_tenant(org_id)

    @transaction.atomic()
    def update(self, request, *args, **kwargs):
        """Update a workspace."""
        return super().update(request, *args, **kwargs)

    @pgtransaction.atomic(isolation_level=pgtransaction.SERIALIZABLE, retry=3)
    def _move_atomic(self, request):
        """
        Move a workspace atomically with SERIALIZABLE isolation level and automatic retries.

        The SERIALIZABLE isolation level ensures the highest data consistency by preventing
        concurrent transactions from interfering with each other. However, when conflicts occur
        (e.g., two transactions trying to move workspaces simultaneously), PostgreSQL raises
        SerializationFailure. The retry=3 parameter automatically retries the transaction up to
        3 times when SerializationFailure or DeadlockDetected errors occur. This is expected
        behavior and retrying usually succeeds as concurrent transactions complete.

        Note: Access checks for both source and target workspaces are handled by
        WorkspaceAccessPermission.has_permission() before this method is called.
        """
        target_workspace_id = self._parent_id_query_param_validation(request)
        workspace = self.get_object()
        serializer = self.get_serializer(workspace)
        result = serializer.move(workspace, target_workspace_id)
        self._log_audit(
            request,
            AuditLog.EDIT,
            workspace,
            f"Moved workspace: {workspace.name} to parent {workspace.parent.name}",
        )
        return result

    @action(detail=True, methods=["post"], url_path="move")
    def move(self, request, *args, **kwargs):
        """Move a workspace."""
        try:
            response_data = self._move_atomic(request)
            return Response(response_data, status=status.HTTP_200_OK)
        except OperationalError as e:
            response = self._handle_operational_error(e, "movement", ws_id=kwargs.get("pk"))
            if response is not None:
                return response
            raise
        except Workspace.DoesNotExist:
            logger.exception("Target Workspace not found during operation, ws id: %s", kwargs.get("pk"))
            return Response(
                {"detail": "Workspace not found."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except ValidationError as e:
            message = ""
            for field, error_message in flatten_validation_error(e):
                if "unique_workspace_name_per_parent" in error_message:
                    message = "A workspace with the same name already exists under the target parent."
                    break
                if "__all__" in field:
                    message = error_message
                    break
            raise serializers.ValidationError(message)

    def _handle_operational_error(
        self, error: OperationalError, operation: str, ws_id: str | None = None
    ) -> Response | None:
        """Handle OperationalError from pgtransaction after retries are exhausted.

        When this method is called, pgtransaction has already retried the transaction
        up to 3 times. This is the final error handler for serialization conflicts
        and deadlocks that persist despite retries.

        Returns a Response for known error types, or None if the error is unrecognized
        (caller should re-raise with bare ``raise`` to preserve the original traceback).
        """
        ws_context = f", ws_id='{ws_id}'" if ws_id else ""
        if hasattr(error, "__cause__"):
            if isinstance(error.__cause__, SerializationFailure):
                logger.error(
                    "SerializationFailure in workspace %s operation after all retries exhausted%s",
                    operation,
                    ws_context,
                )
                response = Response(
                    {
                        "detail": "The server is temporarily unable to handle this request due to concurrent updates. "
                        "Please try again shortly."
                    },
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
                response["Retry-After"] = "1"
                return response
            elif isinstance(error.__cause__, DeadlockDetected):
                logger.error(
                    "DeadlockDetected in workspace %s operation after all retries exhausted%s",
                    operation,
                    ws_context,
                )
                return Response(
                    {"detail": "Internal server error in concurrent updates. Please try again later."},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
        return None

    @staticmethod
    def _parent_id_query_param_validation(request: Request) -> uuid.UUID:
        """Validate the parent_id query parameter."""
        new_parent_id = request.data.get("parent_id")
        if not new_parent_id:
            raise serializers.ValidationError({"parent_id": "The 'parent_id' field is required."})
        validate_uuid(new_parent_id)
        return uuid.UUID(new_parent_id)
