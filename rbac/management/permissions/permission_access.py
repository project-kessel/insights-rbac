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
"""Defines the Permission Access Permissions class."""

import logging

from management.permissions.utils import check_v2_kessel_access
from rest_framework import permissions

from rbac.env import ENVIRONMENT

logger = logging.getLogger(__name__)


class PermissionAccessPermission(permissions.BasePermission):
    """Determines if a user has access to Permission APIs."""

    def has_permission(self, request, view):
        """Check permission based on the defined access."""
        if ENVIRONMENT.get_value("ALLOW_ANY", default=False, cast=bool):
            return True
        if request.user.admin:
            return True
        if request.method in permissions.SAFE_METHODS:
            permission_read = request.user.access.get("permission", {}).get("read", [])
            if permission_read:
                return True
            if check_v2_kessel_access(request):
                return True

        # Authorization failure - SEC-MON-REQ-1 compliance (EOI-8 authorization_failure)
        logger.warning(
            "Authorization denied",
            extra={
                "action": request.method,
                "resource_type": "permission",
                "outcome": "failure",
                "org_id": getattr(request.user, "org_id", None),
                "username": getattr(request.user, "username", None),
                "reason": "insufficient_permissions",
                "endpoint": request.path,
            },
        )
        return False
