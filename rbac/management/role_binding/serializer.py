#
# Copyright 2025 Red Hat, Inc.
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
"""Serializers for role binding management.

This module contains:
- Input serializers: For validating query parameters
- Output serializers: For serializing response data
"""

import uuid
from typing import Optional

from management.models import Group
from management.principal.model import Principal
from management.role.v2_model import RoleV2
from management.role.v2_serializer import RoleIdSerializer
from management.role_binding.model import RoleBinding
from management.role_binding.service import CreateBindingRequest, ExcludeSources, RoleBindingService
from management.subject import SubjectType
from management.utils import FieldSelection, FieldSelectionValidationError
from rest_framework import serializers

from api.models import Tenant

_SUBJECT_TYPE_GROUP = "group"
_SUBJECT_TYPE_USER = "user"
_GROUP_FIELD_PREFIX = "group."


class RoleBindingFieldSelection(FieldSelection):
    """Field selection for list/batch-create endpoints (singular ``role``)."""

    VALID_ROOT_FIELDS = {"last_modified"}
    VALID_NESTED_FIELDS = {
        "subject": {"id", "type", "group.name", "group.description", "group.user_count", "user.username"},
        "role": {"id", "name"},
        "resource": {"id", "name", "type"},
        "sources": {"id", "name", "type"},
    }


class RoleBindingBySubjectFieldSelection(FieldSelection):
    """Field selection for by-subject endpoints (plural ``roles``)."""

    VALID_ROOT_FIELDS = {"last_modified"}
    VALID_NESTED_FIELDS = {
        "subject": {"id", "type", "group.name", "group.description", "group.user_count", "user.username"},
        "roles": {"id", "name"},
        "resource": {"id", "name", "type"},
        "sources": {"id", "name", "type"},
    }


def _normalize_blank_or_none(value: str | None) -> str | None:
    """Return None for empty/blank values, pass through otherwise."""
    return (value and value.strip()) or None


def _normalize_uuid_or_none(value: str | None) -> uuid.UUID | None:
    """Return None for empty/blank values, validate and return UUID otherwise.

    Raises serializers.ValidationError for non-empty, non-UUID strings.
    """
    if not value or not value.strip():
        return None
    try:
        return uuid.UUID(value.strip())
    except ValueError as e:
        raise serializers.ValidationError("Enter a valid UUID.") from e


def _validate_resource_identifiers(attrs: dict) -> None:
    """Validate resource.tenant.org_id vs resource_id/resource_type mutual exclusivity.

    Raises serializers.ValidationError on invalid combinations.
    Used by both by-subject serializers (GET and PUT).
    """
    resource_tenant_org_id = attrs.get("resource_tenant_org_id")
    resource_id = attrs.get("resource_id")
    resource_type = attrs.get("resource_type")

    if resource_tenant_org_id:
        if resource_id:
            raise serializers.ValidationError("resource.tenant.org_id cannot be combined with resource_id.")
        if resource_type and resource_type != "tenant":
            raise serializers.ValidationError(
                "resource_type must be 'tenant' when resource.tenant.org_id is provided."
            )
    else:
        if not resource_id:
            raise serializers.ValidationError(
                {"resource_id": "resource_id is required (or use resource.tenant.org_id)."}
            )
        if not resource_type:
            raise serializers.ValidationError(
                {"resource_type": "resource_type is required (or use resource.tenant.org_id)."}
            )


def resolve_resource_identifiers(params: dict) -> tuple[str, str]:
    """Convert resource params (possibly resource_tenant_org_id) to (resource_type, resource_id).

    Used by serializer save() and view methods that need to resolve
    the resource.tenant.org_id shorthand into concrete resource_type/resource_id.
    """
    resource_tenant_org_id = params.get("resource_tenant_org_id")
    if resource_tenant_org_id:
        resource_id = Tenant.org_id_to_tenant_resource_id(resource_tenant_org_id)
        resource_type = params.get("resource_type") or "tenant"
    else:
        resource_id = params["resource_id"]
        resource_type = params["resource_type"]
    return resource_type, resource_id


class RoleBindingInputSerializerMixin:
    """Shared validation methods for role binding input serializers."""

    DOTTED_PARAM_MAP: dict[str, str] = {}

    def to_internal_value(self, data):
        """Remap dotted query param keys and sanitize NUL bytes."""
        remapped = {key: data[key] for key in data}
        for dotted, underscored in self.DOTTED_PARAM_MAP.items():
            if dotted in remapped:
                remapped[underscored] = remapped.pop(dotted)
        sanitized = {
            key: value.replace("\x00", "") if isinstance(value, str) else value for key, value in remapped.items()
        }
        return super().to_internal_value(sanitized)

    def validate_fields(self, value):
        """Parse and validate fields parameter into RoleBindingFieldSelection object."""
        if not value:
            return None
        try:
            return RoleBindingFieldSelection.parse(value)
        except FieldSelectionValidationError as e:
            raise serializers.ValidationError(e.message)

    def validate_order_by(self, value):
        """Return None for empty values."""
        return value or None


