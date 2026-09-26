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

"""Group V2 access permissions using Kessel Inventory API."""

import logging

from management.permissions.workspace_inventory_access import (
    WorkspaceInventoryAccessChecker,
)
from management.principal.proxy import get_kessel_principal_id
from rest_framework import permissions

logger = logging.getLogger(__name__)


class GroupV2KesselAccessPermission(permissions.BasePermission):
    """
    Permission class for Group V2 API access using Kessel Inventory API.

    Checks if the principal has rbac_groups_read or rbac_groups_write permission
    on the org resource via the Inventory API's CheckForUpdate gRPC call.

    Read actions (list, retrieve) and read methods on the principals action require rbac_groups_read.
    Everything else, including any method or action not explicitly classified as a read, requires
    rbac_groups_write.
    """

    RESOURCE_TYPE = "tenant"
    GROUPS_READ_RELATION = "rbac_groups_read"
    GROUPS_WRITE_RELATION = "rbac_groups_write"
    # Only actions in this explicit allowlist get rbac_groups_read. Any action name not listed here --
    # including ones added later without updating this file -- fails closed to rbac_groups_write.
    READ_ACTIONS = {"list", "retrieve"}
    # Actions serving both read and write HTTP methods on the same route cannot be classified by action
    # name alone. Only an explicit allowlist of read methods gets rbac_groups_read, so any other method
    # (including ones DRF routes implicitly, like HEAD, or ones added later) fails closed to rbac_groups_write.
    MIXED_METHOD_ACTIONS = {"principals"}
    READ_METHODS = {"GET", "HEAD", "OPTIONS"}

    def _get_relation(self, view, request=None) -> str:
        """Get the relation to check based on the view action (and, for mixed-method actions, the HTTP method)."""
        action = getattr(view, "action", None)
        if action in self.MIXED_METHOD_ACTIONS:
            if getattr(request, "method", None) in self.READ_METHODS:
                return self.GROUPS_READ_RELATION
            return self.GROUPS_WRITE_RELATION
        if action in self.READ_ACTIONS:
            return self.GROUPS_READ_RELATION
        # DRF leaves the action unset for methods the route does not map (e.g. PUT on "principals"). Such
        # requests end in a 405, but the permission check runs first, so fail closed. Any action name not
        # explicitly allowlisted above (present or future) also falls here, closed to rbac_groups_write.
        return self.GROUPS_WRITE_RELATION

    def has_permission(self, request, view):
        """
        Check if the user has permission to access Group V2 APIs.

        Args:
            request: The HTTP request object
            view: The view being accessed

        Returns:
            bool: True if the user has permission, False otherwise
        """
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            logger.debug("Denied group access: no tenant on request")
            return False

        org_resource_id = tenant.tenant_resource_id()
        if not org_resource_id:
            logger.debug("Denied group access: tenant has no resource ID")
            return False

        principal_id = get_kessel_principal_id(request)
        if not principal_id:
            logger.debug("Denied group access: could not determine principal ID")
            return False

        relation = self._get_relation(view, request)
        checker = WorkspaceInventoryAccessChecker()
        has_access = checker.check_resource_access(
            resource_type=self.RESOURCE_TYPE,
            resource_id=org_resource_id,
            principal_id=principal_id,
            relation=relation,
        )
        if not has_access:
            # Authorization failure - SEC-MON-REQ-1 compliance (EOI-8 authorization_failure, EOI-1 pii_manipulation)
            logger.warning(
                "Authorization denied",
                extra={
                    "action": request.method,
                    "resource_type": "group_v2",
                    "outcome": "failure",
                    "org_id": getattr(request.user, "org_id", None),
                    "username": getattr(request.user, "username", None),
                    "reason": "kessel_permission_denied",
                    "endpoint": request.path,
                    "required_relation": relation,
                },
            )
        return has_access
