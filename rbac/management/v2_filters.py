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
"""Shared V2 query filter utilities."""

import re

from django.db.models import QuerySet


def _glob_to_regex(pattern: str) -> str:
    """Convert a glob pattern with '*' wildcards to a regex."""
    parts = pattern.split("*", maxsplit=10)
    return "^" + ".*".join(re.escape(p) for p in parts) + "$"


def v2_name_filter(queryset: QuerySet, name: str, field: str = "name") -> QuerySet:
    """Filter a queryset by name with '*' glob support.

    Without wildcards, performs case-insensitive exact match.
    With '*' wildcards, converts to regex for pattern matching.
    A bare '*' matches everything (no filter applied).
    """
    if name == "*":
        return queryset
    if "*" in name:
        return queryset.filter(**{f"{field}__iregex": _glob_to_regex(name)})
    return queryset.filter(**{f"{field}__iexact": name})
