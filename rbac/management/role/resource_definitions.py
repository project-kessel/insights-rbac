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
"""Utilities for parsing resource definitions."""

import dataclasses
import uuid
from typing import Any, Optional, Union

from django.conf import settings
from management.relation_replicator.types import ObjectType
from management.utils import is_str_valid_uuid


@dataclasses.dataclass(frozen=True)
class ParsedAttributeFilter:
    """
    Represents an attribute filter used for V2 access.

    "named_ids" is used rather than e.g. "valid_ids" because not all meaningful IDs are strings, as a null ID is not
    necessarily invalid.  (In the case of workspaces, it may represent the ungrouped hosts workspace.) As such,
    None is also never included in invalid_ids.
    """

    resource_type: ObjectType

    named_ids: frozenset[str]
    has_null: bool
    invalid_ids: tuple

    def __post_init__(self):
        """Validate the ParsedAttributeFilter instance."""
        if not isinstance(self.resource_type, ObjectType):
            raise TypeError(f"Expected resource_type to be ObjectType, got: {self.resource_type!r}")

        if not isinstance(self.named_ids, frozenset) or not all(isinstance(x, str) for x in self.named_ids):
            raise TypeError(f"Expected named_ids to be frozenset of strs, got: {self.named_ids!r}")

        if not isinstance(self.has_null, bool):
            raise TypeError(f"Expected has_null to be bool, got: {self.has_null!r}")

        if not isinstance(self.invalid_ids, tuple):
            raise TypeError(f"Expected invalid_ids to be tuple, got: {self.invalid_ids!r}")

        if None in self.invalid_ids:
            raise TypeError("None should not be in invalid_ids; instead, set has_null to True")

    def is_for_workspaces(self):
        """Get whether this attribute filter's resource type is the workspace resource type."""
        return self.resource_type == _workspace_type


_workspace_type = ObjectType("rbac", "workspace")
_resource_type_by_attribute = {"group.id": _workspace_type}


def _parse_id_for_resource_type(id, resource_type: ObjectType) -> Optional[str]:
    if resource_type == _workspace_type:
        if is_str_valid_uuid(id):
            # Normalize the string so that we don't create duplicate relations (if, e.g., the resource definition
            # contains the same UUID twice with different casing).
            return str(uuid.UUID(id))

        return None

    # We don't know how to parse IDs for any resource other than workspaces.
    return id


def parse_attribute_filter(attribute_filter: dict) -> Optional[ParsedAttributeFilter]:
    """Parse a raw attribute filter dict into a ParsedAttributeFilter."""
    attribute = attribute_filter.get("key")

    if not isinstance(attribute, str):
        return None

    resource_type = _resource_type_by_attribute.get(attribute)

    if resource_type is None:
        return None

    valid = []
    invalid = []
    has_null = False

    for value in values_from_attribute_filter(attribute_filter):
        if value is None:
            has_null = True
            continue

        if not isinstance(value, str):
            invalid.append(value)
            continue

        parsed = _parse_id_for_resource_type(value, resource_type=resource_type)

        if parsed is None:
            invalid.append(value)
            continue

        valid.append(parsed)

    return ParsedAttributeFilter(
        resource_type=resource_type,
        named_ids=frozenset(valid),
        has_null=has_null,
        invalid_ids=tuple(invalid),
    )


def is_resource_a_workspace(application: str, resource_type: str, attributeFilter: dict) -> bool:
    """Check if a given ResourceDefinition is a Workspace."""
    is_workspace_application = application == settings.WORKSPACE_APPLICATION_NAME
    is_workspace_resource_type = resource_type in settings.WORKSPACE_RESOURCE_TYPE
    is_workspace_group_filter = attributeFilter.get("key") == settings.WORKSPACE_ATTRIBUTE_FILTER
    return is_workspace_application and is_workspace_resource_type and is_workspace_group_filter


def get_workspace_ids_from_resource_definition_with_malformed(attributeFilter: dict) -> tuple[list[uuid.UUID], list]:
    """Get workspace id from a resource definition. Returns a tuple of the valid entries and the invalid entries."""
    valid = []
    invalid = []

    for value in values_from_attribute_filter(attributeFilter):
        if is_str_valid_uuid(value):
            valid.append(uuid.UUID(value))
        else:
            invalid.append(value)

    return valid, invalid


def get_workspace_ids_from_resource_definition(attributeFilter: dict) -> list[uuid.UUID]:
    """Get workspace id from a resource definition."""
    return get_workspace_ids_from_resource_definition_with_malformed(attributeFilter)[0]


def values_from_attribute_filter(attribute_filter: dict[str, Any]) -> list[str]:
    """Split a resource definition into a list of resource IDs."""
    resource_id: Union[list[str], str] = attribute_filter.get("value", [])

    if isinstance(resource_id, list):
        return resource_id

    return [resource_id]
