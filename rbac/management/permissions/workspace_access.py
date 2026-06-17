#
# Copyright 2024 Red Hat, Inc.
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
"""Workspace access permission - coarse-grained endpoint checks.

This permission class handles:
- Endpoint-level access (can this user type call this endpoint?)
- System user authentication
- Move operation: target workspace access check
- V1 mode: source workspace access check (legacy behavior)
- V2 mode: source workspace access check via is_user_allowed_v2

Data filtering for list operations is handled by WorkspaceAccessFilterBackend
in management/workspace/filters.py.
"""

import logging

from feature_flags import FEATURE_FLAGS
from management.permissions.system_user_utils import SystemUserAccessResult, check_system_user_access
from management.workspace.utils import (
    is_user_allowed_v1,
    is_user_allowed_v2,
    operation_from_request,
    permission_from_request,
    workspace_from_request,
)
from rest_framework import permissions

# Custom message for target workspace access denial
# This message is relation-agnostic: V1 uses 'write' operation, V2 uses 'create' permission
TARGET_WORKSPACE_ACCESS_DENIED_MESSAGE = "You do not have permission to access the target workspace."

logger = logging.getLogger(__name__)


def _build_s2s_log_context(request, view, ws_id=None):
    """Build log context string for S2S system user access."""
    user = request.user
    request_id = getattr(request, "req_id", None)
    org_id = getattr(user, "org_id", None)
    username = getattr(user, "username", None)
    user_id = getattr(user, "user_id", None)
    is_admin = getattr(user, "admin", False)
    action = getattr(view, "action", None)
    return (
        f"[request_id={request_id}, org_id={org_id}, username={username}, "
        f"user_id={user_id}, is_admin={is_admin}, action={action}, "
        f"method={request.method}, path={request.path}, workspace_id={ws_id}]"
    )