class RoleBindingListInputSerializer(RoleBindingInputSerializerMixin, serializers.Serializer):
    """Input serializer for role binding list endpoint query parameters.

    GET /role-bindings/
    """

    DOTTED_PARAM_MAP = {
        "resource.tenant.org_id": "resource_tenant_org_id",
        "granted_subject.principal.user_id": "granted_subject_principal_user_id",
    }

    role_id = serializers.CharField(required=False, allow_blank=True, help_text="Filter by role ID")
    resource_id = serializers.CharField(
        required=False, allow_blank=True, max_length=256, help_text="Filter by resource ID"
    )
    resource_type = serializers.CharField(required=False, allow_blank=True, help_text="Filter by resource type")
    resource_tenant_org_id = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Org ID of the tenant resource to filter by",
    )
    subject_type = serializers.CharField(required=False, allow_blank=True, help_text="Filter by subject type")
    subject_id = serializers.CharField(required=False, allow_blank=True, help_text="Filter by subject ID")
    granted_subject_type = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Filter by the type of subject effectively granted access ('user', 'group', or 'principal')",
    )
    granted_subject_id = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text=("ID effectively granted access: for 'user', principal UUID or user_id; for 'group', group UUID"),
    )
    granted_subject_principal_user_id = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="External user ID of the principal effectively granted access",
    )
    fields = serializers.CharField(required=False, allow_blank=True, help_text="Control which fields are included")
    order_by = serializers.CharField(required=False, allow_blank=True, help_text="Sort by specified field(s)")
    exclude_sources = serializers.ChoiceField(
        choices=ExcludeSources.values,
        required=False,
        allow_blank=True,
        default=ExcludeSources.NONE,
        help_text="Exclude bindings: 'none' (default) shows all, 'indirect' hides inherited, 'direct' hides direct. "
        "Requires both resource_id and resource_type to be specified for inherited binding lookups.",
    )

    def validate_role_id(self, value: str | None) -> uuid.UUID | None:
        """Return None for empty values, validate UUID format otherwise."""
        return _normalize_uuid_or_none(value)

    def validate_subject_id(self, value: str | None) -> uuid.UUID | None:
        """Return None for empty values, validate UUID format otherwise."""
        return _normalize_uuid_or_none(value)

    validate_resource_id = staticmethod(_normalize_blank_or_none)
    validate_resource_type = staticmethod(_normalize_blank_or_none)
    validate_resource_tenant_org_id = staticmethod(_normalize_blank_or_none)
    validate_subject_type = staticmethod(_normalize_blank_or_none)
    validate_granted_subject_type = staticmethod(_normalize_blank_or_none)
    validate_granted_subject_id = staticmethod(_normalize_blank_or_none)
    validate_granted_subject_principal_user_id = staticmethod(_normalize_blank_or_none)

    def validate_exclude_sources(self, value):
        """Map empty string to the default (none = show all)."""
        return ExcludeSources.NONE if not value else value

    def validate(self, attrs):
        """Cross-field validation for exclude_sources, granted_subject, resource, and subject params."""
        attrs = super().validate(attrs)

        # Validate exclude_sources with resource_id/resource_type
        exclude_sources = attrs.get("exclude_sources", ExcludeSources.NONE)
        resource_id = attrs.get("resource_id")
        resource_type = attrs.get("resource_type")

        # resource.tenant.org_id validations
        resource_tenant_org_id = attrs.get("resource_tenant_org_id")
        if resource_tenant_org_id:
            if resource_id:
                raise serializers.ValidationError("resource.tenant.org_id cannot be combined with resource_id.")
            if resource_type and resource_type != "tenant":
                raise serializers.ValidationError(
                    "resource_type must be 'tenant' when resource.tenant.org_id is provided."
                )

        # Inherited binding lookups require both resource_id and resource_type.
        # This applies when exclude_sources is 'none' (include all) or 'direct' (inherited only).
        # When exclude_sources is 'indirect', only direct bindings are returned, so no lookup needed.
        # Skip this check when resource_tenant_org_id is provided, as it will be converted to resource_id in the view.
        needs_inherited_lookup = exclude_sources in (ExcludeSources.NONE, ExcludeSources.DIRECT)
        if needs_inherited_lookup and not resource_tenant_org_id:
            if resource_id and not resource_type:
                raise serializers.ValidationError(
                    {
                        "resource_type": "resource_type is required when resource_id is specified "
                        "and exclude_sources is not 'indirect'."
                    }
                )
            if resource_type and not resource_id:
                raise serializers.ValidationError(
                    {
                        "resource_id": "resource_id is required when resource_type is specified "
                        "and exclude_sources is not 'indirect'."
                    }
                )

        # Validate granted_subject params
        granted_type = attrs.get("granted_subject_type")
        granted_id = attrs.get("granted_subject_id")
        granted_principal_user_id = attrs.get("granted_subject_principal_user_id")

        # granted_subject.principal.user_id requires granted_subject_type=principal
        if granted_principal_user_id and granted_type != SubjectType.PRINCIPAL:
            raise serializers.ValidationError(
                "granted_subject_type must be 'principal' when granted_subject.principal.user_id is provided."
            )

        # granted_subject_id without granted_subject_type is invalid
        if granted_id and not granted_type:
            raise serializers.ValidationError(
                "Both granted_subject_type and granted_subject_id must be provided together."
            )

        if granted_type:
            if not SubjectType.is_valid(granted_type):
                raise serializers.ValidationError(
                    f"granted_subject_type must be one of: {', '.join(SubjectType.values())}."
                )
            if attrs.get("subject_type") or attrs.get("subject_id"):
                raise serializers.ValidationError(
                    "granted_subject_type/granted_subject_id cannot be combined with subject_type/subject_id."
                )
            if granted_type in (SubjectType.USER, SubjectType.GROUP) and not granted_id:
                raise serializers.ValidationError(
                    "granted_subject_id is required when granted_subject_type is 'user' or 'group'."
                )
            if granted_type == SubjectType.PRINCIPAL and not granted_principal_user_id:
                raise serializers.ValidationError(
                    "granted_subject.principal.user_id is required when granted_subject_type is 'principal'."
                )

        return attrs


