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
"""Domain exceptions for Subject operations."""


class SubjectError(Exception):
    """Base exception for Subject domain errors."""

    pass


class UnsupportedSubjectTypeError(SubjectError):
    """Raised when an unsupported subject type is provided."""

    def __init__(self, subject_type: str, supported: list[str] | None = None):
        """Initialize with the unsupported subject type."""
        from management.subject.model import SubjectType

        self.subject_type = subject_type
        self.supported = supported or SubjectType.values()
        supported_str = ", ".join(self.supported)
        super().__init__(f"Unsupported subject type: '{subject_type}'. Supported types: {supported_str}")
