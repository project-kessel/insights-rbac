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


def is_v2_edit_enabled_for_request(request) -> bool:
    """Check if V2 edit API is enabled via feature flag OR DB activation state."""
    if FEATURE_FLAGS.is_v2_edit_api_enabled(request.user.org_id):
        return True

    return is_v2_write_activated(request.tenant)


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
        return not is_v2_edit_enabled_for_request(request)


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
        return not is_v2_edit_enabled_for_request(request)


class V2WriteRequiresWorkspacesEnabled(permissions.BasePermission):
    """Deny V2 write operations when workspaces (v2 edit API) is NOT enabled for the org.

    Checks both the feature flag and the DB activation state. A tenant that has already
    written via V2 must remain able to do so even if the feature flag is later disabled;
    otherwise they would be locked out of both V1 (permanently blocked by assert_v1_write_allowed)
    and V2 (blocked here).

    Add to V2 viewsets (RoleV2ViewSet, RoleBindingViewSet) to block write requests
    for orgs that have not been migrated to workspaces.
    """

    message = "V2 write operations require the workspaces feature to be enabled for this org."

    def has_permission(self, request, view):
        """Allow reads always; deny writes when v2 edit API is disabled for this org."""
        if request.method in permissions.SAFE_METHODS:
            return True
        return is_v2_edit_enabled_for_request(request)
