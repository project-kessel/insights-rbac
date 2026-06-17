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
"""Shared domain exceptions for the management module."""


class RequiredFieldError(Exception):
    """Raised when a required field is missing."""

    def __init__(self, field_name: str):
        """Initialize with the missing field name."""
        super().__init__(f"{field_name} is required")
        self.field_name = field_name


class InvalidFieldError(Exception):
    """Raised when a field value fails validation."""

    def __init__(self, field: str, message: str):
        """Initialize with the field name and validation message."""
        super().__init__(f"Invalid field '{field}': {message}")
        self.field = field


class NotFoundError(Exception):
    """Raised when a resource cannot be found."""

    def __init__(self, resource_type: str, resource_id: str):
        """Initialize with resource type and identifier."""
        self.resource_type = resource_type
        self.resource_id = resource_id
        super().__init__(f"{resource_type} with id '{resource_id}' not found")
