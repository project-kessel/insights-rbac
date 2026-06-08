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
"""Serializers for RoleV2 API."""

import uuid

from django.utils.translation import gettext as _
from management.dotted_query_param_mixin import DottedQueryParamSerializerMixin
from management.exceptions import RequiredFieldError
from management.role.v2_exceptions import (
    InvalidRolePermissionsError,
    PermissionsNotFoundError,
    RoleAlreadyExistsError,
    RoleDatabaseError,
)
from management.role.v2_model import RoleV2
from management.role.v2_service import RoleV2Service
from management.utils import FieldSelection, FieldSelectionValidationError, UUIDStringField, normalize_blank_or_none
from rest_framework import serializers


class PermissionSerializer(serializers.Serializer):
    """Serializer for permission data."""

    application = serializers.CharField(required=True, help_text="Application name")
    resource_type = serializers.CharField(required=True, help_text="Resource type")
    operation = serializers.CharField(required=True, source="verb", help_text="Operation/verb")


class RoleV2ResponseSerializer(serializers.ModelSerializer):
    """Serializer for RoleV2 API responses."""

    id = serializers.UUIDField(source="uuid", read_only=True)
    name = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    permissions_count = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()
    last_modified = serializers.DateTimeField(source="modified", read_only=True)
    org_id = serializers.SerializerMethodField()

    class Meta:
        model = RoleV2
        fields = ("id", "name", "description", "permissions_count", "permissions", "last_modified", "org_id")

    def __init__(self, *args, **kwargs):
        """Initialize with dynamic field selection from context."""
        super().__init__(*args, **kwargs)

        allowed = self.context.get("fields")
        if allowed is not None:
            for field_name in set(self.fields) - allowed:
                self.fields.pop(field_name)

    def _get_permissions_iterable(self, obj):
        """Return iterable of permissions using resolved/prefetched/query fallback."""
        if hasattr(obj, "_resolved_permissions"):
            return list(obj._resolved_permissions)

        cache = getattr(obj, "_prefetched_objects_cache", None)
        if cache and "permissions" in cache:
            return list(cache["permissions"])

        return list(obj.permissions.all())

    def get_permissions(self, obj):
        """Return permissions, ordered by input order if available, otherwise alphabetically."""
        permissions = self._get_permissions_iterable(obj)

        input_permissions = self.context.get("input_permissions")
        if input_permissions:
            order_map = {
                f"{p.get('application')}:{p.get('resource_type')}:{p.get('operation')}": i
                for i, p in enumerate(input_permissions)
            }
            permissions.sort(key=lambda p: order_map.get(p.permission, float("inf")))
        else:
            permissions.sort(key=lambda p: p.permission)

        return PermissionSerializer(permissions, many=True).data

    def get_permissions_count(self, obj):
        """Return permissions count, using annotation if available."""
        count = getattr(obj, "permissions_count_annotation", None)
        if count is not None:
            return count

        return len(self._get_permissions_iterable(obj))

    def get_org_id(self, obj):
        """Return org_id from the role."""
        return obj.org_id


class RoleFieldSelection(FieldSelection):
    """Field selection for roles endpoint."""

    VALID_ROOT_FIELDS = set(RoleV2ResponseSerializer.Meta.fields)


def validate_fields_parameter(value: str, default_fields: set, strict: bool = False) -> set:
    """
    Validate and parse the fields parameter for role endpoints.

    Args:
        value: The raw fields parameter value from request
        default_fields: The default fields to return when value is empty
        strict: If True, raise ValidationError for invalid fields (write operations per AIP-161).
                If False, silently filter invalid fields (read operations per AIP-161).

    Returns:
        Set of field names to include in response

    Raises:
        ValidationError: If fields parameter has invalid syntax or (when strict=True) invalid field names
    """
    if not value:
        return default_fields

    try:
        field_selection = RoleFieldSelection.parse(value)
    except FieldSelectionValidationError as e:
        raise serializers.ValidationError(e.message)

    if not field_selection:
        return default_fields

    valid_fields = set(RoleV2ResponseSerializer.Meta.fields)
    requested = field_selection.root_fields

    if strict:
        invalid = requested - valid_fields
        if invalid:
            raise serializers.ValidationError(
                f"Invalid field(s): {', '.join(sorted(invalid))}. "
                f"Valid fields are: {', '.join(sorted(valid_fields))}"
            )

    resolved = requested & valid_fields
    return resolved or default_fields


