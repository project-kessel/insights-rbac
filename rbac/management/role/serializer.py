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

"""Serializer for role management."""

from django.conf import settings
from django.utils.translation import gettext as _
from feature_flags import FEATURE_FLAGS
from internal.utils import get_or_create_ungrouped_workspace, is_resource_a_workspace
from management.models import Group, Workspace
from management.serializer_override_mixin import SerializerCreateOverrideMixin
from management.utils import (
    filter_queryset_by_tenant,
    get_principal,
    is_permission_blocked_for_v1,
    is_valid_uuid,
    validate_and_get_key,
    value_to_list,
)
from rest_framework import serializers

from api.models import Tenant
from .model import Access, BindingMapping, Permission, ResourceDefinition, Role
from ..querysets import ORG_ID_SCOPE, PRINCIPAL_SCOPE, SCOPE_KEY, VALID_SCOPES

ALLOWED_OPERATIONS = ["in", "equal"]
FILTER_FIELDS = {"key", "value", "operation"}


class ResourceDefinitionSerializer(SerializerCreateOverrideMixin, serializers.ModelSerializer):
    """Serializer for the ResourceDefinition model."""

    attributeFilter = serializers.JSONField()

    def validate_attributeFilter(self, value):
        """Validate the given attributeFilter."""
        if value.keys() != FILTER_FIELDS:
            key = "format"
            message = f"attributeFilter fields must be {FILTER_FIELDS}"
            error = {key: [_(message)]}
            raise serializers.ValidationError(error)

        op = value.get("operation")
        if op not in ALLOWED_OPERATIONS:
            key = "format"
            message = f"attributeFilter operation must be one of {ALLOWED_OPERATIONS}"
            error = {key: [_(message)]}
            raise serializers.ValidationError(error)
        else:
            values = value.get("value")
            error = False
            if op == "in" and not isinstance(values, list):
                key = "format"
                message = "attributeFilter operation 'in' expects a List value"
                error = {key: [_(message)]}
            elif op == "equal" and not isinstance(values, (str, type(None))):
                key = "format"
                message = "attributeFilter operation 'equal' expects a String value or None"
                error = {key: [_(message)]}
            if error:
                raise serializers.ValidationError(error)

            # Validate that group.id only contains UUIDs or None
            filter_key = value.get("key")
            if filter_key == "group.id":
                if op == "in":
                    if isinstance(values, list):
                        invalid_values = [v for v in values if v is not None and not is_valid_uuid(v)]
                        if invalid_values:
                            key = "format"
                            message = (
                                f"attributeFilter with key 'group.id' must contain only valid UUIDs or None, "
                                f"invalid values: {invalid_values}"
                            )
                            error = {key: [_(message)]}
                            raise serializers.ValidationError(error)
                elif op == "equal":
                    if values is not None and not is_valid_uuid(values):
                        key = "format"
                        message = f"attributeFilter with key 'group.id' must be a valid UUID or None, got: {values}"
                        error = {key: [_(message)]}
                        raise serializers.ValidationError(error)

        return value

    def to_representation(self, instance):
        """Convert the ResourceDefinition instance to a dictionary."""
        serialized_data = super().to_representation(instance)
        if FEATURE_FLAGS.is_remove_null_value_enabled() and instance.attributeFilter["key"] == "group.id":
            value = instance.attributeFilter["value"]
            if isinstance(value, list) and None in value:
                ungrouped_hosts = get_or_create_ungrouped_workspace(instance.tenant)
                value = [v for v in value if v is not None]
                value.append(str(ungrouped_hosts.id))
            elif value is None:
                ungrouped_hosts = get_or_create_ungrouped_workspace(instance.tenant)
                value = str(ungrouped_hosts.id)
            serialized_data["attributeFilter"]["value"] = value
        if self._should_add_hierarchy(instance):
            serialized_data.get("attributeFilter").update(
                {"operation": "in", "value": self._original_vals_and_descendant_ids(instance)}
            )
        return serialized_data

    class Meta:
        """Metadata for the serializer."""

        model = ResourceDefinition
        fields = ("attributeFilter",)

    def _original_vals_and_descendant_ids(self, instance):
        attr_filter_list = value_to_list(instance.attributeFilter.get("value"))
        uuids = [val for val in attr_filter_list if is_valid_uuid(val)]
        non_uuids = [val for val in attr_filter_list if not is_valid_uuid(val)]
        ids_with_parents = Workspace.objects.descendant_ids_with_parents(uuids, instance.tenant_id)
        return list(set(non_uuids + ids_with_parents))

    def _should_add_hierarchy(self, instance):
        hierarchy_enabled = settings.WORKSPACE_HIERARCHY_ENABLED is True
        is_access_request = self.context.get("for_access") is True
        return hierarchy_enabled and is_access_request and self._is_workspace_filter(instance)

    def _is_workspace_filter(self, instance):
        return is_resource_a_workspace(instance.application, instance.resource_type, instance.attributeFilter)


