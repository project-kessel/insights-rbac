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
"""Defines the Access Permissions Utility Class."""

import logging

from rest_framework import permissions

logger = logging.getLogger(__name__)

SCOPE_KEY = "scope"
ORG_ID_SCOPE = "org_id"
PRINCIPAL_SCOPE = "principal"

KESSEL_READ_RELATION = "rbac_roles_read"


def is_scope_principal(request):
    """Check permission based on the defined scope principal query param."""
    if request.method not in permissions.SAFE_METHODS:
        return False

    scope = request.query_params.get(SCOPE_KEY, ORG_ID_SCOPE)
    return scope == PRINCIPAL_SCOPE


def check_v2_kessel_access(request, relation=KESSEL_READ_RELATION):
    """Check Kessel access for V2-migrated orgs.

    Serves as a fallback when V1 request.user.access check fails. For V2 orgs,
    checks if the principal has the specified relation on the tenant resource
    via Kessel Inventory API.

    Returns False for V1 orgs (no fallback needed) or on any error.
    """
    from management.permissions.workspace_inventory_access import WorkspaceInventoryAccessChecker
    from management.principal.proxy import get_kessel_principal_id
    from management.tenant_mapping.v2_activation import is_v2_write_activated

    tenant = getattr(request, "tenant", None)
    if not tenant:
        return False

    try:
        if not is_v2_write_activated(tenant):
            return False
    except (TypeError, ValueError):
        # tenant is not a valid Tenant model instance (e.g. in tests with Mocks)
        return False

    resource_id = tenant.tenant_resource_id()
    if not resource_id:
        return False

    principal_id = get_kessel_principal_id(request)
    if not principal_id:
        logger.debug("V2 Kessel fallback denied: could not determine principal ID")
        return False

    return WorkspaceInventoryAccessChecker().check_resource_access(
        resource_type="tenant",
        resource_id=resource_id,
        principal_id=principal_id,
        relation=relation,
    )
