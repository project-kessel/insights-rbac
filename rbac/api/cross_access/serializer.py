#
# Copyright 2020 Red Hat, Inc.
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

"""Serializer for CrossAccountRequest."""

from django.db import transaction
from management.models import Role
from management.notifications.notification_handlers import cross_account_access_handler
from management.permission.serializer import PermissionSerializer
from management.utils import raise_validation_error
from rest_framework import serializers

from api.models import CrossAccountRequest, Tenant


class CrossAccountRequestSerializer(serializers.ModelSerializer):
    """Serializer for the cross access request model."""

    request_id = serializers.UUIDField(read_only=True)
    target_org = serializers.CharField(max_length=36)
    user_id = serializers.CharField(max_length=15)
    start_date = serializers.DateTimeField(format="%d %b %Y")
    end_date = serializers.DateTimeField(format="%d %b %Y")
    created = serializers.DateTimeField(format="%d %b %Y, %H:%M UTC")
    status = serializers.CharField(max_length=10)

    class Meta:
        """Metadata for the serializer."""

        model = CrossAccountRequest
        fields = (
            "request_id",
            "target_org",
            "user_id",
            "start_date",
            "end_date",
            "created",
            "status",
        )


class RoleSerializer(serializers.ModelSerializer):
    """Serializer for the roles of cross access request model."""

    uuid = serializers.UUIDField(read_only=True)
    display_name = serializers.CharField(max_length=150)
    description = serializers.CharField(max_length=150, read_only=True)
    permissions = serializers.SerializerMethodField(read_only=True)

    class Meta:
        """Metadata for the serializer."""

        model = Role
        fields = ("uuid", "display_name", "description", "permissions")

    def get_permissions(self, obj):
        """Permissions constructor for the serializer."""
        serialized_permissions = [PermissionSerializer(access.permission).data for access in obj.access.all()]
        return serialized_permissions


class CrossAccountRequestDetailSerializer(serializers.ModelSerializer):
    """Serializer for the cross access request model with details."""

    request_id = serializers.UUIDField(read_only=True)
    target_org = serializers.CharField(max_length=36)
    user_id = serializers.CharField(max_length=15)
    start_date = serializers.DateTimeField(format="%m/%d/%Y", input_formats=["%m/%d/%Y"])
    end_date = serializers.DateTimeField(format="%m/%d/%Y", input_formats=["%m/%d/%Y"])
    created = serializers.DateTimeField(format="%m/%d/%Y", read_only=True)
    status = serializers.CharField(max_length=10, read_only=True)
    roles = RoleSerializer(many=True)

    class Meta:
        """Metadata for the serializer."""

        model = CrossAccountRequest
        fields = (
            "request_id",
            "target_org",
            "user_id",
            "start_date",
            "end_date",
            "created",
            "status",
            "roles",
        )

    def get_roles(self, obj):
        """Roles constructor for the serializer."""
        serialized_roles = [RoleSerializer(role).data for role in obj.roles.all()]
        return serialized_roles

    def validate_roles(self, roles):
        """Format role list as expected for cross-account-request."""
        public_tenant = Tenant.objects.get(tenant_name="public")

        role_display_names = [role["display_name"] for role in roles]
        roles_queryset = Role.objects.filter(tenant=public_tenant, display_name__in=role_display_names)
        role_dict = {role.display_name: role for role in roles_queryset}

        system_role_uuids = []
        for role in roles:
            role_display_name = role["display_name"]

            if role_display_name not in role_dict:
                raise raise_validation_error("cross-account-request", f"Role '{role_display_name}' does not exist.")

            role = role_dict[role_display_name]
            if not role.system:
                raise_validation_error(
                    "cross-account-request", "Only system roles may be assigned to a cross-account-request."
                )

            system_role_uuids.append(role.uuid)

        return system_role_uuids

    def to_internal_value(self, data):
        """Convert the incoming 'roles' data into the expected format."""
        if "roles" in data:
            data["roles"] = [{"display_name": role_name} for role_name in data["roles"]]
        return super().to_internal_value(data)

    def create(self, validated_data):
        """Override the create method to associate the roles to cross account request after it is created."""
        role_uuids = validated_data.pop("roles")
        request = CrossAccountRequest.objects.create(**validated_data)
        cross_account_access_handler(request, self.context["user"])
        request.roles.add(*role_uuids)
        return request

    @transaction.atomic
    def update(self, instance, validated_data):
        """Override the update method to associate the roles to cross account request after it is updated."""
        if "roles" in validated_data:
            role_uuids = validated_data.pop("roles")
            instance.roles.clear()
            instance.roles.add(*role_uuids)

        for field in validated_data:
            setattr(instance, field, validated_data.get(field))

        instance.save()
        return instance
