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

"""Serializer for principal management."""

from collections import OrderedDict

from django.core.exceptions import ValidationError
from management.serializer_override_mixin import SerializerCreateOverrideMixin
from rest_framework import serializers

from .model import Principal


class PrincipalSerializer(SerializerCreateOverrideMixin, serializers.ModelSerializer):
    """Serializer for the Principal model."""

    class Meta:
        """Metadata for the serializer."""

        model = Principal
        fields = ("username",)


class PrincipalInputSerializer(serializers.Serializer):
    """Serializer for the Principal model."""

    username = serializers.CharField(required=False, max_length=150)
    clientId = serializers.UUIDField(required=False, source="service_account_id")
    type = serializers.CharField(required=False)

    def validate(self, data: OrderedDict):
        """
        Assert that the correct fields are specified.

        Assert that when the specified type is 'service-account', the corresponding 'clientId' field
        has been specified.
        """
        # If the "type" has not been specified, we assume it is a user principal.
        if ("type" not in data) or (data["type"] == "user"):
            if "username" not in data:
                raise ValidationError(code="missing", message="the username is required for user principals")

            return data
        elif data["type"] == "service-account":
            if "service_account_id" not in data:
                raise ValidationError(code="missing", message="the clientId field is required for service accounts")

            return data
        else:
            raise ValidationError(
                code="invalid", message="The principal type must be either 'user' or 'service-account'"
            )

    class Meta:
        """Metadata for the serializer."""

        fields = ("username", "clientId", "type")


class ServiceAccountSerializer(serializers.Serializer):
    """Serializer for Service Account."""

    clientId = serializers.UUIDField()
    name = serializers.CharField()
    description = serializers.CharField(allow_null=True, required=False)
    owner = serializers.CharField()
    time_created = serializers.IntegerField()
    # "type" tells a service account apart from a user, so it stays "service-account" for service access tokens as
    # well. "service_account_type" is what distinguishes a service access token from a regular service account.
    type = serializers.CharField()
    # IT only sends these for service accounts created in service access token mode. Declared as optional so that a
    # regular service account is serialized exactly as it was before, with the keys left out entirely rather than
    # rendered as null.
    # CIAM-12576 confirms the current set as "service_access_token" or omitted, which is what the OpenAPI enum
    # documents. Still a CharField rather than a ChoiceField: "current" implies the set can grow, and rejecting
    # an unrecognised value would fail the whole response instead of degrading to "treat it as a regular
    # service account", which is already the documented meaning of an absent value.
    service_account_type = serializers.CharField(required=False)
    time_expires = serializers.IntegerField(required=False)
    username = serializers.CharField()

    class Meta:
        """Metadata for the serializer."""

        fields = "__all__"