class AccessListSerializer(serializers.ListSerializer):
    """List serializer that filters out blocked permissions for v1 API."""

    def to_representation(self, data):
        """Filter blocked permissions before serializing the list."""
        request = self.context.get("request")
        if request:
            iterable = data.all() if hasattr(data, "all") else data
            data = [item for item in iterable if not is_permission_blocked_for_v1(item.permission.permission, request)]
        return super().to_representation(data)


class AccessSerializer(SerializerCreateOverrideMixin, serializers.ModelSerializer):
    """Serializer for the Access model."""

    resourceDefinitions = ResourceDefinitionSerializer(many=True)
    permission = serializers.CharField(source="permission.permission")

    def validate_permission(self, value):
        """Validate the permissions input."""
        split_value = value.split(":")
        split_value_len = len(split_value)
        if split_value_len != 3:
            key = "format"
            message = 'Permission must be of the format "application:resource_type:operation".'
            error = {key: [_(message)]}
            raise serializers.ValidationError(error)
        return value

    class Meta:
        """Metadata for the serializer."""

        model = Access
        fields = ("resourceDefinitions", "permission")
        list_serializer_class = AccessListSerializer


class RoleSerializer(serializers.ModelSerializer):
    """Serializer for the Role model."""

    uuid = serializers.UUIDField(read_only=True)
    name = serializers.CharField(required=True, max_length=150)
    display_name = serializers.CharField(required=False, max_length=150, allow_blank=True)
    description = serializers.CharField(allow_null=True, required=False)
    access = AccessSerializer(many=True)
    policyCount = serializers.IntegerField(read_only=True)
    accessCount = serializers.IntegerField(read_only=True)
    applications = serializers.SerializerMethodField()
    system = serializers.BooleanField(read_only=True)
    platform_default = serializers.BooleanField(read_only=True)
    admin_default = serializers.BooleanField(read_only=True)
    created = serializers.DateTimeField(read_only=True)
    modified = serializers.DateTimeField(read_only=True)
    external_role_id = serializers.SerializerMethodField()
    external_tenant = serializers.SerializerMethodField()

    class Meta:
        """Metadata for the serializer."""

        model = Role
        fields = (
            "uuid",
            "name",
            "display_name",
            "description",
            "access",
            "policyCount",
            "accessCount",
            "applications",
            "system",
            "platform_default",
            "admin_default",
            "created",
            "modified",
            "external_role_id",
            "external_tenant",
        )

    def get_applications(self, obj):
        """Get the list of applications in the role."""
        return obtain_applications(obj)

    def create(self, validated_data):
        """Create the role object in the database."""
        name = validated_data.pop("name")
        display_name = validated_data.pop("display_name", name)
        description = validated_data.pop("description", None)
        access_list = validated_data.pop("access")
        tenant = self.context["request"].tenant

        role = Role.objects.create(name=name, description=description, display_name=display_name, tenant=tenant)
        create_access_for_role(role, access_list, tenant)

        return role

    def update(self, instance, validated_data):
        """Update the role object in the database."""
        access_list = validated_data.pop("access")
        tenant = self.context["request"].tenant

        instance = update_role(instance, validated_data)

        create_access_for_role(instance, access_list, tenant)

        return instance

    def get_external_role_id(self, obj):
        """Get the external role id if it's from an external tenant."""
        return obj.external_role_id()

    def get_external_tenant(self, obj):
        """Get the external tenant name if it's from an external tenant."""
        return obj.external_tenant_name()

    def validate(self, data):
        """Validate the input data of role."""
        if self.instance and self.instance.system:
            key = "role.update"
            message = "System roles may not be updated."
            error = {key: [_(message)]}
            raise serializers.ValidationError(error)
        _validate_name_not_system_role(data.get("name"), data.get("display_name"), self.instance)
        return super().validate(data)