class RoleBindingInputSerializer(RoleBindingInputSerializerMixin, serializers.Serializer):
    """Input serializer for role binding by-subject query parameters.

    Handles validation of query parameters for GET /role-bindings/by-subject/.
    Supports resource.tenant.org_id as an alternative to resource_id + resource_type.
    """

    DOTTED_PARAM_MAP = {
        "resource.tenant.org_id": "resource_tenant_org_id",
    }

    resource_id = serializers.CharField(required=False, allow_blank=True, help_text="Filter by resource ID")
    resource_type = serializers.CharField(required=False, allow_blank=True, help_text="Filter by resource type")
    resource_tenant_org_id = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Org ID of the tenant resource to filter by",
    )
    subject_type = serializers.CharField(required=False, allow_blank=True, help_text="Filter by subject type")
    subject_id = serializers.CharField(required=False, allow_blank=True, help_text="Filter by subject ID (UUID)")
    fields = serializers.CharField(required=False, allow_blank=True, help_text="Control which fields are included")
    order_by = serializers.CharField(required=False, allow_blank=True, help_text="Sort by specified field(s)")
    exclude_sources = serializers.ChoiceField(
        choices=ExcludeSources.values,
        required=False,
        default=ExcludeSources.NONE,
        help_text="Exclude bindings: 'none' (default) shows all, 'indirect' hides inherited, 'direct' hides direct",
    )

    validate_resource_id = staticmethod(_normalize_blank_or_none)
    validate_resource_type = staticmethod(_normalize_blank_or_none)
    validate_resource_tenant_org_id = staticmethod(_normalize_blank_or_none)
    validate_subject_type = staticmethod(_normalize_blank_or_none)
    validate_subject_id = staticmethod(_normalize_blank_or_none)

    def validate(self, attrs):
        """Cross-field validation for resource params."""
        attrs = super().validate(attrs)
        _validate_resource_identifiers(attrs)
        return attrs

    def validate_fields(self, value):
        """Parse and validate fields parameter using by-subject selection."""
        if not value:
            return None
        try:
            return RoleBindingBySubjectFieldSelection.parse(value)
        except FieldSelectionValidationError as e:
            raise serializers.ValidationError(e.message)


