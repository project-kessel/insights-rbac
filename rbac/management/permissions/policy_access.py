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
"""Defines the Policy Access Permissions class."""

from management.permissions.utils import is_scope_principal
from rest_framework import permissions

from rbac.env import ENVIRONMENT


class PolicyAccessPermission(permissions.BasePermission):
    """Determines if a user has access to Policy APIs."""

    def has_permission(self, request, view):
        """Check permission based on the defined access."""
        if ENVIRONMENT.get_value("ALLOW_ANY", default=False, cast=bool):
            return True
        if request.user.admin:
            return True
        if request.method in permissions.SAFE_METHODS:
            if is_scope_principal(request):
                return True
            policy_read = request.user.access.get("policy", {}).get("read", [])
            if policy_read:
                return True
        else:
            policy_write = request.user.access.get("policy", {}).get("write", [])
            if policy_write:
                return True

        return False
