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

"""Model for principal management."""

from typing import Optional
from uuid import uuid4

from django.conf import settings
from django.db import models

from api.models import TenantAwareModel


class Principal(TenantAwareModel):
    """A principal."""

    class Types(models.TextChoices):
        USER = "user", "User"
        SERVICE_ACCOUNT = "service-account", "Service Account"

    @staticmethod
    def user_id_to_principal_resource_id(user_id: str) -> str:
        """Convert a user ID to a principal resource ID suitable for use in the Kessel access graph."""
        domain = settings.PRINCIPAL_USER_DOMAIN
        return f"{domain}/{user_id}"

    uuid = models.UUIDField(default=uuid4, editable=False, unique=True, null=False)
    username = models.CharField(max_length=150)
    cross_account = models.BooleanField(default=False)
    type = models.CharField(null=False, default=Types.USER, choices=Types.choices, max_length=20, db_index=True)
    service_account_id = models.TextField(null=True, db_index=True)
    user_id = models.CharField(max_length=256, null=True, db_index=True)

    def principal_resource_id(self) -> Optional[str]:
        """Return the principal resource ID suitable for use in the Kessel access graph."""
        if self.user_id is None:
            return None
        return Principal.user_id_to_principal_resource_id(self.user_id)

    def save(self, *args, **kwargs):
        """Override save to only store lower case username."""
        self.username = self.username.lower()
        super(Principal, self).save(*args, **kwargs)

    class Meta:
        ordering = ["username"]
        constraints = [
            models.UniqueConstraint(fields=["username", "tenant"], name="unique principal username per tenant"),
            models.UniqueConstraint(fields=["user_id"], name="management_principal_user_id_key"),
        ]