class RoleBindingOutputSerializer(serializers.Serializer):
    """Serializer for role bindings by subject (group or user).

    This serializer formats Group or Principal objects that have been annotated with
    role binding information via the service layer.

    Supports dynamic field selection through the 'field_selection' context parameter.
    Fields are accessed directly on the model using dot notation from the query parameter.

    Field selection syntax:
    - subject(group.name, group.description) - accesses group fields when type="group"
    - subject(user.username) - accesses user fields when type="user"
    - role(name, description) - accesses role.name, role.description
    - resource(name, type) - accesses resource name and type from context
    - last_modified - include root-level field
    - sources(name, type) - accesses sources with optional name and type
    """

    last_modified = serializers.SerializerMethodField()
    subject = serializers.SerializerMethodField()
    roles = serializers.SerializerMethodField()
    resource = serializers.SerializerMethodField()
    sources = serializers.SerializerMethodField()

    def _get_field_selection(self):
        """Get field selection from context."""
        return self.context.get("field_selection")

    def to_representation(self, instance):
        """Override to support field selection.

        Default (no fields param): Returns only basic required fields.
        With fields param: Only explicitly requested fields are included,
        except subject.type which is always included.
        """
        ret = super().to_representation(instance)

        field_selection = self._get_field_selection()

        # Base response always includes core objects
        filtered = {
            "subject": ret.get("subject"),
            "roles": ret.get("roles"),
            "resource": ret.get("resource"),
        }

        # Include sources only if no field selection, or if explicitly requested
        if field_selection is None or "sources" in field_selection.nested_fields:
            filtered["sources"] = ret.get("sources")

        # Include last_modified only if explicitly requested
        if field_selection and "last_modified" in field_selection.root_fields:
            filtered["last_modified"] = ret.get("last_modified")

        return filtered

    def get_last_modified(self, obj):
        """Extract last modified timestamp."""
        if isinstance(obj, dict):
            return obj.get("modified") or obj.get("latest_modified")
        return getattr(obj, "latest_modified", None)

    # Field name mapping for special cases (e.g., API field name -> model attribute)
    SUBJECT_FIELD_MAPPING = {
        "group": {"user_count": "principalCount"},
        "user": {},
    }

    def get_subject(self, obj):
        """Extract subject information from the Group or Principal.

        Default (no fields param): Returns only id and type.
        With fields param: Only type is always included. Other fields
        (including id) are only included if explicitly requested.
        """
        if isinstance(obj, Principal):
            return self._build_subject(obj, "user")
        elif isinstance(obj, Group):
            return self._build_subject(obj, "group")
        return None

    def _build_subject(self, obj, subject_type: str):
        """Build subject dict for a Group or Principal.

        Args:
            obj: Group or Principal object
            subject_type: The subject type string ("group" or "user")

        Returns:
            Subject dict with type and requested fields
        """
        field_selection = self._get_field_selection()

        # Default behavior: only basic fields
        if field_selection is None:
            return {
                "id": obj.uuid,
                "type": subject_type,
            }

        # With fields param: type is always included
        subject: dict = {"type": subject_type}

        # Check if id is explicitly requested
        subject_fields = field_selection.get_nested("subject")
        if "id" in subject_fields:
            subject["id"] = obj.uuid

        # Extract field names from "{subject_type}.X" paths
        prefix = f"{subject_type}."
        prefix_len = len(prefix)
        fields_to_include = set()
        for field_path in subject_fields:
            if field_path.startswith(prefix):
                fields_to_include.add(field_path[prefix_len:])

        # Dynamically extract requested fields from the object
        if fields_to_include:
            field_mapping = self.SUBJECT_FIELD_MAPPING.get(subject_type, {})
            details = {}
            for field_name in fields_to_include:
                # Map API field name to model attribute if needed
                model_attr = field_mapping.get(field_name, field_name)
                value = getattr(obj, model_attr, None)
                if value is not None:
                    details[field_name] = value

            if details:
                subject[subject_type] = details

        return subject

    def _build_role_data(self, role: RoleV2, field_selection: Optional[FieldSelection]) -> dict:
        """Build role data dictionary from a role object.

        Args:
            role: The role to build data for
            field_selection: Optional field selection to determine which fields to include

        Returns:
            Dictionary with role data (always includes 'id')
        """
        role_data = {"id": role.uuid}

        if field_selection is not None:
            # By-subject uses "roles", list endpoint uses "role" - support both
            role_fields = field_selection.get_nested("role") or field_selection.get_nested("roles")
            for field_name in role_fields:
                if field_name != "id":
                    value = getattr(role, field_name, None)
                    if value is not None:
                        role_data[field_name] = value

        return role_data

    def get_roles(self, obj):
        """Extract roles from the prefetched role bindings.

        Default (no fields param): Returns only role id.
        With fields param: id is always included, plus explicitly requested fields.

        For platform roles, returns their children instead of the platform role itself.
        """
        if isinstance(obj, dict):
            return obj.get("roles", [])

        # Collect all binding entries from Group or Principal
        binding_entries = self._get_binding_entries(obj)
        return self._extract_roles_from_bindings(binding_entries)

    def _get_binding_entries(self, obj):
        """Get all binding entries from a Group or Principal.

        Args:
            obj: Group or Principal object with prefetched bindings

        Returns:
            Iterable of RoleBindingGroup (for Group) or RoleBindingPrincipal (for Principal) objects
        """
        if isinstance(obj, Group):
            # Group.filtered_bindings contains RoleBindingGroup objects
            return getattr(obj, "filtered_bindings", [])
        elif isinstance(obj, Principal):
            # Principal.filtered_bindings contains RoleBindingPrincipal objects
            return getattr(obj, "filtered_bindings", [])
        return []

    def _extract_roles_from_bindings(self, binding_entries):
        """Extract deduplicated roles from binding entries.

        Args:
            binding_entries: Iterable of RoleBindingGroup or RoleBindingPrincipal objects

        Returns:
            List of role dicts with id and requested fields
        """
        field_selection = self._get_field_selection()

        # Normalize roles: collect all roles to process (children for platform, role itself for non-platform)
        roles_to_process = []
        seen_role_ids = set()

        for binding_entry in binding_entries:
            if not hasattr(binding_entry, "binding") or not binding_entry.binding:
                continue

            role = binding_entry.binding.role
            if not role:
                continue

            # For platform roles, use children; for others, use the role itself
            # Note: role.children.all() uses prefetched data from service layer's
            # prefetch_related("role__children"), so this should not cause N+1 queries
            if role.type == RoleV2.Types.PLATFORM:
                roles_to_process.extend(role.children.all())
            else:
                roles_to_process.append(role)

        # Process all normalized roles in a single loop
        roles = []
        for role in roles_to_process:
            if role.uuid in seen_role_ids:
                continue

            role_data = self._build_role_data(role, field_selection)
            roles.append(role_data)
            seen_role_ids.add(role.uuid)

        return roles

    def get_resource(self, obj):
        """Extract resource information from the request context.

        Default (no fields param): Returns only resource id.
        With fields param: id is always included, plus explicitly requested fields.
        Returns None if context has no resource information.
        """
        if isinstance(obj, dict):
            return obj.get("resource", {})

        # Check if context has any resource information
        resource_id = self.context.get("resource_id")
        resource_name = self.context.get("resource_name")
        resource_type = self.context.get("resource_type")

        if not any([resource_id, resource_name, resource_type]):
            return None

        field_selection = self._get_field_selection()

        # id is always included
        resource_data = {"id": resource_id}

        if field_selection is not None:
            # Add explicitly requested fields
            field_values = {
                "name": resource_name,
                "type": resource_type,
            }

            for field_name in field_selection.get_nested("resource"):
                if field_name != "id":
                    value = field_values.get(field_name)
                    if value is not None:
                        resource_data[field_name] = value

        return resource_data

    def get_sources(self, obj):
        """Extract sources information indicating where role bindings are attached.

        For by-subject endpoints, a subject may have bindings from multiple resources
        (direct and inherited). This returns the unique list of resources where bindings
        are attached.

        Default (no fields param): Returns only source id.
        With fields param: id is always included, plus explicitly requested fields (name, type).
        """
        if isinstance(obj, dict):
            return obj.get("sources", [])

        field_selection = self._get_field_selection()

        # Get all binding entries from the subject's filtered_bindings
        binding_entries = self._get_binding_entries(obj)

        # Extract unique sources from bindings
        seen_sources = set()
        sources = []

        for binding_entry in binding_entries:
            if not hasattr(binding_entry, "binding") or not binding_entry.binding:
                continue

            binding = binding_entry.binding
            source_key = (binding.resource_type, binding.resource_id)

            if source_key in seen_sources:
                continue
            seen_sources.add(source_key)

            source_data: dict = {"id": binding.resource_id}

            if field_selection is not None:
                source_fields = field_selection.get_nested("sources")
                if "type" in source_fields:
                    source_data["type"] = binding.resource_type
                if "name" in source_fields:
                    source_data["name"] = getattr(binding, "resource_name", None)

            sources.append(source_data)

        return sources


