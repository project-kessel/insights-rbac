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

"""Audit Log V2 access permissions using Kessel Inventory API."""

import logging

from management.permissions.workspace_inventory_access import (
    WorkspaceInventoryAccessChecker,
)
from management.principal.proxy import get_kessel_principal_id
from rest_framework import permissions

logger = logging.getLogger(__name__)

# Kessel relation on the `tenant` resource granting read access to the tenant's audit history.
# Backed by the `rbac:audit_log:read` permission in the Kessel schema (rbac-config).
KESSEL_AUDIT_LOG_READ_RELATION = "rbac_audit_log_read"


class AuditLogV2KesselAccessPermission(permissions.BasePermission):
    """
    Permission class for Audit Log V2 API access using Kessel Inventory API.

    Checks whether the principal holds the ``rbac_audit_log_read`` relation on the
    tenant resource via the Inventory API's CheckForUpdate gRPC call.

    Unlike the V1 audit log endpoint, there is no org-admin bypass: access is decided
    solely by the Kessel relationship check. Org admins are granted the relation
    through the platform admin default role binding.
    """

    RESOURCE_TYPE = "tenant"
    AUDIT_LOG_READ_RELATION = KESSEL_AUDIT_LOG_READ_RELATION

    def has_permission(self, request, view):
        """
        Check if the user has permission to access Audit Log V2 APIs.

        Args:
            request: The HTTP request object
            view: The view being accessed

        Returns:
            bool: True if the user has permission, False otherwise
        """
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            logger.debug("Denied audit log access: no tenant on request")
            return False

        org_resource_id = tenant.tenant_resource_id()
        if not org_resource_id:
            logger.debug("Denied audit log access: tenant has no resource ID")
            return False

        principal_id = get_kessel_principal_id(request)
        if not principal_id:
            logger.debug("Denied audit log access: could not determine principal ID")
            return False

        checker = WorkspaceInventoryAccessChecker()
        has_access = checker.check_resource_access(
            resource_type=self.RESOURCE_TYPE,
            resource_id=org_resource_id,
            principal_id=principal_id,
            relation=self.AUDIT_LOG_READ_RELATION,
        )
        if not has_access:
            # Authorization failure - SEC-MON-REQ-1 compliance (EOI-8 authorization_failure)
            logger.warning(
                "Authorization denied",
                extra={
                    "action": request.method,
                    "resource_type": "audit_log",
                    "outcome": "failure",
                    "org_id": getattr(request.user, "org_id", None),
                    "username": getattr(request.user, "username", None),
                    "reason": "kessel_permission_denied",
                    "endpoint": request.path,
                    "required_relation": self.AUDIT_LOG_READ_RELATION,
                },
            )
        return has_access
