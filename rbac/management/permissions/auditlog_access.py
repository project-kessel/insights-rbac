#
# Copyright 2024 Red Hat, Inc.
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
"""Defines the Audit Log Access Permissions class."""

import logging

from rest_framework import permissions

logger = logging.getLogger(__name__)


class AuditLogAccessPermission(permissions.BasePermission):
    """Determines if a user is an Account Admin."""

    def has_permission(self, request, view):
        """Check permission based on Account Admin property."""
        if not request.user.admin:
            # Authorization failure - SEC-MON-REQ-1 compliance (EOI-8 authorization_failure)
            logger.warning(
                "Authorization denied",
                extra={
                    "action": request.method,
                    "resource_type": "audit_log",
                    "outcome": "failure",
                    "org_id": getattr(request.user, "org_id", None),
                    "username": getattr(request.user, "username", None),
                    "reason": "not_admin",
                    "endpoint": request.path,
                },
            )
            return False
        return True
