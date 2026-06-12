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

    valid_ids is the set of valid IDs. A None ID is valid only for workspaces (as it represents the "Ungrouped Hosts"
    workspace if the REMOVE_NULL_VALUE flag is enabled); otherwise, all IDs must be strings.
    """

    resource_type: ObjectType

    valid_ids: frozenset[str | None]
    invalid_ids: tuple[Any, ...]

    def __init__(
        self,
        *,
        resource_type: ObjectType,
        valid_ids: Iterable[str | None],
        invalid_ids: Iterable[Any] = tuple(),
    ):
        """Create a new ParsedAttributeFilter."""
        super().__init__()

        object.__setattr__(self, "resource_type", resource_type)
        object.__setattr__(self, "valid_ids", frozenset(valid_ids))
        object.__setattr__(self, "invalid_ids", tuple(invalid_ids))

        if not isinstance(self.resource_type, ObjectType):
            raise TypeError(f"Expected resource_type to be ObjectType, but got: {self.resource_type!r}")

        if not all(isinstance(x, str) or x is None for x in self.valid_ids):
            raise TypeError(f"Expected valid_ids to be frozenset of str or None, but got: {self.valid_ids!r}")

        if (not self.is_for_workspaces()) and (None in self.valid_ids):
            raise TypeError("None is only a valid ID for workspaces")

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

    valid: list[str | None] = []
    invalid: list[Any] = []

    for value in _values_from_attribute_filter(attribute_filter):
        if value is None:
            if resource_type == _workspace_type:
                valid.append(value)
            else:
                invalid.append(value)

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
        valid_ids=valid,
        invalid_ids=invalid,
    )


def updated_attribute_filter_with_ids(
    attribute_filter: dict, new_ids: Iterable[Any], *, force_in_operation: bool = False
) -> dict:
    """
    Return an attribute filter equivalent to the provided one, but with new resource IDs.

    If force_in_operation is set, the resulting filter will always have operation "in", and any number of IDs can be
    provided. Otherwise, the original operation will be preserved; this means that, if attribute_filter has operation
    "equal", only at most one ID can be provided.

    The types of new_ids are not validated, since we need to be able to traffic in attribute filters with invalid IDs.
    """
    operation = attribute_filter["operation"]
    new_ids = list(new_ids)

    if force_in_operation or (operation == "in"):
        return {
            **attribute_filter,
            "operation": "in",
            "value": new_ids,
        }

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

    raise ValueError(f"Unexpected operation: {operation!r}")
