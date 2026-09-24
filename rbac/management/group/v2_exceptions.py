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
"""Domain exceptions for GroupV2 operations."""


class GroupV2Error(Exception):
    """Base exception for GroupV2 domain errors."""

    pass


class GroupAlreadyExistsError(GroupV2Error):
    """Raised when a group with the same name already exists for the tenant."""

    def __init__(self, name: str):
        """Initialize with the duplicate group name."""
        self.name = name
        super().__init__(f"A group with name '{name}' already exists for this tenant.")


class ProtectedGroupError(GroupV2Error):
    """Raised when an operation is not allowed on a protected (system, platform default, admin default) group."""

    def __init__(self, action: str, flag: str):
        """Initialize with the attempted action and the protecting flag."""
        self.action = action
        self.flag = flag
        super().__init__(f"Groups with {flag}=true may not be {action}.")


class GroupHasRoleBindingsError(GroupV2Error):
    """Raised when deleting a group that is still referenced by role bindings."""

    def __init__(self, binding_count: int):
        """Initialize with the number of role bindings referencing the group."""
        self.binding_count = binding_count
        super().__init__(
            f"Group is referenced by {binding_count} active role binding(s). "
            "Remove the group from all role bindings before deleting it."
        )