# Backward compatibility alias
RoleBindingByGroupSerializer = RoleBindingOutputSerializer


class RoleBindingOutputSerializerMixin:
    """Shared serializer methods for role binding output serializers.

    Provides common functionality for field selection parsing and data building.
    """

    context: dict

    def _get_field_selection(self) -> Optional[FieldSelection]:
        """Get field selection from context."""
        return self.context.get("field_selection")

    def _extract_group_details(self, group: Group, field_selection: FieldSelection) -> dict:
        """Extract group.* fields from a Group object based on field selection.

        Args:
            group: The Group object to extract fields from
            field_selection: The field selection specifying which fields to include

        Returns:
            Dictionary with extracted group details, or empty dict if none requested
        """
        subject_fields = field_selection.get_nested("subject")
        # Extract field names from "group.X" paths
        fields_to_include = {
            field_path.removeprefix(_GROUP_FIELD_PREFIX)
            for field_path in subject_fields
            if field_path.startswith(_GROUP_FIELD_PREFIX)
        }

        if not fields_to_include:
            return {}

        group_details = {}
        for field_name in fields_to_include:
            # Handle special case for user_count -> principalCount
            if field_name == "user_count":
                group_details[field_name] = getattr(group, "principalCount", 0)
            else:
                value = getattr(group, field_name, None)
                if value is not None:
                    group_details[field_name] = value

        return group_details

    def _build_subject_data(self, group: Group, field_selection: Optional[FieldSelection]) -> dict:
        """Build subject data dictionary from a Group object.

        Args:
            group: The Group object to build subject data from
            field_selection: Optional field selection to determine which fields to include

        Returns:
            Dictionary with subject data (always includes 'type')
        """
        # Default behavior: only basic fields
        if field_selection is None:
            return {
                "id": group.uuid,
                "type": _SUBJECT_TYPE_GROUP,
            }

        # With fields param: type is always included
        subject: dict = {"type": _SUBJECT_TYPE_GROUP}

        # Check if id is explicitly requested
        if "id" in field_selection.get_nested("subject"):
            subject["id"] = group.uuid

        # Extract group.* fields
        group_details = self._extract_group_details(group, field_selection)
        if group_details:
            subject[_SUBJECT_TYPE_GROUP] = group_details

        return subject

    def _build_role_data(self, role: RoleV2, field_selection: Optional[FieldSelection]) -> dict:
        """Build role data dictionary from a role object.

        Args:
            role: The role to build data for
            field_selection: Optional field selection to determine which fields to include

        Returns:
            Dictionary with role data (always includes 'id')
        """
        role_data = {"id": role.uuid}

        if field_selection is not None:
            # Add explicitly requested fields
            for field_name in field_selection.get_nested("role"):
                if field_name != "id":
                    value = getattr(role, field_name, None)
                    if value is not None:
                        role_data[field_name] = value

        return role_data


