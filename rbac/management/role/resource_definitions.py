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

import uuid
from typing import Any, Optional, Tuple, Union

from django.conf import settings
from management.utils import is_str_valid_uuid


def is_resource_a_workspace(application: str, resource_type: str, attributeFilter: dict) -> bool:
    """Check if a given ResourceDefinition is a Workspace."""
    is_workspace_application = application == settings.WORKSPACE_APPLICATION_NAME
    is_workspace_resource_type = resource_type in settings.WORKSPACE_RESOURCE_TYPE
    is_workspace_group_filter = attributeFilter.get("key") == settings.WORKSPACE_ATTRIBUTE_FILTER
    return is_workspace_application and is_workspace_resource_type and is_workspace_group_filter


def get_workspace_ids_from_resource_definition_with_malformed(attributeFilter: dict) -> tuple[list[uuid.UUID], list]:
    """Get workspace id from a resource definition. Returns a tuple of the valid entries and the invalid entries."""
    operation = attributeFilter.get("operation")

    valid = []
    invalid = []

    if operation == "in":
        value = attributeFilter.get("value", [])

        for val in value:
            if is_str_valid_uuid(val):
                valid.append(uuid.UUID(val))
            else:
                invalid.append(val)

    elif operation == "equal":
        value = attributeFilter.get("value", "")

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
    resource_id: Union[list[str], str] = attribute_filter["value"]

    if isinstance(resource_id, list):
        return resource_id

    return [resource_id]


V2_RESOURCE_BY_ATTRIBUTE = {"group.id": ("rbac", "workspace")}


def attribute_key_to_v2_related_resource_type(resourceType: str) -> Optional[Tuple[str, str]]:
    """Convert a V1 resource type to a V2 resource type."""
    if resourceType in V2_RESOURCE_BY_ATTRIBUTE:
        return V2_RESOURCE_BY_ATTRIBUTE[resourceType]
    return None
