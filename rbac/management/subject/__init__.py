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
"""Domain types and services for subjects in the RBAC system.

A "subject" is an entity that can be granted permissions via role bindings.
Currently supported subjects are:
- Groups (collections of users)
- Users (individual principals)
"""

from management.subject.exceptions import SubjectError, UnsupportedSubjectTypeError
from management.subject.model import Subject, SubjectType
from management.subject.queryset import SubjectQuerySet

__all__ = [
    "Subject",
    "SubjectQuerySet",
    "SubjectError",
    "SubjectType",
    "UnsupportedSubjectTypeError",
]