class WorkspaceAccessPermission(permissions.BasePermission):
    """
    Workspace access permission checker.

    This is the single entry point for workspace access control, handling
    the V1/V2 feature flag branching in one place.

    Responsibilities:
    - Endpoint-level access (can this user type call this endpoint?)
    - System user authentication
    - Move operation: target workspace access check
    - V1 mode: source workspace access check (legacy behavior)
    - V2 mode: source workspace access check via is_user_allowed_v2

    Note: For V2 list operations, the WorkspaceAccessFilterBackend handles
    queryset filtering via Kessel Inventory API. This permission class
    allows list requests to proceed, and the FilterBackend controls visibility.
    """

    def has_permission(self, request, view):
        """
        Check if the user has permission to access the workspace.

        Handles V1/V2 feature flag branching:
        - V2: Uses Inventory API with fine-grained permissions (view, create, edit, move, delete)
        - V1: Uses legacy role-based checks with read/write operations

        For move operations (POST to /move endpoint), this method checks both:
        - Source workspace: user needs 'create' permission (V2, from POST method) or 'write' operation (V1)
        - Target workspace: user needs 'create' permission (V2) or 'write' operation (V1)

        Note: The permission_from_request function maps HTTP POST to 'create' permission,
        which is used for the source workspace check in V2 mode. This is by design since
        the move action is a POST request.

        Args:
            request: The HTTP request object
            view: The view being accessed

        Returns:
            bool: True if the user has permission, False otherwise
        """
        # Get the permission/operation and target workspace
        perm = permission_from_request(request, view)
        ws_id = workspace_from_request(request, view)

        # Branch based on feature flag - this is the ONLY place this check should occur
        if FEATURE_FLAGS.is_workspace_access_check_v2_enabled():
            return self._has_permission_v2(request, view, perm, ws_id)

        # V1: Use legacy role-based checks with read/write operations
        return self._has_permission_v1(request, view, ws_id)

    def _has_permission_v2(self, request, view, perm, ws_id) -> bool:
        """
        V2 permission check - coarse-grained for create/move, FilterBackend for detail/list.

        The WorkspaceAccessFilterBackend handles access filtering for list/detail operations
        via queryset. This ensures consistent 404 behavior for both non-existing and
        inaccessible workspaces (prevents existence leakage).

        This permission class handles:
        - System user bypass/denial
        - Create operation: check 'create' permission on parent workspace
        - Move operation: check 'create' permission on target workspace

        For list/detail operations, allow the request to proceed and let the
        FilterBackend handle access via queryset filtering.
        """
        # For system users (s2s communication), bypass v2 access checks and rely on user.admin
        # Uses unified check_system_user_access to prevent behavior drift
        system_check = check_system_user_access(request.user, action=view.action)
        if system_check.result == SystemUserAccessResult.ALLOWED:
            log_ctx = _build_s2s_log_context(request, view, ws_id)
            logger.info("S2S system user admin access granted (v2) %s", log_ctx)
            return True
        if system_check.result == SystemUserAccessResult.DENIED:
            log_ctx = _build_s2s_log_context(request, view, ws_id)
            logger.info("S2S system user access denied: not admin (v2) %s", log_ctx)
            return False
        if system_check.result == SystemUserAccessResult.CHECK_MOVE_TARGET:
            result = self._check_move_target_exists_v1(request)
            log_ctx = _build_s2s_log_context(request, view, ws_id)
            if result:
                logger.info("S2S system user admin access granted for move (v2) %s", log_ctx)
            else:
                logger.info("S2S system user admin denied: target ws not found (v2) %s", log_ctx)
            return result
        # SystemUserAccessResult.NOT_SYSTEM_USER - continue with normal checks

        # For create operations, check permission on parent workspace (ws_id)
        # ws_id is the parent workspace ID where the new workspace will be created
        if view.action == "create":
            if not is_user_allowed_v2(request, perm, ws_id):
                return False
            return True

        # For move operations, check target workspace access
        # Source workspace access is handled by FilterBackend
        if view.action == "move":
            return self._check_move_target_access_v2(request)

        # For list/detail operations, allow request to proceed
        # FilterBackend handles access filtering via queryset
        # For list: users with no real workspace access get fallback workspaces
        # (root, default, ungrouped) via FilterBackend instead of 403
        # This ensures 404 for both non-existing and inaccessible workspaces
        return True

    def _has_permission_v1(self, request, view, ws_id) -> bool:
        """
        V1 permission check using legacy role-based access control.

        Admin users have full access (except move requires target validation).
        Non-admin users are checked against role-based permissions.
        """
        is_system_user = getattr(request.user, "system", False)

        # Admin users have full access, but for move operations they still need
        # the target workspace to exist within their tenant
        if request.user.admin:
            if view.action == "move":
                result = self._check_move_target_exists_v1(request)
                if is_system_user:
                    log_ctx = _build_s2s_log_context(request, view, ws_id)
                    if result:
                        logger.info("S2S system user admin access granted for move %s", log_ctx)
                    else:
                        logger.info("S2S system user admin denied: target ws not found %s", log_ctx)
                return result
            if is_system_user:
                log_ctx = _build_s2s_log_context(request, view, ws_id)
                logger.info("S2S system user admin access granted %s", log_ctx)
            return True

        # Non-admin user (including system users without admin)
        if is_system_user:
            log_ctx = _build_s2s_log_context(request, view, ws_id)
            logger.info("S2S system user access denied: not admin %s", log_ctx)

        op = operation_from_request(request)
        if not is_user_allowed_v1(request, op, ws_id):
            return False

        # For move operations, also check target workspace access (V1 non-admin only)
        if view.action == "move":
            return self._check_move_target_access_v1(request)

        return True

    def _get_target_workspace_id(self, request) -> str | None:
        """
        Get and validate the target workspace ID from request body.

        Aligns with the view's source of truth: only reads from request.data (POST body).
        The view's _parent_id_query_param_validation uses request.data.get("parent_id"),
        so we must use the same source to ensure consistent security enforcement.

        Args:
            request: The HTTP request object

        Returns:
            str: The valid UUID string, or None if missing/invalid (let validation handle it)
        """
        import uuid

        # Only check request.data (POST body) - aligns with view's source of truth
        target_workspace_id = request.data.get("parent_id")
        if not target_workspace_id:
            return None

        # Validate it's a valid UUID - if not, let the view's validation handle it
        try:
            uuid.UUID(str(target_workspace_id))
            return str(target_workspace_id)
        except (ValueError, AttributeError):
            return None

    def _check_move_target_access_v2(self, request) -> bool:
        """
        Check target workspace access for move operations in V2 mode.

        In V2, we use the Inventory API to check if the user has 'create' permission
        on the target workspace (from SpiceDB schema: create, view, edit, move, delete).

        Args:
            request: The HTTP request object

        Returns:
            bool: True if the user has 'create' permission on target workspace
        """
        target_workspace_id = self._get_target_workspace_id(request)
        if target_workspace_id is None:
            # Let validation handle missing/invalid parent_id
            return True

        # V2: Check 'create' permission on target workspace via Inventory API
        if not is_user_allowed_v2(request, "create", target_workspace_id):
            self.message = TARGET_WORKSPACE_ACCESS_DENIED_MESSAGE
            return False

        return True

    def _check_move_target_access_v1(self, request) -> bool:
        """
        Check target workspace access for move operations in V1 mode (non-admin users).

        In V1, we use legacy role-based checks with 'write' operation.

        Args:
            request: The HTTP request object

        Returns:
            bool: True if the user has 'write' access on target workspace
        """
        target_workspace_id = self._get_target_workspace_id(request)
        if target_workspace_id is None:
            # Let validation handle missing/invalid parent_id
            return True

        # V1: Check 'write' operation on target workspace
        if not is_user_allowed_v1(request, "write", target_workspace_id):
            self.message = TARGET_WORKSPACE_ACCESS_DENIED_MESSAGE
            return False

        return True

    def _check_move_target_exists_v1(self, request) -> bool:
        """
        Check that target workspace exists for V1 admin users.

        Admin users bypass permission checks but still need the target workspace
        to exist within their tenant for move operations.

        Args:
            request: The HTTP request object

        Returns:
            bool: True if target workspace exists, False otherwise
        """
        from management.workspace.model import Workspace

        target_workspace_id = self._get_target_workspace_id(request)
        if target_workspace_id is None:
            # Let validation handle missing/invalid parent_id
            return True

        if not Workspace.objects.filter(id=target_workspace_id, tenant=request.tenant).exists():
            self.message = TARGET_WORKSPACE_ACCESS_DENIED_MESSAGE
            return False

        return True
