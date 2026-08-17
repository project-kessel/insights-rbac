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
"""Group V2 filter backends for Kessel-based access control."""

import logging

from management.permissions.workspace_inventory_access import (
    WorkspaceInventoryAccessChecker,
)
from management.principal.proxy import get_kessel_principal_id
from rest_framework.filters import BaseFilterBackend

logger = logging.getLogger(__name__)


class GroupV2AccessFilterBackend(BaseFilterBackend):
    """Filter backend to narrow group list queryset based on Kessel permissions.

    Uses the Inventory API's StreamedListObjects to return only groups
    the user has rbac_groups_read permission on (at the tenant level).
    """

    RESOURCE_TYPE = "tenant"
    GROUPS_READ_RELATION = "rbac_groups_read"

    def filter_queryset(self, request, queryset, view):
        """Filter queryset to groups user has access to."""
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            logger.debug("No tenant on request, returning empty queryset")
            return queryset.none()

        org_resource_id = tenant.tenant_resource_id()
        if not org_resource_id:
            logger.debug("Tenant has no resource ID, returning empty queryset")
            return queryset.none()

        principal_id = get_kessel_principal_id(request)
        if not principal_id:
            logger.debug("Could not determine principal ID, returning empty queryset")
            return queryset.none()

        checker = WorkspaceInventoryAccessChecker()
        # For group list filtering, check if user has read permission on the tenant.
        # Since group permissions are tenant-scoped (not per-group instance),
        # if they have read on the tenant, they can see all groups in the tenant.
        has_access = checker.check_resource_access(
            resource_type=self.RESOURCE_TYPE,
            resource_id=org_resource_id,
            principal_id=principal_id,
            relation=self.GROUPS_READ_RELATION,
        )
        if not has_access:
            logger.warning(
                "Group read access denied",
                extra={
                    "resource_type": "group_v2",
                    "outcome": "access_denied",
                    "org_id": getattr(request.user, "org_id", None),
                    "username": getattr(request.user, "username", None),
                    "required_relation": self.GROUPS_READ_RELATION,
                },
            )
            return queryset.none()

        return queryset