class RoleBindingListOutputSerializer(RoleBindingOutputSerializerMixin, serializers.Serializer):
    """Output serializer for the role binding list endpoint.

    Handles RoleBinding objects and returns {role, subject, resource, sources}.

    Supports dynamic field selection through the 'field_selection' context parameter.
    Fields are accessed directly on the model using dot notation from the query parameter.

    Field selection syntax:
    - subject(group.name, group.description) - accesses obj.name, obj.description
    - role(name) - accesses role.name
    - resource(name, type) - display name via ``RoleBindingQuerySet.with_resource_names()`` annotation;
      type from the binding
    - sources(name, type) - accesses sources with optional name and type
    """

    subject = serializers.SerializerMethodField()
    role = serializers.SerializerMethodField()
    resource = serializers.SerializerMethodField()
    sources = serializers.SerializerMethodField()

    def get_subject(self, obj: RoleBinding):
        """Extract subject information from the RoleBinding.

        Checks group_entries first, then principal_entries.

        Default (no fields param): Returns only id and type.
        With fields param: Only type is always included. Other fields
        (including id) are only included if explicitly requested.
        """
        field_selection = self._get_field_selection()

        # Try group subject first
        group_entries = getattr(obj, "group_entries", None)
        if group_entries is not None:
            first_entry = group_entries.all()[:1]
            if first_entry:
                group = first_entry[0].group
                return self._build_subject_data(group, field_selection)

        # Try principal subject
        principal_entries = getattr(obj, "principal_entries", None)
        if principal_entries is not None:
            first_entry = principal_entries.all()[:1]
            if first_entry:
                principal = first_entry[0].principal
                if field_selection is None:
                    return {"id": principal.uuid, "type": _SUBJECT_TYPE_USER}
                subject = {"type": _SUBJECT_TYPE_USER}
                if "id" in field_selection.get_nested("subject"):
                    subject["id"] = principal.uuid
                return subject

        return {"type": _SUBJECT_TYPE_GROUP}

    def get_role(self, obj: RoleBinding):
        """Extract role information from the RoleBinding.

        Default (no fields param): Returns only role id.
        With fields param: id is always included, plus explicitly requested fields.
        """
        if not obj.role:
            return None

        field_selection = self._get_field_selection()
        return self._build_role_data(obj.role, field_selection)

    def get_resource(self, obj: RoleBinding):
        """Extract resource information from the RoleBinding.

        Default (no fields param): Returns only resource id.
        With fields param: id is always included, plus explicitly requested fields.
        ``name`` requires the queryset to have been annotated via
        ``RoleBindingQuerySet.with_resource_names()``.
        """
        field_selection = self._get_field_selection()

        resource_data = {"id": obj.resource_id}

        if field_selection is not None:
            nested_resource = field_selection.get_nested("resource")
            if "type" in nested_resource:
                resource_data["type"] = obj.resource_type
            if "name" in nested_resource:
                resource_data["name"] = getattr(obj, "resource_name", None)

        return resource_data

    def get_sources(self, obj: RoleBinding):
        """Extract sources information indicating where the role binding is attached.

        Returns a list of resources from which this role binding is sourced.
        For direct bindings, this is the binding's own resource.
        For inherited bindings, this is the parent resource where the binding is attached.

        Default (no fields param): Returns only source id.
        With fields param: id is always included, plus explicitly requested fields (name, type).
        """
        field_selection = self._get_field_selection()

        # The source is always the resource where the binding is actually attached
        source_data: dict = {"id": obj.resource_id}

        if field_selection is not None:
            source_fields = field_selection.get_nested("sources")
            if "type" in source_fields:
                source_data["type"] = obj.resource_type
            if "name" in source_fields:
                source_data["name"] = getattr(obj, "resource_name", None)

        return [source_data]


class ResourceInputSerializer(serializers.Serializer):
    """Validates the resource portion of a role binding request."""

    id = serializers.CharField(max_length=256, help_text="ID of the resource")
    type = serializers.CharField(help_text="Type of resource")


class SubjectInputSerializer(serializers.Serializer):
    """Validates the subject portion of a role binding request."""

    id = serializers.UUIDField(help_text="UUID of the subject")
    type = serializers.ChoiceField(choices=["user", "group"], help_text="Type of subject")


class CreateRoleBindingItemSerializer(serializers.Serializer):
    """Validates a single role binding request item."""

    resource = ResourceInputSerializer()
    subject = SubjectInputSerializer()
    role = RoleIdSerializer()


