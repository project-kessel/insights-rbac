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

"""Serializers for the Audit Log V2 API."""

from typing import Optional

from management.audit_log.model import AuditLog
from management.utils import FieldSelection, FieldSelectionValidationError, normalize_blank_or_none
from rest_framework import serializers

# Ordering fields accepted by the ``order_by`` query parameter.
VALID_ORDER_BY_FIELDS = {"created", "-created"}


class AuditLogV2OutputSerializer(serializers.ModelSerializer):
    """Output serializer for the Audit Log V2 API.

    The integer primary key exposed by V1 as ``sequence`` is deliberately omitted;
    V2 clients page through the log with an opaque cursor instead.
    """

    resource_id = serializers.UUIDField(source="resource_uuid", read_only=True, allow_null=True)

    class Meta:
        model = AuditLog
        fields = (
            "created",
            "principal_username",
            "description",
            "resource_type",
            "resource_id",
            "action",
            "source",
        )

    def __init__(self, *args, **kwargs):
        """Initialize with dynamic field selection from context."""
        super().__init__(*args, **kwargs)

        allowed = self.context.get("fields")
        if allowed is not None:
            for field_name in set(self.fields) - allowed:
                self.fields.pop(field_name)


class AuditLogFieldSelection(FieldSelection):
    """Field selection for the audit logs endpoint."""

    VALID_ROOT_FIELDS = set(AuditLogV2OutputSerializer.Meta.fields)


DEFAULT_AUDIT_LOG_FIELDS = set(AuditLogV2OutputSerializer.Meta.fields)


def validate_fields_parameter(value: str, default_fields: Optional[set] = None) -> set:
    """Validate and parse the ``fields`` parameter for the audit log endpoint.

    Args:
        value: The raw fields parameter value from the request.
        default_fields: Fields to return when value is empty or selects nothing valid.

    Returns:
        Set of field names to include in the response.

    Raises:
        serializers.ValidationError: If the fields parameter has invalid syntax or
            names a field that the endpoint does not return.
    """
    defaults = DEFAULT_AUDIT_LOG_FIELDS if default_fields is None else default_fields

    if not value:
        return defaults

    try:
        field_selection = AuditLogFieldSelection.parse(value)
    except FieldSelectionValidationError as e:
        raise serializers.ValidationError({"fields": e.message})

    if not field_selection:
        return defaults

    resolved = field_selection.root_fields & DEFAULT_AUDIT_LOG_FIELDS
    return resolved or defaults


class AuditLogV2ListInputSerializer(serializers.Serializer):
    """Input serializer for Audit Log V2 list query parameters."""

    RESOURCE_TYPES = tuple(choice[0] for choice in AuditLog.RESOURCE_CHOICES)
    ACTIONS = tuple(choice[0] for choice in AuditLog.ACTION_CHOICES)

    principal_username = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Filter by principal username. Case-insensitive substring match; use * for glob patterns.",
    )
    resource_type = serializers.ChoiceField(
        choices=RESOURCE_TYPES,
        required=False,
        allow_blank=True,
        help_text=f"Filter by resource type. One of: {', '.join(RESOURCE_TYPES)}.",
    )
    resource_id = serializers.UUIDField(
        required=False,
        help_text="Filter by the UUID of the resource the entry refers to.",
    )
    action = serializers.ChoiceField(
        choices=ACTIONS,
        required=False,
        allow_blank=True,
        help_text=f"Filter by action. One of: {', '.join(ACTIONS)}.",
    )
    created_after = serializers.DateTimeField(
        required=False,
        help_text="Only return entries created at or after this ISO 8601 timestamp.",
    )
    created_before = serializers.DateTimeField(
        required=False,
        help_text="Only return entries created at or before this ISO 8601 timestamp.",
    )
    order_by = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Sort by created, prefix with '-' for descending. Valid: created, -created.",
    )

    def validate_resource_type(self, value):
        """Return None for empty values."""
        return value or None

    def validate_action(self, value):
        """Return None for empty values."""
        return value or None

    validate_principal_username = staticmethod(normalize_blank_or_none)
    validate_order_by = staticmethod(normalize_blank_or_none)

    def validate(self, data):
        """Cross-field validation."""
        order_by = data.get("order_by")
        if order_by and order_by not in VALID_ORDER_BY_FIELDS:
            raise serializers.ValidationError(
                {
                    "order_by": (
                        f"Invalid order_by value '{order_by}'. "
                        f"Valid values: {', '.join(sorted(VALID_ORDER_BY_FIELDS))}"
                    )
                }
            )

        created_after = data.get("created_after")
        created_before = data.get("created_before")
        if created_after and created_before and created_after > created_before:
            raise serializers.ValidationError(
                {"created_after": "created_after must not be later than created_before."}
            )

        return data
