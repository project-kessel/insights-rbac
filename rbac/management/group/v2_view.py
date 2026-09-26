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
from functools import wraps

from django.db import transaction
from management.atomic_transactions import atomic_block
from management.audit_log.model import AuditLog
from management.base_viewsets import BaseV2ViewSet
from management.group.v2_exceptions import (
    GroupAlreadyExistsError,
    GroupHasRoleBindingsError,
    PrincipalNotFoundError,
    ProtectedGroupError,
)
from management.group.v2_serializer import (
    GroupV2AddPrincipalsInputSerializer,
    GroupV2ListInputSerializer,
    GroupV2ListPrincipalsInputSerializer,
    GroupV2RemovePrincipalsInputSerializer,
    GroupV2RequestSerializer,
    GroupV2ResponseSerializer,
)
from management.group.v2_service import GroupV2Service
from management.notifications.notification_handlers import group_obj_change_notification_handler
from management.permissions.group_v2_access import GroupV2KesselAccessPermission
from management.permissions.v2_edit_api_access import V2WriteRequiresWorkspacesEnabled
from management.principal.backfill import backfill_remote_principals
from management.principal.proxy import PrincipalProxy, external_principal_to_user
from management.principal.v2_serializer import PrincipalV2OutputSerializer
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.tenant_service import get_tenant_bootstrap_service
from management.utils import v2response_error_from_errors
from management.v2_mixins import AtomicOperationsMixin
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

logger = logging.getLogger(__name__)

ALREADY_EXISTS_PROBLEM_TYPE = "http://project-kessel.org/problems/already-exists"