class BatchCreateRoleBindingRequestSerializer(serializers.Serializer):
    """Validates and processes a batch create role bindings request."""

    service_class = RoleBindingService

    DEFAULT_FIELDS = "resource(id,type),role(id),subject(id,type)"

    requests = CreateRoleBindingItemSerializer(many=True, min_length=1, max_length=100)
    fields = serializers.CharField(
        required=False, default="", allow_blank=True, help_text="Control which fields are included"
    )

    @property
    def service(self):
        """Return the service instance."""
        return self.context.get("role_binding_service") or self.service_class(tenant=self.context["request"].tenant)

    def validate_fields(self, value):
        """Parse and validate the fields query parameter for response field masking."""
        if not value:
            return RoleBindingFieldSelection.parse(self.DEFAULT_FIELDS)
        try:
            return RoleBindingFieldSelection.parse(value)
        except FieldSelectionValidationError as e:
            raise serializers.ValidationError(e.message)

    def create(self, validated_data):
        """Create role bindings using the service layer."""
        requests = [
            CreateBindingRequest(
                role_id=str(item["role"]["id"]),
                resource_type=item["resource"]["type"],
                resource_id=str(item["resource"]["id"]),
                subject_type=item["subject"]["type"],
                subject_id=str(item["subject"]["id"]),
            )
            for item in validated_data["requests"]
        ]
        return self.service.batch_create(requests)


class RoleBindingFieldMaskingMixin:
    """Shared field-masking logic for role binding response serializers.

    Provides building blocks that apply the ``field_selection`` context to
    individual response sections (subject, roles, resource).

    Subclasses declare ``SerializerMethodField`` and implement ``get_*``
    methods that extract data from their specific input type (e.g. Group
    model vs ``UpdateRoleBindingResult`` dataclass), then delegate to these
    helpers for consistent masking.

    Masking rules
    -------------
    * Default (no ``field_selection``): subject returns ``id`` + ``type``;
      roles returns ``id`` only; resource returns ``id`` only.
    * With ``field_selection``: only explicitly requested fields appear
      and unrequested top-level sections are stripped entirely. Subject
      objects always include ``type`` (OpenAPI discriminator for
      UserSubject | GroupSubject) even when not listed in ``fields``.
    """

    def __init__(self, *args, **kwargs):
        """Strip unrequested top-level sections when field_selection is present."""
        super().__init__(*args, **kwargs)

        field_selection = self._get_field_selection()
        if field_selection is not None:
            requested = set(field_selection.nested_fields.keys())
            for field_name in set(self.fields) - requested:
                self.fields.pop(field_name)

    def _get_field_selection(self):
        """Get field selection from context."""
        return self.context.get("field_selection")

    # ── Building-block helpers ───────────────────────────────────────

    def _build_subject_data(self, subject_type, subject_obj):
        """Build a subject dict with field masking applied.

        Args:
            subject_type: ``"group"`` or ``"user"`` (a SubjectType value).
            subject_obj: The underlying Group or Principal model instance.
        """
        field_selection = self._get_field_selection()

        if field_selection is None:
            subject = {"id": subject_obj.uuid, "type": subject_type}
            if subject_type == SubjectType.USER:
                subject["user"] = {"username": subject_obj.username}
            return subject

        subject = {}
        subject_fields = field_selection.get_nested("subject")

        # UserSubject / GroupSubject require ``type`` for valid JSON and generated clients.
        subject["type"] = subject_type
        if "id" in subject_fields:
            subject["id"] = subject_obj.uuid

        if subject_type == SubjectType.GROUP:
            group_details = self._extract_nested_fields("group.", subject_fields, subject_obj)
            if group_details:
                subject["group"] = group_details
        elif subject_type == SubjectType.USER:
            user_details = self._extract_nested_fields("user.", subject_fields, subject_obj)
            if user_details:
                subject["user"] = user_details

        return subject

    def _build_role_data(self, role):
        """Build a role dict with field masking applied."""
        field_selection = self._get_field_selection()

        if field_selection is None:
            return {"id": role.uuid}

        role_data = {}
        role_fields = field_selection.get_nested("role") or field_selection.get_nested("roles")
        for field_name in role_fields:
            if field_name == "id":
                role_data["id"] = role.uuid
            else:
                value = getattr(role, field_name, None)
                if value is not None:
                    role_data[field_name] = value

        return role_data

    def _build_resource_data(self, resource_id, resource_name=None, resource_type=None):
        """Build a resource dict with field masking applied."""
        field_selection = self._get_field_selection()

        if field_selection is None:
            return {"id": resource_id}

        field_values = {"id": resource_id, "name": resource_name, "type": resource_type}
        resource_data = {}
        for field_name in field_selection.get_nested("resource"):
            value = field_values.get(field_name)
            if value is not None:
                resource_data[field_name] = value

        return resource_data

    @staticmethod
    def _extract_nested_fields(prefix, field_paths, obj):
        """Extract attribute values matching field paths with a given prefix.

        Handles the ``user_count`` → ``principalCount`` special case for groups.
        """
        details = {}
        prefix_len = len(prefix)
        for field_path in field_paths:
            if field_path.startswith(prefix):
                attr_name = field_path[prefix_len:]
                if attr_name == "user_count":
                    details[attr_name] = getattr(obj, "principalCount", 0)
                else:
                    value = getattr(obj, attr_name, None)
                    if value is not None:
                        details[attr_name] = value
        return details


