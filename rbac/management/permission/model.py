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

"""Model for permission management."""

import dataclasses

from django.db import models
from management.exceptions import RequiredFieldError
from management.permission.exceptions import InvalidPermissionDataError
from migration_tool.models import cleanNameForV2SchemaCompatibility

from api.models import TenantAwareModel


@dataclasses.dataclass(frozen=True)
class PermissionValue:
    """
    Represents the value of a single permission.

    A permission consists of an app name, a resource type name, and a verb.

    The resource type and verb may either be normal strings or the wildcard '*'. If either component contains a
    wildcard, it must be the entire component. (That is, 'foo*' is not a glob.) The app name may not be a wildcard.

    No component may contain a colon; this ensures that the V1 string representation remains unambiguous.
    """

    application: str
    resource_type: str
    verb: str

    @staticmethod
    def _check_valid_component(name: str, value: str):
        """
        Check that the component is valid in a permission.

        Name is used for producing error messages.
        """
        if value is None:
            raise ValueError(f"{name} cannot be None")

        if ":" in value:
            raise ValueError(f"Portions of a permission cannot contain colons, but got {name}: {value}")

        if (value != "*") and ("*" in value):
            raise ValueError(f"Wildcard portions of a permission must be only an asterisk, but got {name}: {value}")

    def __post_init__(self):
        """Verify that this permission is valid."""
        self._check_valid_component("app", self.application)
        self._check_valid_component("resource", self.resource_type)
        self._check_valid_component("action", self.verb)

        if "*" in self.application:
            raise ValueError("Wildcards are not permitted in the app portion of permissions.")

    @classmethod
    def parse_v1(cls, permission_str: str) -> "PermissionValue":
        """Parse a V1 permission string, e.g. app:resource:verb."""
        parts = permission_str.split(":")

        if len(parts) != 3:
            raise ValueError(f"Permission string must contain three colon-separated parts: {permission_str}")

        return cls(application=parts[0], resource_type=parts[1], verb=parts[2])

    def with_unconstrained_resource_type(self) -> "PermissionValue":
        """Return a new permission with a wildcard resource."""
        return PermissionValue(
            application=self.application,
            resource_type="*",
            verb=self.verb,
        )

    def with_unconstrained_verb(self) -> "PermissionValue":
        """Return a new permission with a wildcard verb."""
        return PermissionValue(
            application=self.application,
            resource_type=self.resource_type,
            verb="*",
        )

    def with_application_only(self) -> "PermissionValue":
        """Return a new permission with a wildcard resource and verb."""
        return PermissionValue(
            application=self.application,
            resource_type="*",
            verb="*",
        )

    def v1_string(self) -> str:
        """Return the V1 representation of this permission: app:resource:verb."""
        return f"{self.application}:{self.resource_type}:{self.verb}"

    @property
    def is_wildcard(self) -> bool:
        """Return True if the resource type or verb is the wildcard ``*``."""
        return self.resource_type == "*" or self.verb == "*"

    def subsumes(self, other: "PermissionValue") -> bool:
        """Return True if this pattern covers ``other`` (is equal to or broader than it).

        A wildcard in this value's resource type or verb matches any value in the
        corresponding component of ``other``.  Applications must match exactly.
        """
        if self.application != other.application:
            return False
        if self.resource_type != "*" and self.resource_type != other.resource_type:
            return False
        if self.verb != "*" and self.verb != other.verb:
            return False
        return True

    @classmethod
    def from_v2_dict(cls, data: dict) -> "PermissionValue":
        """
        Create from API dict where 'operation' maps to internal 'verb'.

        When DB column is renamed from 'verb' to 'operation', update this method.
        """
        application = data.get("application")
        resource_type = data.get("resource_type")
        operation = data.get("operation")
        verb = data.get("verb")

        if operation and verb:
            raise InvalidPermissionDataError("Cannot specify both 'operation' and 'verb'")

        if application is None:
            raise RequiredFieldError("application")
        if resource_type is None:
            raise RequiredFieldError("resource_type")

        verb_value = operation or verb
        if verb_value is None:
            raise RequiredFieldError("operation")

        return cls(application=application, resource_type=resource_type, verb=verb_value)

    def to_v2_dict(self) -> dict:
        """
        Convert to API dict where internal 'verb' maps to 'operation'.

        When DB column is renamed from 'verb' to 'operation', update this method.
        """
        return {
            "application": self.application,
            "resource_type": self.resource_type,
            "operation": self.verb,
        }


class Permission(TenantAwareModel):
    """A Permission."""

    application = models.TextField(null=False)
    resource_type = models.TextField(null=False)
    verb = models.TextField(null=False)
    permission = models.TextField(null=False, unique=True)
    description = models.TextField(default="")
    permissions = models.ManyToManyField("self", symmetrical=False, related_name="requiring_permissions")

    def save(self, *args, **kwargs):
        """Populate the application, resource_type and verb field before saving."""
        context = self.permission.split(":")
        self.application = context[0]
        self.resource_type = context[1]
        self.verb = context[2]
        super(Permission, self).save(*args, **kwargs)

    def v2_string(self) -> str:
        """Convert this V1 permission to the string representation for a V2 permission."""
        return cleanNameForV2SchemaCompatibility(self.application + "_" + self.resource_type + "_" + self.verb)
