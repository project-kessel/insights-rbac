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
"""Mixin for v2 query-parameter serializers with dotted keys."""

from typing import ClassVar


class DottedQueryParamSerializerMixin:
    """Remap dotted query param keys and strip NUL bytes before field validation.

    Subclasses set ``DOTTED_PARAM_MAP`` to map API query names (e.g.
    ``resource.tenant.org_id``) to serializer field names (e.g.
    ``resource_tenant_org_id``).
    """

    DOTTED_PARAM_MAP: ClassVar[dict[str, str]] = {}

    def to_internal_value(self, data):
        """Remap dotted query param keys and sanitize NUL bytes."""
        remapped = {key: data[key] for key in data}
        for dotted, underscored in self.DOTTED_PARAM_MAP.items():
            if dotted in remapped:
                remapped[underscored] = remapped.pop(dotted)
        sanitized = {
            key: value.replace("\x00", "") if isinstance(value, str) else value for key, value in remapped.items()
        }
        return super().to_internal_value(sanitized)