class BatchCreateRoleBindingResponseItemSerializer(RoleBindingFieldMaskingMixin, serializers.Serializer):
    """Serializes a single created role binding for the batch create response."""

    role = serializers.SerializerMethodField()
    subject = serializers.SerializerMethodField()
    resource = serializers.SerializerMethodField()

    def get_role(self, obj):
        """Delegate to ``_build_role_data``."""
        return self._build_role_data(obj["role"])

    def get_subject(self, obj):
        """Delegate to ``_build_subject_data``."""
        return self._build_subject_data(obj["subject_type"], obj["subject"])

    def get_resource(self, obj):
        """Delegate to ``_build_resource_data``."""
        return self._build_resource_data(obj["resource_id"], obj.get("resource_name"), obj["resource_type"])


class UpdateRoleBindingRequestSerializer(RoleBindingInputSerializerMixin, serializers.Serializer):
    """Input serializer for update role binding API.

    Inherits from ``RoleBindingInputSerializerMixin`` for shared dotted-param
    remapping, NUL-byte sanitization, and ``validate_fields``.
    Supports resource.tenant.org_id as an alternative to resource_id + resource_type.
    """

    DOTTED_PARAM_MAP = {
        "resource.tenant.org_id": "resource_tenant_org_id",
    }

    DEFAULT_FIELDS = "resource(id),subject(id,type),roles(id)"

    # Query parameters
    resource_id = serializers.CharField(
        required=False, allow_blank=True, help_text="Resource ID to update bindings for"
    )
    resource_type = serializers.CharField(
        required=False, allow_blank=True, help_text="Resource type (e.g., 'workspace')"
    )
    resource_tenant_org_id = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Org ID of the tenant resource to update bindings for",
    )
    subject_id = serializers.CharField(required=True, help_text="Subject ID (UUID)")
    subject_type = serializers.CharField(required=True, help_text="Subject type (e.g., 'group')")
    fields = serializers.CharField(
        required=False, default="", allow_blank=True, help_text="Control which fields are included"
    )

    # Request body
    roles = RoleIdSerializer(many=True, required=True, help_text="Roles to assign")

    validate_resource_id = staticmethod(_normalize_blank_or_none)
    validate_resource_type = staticmethod(_normalize_blank_or_none)
    validate_resource_tenant_org_id = staticmethod(_normalize_blank_or_none)

    def validate_fields(self, value):
        """Parse and validate fields parameter using by-subject selection."""
        if not value:
            return RoleBindingBySubjectFieldSelection.parse(self.DEFAULT_FIELDS)
        try:
            return RoleBindingBySubjectFieldSelection.parse(value)
        except FieldSelectionValidationError as e:
            raise serializers.ValidationError(e.message)

    SUPPORTED_SUBJECT_TYPES = (SubjectType.GROUP, SubjectType.USER)

    def validate_subject_type(self, value):
        """Validate subject_type is a supported enum value for by-subject operations."""
        if value not in self.SUPPORTED_SUBJECT_TYPES:
            supported = ", ".join(self.SUPPORTED_SUBJECT_TYPES)
            raise serializers.ValidationError(f"Unsupported subject type: '{value}'. Supported types: {supported}")
        return value

    def validate(self, attrs):
        """Cross-field validation for resource params."""
        attrs = super().validate(attrs)
        _validate_resource_identifiers(attrs)
        return attrs

    def save(self):
        """Execute the update via the service layer and return the result.

        Domain exceptions (NotFoundError, InvalidFieldError, RequiredFieldError)
        propagate to the global exception handler which formats them as
        Problem Details responses.
        """
        validated = self.validated_data
        tenant = self.context["request"].tenant
        role_ids = [str(role["id"]) for role in validated["roles"]]
        service = RoleBindingService(tenant=tenant)

        resource_type, resource_id = resolve_resource_identifiers(validated)

        return service.update_role_bindings_for_subject(
            resource_type=resource_type,
            resource_id=resource_id,
            subject_type=validated["subject_type"],
            subject_id=validated["subject_id"],
            role_ids=role_ids,
        )


class UpdateRoleBindingResponseSerializer(RoleBindingFieldMaskingMixin, serializers.Serializer):
    """Output serializer for the update role binding API.

    Serializes an ``UpdateRoleBindingResult`` dataclass into the API response.
    Data extraction is result-specific; field masking is delegated to
    ``RoleBindingFieldMaskingMixin``.
    """

    subject = serializers.SerializerMethodField()
    roles = serializers.SerializerMethodField()
    resource = serializers.SerializerMethodField()

    def get_subject(self, result):
        """Delegate to ``_build_subject_data`` with result's subject info."""
        return self._build_subject_data(result.subject_type, result.subject)

    def get_roles(self, result):
        """Delegate per-role masking to ``_build_role_data``."""
        return [self._build_role_data(role) for role in result.roles]

    def get_resource(self, result):
        """Delegate to ``_build_resource_data`` with result's resource info."""
        return self._build_resource_data(result.resource_id, result.resource_name, result.resource_type)
