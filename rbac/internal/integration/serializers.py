#
# Copyright 2022 Red Hat, Inc.
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

"""Serializers for OCM/integrations API."""

from management.role.v2_model import RoleV2
from rest_framework import serializers

from api.models import Tenant


class TenantSerializer(serializers.ModelSerializer):
    """Tenant serializer."""

    org_id = serializers.IntegerField(read_only=True)
    account_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = Tenant
        fields = ("id", "org_id", "account_id")


class IntegrationRoleV2Serializer(serializers.ModelSerializer):
    """Represent V2 roles using the existing OCM integration response contract."""

    display_name = serializers.CharField(read_only=True)
    policyCount = serializers.IntegerField(read_only=True)
    accessCount = serializers.IntegerField(source="permissions.count", read_only=True)
    applications = serializers.SerializerMethodField()
    system = serializers.BooleanField(read_only=True)
    platform_default = serializers.BooleanField(read_only=True)
    admin_default = serializers.BooleanField(read_only=True)
    external_role_id = serializers.CharField(source="v1_source.ext_relation.ext_id", default=None, read_only=True)
    external_tenant = serializers.CharField(
        source="v1_source.ext_relation.ext_tenant.name", default=None, read_only=True
    )

    class Meta:
        model = RoleV2
        fields = (
            "uuid",
            "name",
            "display_name",
            "description",
            "created",
            "modified",
            "policyCount",
            "accessCount",
            "applications",
            "system",
            "platform_default",
            "admin_default",
            "external_role_id",
            "external_tenant",
        )

    def get_applications(self, obj):
        """Use prefetched permissions to avoid per-role queries."""
        return sorted({permission.application for permission in obj.permissions.all()})