class RoleMinimumSerializer(SerializerCreateOverrideMixin, serializers.ModelSerializer):
    """Serializer for the Role model that doesn't return access info."""

    uuid = serializers.UUIDField(read_only=True)
    name = serializers.CharField(required=True, max_length=150)
    display_name = serializers.CharField(required=False, max_length=150, allow_blank=True)
    description = serializers.CharField(allow_null=True, required=False)
    created = serializers.DateTimeField(read_only=True)
    modified = serializers.DateTimeField(read_only=True)
    policyCount = serializers.IntegerField(read_only=True)
    accessCount = serializers.IntegerField(read_only=True)
    applications = serializers.SerializerMethodField()
    system = serializers.BooleanField(read_only=True)
    platform_default = serializers.BooleanField(read_only=True)
    admin_default = serializers.BooleanField(read_only=True)
    external_role_id = serializers.SerializerMethodField()
    external_tenant = serializers.SerializerMethodField()

    class Meta:
        """Metadata for the serializer."""

        model = Role
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
        """Get the list of applications in the role."""
        return obtain_applications(obj)

    def get_external_role_id(self, obj):
        """Get the external role id if it's from an external tenant."""
        return obj.external_role_id()

    def get_external_tenant(self, obj):
        """Get the external tenant name if it's from an external tenant."""
        return obj.external_tenant_name()


class DynamicFieldsModelSerializer(SerializerCreateOverrideMixin, serializers.ModelSerializer):
    """A ModelSerializer that controls which fields should be displayed."""

    def __init__(self, *args, **kwargs):
        """Instantiate the serializer."""
        fields = kwargs.pop("fields", None)

        # Instantiate the superclass normally
        super(DynamicFieldsModelSerializer, self).__init__(*args, **kwargs)

        if fields is not None:
            # Drop any fields that are not specified in the `fields` argument.
            allowed = set(fields)
            existing = set(self.fields)
            for field_name in existing - allowed:
                self.fields.pop(field_name)


class RoleDynamicSerializer(DynamicFieldsModelSerializer):
    """Serializer for the Role model that could dynamically return required field."""

    uuid = serializers.UUIDField(read_only=True)
    name = serializers.CharField(required=True, max_length=150)
    display_name = serializers.CharField(required=False, max_length=150, allow_blank=True)
    description = serializers.CharField(allow_null=True, required=False)
    created = serializers.DateTimeField(read_only=True)
    modified = serializers.DateTimeField(read_only=True)
    policyCount = serializers.IntegerField(read_only=True)
    groups_in = serializers.SerializerMethodField()
    groups_in_count = serializers.SerializerMethodField()
    accessCount = serializers.IntegerField(read_only=True)
    access = AccessSerializer(many=True)
    applications = serializers.SerializerMethodField()
    system = serializers.BooleanField(read_only=True)
    platform_default = serializers.BooleanField(read_only=True)
    admin_default = serializers.BooleanField(read_only=True)
    external_role_id = serializers.SerializerMethodField()
    external_tenant = serializers.SerializerMethodField()

    class Meta:
        """Metadata for the serializer."""

        model = Role
        fields = (
            "uuid",
            "name",
            "display_name",
            "description",
            "created",
            "modified",
            "policyCount",
            "groups_in",
            "groups_in_count",
            "accessCount",
            "access",
            "applications",
            "system",
            "platform_default",
            "admin_default",
            "external_role_id",
            "external_tenant",
        )

    def get_applications(self, obj):
        """Get the list of applications in the role."""
        return obtain_applications(obj)

    def get_groups_in_count(self, obj):
        """Get the total count of groups where the role is in."""
        request = self.context.get("request")
        return obtain_groups_in(obj, request).count()

    def get_groups_in(self, obj):
        """Get the groups where the role is in."""
        request = self.context.get("request")
        return obtain_groups_in(obj, request).values("name", "uuid", "description")

    def get_external_role_id(self, obj):
        """Get the external role id if it's from an external tenant."""
        return obj.external_role_id()

    def get_external_tenant(self, obj):
        """Get the external tenant name if it's from an external tenant."""
        return obj.external_tenant_name()


class RolePatchSerializer(RoleSerializer):
    """Serializer for Role patch."""

    access = AccessSerializer(many=True, required=False)
    name = serializers.CharField(required=False, max_length=150)

    def update(self, instance, validated_data):
        """Patch the role object."""
        instance = update_role(instance, validated_data, clear_access=False)
        return instance


