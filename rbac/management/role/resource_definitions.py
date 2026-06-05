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
from typing import Any, Iterable, Optional

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
    has_null: bool = False
    invalid_ids: tuple = tuple()

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


def _resource_type_for(attribute_filter: dict) -> Optional[ObjectType]:
    if attribute_filter["key"] == settings.WORKSPACE_ATTRIBUTE_FILTER:
        return _workspace_type

    return None


def _parse_id_for_resource_type(id, resource_type: ObjectType) -> Optional[str]:
    if resource_type == _workspace_type:
        if is_str_valid_uuid(id):
            # Normalize the string so that we don't create duplicate relations (if, e.g., the resource definition
            # contains the same UUID twice with different casing).
            return str(uuid.UUID(id))

        return None

    # We don't know how to parse IDs for any resource other than workspaces.
    return id


# We have established that only "in" and "equal" are used as operators in all environments, so it's safe to check for
# those here. We've also established that all "equal" operator values are strings.
#
# There is one entry that has a string as an "in" operator value, but its key is irrelevant and will never be used, so
# we can ignore it.
#
# We have not established that all existing "in" operator values that are lists contain only strings, so we can't add a
# stronger type hint. (We want to allow them to be returned as invalid values.)
#
# For attribute filters that aren't already stored, nulls are valid as values, so we need to handle them.
def _values_from_attribute_filter(attribute_filter: dict[str, Any]) -> list:
    """Split a resource definition into a list of resource IDs."""
    operation = attribute_filter["operation"]
    value = attribute_filter["value"]

    if operation == "equal":
        if not (value is None or isinstance(value, str)):
            raise TypeError(f'Expected "equal" value to be a string, but got: {value!r}')

        return [value]

    if operation == "in":
        if not isinstance(value, list):
            raise TypeError(f'Expected "in" value to be a list, but got: {value!r}')

        return list(value)

    raise ValueError(f"Unexpected operation: {operation!r}")


def parse_attribute_filter(attribute_filter: dict) -> Optional[ParsedAttributeFilter]:
    """Parse a raw attribute filter dict into a ParsedAttributeFilter."""
    resource_type = _resource_type_for(attribute_filter)

    if resource_type is None:
        return None

    valid = []
    invalid = []
    has_null = False

    for value in _values_from_attribute_filter(attribute_filter):
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


def is_resource_a_workspace(attribute_filter: dict) -> bool:
    """Check if a given ResourceDefinition is a Workspace."""
    return _resource_type_for(attribute_filter=attribute_filter) == _workspace_type


def updated_attribute_filter_with_ids(attribute_filter: dict, new_ids: Iterable[str | None]) -> dict:
    """Return an attribute filter equivalent to the provided one, but with new resource IDs."""
    operation = attribute_filter["operation"]
    new_ids = list(new_ids)

    if not all((x is None) or (isinstance(x, str)) for x in new_ids):
        raise TypeError(f"Expected all IDs to be strings or None, but got: {new_ids}")

    if operation == "equal":
        if len(new_ids) == 1:
            return {
                **attribute_filter,
                "value": new_ids[0],
            }
        elif len(new_ids) == 0:
            # Empty string represents something that cannot match (distinct from None).
            return {
                **attribute_filter,
                "value": "",
            }
        else:
            raise ValueError(f'Cannot add more than one ID for a filter with operation "equal", but got: {new_ids}')
    elif operation == "in":
        return {
            **attribute_filter,
            "value": new_ids,
        }
    else:
        raise ValueError(f"Unexpected operation: {operation!r}")
