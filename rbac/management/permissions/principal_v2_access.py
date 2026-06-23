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

"""Principal V2 access permissions using Kessel Inventory API."""

import logging

from management.permissions.workspace_inventory_access import (
    WorkspaceInventoryAccessChecker,
)
from management.principal.proxy import get_kessel_principal_id
from rest_framework import permissions

logger = logging.getLogger(__name__)


class PrincipalV2KesselAccessPermission(permissions.BasePermission):
    """
    Permission class for Principal V2 API access using Kessel Inventory API.

    Checks if the principal has rbac_principals_read permission
    on the org resource via the Inventory API's CheckForUpdate gRPC call.

    Read-only endpoint -- only rbac_principals_read is needed.
    """

    RESOURCE_TYPE = "tenant"
    PRINCIPALS_READ_RELATION = "rbac_principals_read"

    def has_permission(self, request, view):
        """Check if the user has permission to access Principal V2 APIs."""
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            logger.debug("Denied principal access: no tenant on request")
            return False

        org_resource_id = tenant.tenant_resource_id()
        if not org_resource_id:
            logger.debug("Denied principal access: tenant has no resource ID")
            return False

        principal_id = get_kessel_principal_id(request)
        if not principal_id:
            logger.debug("Denied principal access: could not determine principal ID")
            return False

        checker = WorkspaceInventoryAccessChecker()
        return checker.check_resource_access(
            resource_type=self.RESOURCE_TYPE,
            resource_id=org_resource_id,
            principal_id=principal_id,
            relation=self.PRINCIPALS_READ_RELATION,
        )