def _catch_principal_errors(fn):
    """Translate ProtectedGroupError/PrincipalNotFoundError into their HTTP error responses."""

    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except ProtectedGroupError as e:
            return self._error_response(e, status.HTTP_400_BAD_REQUEST)
        except PrincipalNotFoundError as e:
            return self._error_response(e, status.HTTP_404_NOT_FOUND)

    return wrapper


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

        queryset = GroupV2Service(tenant=request.tenant).list(
            input_serializer.validated_data, requester_username=request.user.username
        )

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

    @action(detail=True, methods=["get", "post", "delete"], url_path="principals")
    def principals(self, request, uuid=None):
        """List, bulk-add, or bulk-remove a group's member principals, dispatched by HTTP method.

        Every method is matched explicitly: DRF routes HEAD to this action too (since it serves GET), so a
        catch-all branch would let HEAD requests fall through to a mutation. GET/HEAD/POST/DELETE are the
        only methods DRF binds to this route (per the ``methods`` list above); any other method never
        reaches this body -- DRF's dispatch() resolves straight to its own 405 handler beforehand.
        """
        if request.method in ("GET", "HEAD"):
            return self._list_principals(request, uuid)
        if request.method == "POST":
            return self._add_principals_to_group(request, uuid)
        if request.method == "DELETE":
            return self._atomic_action(
                self._perform_remove_principals_bulk, "remove_principals_bulk", request, uuid=uuid
            )
        # Unreachable: DRF only binds GET/HEAD/POST/DELETE to this route (per the ``methods`` list above);
        # any other method is rejected by DRF's dispatch() with a 405 before this body ever runs.
        raise AssertionError(f"Unexpected HTTP method {request.method!r} routed to principals()")

    @action(detail=True, methods=["delete"], url_path=r"principals/(?P<principal_uuid>[0-9a-f-]+)")
    def remove_principal(self, request, uuid=None, principal_uuid=None):
        """Remove a single principal from a group by principal UUID."""
        return self._atomic_action(
            self._perform_remove_principal, "remove_principal", request, uuid=uuid, principal_uuid=principal_uuid
        )

    def _list_principals(self, request, uuid=None):
        """List the group's member principals with optional filtering."""
        group = self.get_object()
        input_serializer = GroupV2ListPrincipalsInputSerializer(data=request.query_params)
        input_serializer.is_valid(raise_exception=True)

        service = GroupV2Service(tenant=request.tenant)
        queryset = service.list_principals(group, input_serializer.validated_data)

        page = self.paginate_queryset(queryset)
        serializer = PrincipalV2OutputSerializer(page, many=True)
        return self.get_paginated_response(serializer.data)

    def _add_principals_to_group(self, request, uuid=None):
        """Add principals to a group, validating user principals via BOP before persisting.

        Follows the V1 parity pattern: validate user principals against the BOP proxy *outside*
        the SERIALIZABLE transaction (to avoid holding open a long transaction during the external
        call), backfill any missing local Principal records, and then persist the group membership
        change inside the retryable atomic block.
        """
        serializer = GroupV2AddPrincipalsInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        usernames = set(serializer.validated_data.get("usernames") or [])
        service_account_client_ids = set(serializer.validated_data.get("service_accounts") or [])

        # Validate user principals against BOP before opening the DB transaction (V1 parity).
        if usernames:
            error_response = self._validate_and_backfill_users(request, usernames)
            if error_response is not None:
                return error_response

        return self._atomic_action(
            self._perform_add_principals,
            "add_principals",
            request,
            uuid=uuid,
            usernames=usernames,
            service_account_client_ids=service_account_client_ids,
        )

    def _validate_and_backfill_users(self, request, usernames):
        """Validate usernames against BOP and backfill local Principal records.

        Returns an error Response if validation fails, or None on success.
        """
        proxy = PrincipalProxy()
        proxy_response = proxy.request_filtered_principals(
            list(usernames),
            org_id=request.user.org_id,
            limit=len(usernames),
            options={"return_id": True},
        )
        if isinstance(proxy_response, dict) and "errors" in proxy_response:
            detail = proxy_response["errors"][0].get("detail", "Principal proxy validation failed")
            return self._error_response(
                Exception(detail),
                proxy_response.get("status_code", status.HTTP_502_BAD_GATEWAY),
            )

        bop_data = proxy_response.get("data", [])
        found_usernames = {u["username"].lower() for u in bop_data}
        missing = usernames - found_usernames
        if missing:
            return self._error_response(
                PrincipalNotFoundError(sorted(missing)),
                status.HTTP_404_NOT_FOUND,
            )

        # Backfill: ensure all BOP-validated users have local Principal records.
        users = [external_principal_to_user(item) for item in bop_data]
        bootstrap_service = get_tenant_bootstrap_service(OutboxReplicator())
        backfill_remote_principals(bootstrap_service, users, request.tenant)

        return None

    @_catch_principal_errors
    def _perform_add_principals(self, request, uuid=None, usernames=None, service_account_client_ids=None):
        """Persist pre-validated principals into a group inside an atomic transaction."""
        service = GroupV2Service(tenant=request.tenant)
        with atomic_block():
            group = self.get_object()

            # Only principals whose membership actually changed are returned (already-member
            # identifiers resolve successfully but are excluded), so this only audit-logs new additions.
            principals = service.add_principals(
                group,
                usernames if usernames is not None else set(),
                service_account_client_ids if service_account_client_ids is not None else set(),
            )

            for principal in principals:
                audit_log = AuditLog()
                audit_log.log_group_assignment(request, AuditLog.GROUP_V2, group, principal, principal.type)

        return Response(GroupV2ResponseSerializer(service.get(group)).data, status=status.HTTP_200_OK)

    @_catch_principal_errors
    def _perform_remove_principals_bulk(self, request, uuid=None):
        """Remove principals from a group in bulk. All identifiers must resolve, or nothing is removed."""
        service = GroupV2Service(tenant=request.tenant)
        with atomic_block():
            group = self.get_object()
            serializer = GroupV2RemovePrincipalsInputSerializer(data=request.query_params)
            serializer.is_valid(raise_exception=True)
            usernames = set(serializer.validated_data.get("usernames") or [])
            service_accounts = set(serializer.validated_data.get("service_accounts") or [])

            principals = service.remove_principals(group, usernames, service_accounts)

            for principal in principals:
                audit_log = AuditLog()
                audit_log.log_group_remove(request, AuditLog.GROUP_V2, group, principal, principal.type)

        return Response(status=status.HTTP_204_NO_CONTENT)

    @_catch_principal_errors
    def _perform_remove_principal(self, request, uuid=None, principal_uuid=None):
        """Remove a single principal from a group by principal UUID."""
        service = GroupV2Service(tenant=request.tenant)
        with atomic_block():
            group = self.get_object()
            principal = service.remove_principal(group, principal_uuid)

            audit_log = AuditLog()
            audit_log.log_group_remove(request, AuditLog.GROUP_V2, group, principal, principal.type)

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