class BindingMappingSerializer(serializers.ModelSerializer):
    """Serializer for the binding mapping."""

    class Meta:
        """Metadata for the serializer."""

        model = BindingMapping
        fields = "__all__"


def _validate_name_not_system_role(name, display_name, instance=None):
    """Validate that name/display_name do not conflict with system role names (case-insensitive)."""
    public_tenant = Tenant.objects.get(tenant_name="public")

    if name and (not instance or instance.name != name):
        if Role.objects.filter(system=True, tenant=public_tenant, name__iexact=name).exists():
            raise serializers.ValidationError(
                {"name": [_("Role name '%(name)s' conflicts with an existing system role.") % {"name": name}]}
            )

    if display_name and (not instance or instance.display_name != display_name):
        if Role.objects.filter(system=True, tenant=public_tenant, display_name__iexact=display_name).exists():
            raise serializers.ValidationError(
                {
                    "display_name": [
                        _("Display name '%(name)s' conflicts with an existing system role.") % {"name": display_name}
                    ]
                }
            )


def obtain_applications(obj):
    """Shared function to get the list of applications in the role."""
    apps = []
    for access_item in obj.access.all():
        apps.append(access_item.permission.application)
    return list(set(apps))


def obtain_groups_in(obj, request):
    """Shared function to get the groups the roles is in."""
    scope_param = validate_and_get_key(request.query_params, SCOPE_KEY, VALID_SCOPES, ORG_ID_SCOPE)
    username_param = request.query_params.get("username")
    policy_ids = list(obj.policies.values_list("id", flat=True))

    if scope_param == PRINCIPAL_SCOPE or username_param:
        principal = get_principal(username_param or request.user.username, request)
        assigned_groups = Group.objects.filter(policies__in=policy_ids, principals__in=[principal])
        assigned_groups = filter_queryset_by_tenant(assigned_groups, request.tenant)
    else:
        assigned_groups = filter_queryset_by_tenant(Group.objects.filter(policies__in=policy_ids), request.tenant)

    public_tenant = Tenant.objects.get(tenant_name="public")

    # Fix: Check if custom default group exists for tenant first
    tenant_has_custom_default = Group.platform_default_set().filter(tenant=request.tenant).exists()

    if tenant_has_custom_default:
        # Use tenant's custom default group only
        platform_default_groups = (
            Group.platform_default_set().filter(tenant=request.tenant).filter(policies__in=policy_ids)
        )
    else:
        # Fall back to public tenant's default group
        platform_default_groups = (
            Group.platform_default_set().filter(tenant=public_tenant).filter(policies__in=policy_ids)
        )

    if username_param and scope_param != PRINCIPAL_SCOPE:
        is_org_admin = request.user_from_query.admin
    else:
        is_org_admin = request.user.admin

    qs = assigned_groups | platform_default_groups

    if is_org_admin:
        # Apply same fix for admin default groups
        tenant_has_custom_admin_default = Group.admin_default_set().filter(tenant=request.tenant).exists()

        if tenant_has_custom_admin_default:
            admin_default_groups = (
                Group.admin_default_set().filter(tenant=request.tenant).filter(policies__in=policy_ids)
            )
        else:
            admin_default_groups = (
                Group.admin_default_set().filter(tenant=public_tenant).filter(policies__in=policy_ids)
            )

        qs = qs | admin_default_groups

    return qs.distinct()


def create_access_for_role(role, access_list, tenant):
    """Create access objects and relate it to role."""
    for access_item in access_list:
        resource_def_list = access_item.get("resourceDefinitions")
        access_permission = access_item.get("permission")
        permission = Permission.objects.get(**access_permission)

        access_obj = Access.objects.create(permission=permission, role=role, tenant=tenant)
        for resource_def_item in resource_def_list:
            ResourceDefinition.objects.create(**resource_def_item, access=access_obj, tenant=tenant)


def update_role(instance, validated_data, clear_access=True):
    """Update role attribute."""
    update_fields = []

    for field_name in ["name", "display_name", "description"]:
        if field_name not in validated_data:
            continue
        setattr(instance, field_name, validated_data[field_name])
        update_fields.append(field_name)

    instance.save(update_fields=update_fields)

    if clear_access:
        instance.access.all().delete()

    return instance
