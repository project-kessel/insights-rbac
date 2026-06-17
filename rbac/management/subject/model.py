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
"""Subject domain model with Active Record pattern."""

from enum import StrEnum

from management.group.model import Group
from management.principal.model import Principal
from management.subject.queryset import SubjectQuerySet


class SubjectType(StrEnum):
    """Valid subject types for role bindings."""

    GROUP = "group"
    USER = "user"
    PRINCIPAL = "principal"

    @classmethod
    def is_valid(cls, value: str) -> bool:
        """Check if a value is a valid subject type."""
        return value in cls._value2member_map_

    @classmethod
    def values(cls) -> list[str]:
        """Return list of all valid subject type values."""
        return list(cls._value2member_map_.keys())


class Subject:
    """Domain type representing a subject (group or user) in role bindings.

    This follows the Active Record pattern with a manager for lookups:
        subject = Subject.objects.group(id=uuid)
        subject = Subject.objects.user(id=uuid)
    """

    objects = SubjectQuerySet.as_manager()

    def __init__(self, type: SubjectType, entity: Group | Principal):
        """Initialize a Subject.

        Args:
            type: The subject type (GROUP or USER)
            entity: The underlying Group or Principal model instance
        """
        self.type = type
        self.entity = entity

    @property
    def uuid(self):
        """Return the UUID of the underlying entity."""
        return self.entity.uuid

    @property
    def is_group(self) -> bool:
        """Check if this subject is a group."""
        return self.type == SubjectType.GROUP

    @property
    def is_user(self) -> bool:
        """Check if this subject is a user."""
        return self.type == SubjectType.USER

    def __eq__(self, other: object) -> bool:
        """Check equality based on type and entity."""
        if not isinstance(other, Subject):
            return NotImplemented
        return self.type == other.type and self.entity == other.entity

    def __repr__(self) -> str:
        """Return a string representation."""
        return f"Subject(type={self.type!r}, entity={self.entity!r})"
