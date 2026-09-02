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

"""Serializers for Principal V2 API."""

from management.principal.model import Principal
from management.utils import normalize_blank_or_none
from rest_framework import serializers

VALID_ORDER_BY_FIELDS = {"username", "-username"}


class PrincipalV2OutputSerializer(serializers.ModelSerializer):
    """Output serializer for the Principal V2 API."""

    class Meta:
        model = Principal
        fields = ("uuid", "username", "type", "user_id", "service_account_id")


class PrincipalV2ListInputSerializer(serializers.Serializer):
    """Input serializer for Principal V2 list query parameters."""

    VALID_TYPES = ("user", "service-account")

    type = serializers.ChoiceField(
        choices=VALID_TYPES,
        required=False,
        allow_blank=True,
        help_text="Filter by principal type: 'user' or 'service-account'.",
    )
    username = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Filter by username. Case-insensitive substring match by default; use * for glob patterns.",
    )
    order_by = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Sort by specified field(s), prefix with '-' for descending. Valid: username, -username.",
    )

    def validate_type(self, value):
        """Return None for empty values."""
        return value or None

    validate_username = staticmethod(normalize_blank_or_none)
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
        return data
