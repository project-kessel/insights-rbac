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

"""Role V2 access permissions using Kessel Inventory API."""

import logging

from management.permissions.utils import KESSEL_READ_RELATION
from management.permissions.workspace_inventory_access import (
    WorkspaceInventoryAccessChecker,
)
from management.principal.proxy import get_kessel_principal_id
from management.role.v2_model import RoleV2
from rest_framework import permissions

logger = logging.getLogger(__name__)


class RoleV2KesselAccessPermission(permissions.BasePermission):
    """
    Permission class for Role V2 API access using Kessel Inventory API.

    Read actions (list, retrieve) are always allowed at the endpoint level
    so that all principals can view seeded (system) roles without additional
    permission. The Kessel check result is stored on the request so the view
    can restrict custom-role visibility via queryset filtering and
    has_object_permission.

    Write actions (create, update, bulk_destroy) require rbac_roles_write.
    """

    RESOURCE_TYPE = "tenant"
    ROLES_READ_RELATION = KESSEL_READ_RELATION
    ROLES_WRITE_RELATION = "rbac_roles_write"
    WRITE_ACTIONS = {"create", "update", "bulk_destroy"}

    def _get_relation(self, view) -> str:
        """Get the relation to check based on the view action."""
        action = getattr(view, "action", None)
        if action in self.WRITE_ACTIONS:
            return self.ROLES_WRITE_RELATION
        return self.ROLES_READ_RELATION

    def has_permission(self, request, view):
        """Check if the user has permission to access Role V2 APIs."""
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            logger.debug("Denied role access: no tenant on request")
            return False

        org_resource_id = tenant.tenant_resource_id()
        if not org_resource_id:
            logger.debug("Denied role access: tenant has no resource ID")
            return False

        principal_id = get_kessel_principal_id(request)
        if not principal_id:
            logger.debug("Denied role access: could not determine principal ID")
            return False

        relation = self._get_relation(view)
        checker = WorkspaceInventoryAccessChecker()
        has_access = checker.check_resource_access(
            resource_type=self.RESOURCE_TYPE,
            resource_id=org_resource_id,
            principal_id=principal_id,
            relation=relation,
        )

        action = getattr(view, "action", None)
        if action not in self.WRITE_ACTIONS:
            request._has_kessel_roles_read = has_access
            return True

        if not has_access:
            logger.warning(
                "Authorization denied",
                extra={
                    "action": request.method,
                    "resource_type": "role_v2",
                    "outcome": "failure",
                    "org_id": getattr(request.user, "org_id", None),
                    "username": getattr(request.user, "username", None),
                    "reason": "kessel_permission_denied",
                    "endpoint": request.path,
                    "required_relation": relation,
                },
            )
        return has_access

    def has_object_permission(self, request, view, obj):
        """Defense-in-depth fallback; queryset filtering is the primary access gate."""
        action = getattr(view, "action", None)
        if action in self.WRITE_ACTIONS:
            return True
        if getattr(request, "_has_kessel_roles_read", False):
            return True
        if getattr(obj, "type", None) == RoleV2.Types.SEEDED:
            return True
        logger.warning(
            "Authorization denied",
            extra={
                "action": request.method,
                "resource_type": "role_v2",
                "outcome": "failure",
                "org_id": getattr(request.user, "org_id", None),
                "username": getattr(request.user, "username", None),
                "reason": "kessel_permission_denied",
                "endpoint": request.path,
                "required_relation": self.ROLES_READ_RELATION,
            },
        )
        return False
