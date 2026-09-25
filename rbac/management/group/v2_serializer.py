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
"""Serializers for GroupV2 API."""

from uuid import UUID

from management.group.model import Group
from management.group.v2_service import GroupV2Service
from management.utils import normalize_blank_or_none
from rest_framework import serializers

RESERVED_GROUP_NAMES = {"custom default access", "default access"}
VALID_ORDER_BY_FIELDS = {prefix + field for field in GroupV2Service.ORDER_BY_FIELD_MAPPING for prefix in ("", "-")}


class GroupV2ResponseSerializer(serializers.ModelSerializer):
    """Output serializer for the Group V2 API."""

    principal_count = serializers.IntegerField(source="principal_count_annotation", read_only=True)
    role_count = serializers.IntegerField(source="role_count_annotation", read_only=True)

    class Meta:
        model = Group
        fields = (
            "uuid",
            "name",
            "description",
            "principal_count",
            "role_count",
            "created",
            "modified",
            "system",
            "platform_default",
            "admin_default",
        )


class GroupV2RequestSerializer(serializers.Serializer):
    """Input serializer for Group V2 create/update requests."""

    name = serializers.CharField(min_length=1, max_length=150)
    description = serializers.CharField(max_length=1000, required=False, allow_blank=True, allow_null=True)

    def validate_name(self, value):
        """Reject names reserved for default groups."""
        if value.strip().lower() in RESERVED_GROUP_NAMES:
            raise serializers.ValidationError(f"'{value}' is reserved, please use another name.")
        return value


class GroupV2ListInputSerializer(serializers.Serializer):
    """Input serializer for Group V2 list query parameters."""

    name = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Filter by name. Case-insensitive substring match by default; use * for glob patterns.",
    )
    uuid = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Filter by comma-separated group UUIDs.",
    )
    system = serializers.BooleanField(required=False, allow_null=True, default=None)
    platform_default = serializers.BooleanField(required=False, allow_null=True, default=None)
    admin_default = serializers.BooleanField(required=False, allow_null=True, default=None)
    order_by = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text=f"Sort by field, prefix with '-' for descending. Valid: {', '.join(sorted(VALID_ORDER_BY_FIELDS))}.",
    )

    validate_name = staticmethod(normalize_blank_or_none)

    def validate_uuid(self, value):
        """Parse comma-separated UUIDs, ignoring empty entries. Returns None when no UUIDs remain."""
        uuids = []
        for item in (v.strip() for v in value.split(",")):
            if not item:
                continue
            try:
                uuids.append(UUID(item))
            except ValueError:
                raise serializers.ValidationError(f"'{item}' is not a valid UUID.")
        return uuids or None

    def validate_order_by(self, value):
        """Reject order_by values outside the allowed set; a blank value falls back to the default."""
        if not value:
            return None
        if value not in VALID_ORDER_BY_FIELDS:
            raise serializers.ValidationError(
                f"Invalid order_by value '{value}'. Valid values: {', '.join(sorted(VALID_ORDER_BY_FIELDS))}"
            )
        return value