class RoleV2ListSerializer(DottedQueryParamSerializerMixin, serializers.Serializer):
    """Input serializer for RoleV2 list query parameters."""

    DOTTED_PARAM_MAP = {
        "resource.tenant.org_id": "resource_tenant_org_id",
    }

    name = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text=(
            "Filter by role name. Case-insensitive substring match by default;" " use * for glob patterns (e.g. foo*)."
        ),
    )
    permission = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Filter by permission string (e.g. 'app:resource:verb'). Comma-separated for multiple.",
    )
    resource_type = serializers.CharField(
        required=False, allow_blank=True, help_text="Filter roles by the resource type they are scoped to"
    )
    resource_id = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=256,
        help_text="Resource ID (requires resource_type unless resource.tenant.org_id is provided)",
    )
    resource_tenant_org_id = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Org ID of the tenant resource to filter by",
    )
    fields = serializers.CharField(required=False, default="", allow_blank=True, help_text="Control included fields")

    def validate_name(self, value: str | None) -> str | None:
        """Return None for empty values."""
        return value or None

    def validate_permission(self, value: str | None) -> str | None:
        """Return None for empty values."""
        return value or None

    def validate_fields(self, value):
        """Parse, validate, and resolve fields parameter into a set of field names."""
        return validate_fields_parameter(value, RoleV2Service.DEFAULT_LIST_FIELDS)

    validate_resource_id = staticmethod(normalize_blank_or_none)
    validate_resource_type = staticmethod(normalize_blank_or_none)
    validate_resource_tenant_org_id = staticmethod(normalize_blank_or_none)

    def validate(self, data):
        """Cross-field validation for resource filters (aligned with role bindings)."""
        resource_tenant_org_id = data.get("resource_tenant_org_id")
        resource_id = data.get("resource_id")
        resource_type = data.get("resource_type")

        if resource_tenant_org_id:
            if resource_id:
                raise serializers.ValidationError(
                    {"resource_tenant_org_id": "resource.tenant.org_id cannot be combined with resource_id."}
                )
            if resource_type and resource_type != "tenant":
                raise serializers.ValidationError(
                    {"resource_type": "resource_type must be 'tenant' when resource.tenant.org_id is provided."}
                )
        elif resource_id and not resource_type:
            raise serializers.ValidationError(
                {"resource_id": "resource_type is required when resource_id is provided."}
            )

        if resource_type == "workspace" and resource_id:
            try:
                data["resource_id"] = str(uuid.UUID(str(resource_id).strip()))
            except ValueError as e:
                raise serializers.ValidationError({"resource_id": _("Enter a valid UUID.")}) from e

        return data


class RoleIdSerializer(serializers.Serializer):
    """Serializer for a role ID reference.

    Reusable nested serializer for any API that accepts role references by UUID.
    """

    id = serializers.UUIDField(required=True, help_text="Role identifier")


class RoleV2RequestSerializer(serializers.ModelSerializer):
    """Serializer for RoleV2 create/update requests."""

    service_class = RoleV2Service

    id = serializers.UUIDField(source="uuid", read_only=True)
    name = serializers.CharField()
    description = serializers.CharField(required=False, allow_blank=True, default="")
    permissions = PermissionSerializer(many=True, write_only=True)

    class Meta:

        model = RoleV2
        fields = ("id", "name", "description", "permissions")

    def validate_name(self, value):
        """Reject names containing '*' or matching system/seeded role names (case-insensitive)."""
        if isinstance(value, str) and "*" in value:
            raise serializers.ValidationError("Role name must not contain asterisks (*).")

        # Skip check if name hasn't changed on update
        if self.instance and self.instance.name == value:
            return value

        from api.models import Tenant

        public_tenant = Tenant.objects.get(tenant_name="public")
        if RoleV2.objects.filter(
            tenant=public_tenant,
            type__in=[RoleV2.Types.SEEDED, RoleV2.Types.PLATFORM],
            name__iexact=value,
        ).exists():
            raise serializers.ValidationError(
                _("Role name '%(name)s' conflicts with an existing system role.") % {"name": value}
            )

        return value

    @property
    def service(self):
        """Return the service instance from context or create a new one."""
        if "role_service" in self.context:
            return self.context["role_service"]
        # Create service with tenant from request context
        tenant = self.context["request"].tenant
        return self.service_class(tenant=tenant)

    def create(self, validated_data):
        """Create a new RoleV2 using the service layer."""
        tenant = self.context["request"].tenant
        permission_data = validated_data.pop("permissions", [])

        try:
            return self.service.create(
                name=validated_data.get("name"),
                description=validated_data.get("description"),
                permission_data=permission_data,
                tenant=tenant,
            )
        except RequiredFieldError as e:
            raise serializers.ValidationError({e.field_name: str(e)})
        except (InvalidRolePermissionsError, PermissionsNotFoundError) as e:
            raise serializers.ValidationError({"permissions": str(e)})
        except RoleAlreadyExistsError as e:
            raise serializers.ValidationError({"name": str(e)})
        except RoleDatabaseError as e:
            raise serializers.ValidationError({"detail": str(e)})

    def update(self, instance, validated_data):
        """Update an existing RoleV2 using the service layer."""
        tenant = self.context["request"].tenant
        permission_data = validated_data.pop("permissions", [])

        try:
            return self.service.update(
                role_uuid=str(instance.uuid),
                name=validated_data.get("name"),
                description=validated_data.get("description"),
                permission_data=permission_data,
                tenant=tenant,
            )
        except RequiredFieldError as e:
            raise serializers.ValidationError({e.field_name: str(e)})
        except (InvalidRolePermissionsError, PermissionsNotFoundError) as e:
            raise serializers.ValidationError({"permissions": str(e)})
        except RoleAlreadyExistsError as e:
            raise serializers.ValidationError({"name": str(e)})
        except RoleDatabaseError as e:
            raise serializers.ValidationError({"detail": str(e)})


class RoleV2BulkDeleteRequestSerializer(serializers.Serializer):
    """Serializer for requests to delete multiple roles."""

    ids = serializers.ListField(child=UUIDStringField(), min_length=1)
