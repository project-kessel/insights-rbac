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
"""Serializers for the PermissionV2 API."""

from management.models import Permission
from management.utils import FieldSelection, FieldSelectionValidationError
from rest_framework import serializers

DEFAULT_PERMISSION_FIELDS = {
    "application",
    "resource_type",
    "operation",
    "permission",
    "description",
    "requires",
}


class PermissionFieldSelection(FieldSelection):
    """Field selection for the permissions endpoint."""

    VALID_ROOT_FIELDS = DEFAULT_PERMISSION_FIELDS


def validate_fields_parameter(value: str, default_fields: set = DEFAULT_PERMISSION_FIELDS) -> set:
    """Validate and parse the ``fields`` parameter for the permissions endpoint.

    Raises a ValidationError for unrecognized field names. Returns the default
    fields when no value is provided.
    """
    if not value:
        return default_fields

    try:
        field_selection = PermissionFieldSelection.parse(value)
    except FieldSelectionValidationError as e:
        raise serializers.ValidationError(e.message)

    if not field_selection or not field_selection.root_fields:
        return default_fields

    return field_selection.root_fields


class PermissionV2ResponseSerializer(serializers.ModelSerializer):
    """Serializer for Permission API responses."""

    operation = serializers.CharField(source="verb", read_only=True)
    requires = serializers.SerializerMethodField()

    class Meta:
        """Metadata for the serializer."""

        model = Permission
        fields = ("application", "resource_type", "operation", "permission", "description", "requires")

    def __init__(self, *args, **kwargs):
        """Initialize with dynamic field selection from context."""
        super().__init__(*args, **kwargs)

        allowed = self.context.get("fields")
        if allowed is not None:
            for field_name in set(self.fields) - allowed:
                self.fields.pop(field_name)

    def get_requires(self, obj):
        """Get dependent/required permissions.

        Reads from prefetched ``permissions`` objects rather than calling
        ``values_list()`` which would issue a new query per row, bypassing
        the ``prefetch_related("permissions")`` applied by the viewset.
        """
        return [p.permission for p in obj.permissions.all()]
