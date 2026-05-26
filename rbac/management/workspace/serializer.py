#
# Copyright 2019 Red Hat, Inc.
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

"""Serializer for workspace management."""

import re
import uuid

from management.workspace.service import WorkspaceService
from rest_framework import serializers

from .model import Workspace

WORKSPACE_NAME_REGEX = re.compile(r"^[\w\s-]+$")

_ALL_TYPE = "all"
_VALID_TYPES = [v.lower() for v in Workspace.Types.values] + [_ALL_TYPE]


class WorkspaceListInputSerializer(serializers.Serializer):
    """Input serializer for workspace list query parameters.

    GET /v2/workspaces/
    """

    type = serializers.CharField(required=False, allow_blank=True, help_text="Filter by workspace type")
    name = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Filter by workspace name. Use * as wildcard for partial matching.",
    )
    parent_id = serializers.CharField(required=False, allow_blank=True, help_text="Filter by parent workspace ID")
    ids = serializers.CharField(required=False, allow_blank=True, help_text="Filter by comma-separated workspace IDs")

    def to_internal_value(self, data):
        """Reject NUL bytes in query parameters."""
        for key, value in data.items():
            if isinstance(value, str) and "\x00" in value:
                raise serializers.ValidationError({key: f"The '{key}' query parameter contains invalid characters."})
        return super().to_internal_value(data)

    def validate_type(self, value: str | None) -> list[str] | None:
        """Normalize empty to None, split comma-separated values, validate against allowed types."""
        if not value or not value.strip():
            return None
        fields = [v.strip().lower() for v in value.split(",") if v.strip()]
        if not fields:
            return None
        for val in fields:
            if val not in _VALID_TYPES:
                raise serializers.ValidationError(
                    f"type query parameter value '{val}' is invalid. "
                    f"Allowed values are {[str(v) for v in _VALID_TYPES]}."
                )
        if _ALL_TYPE in fields:
            return None
        return fields

    def validate_name(self, value: str | None) -> str | None:
        """Return None for empty values, strip surrounding whitespace."""
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        return cleaned

    def validate_parent_id(self, value: str | None) -> str | None:
        """Return None for empty values, validate UUID format otherwise."""
        if not value:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            uuid.UUID(cleaned)
        except ValueError as e:
            raise serializers.ValidationError(f"{cleaned} is not a valid UUID.") from e
        return cleaned

    def validate_ids(self, value: str | None) -> list[str] | None:
        """Return None for empty values, split and validate UUIDs otherwise."""
        if not value or not value.strip():
            return None
        ids = list(dict.fromkeys(stripped for id_val in value.split(",") if (stripped := id_val.strip().lower())))
        for workspace_id in ids:
            try:
                uuid.UUID(workspace_id)
            except ValueError as e:
                raise serializers.ValidationError(f"{workspace_id} is not a valid UUID.") from e
        return ids

    def validate(self, data):
        """Cross-field validation: ids without explicit type defaults to standard."""
        if data.get("ids") is not None and "type" not in self.initial_data:
            data["type"] = [Workspace.Types.STANDARD]
        return data


class WorkspaceSerializer(serializers.ModelSerializer):
    """Serializer for the Workspace model."""

    id = serializers.UUIDField(read_only=True, required=False)
    name = serializers.CharField(required=True, max_length=255)
    description = serializers.CharField(allow_null=True, required=False, max_length=255)
    parent_id = serializers.UUIDField(required=False)
    created = serializers.DateTimeField(read_only=True)
    modified = serializers.DateTimeField(read_only=True)
    type = serializers.CharField(read_only=True)

    class Meta:
        """Metadata for the serializer."""

        model = Workspace
        fields = (
            "name",
            "id",
            "parent_id",
            "description",
            "created",
            "modified",
            "type",
        )

    @property
    def _service(self) -> WorkspaceService:
        return self.context["view"]._service

    def validate_name(self, value):
        """Reject names with characters other than letters, numbers, spaces, hyphens, and underscores.

        Existing names are grandfathered: skip validation when the name is unchanged on update.
        """
        if self.instance and self.instance.name == value:
            return value
        if not WORKSPACE_NAME_REGEX.match(value):
            raise serializers.ValidationError(
                "Workspace name may only contain letters, numbers, spaces, hyphens, and underscores."
            )
        return value

    def validate(self, attrs):
        """Require parent_id in the body for PUT (full update) requests."""
        request = self.context.get("request")
        if request and request.method == "PUT" and "parent_id" not in attrs:
            instance = self.instance
            if instance and instance.type not in (Workspace.Types.ROOT, Workspace.Types.UNGROUPED_HOSTS):
                raise serializers.ValidationError({"parent_id": "This field is required."})
        return attrs

    def create(self, validated_data):
        """Create the workspace object in the database."""
        tenant = self.context["request"].tenant
        return self._service.create(validated_data, tenant)

    def update(self, instance, validated_data):
        """Update the workspace object in the database."""
        return self._service.update(instance, validated_data)

    def move(self, instance, target_workspace):
        """Move the workspace object in the database."""
        updated_workspace = self._service.move(instance, target_workspace)
        return {"id": str(updated_workspace.id), "parent_id": str(updated_workspace.parent_id)}


class WorkspaceAncestrySerializer(serializers.ModelSerializer):
    """Serializer for the Workspace ancestry."""

    id = serializers.UUIDField(read_only=True, required=False)
    name = serializers.CharField(required=False, max_length=255)
    parent_id = serializers.UUIDField(allow_null=True, required=False)

    class Meta:
        """Metadata for the serializer."""

        model = Workspace
        fields = ("name", "id", "parent_id")


class WorkspaceWithAncestrySerializer(WorkspaceSerializer):
    """Serializer for the Workspace model with ancestry."""

    ancestry = serializers.SerializerMethodField()

    class Meta:
        """Metadata for the serializer."""

        model = Workspace
        fields = WorkspaceSerializer.Meta.fields + ("ancestry",)

    def get_ancestry(self, obj):
        """Serialize the workspace's ancestors."""
        ancestors = obj.ancestors().only("name", "id", "parent_id")
        return WorkspaceAncestrySerializer(ancestors, many=True).data


class WorkspaceEventSerializer(serializers.ModelSerializer):
    """Serializer for the Workspace model for sending event."""

    id = serializers.UUIDField()
    name = serializers.CharField()
    type = serializers.CharField()
    created = serializers.DateTimeField()
    modified = serializers.DateTimeField()

    class Meta:
        """Metadata for the serializer."""

        model = Workspace
        fields = (
            "name",
            "id",
            "created",
            "modified",
            "type",
        )
        read_only_fields = (
            "name",
            "id",
            "created",
            "modified",
            "type",
        )
