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

from __future__ import annotations

from management.group.model import Group
from rest_framework import serializers


class GroupV2ResponseSerializer(serializers.ModelSerializer):
    """Serializer for GroupV2 API responses."""

    id = serializers.UUIDField(source="uuid", read_only=True)
    name = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    principal_count = serializers.SerializerMethodField()
    created = serializers.DateTimeField(read_only=True)
    last_modified = serializers.DateTimeField(source="modified", read_only=True)
    platform_default = serializers.BooleanField(read_only=True)
    admin_default = serializers.BooleanField(read_only=True)

    class Meta:
        model = Group
        fields = (
            "id",
            "name",
            "description",
            "principal_count",
            "created",
            "last_modified",
            "platform_default",
            "admin_default",
        )

    def get_principal_count(self, obj):
        """Return principal count, using annotation if available."""
        count = getattr(obj, "principal_count_annotation", None)
        if count is not None:
            return count

        return obj.principals.count()


class GroupV2ListSerializer(serializers.Serializer):
    """Input serializer for GroupV2 list query parameters."""

    name = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text=(
            "Filter by group name. Case-insensitive substring match by default; use * for glob patterns (e.g. foo*)."
        ),
    )

    def validate_name(self, value: str | None) -> str | None:
        """Return None for empty values."""
        return value or None


class GroupV2RequestSerializer(serializers.ModelSerializer):
    """Serializer for GroupV2 create/update requests."""

    name = serializers.CharField()
    description = serializers.CharField(required=False, allow_blank=True, default="")
    platform_default = serializers.BooleanField(write_only=True, required=False, default=False)
    admin_default = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = Group
        fields = ("name", "description", "platform_default", "admin_default")

    def validate(self, data):
        """Validate that platform_default and admin_default are not being set."""
        if data.get("platform_default"):
            raise serializers.ValidationError(
                {"platform_default": "Platform default groups cannot be created through the API."}
            )
        if data.get("admin_default"):
            raise serializers.ValidationError(
                {"admin_default": "Admin default groups cannot be created through the API."}
            )
        return data


class PrincipalIdSerializer(serializers.Serializer):
    """Serializer for a principal ID reference."""

    id = serializers.UUIDField(required=True, help_text="Principal identifier")


class GroupV2AddPrincipalsSerializer(serializers.Serializer):
    """Serializer for adding principals to a group."""

    principals = PrincipalIdSerializer(many=True, min_length=1)


class PrincipalV2ResponseSerializer(serializers.Serializer):
    """Serializer for principal responses in group context."""

    id = serializers.UUIDField(source="uuid")
    username = serializers.CharField()
    type = serializers.CharField()
    user_id = serializers.CharField(allow_null=True)
    service_account_id = serializers.CharField(allow_null=True)
