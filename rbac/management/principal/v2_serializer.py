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
from rest_framework import serializers


class PrincipalV2OutputSerializer(serializers.ModelSerializer):
    """Output serializer for the Principal V2 API."""

    class Meta:
        model = Principal
        fields = ("uuid", "username", "type", "user_id", "service_account_id")


class PrincipalV2ListInputSerializer(serializers.Serializer):
    """Input serializer for Principal V2 list query parameters."""

    VALID_TYPES = ("user", "service-account")
    VALID_MATCH_CRITERIA = ("exact", "partial")
    VALID_SORT_ORDER = ("asc", "desc")

    type = serializers.ChoiceField(
        choices=VALID_TYPES,
        required=False,
        allow_blank=True,
        help_text="Filter by principal type: 'user' or 'service-account'.",
    )
    username = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Filter by username. Use match_criteria to control matching behavior.",
    )
    match_criteria = serializers.ChoiceField(
        choices=VALID_MATCH_CRITERIA,
        required=False,
        default="exact",
        help_text="Username matching: 'exact' (default) or 'partial' (case-insensitive substring).",
    )
    sort_order = serializers.ChoiceField(
        choices=VALID_SORT_ORDER,
        required=False,
        default="asc",
        help_text="Sort direction for username: 'asc' (default) or 'desc'.",
    )

    def validate_type(self, value):
        """Return None for empty values."""
        return value or None

    def validate_username(self, value):
        """Return None for empty values."""
        return value or None
