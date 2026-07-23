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
"""Defines the Group Access Permissions class."""

from management.permissions.utils import is_scope_principal
from rest_framework import permissions
from rest_framework.request import Request

from rbac.env import ENVIRONMENT

# Allowed methods to be able to modify principals from a group.
ALLOWED_METHODS = ["DELETE", "POST"]


class GroupAccessPermission(permissions.BasePermission):
    """Determines if a user has access to Group APIs."""

    def has_permission(self, request: Request, view):
        """Check permission based on the defined access."""
        if ENVIRONMENT.get_value("ALLOW_ANY", default=False, cast=bool):
            return True
        if request.user.admin:
            return True
        if request.method in permissions.SAFE_METHODS:
            group_read = request.user.access.get("group", {}).get("read", [])
            if group_read:
                return True
            if view.basename == "group" and view.action == "list":
                username = request.query_params.get("username")
                if username:
                    return username == request.user.username
                if not username and is_scope_principal(request):
                    return True
        else:
            group_write = request.user.access.get("group", {}).get("write", [])

            # In the case that group principals are trying to be modified, check that the user or the service account
            # has the proper "User Access Administrator" rights.
            if request.method in ALLOWED_METHODS and view.basename == "group" and view.action == "principals":
                principal_write = request.user.access.get("principal", {}).get("write", [])
                if group_write and principal_write:
                    return True
                else:
                    return False

            if group_write:
                return True

        return False
