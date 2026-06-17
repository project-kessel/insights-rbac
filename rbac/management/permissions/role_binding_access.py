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

"""Role binding access permissions using Kessel Inventory API."""

import logging
import uuid

from feature_flags import FEATURE_FLAGS
from management.permissions.workspace_inventory_access import (
    WorkspaceInventoryAccessChecker,
)
from management.principal.proxy import get_kessel_principal_id
from rest_framework import permissions
from rest_framework.exceptions import ParseError

from api.models import Tenant

logger = logging.getLogger(__name__)


def _is_system_user_without_admin(user) -> bool:
    """
    Check if user is a system user without admin privileges.

    Args:
        user: The user object from the request

    Returns:
        bool: True if user is system but not admin, False otherwise
    """
    is_system = getattr(user, "system", False)
    is_admin = getattr(user, "admin", False)
    return is_system and not is_admin


class RoleBindingSystemUserAccessPermission(permissions.BasePermission):
    """
    Permission class for system user access to role bindings.

    Checks if system users (s2s communication) have proper access.
    Non-admin system users are denied.
    All other users (including admins) pass through to next permission class.
    """

    def has_permission(self, request, view):
        """
        Check if user has access based on system user status.

        Args:
            request: The HTTP request object
            view: The view being accessed

        Returns:
            bool: True to pass through to next permission, False if denied
        """
        user = request.user

        # System users without admin are denied
        if _is_system_user_without_admin(user):
            return False

        # All other users pass through to next permission class (Kessel check)
        return True


