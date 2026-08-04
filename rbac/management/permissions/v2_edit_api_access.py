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
"""Permission classes for gating V1/V2 write operations by the v2_edit_api feature flag.

These permission classes serve as a fast, non-locking first line of defense.
The authoritative check with row-level locking happens inside the transaction
(see management.tenant_mapping.v2_activation).
"""

import logging

from feature_flags import FEATURE_FLAGS
from management.tenant_mapping.v2_activation import is_v2_write_activated
from rest_framework import permissions

logger = logging.getLogger(__name__)


def is_v2_read_enabled_for_request(request) -> bool:
    """Check if V2 read API is enabled via feature flag OR DB activation state."""
    return is_v2_write_activated(request.tenant) or FEATURE_FLAGS.is_v2_read_api_enabled(request.user.org_id)


def is_v2_edit_enabled_for_request(request) -> bool:
    """Check if V2 edit API is enabled via feature flag OR DB activation state."""
    return is_v2_write_activated(request.tenant) or FEATURE_FLAGS.is_v2_edit_api_enabled(request.user.org_id)


class V1WriteBlockedWhenWorkspacesEnabled(permissions.BasePermission):
    """Deny V1 write operations when workspaces (v2 edit API) is enabled for the org.

    Checks both the feature flag and the database activation state. If either
    indicates V2 is active, V1 writes are blocked.

    Add to V1 viewsets (RoleViewSet, GroupViewSet) to block write requests
    for orgs that have been migrated to workspaces.
    """

    message = "V1 write operations are not allowed for orgs using workspaces."

    def has_permission(self, request, view):
        """Allow reads always; deny writes when v2 edit API is enabled for this org."""
        if request.method in permissions.SAFE_METHODS:
            return True
        if is_v2_edit_enabled_for_request(request):
            # Authorization failure - SEC-MON-REQ-1 compliance (EOI-8 authorization_failure)
            logger.warning(
                "Authorization denied",
                extra={
                    "action": request.method,
                    "resource_type": view.basename if hasattr(view, "basename") else "unknown",
                    "outcome": "failure",
                    "org_id": getattr(request.user, "org_id", None),
                    "username": getattr(request.user, "username", None),
                    "reason": "v1_write_blocked_workspaces_enabled",
                    "endpoint": request.path,
                },
            )
            return False
        return True


class V1ApiBlockedWhenWorkspacesEnabled(permissions.BasePermission):
    """Block all access to a V1 API endpoint when workspaces are enabled for the org.

    Unlike V1WriteBlockedWhenWorkspacesEnabled, this blocks read methods too. Use on
    V1 endpoints that return data from the V1 data model, which is no longer authoritative
    once a tenant has been migrated to workspaces.

    Note: The read-only ``/access`` endpoint does not use this class; it filters results
    to permissions in ``V2_MIGRATION_APP_EXCLUDE_LIST`` applications when workspaces are enabled.
    """

    message = "This V1 API is not available for orgs using workspaces."

    def has_permission(self, request, view):
        """Deny all requests when v2 edit API is enabled for this org."""
        if is_v2_edit_enabled_for_request(request):
            # Authorization failure - SEC-MON-REQ-1 compliance (EOI-8 authorization_failure)
            logger.warning(
                "Authorization denied",
                extra={
                    "action": request.method,
                    "resource_type": view.basename if hasattr(view, "basename") else "unknown",
                    "outcome": "failure",
                    "org_id": getattr(request.user, "org_id", None),
                    "username": getattr(request.user, "username", None),
                    "reason": "v1_api_blocked_workspaces_enabled",
                    "endpoint": request.path,
                },
            )
            return False
        return True


class RequiresV2OptIn(permissions.BasePermission):
    """Deny V2 operations when an org is not opted-in to them.

    Read and write operations are controlled by different feature flags: see FeatureFlags.is_v2_read_api_enabled and
    FeatureFlags.is_v2_edit_api_enabled.

    Notwithstanding whether the feature flag is enabled, this permission will always allow tenants for which
    TenantMapping.v2_write_activated_at is set, since such a tenant can no longer use the V1 API.
    """

    message = "V2 operations require the org to have opted-in."

    def has_permission(self, request, view):
        """Allow reads always; deny writes when v2 edit API is disabled for this org."""
        is_write = request.method not in permissions.SAFE_METHODS

        if is_write:
            if is_v2_edit_enabled_for_request(request):
                return True
        else:
            if is_v2_read_enabled_for_request(request):
                return True

        # Authorization failure - SEC-MON-REQ-1 compliance (EOI-8 authorization_failure)
        logger.warning(
            "Authorization denied",
            extra={
                "action": request.method,
                "resource_type": view.basename if hasattr(view, "basename") else "unknown",
                "outcome": "failure",
                "org_id": getattr(request.user, "org_id", None),
                "username": getattr(request.user, "username", None),
                "reason": "v2_write_requires_workspaces_enabled" if is_write else "v2_read_requires_api_enabled",
                "endpoint": request.path,
            },
        )

        return False
