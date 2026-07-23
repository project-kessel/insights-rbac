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

"""Serializer for group management."""

from management.group.model import Group
from management.notifications.notification_handlers import group_obj_change_notification_handler
from management.permissions.v2_edit_api_access import is_v2_edit_enabled_for_request
from management.principal.proxy import PrincipalProxy
from management.principal.serializer import PrincipalInputSerializer, PrincipalSerializer
from management.role.serializer import RoleMinimumSerializer
from management.serializer_override_mixin import SerializerCreateOverrideMixin
from rest_framework import serializers, status

# V2 description overrides to temporary shim while tenants are split between v1/v2.
# Remove these (and _v2_description_override/to_representation) once all tenants
# are migrated to v2 and group descriptions have been updated in RBAC config.
_V2_GROUP_DESCRIPTION = (
    "This group contains all users in your organization. "
    "It can be bound to roles at any workspace level to grant permissions to all users."
)

_V2_ADMIN_GROUP_DESCRIPTION = (
    "This group contains all org admin users in your organization. "
    "It can be bound to roles at any workspace level to grant permissions to all org admins."
)


_V2_DESCRIPTIONS = {
    "platform_default": _V2_GROUP_DESCRIPTION,
    "admin_default": _V2_ADMIN_GROUP_DESCRIPTION,
}


def _is_v2_org(request):
    """Check/set whether or not the requesting org has moved to v2."""
    if not hasattr(request, "_is_v2_org"):
        request._is_v2_org = is_v2_edit_enabled_for_request(request)
    return request._is_v2_org


def _v2_description_override(data, request):
    """Conditionally override the default group descriptions for v2 orgs."""
    if request is None:
        return
    for flag, description in _V2_DESCRIPTIONS.items():
        if data.get(flag) and _is_v2_org(request):
            data["description"] = description
            return


class GroupInputSerializer(SerializerCreateOverrideMixin, serializers.ModelSerializer):
    """Serializer for Group input model."""

    uuid = serializers.UUIDField(read_only=True)
    name = serializers.CharField(required=True, max_length=150)
    description = serializers.CharField(allow_null=True, required=False)
    principalCount = serializers.IntegerField(read_only=True)
    platform_default = serializers.BooleanField(read_only=True)
    admin_default = serializers.BooleanField(read_only=True)
    system = serializers.BooleanField(read_only=True)
    roleCount = serializers.SerializerMethodField()
    created = serializers.DateTimeField(read_only=True)
    modified = serializers.DateTimeField(read_only=True)

    def get_roleCount(self, obj):
        """Role count for the serializer."""
        return obj.role_count()

    def to_representation(self, obj):
        """Override representation to update description for v2 tenants."""
        data = super().to_representation(obj)
        _v2_description_override(data, self.context.get("request"))
        return data

    class Meta:
        """Metadata for the serializer."""

        model = Group
        fields = (
            "uuid",
            "name",
            "description",
            "principalCount",
            "platform_default",
            "admin_default",
            "roleCount",
            "created",
            "modified",
            "system",
        )

    def create(self, validated_data):
        """Create the role object in the database."""
        group = super().create(validated_data)
        group_obj_change_notification_handler(self.context["request"].user, group, "created")
        return group

    def update(self, instance, validated_data):
        """Update the role object in the database."""
        group = super().update(instance, validated_data)
        group_obj_change_notification_handler(self.context["request"].user, group, "updated")
        return group


class GroupSerializer(SerializerCreateOverrideMixin, serializers.ModelSerializer):
    """Serializer for the Group model."""

    uuid = serializers.UUIDField(read_only=True)
    name = serializers.CharField(required=True, max_length=150)
    description = serializers.CharField(allow_null=True, required=False)
    principals = PrincipalSerializer(read_only=True, many=True)
    platform_default = serializers.BooleanField(read_only=True)
    admin_default = serializers.BooleanField(read_only=True)
    system = serializers.BooleanField(read_only=True)
    roles = serializers.SerializerMethodField()
    roleCount = serializers.SerializerMethodField()
    created = serializers.DateTimeField(read_only=True)
    modified = serializers.DateTimeField(read_only=True)

    class Meta:
        """Metadata for the serializer."""

        model = Group
        fields = (
            "uuid",
            "name",
            "description",
            "principals",
            "platform_default",
            "admin_default",
            "created",
            "modified",
            "roles",
            "roleCount",
            "system",
        )

    def to_representation(self, obj):
        """Convert representation to dictionary object."""
        proxy = PrincipalProxy()
        formatted = super().to_representation(obj)
        principals = formatted.pop("principals")
        users = [principal.get("username") for principal in principals]
        resp = proxy.request_filtered_principals(users, limit=len(users))
        if resp.get("status_code") == status.HTTP_200_OK:
            principals = resp.get("data")
        formatted["principals"] = principals
        _v2_description_override(formatted, self.context.get("request"))
        return formatted

    def get_roleCount(self, obj):
        """Role count for the serializer."""
        return obj.role_count()

    def get_roles(self, obj):
        """Role constructor for the serializer."""
        serialized_roles = [RoleMinimumSerializer(role).data for role in obj.roles_with_access()]
        return serialized_roles


class GroupPrincipalInputSerializer(serializers.Serializer):
    """Serializer for adding principals to a group."""

    principals = PrincipalInputSerializer(many=True)

    class Meta:
        """Metadata for the serializer."""

        fields = ("principals",)


class GroupRoleSerializerOut(serializers.Serializer):
    """Serializer for getting roles for a group."""

    def to_representation(self, obj):
        """Return the collection to be serialized."""
        return obj


class GroupRoleSerializerIn(serializers.Serializer):
    """Serializer for managing roles for a group."""

    roles = serializers.ListField(child=serializers.UUIDField())

    def to_representation(self, obj):
        """Convert representation to dictionary object."""
        serialized_roles = [RoleMinimumSerializer(role).data for role in obj.roles_with_access()]
        return {"data": serialized_roles}