class RoleBindingKesselAccessPermission(permissions.BasePermission):
    """
    Permission class for role binding access using Kessel Inventory API.

    Action-aware: dispatches to different check methods based on the view action
    and HTTP method. Each action maps to specific Kessel relations controlled by
    the USE_ROLE_BINDING_VIEW_PERMISSION feature flag.

    Read actions use view/role_binding_view relations.
    Write actions use create/edit or role_binding_grant/role_binding_revoke relations.

    This permission class should be used after RoleBindingSystemUserAccessPermission
    which handles system user denial logic.
    """

    # Read relations
    ROLE_BINDING_VIEW_RELATION = "role_binding_view"
    VIEW_RELATION = "view"

    # Write relations
    CREATE_RELATION = "create"
    EDIT_RELATION = "edit"
    ROLE_BINDING_GRANT_RELATION = "role_binding_grant"
    ROLE_BINDING_REVOKE_RELATION = "role_binding_revoke"

    # Allowlist of valid resource types for role binding access checks
    ALLOWED_RESOURCE_TYPES = {"workspace", "tenant"}

    def _get_read_relation(self) -> str:
        """Get the read relation based on feature flag."""
        if FEATURE_FLAGS.is_use_role_binding_view_permission_enabled():
            return self.ROLE_BINDING_VIEW_RELATION
        return self.VIEW_RELATION

    def _get_write_relation(self) -> str:
        """Get the write (grant) relation based on feature flag."""
        if FEATURE_FLAGS.is_use_role_binding_view_permission_enabled():
            return self.ROLE_BINDING_GRANT_RELATION
        return self.CREATE_RELATION

    def has_permission(self, request, view):
        """
        Check if the user has permission to access role bindings.

        Dispatches to action-specific checks based on the view action and HTTP method.
        """
        action = getattr(view, "action", None)

        if action == "batch_create":
            return self._check_batch_create_permission(request)
        elif action == "by_subject" and request.method == "PUT":
            return self._check_by_subject_write_permission(request)
        elif action in ("list", "by_subject"):
            return self._check_read_permission(request)
        else:
            logger.warning("Denied access: unrecognized action %s", action)
            return False

    def _parse_query_resource(self, request):
        """Parse and sanitize resource_id/resource_type from query params.

        Handles both old-style (resource_type + resource_id) and new-style
        (resource.tenant.org_id) query parameters.

        Returns (resource_type, resource_id) or (None, None) if not provided.
        """
        org_id = request.query_params.get("resource.tenant.org_id", "").replace("\x00", "")
        if org_id:
            return "tenant", Tenant.org_id_to_tenant_resource_id(org_id)

        resource_id = request.query_params.get("resource_id", "").replace("\x00", "")
        resource_type = request.query_params.get("resource_type", "").replace("\x00", "").lower()
        if not resource_id or not resource_type:
            return None, None
        return resource_type, resource_id

    def _check_read_permission(self, request):
        """Check read permission using resource info from query params.

        Returns True if no resource params provided (pass-through for list endpoint).
        """
        resource_type, resource_id = self._parse_query_resource(request)
        if not resource_type:
            return True

        return self._check_single_resource(request, resource_type, resource_id, self._get_read_relation())

    def _check_batch_create_permission(self, request):
        """Check write permission for batch create.

        Extracts resources from request body and checks permission on each unique
        (resource_type, resource_id) pair. Fails closed on malformed input.
        """
        data = getattr(request, "data", {})
        if not isinstance(data, dict):
            logger.debug("Denied batch_create: request body is not a JSON object")
            return False
        requests_data = data.get("requests")
        if not requests_data or not isinstance(requests_data, list):
            logger.debug("Denied batch_create: missing or invalid 'requests' in body")
            return False

        unique_resources = set()
        for item in requests_data:
            if not isinstance(item, dict):
                logger.debug("Denied batch_create: request item is not a dict")
                return False
            resource = item.get("resource", {})
            if not isinstance(resource, dict):
                logger.debug("Denied batch_create: resource is not a dict")
                return False
            resource_id = str(resource.get("id") or "").replace("\x00", "")
            resource_type = str(resource.get("type") or "").replace("\x00", "").lower()
            if not resource_id or not resource_type:
                logger.debug("Denied batch_create: missing resource id or type")
                return False
            unique_resources.add((resource_type, resource_id))

        relation = self._get_write_relation()
        principal_id = self._resolve_principal_id(request, unique_resources)

        for resource_type, resource_id in unique_resources:
            if not self._check_single_resource(request, resource_type, resource_id, relation, principal_id):
                return False
        return True

    def _check_by_subject_write_permission(self, request):
        """Check write permission for PUT by_subject.

        MVP (flag off): checks 'edit' relation.
        POST-MVP (flag on): checks BOTH 'role_binding_grant' AND 'role_binding_revoke'.
        """
        resource_type, resource_id = self._parse_query_resource(request)
        if not resource_type:
            logger.debug("Denied PUT by_subject: missing resource_id or resource_type")
            return False

        if FEATURE_FLAGS.is_use_role_binding_view_permission_enabled():
            return self._check_single_resource(
                request, resource_type, resource_id, self.ROLE_BINDING_GRANT_RELATION
            ) and self._check_single_resource(request, resource_type, resource_id, self.ROLE_BINDING_REVOKE_RELATION)
        else:
            return self._check_single_resource(request, resource_type, resource_id, self.EDIT_RELATION)

    def _resolve_principal_id(self, request, unique_resources):
        """Resolve principal_id once if any non-tenant resources need Kessel checks."""
        needs_kessel = any(rt != "tenant" for rt, _ in unique_resources)
        if needs_kessel:
            return get_kessel_principal_id(request)
        return None

    def _check_single_resource(self, request, resource_type, resource_id, relation, principal_id=None) -> bool:
        """Check access on a single resource via Kessel or org-admin check.

        Args:
            principal_id: Pre-resolved principal ID for Kessel checks. If None and needed,
                         will be resolved from the request (fallback for callers that
                         don't pre-resolve).
        """
        if resource_type not in self.ALLOWED_RESOURCE_TYPES:
            allowed = ", ".join(sorted(self.ALLOWED_RESOURCE_TYPES))
            raise ParseError(f"'{resource_type}' is not a valid resource_type. Allowed values: {allowed}.")

        if resource_type == "workspace":
            try:
                uuid.UUID(str(resource_id))
            except ValueError:
                raise ParseError(detail=f"'{resource_id}' is not a valid UUID for resource_id.")

        if resource_type == "tenant":
            is_org_admin = getattr(request.user, "admin", False)
            if not is_org_admin:
                logger.debug("Denied access for tenant resource: only org admins allowed")
                return False
            tenant = getattr(request, "tenant", None)
            if tenant is None:
                logger.debug("Denied access for tenant resource: no tenant on request")
                return False
            expected_resource_id = tenant.tenant_resource_id()
            if expected_resource_id is None or resource_id != expected_resource_id:
                logger.debug(
                    "Denied access for tenant resource: resource_id %s does not match tenant %s",
                    resource_id,
                    expected_resource_id,
                )
                return False
            return True

        if principal_id is None:
            principal_id = get_kessel_principal_id(request)
        if not principal_id:
            return False

        checker = WorkspaceInventoryAccessChecker()
        return checker.check_resource_access(
            resource_type=resource_type,
            resource_id=resource_id,
            principal_id=principal_id,
            relation=relation,
        )
