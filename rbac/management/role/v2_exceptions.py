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
"""Domain exceptions for RoleV2 operations."""

import uuid
from collections.abc import Iterable

from management.utils import as_uuid


class RoleV2Error(Exception):
    """Base exception for RoleV2 domain errors."""

    pass


class RoleAlreadyExistsError(RoleV2Error):
    """Raised when attempting to create a role with a name that already exists for the tenant."""

    def __init__(self, name: str):
        """Initialize with the duplicate role name."""
        self.name = name
        super().__init__(f"A role with name '{name}' already exists for this tenant.")


class PermissionsNotFoundError(RoleV2Error):
    """Raised when one or more permissions cannot be found."""

    def __init__(self, missing_permissions: list[str]):
        """Initialize with the list of missing permission strings."""
        self.missing_permissions = missing_permissions
        super().__init__(f"The following permissions do not exist: {', '.join(missing_permissions)}")


class RoleDatabaseError(RoleV2Error):
    """Raised when an unexpected database error occurs."""

    def __init__(self, message: str = "An unexpected database error occurred."):
        """Initialize with optional custom message."""
        super().__init__(message)


class InvalidRolePermissionsError(RoleV2Error):
    """Raised when permission data for a role is malformed or invalid."""

    def __init__(self, message: str):
        """Initialize with the validation error message."""
        super().__init__(message)


class RolesNotFoundError(RoleV2Error):
    """Raised when one or more roles cannot be found."""

    def __init__(self, uuids: Iterable[str | uuid.UUID]):
        """Initialize RolesNotFoundError with UUIDs."""
        self.uuids = list(as_uuid(u) for u in uuids)

        if len(self.uuids) == 1:
            super().__init__(f"Role with UUID {str(self.uuids[0])!r} not found.")
        else:
            super().__init__(f"Roles with UUIDs {', '.join(repr(str(u)) for u in self.uuids)} not found.")


class CustomRoleRequiredError(RoleV2Error):
    """Raised when an operation requires a custom role, but a custom role was not provided."""

    def __init__(self, message: str):
        """Initialize the exception with a message."""
        super().__init__(message)
