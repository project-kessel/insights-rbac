#
# Copyright 2019 Red Hat, Inc.
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""API models for import organization."""

from typing import Any, Optional

from django.conf import settings
from django.db import models
from django.db.models import Q

from api.cross_access.model import CrossAccountRequest  # noqa: F401
from api.status.model import Status  # noqa: F401


class TenantModifiedQuerySet(models.QuerySet):
    """Queryset for modified tenants."""

    def modified_only(self):
        """Return only modified tenants."""
        return (
            self.filter(Q(group__system=False) | Q(role__system=False))
            .prefetch_related("group_set", "role_set")
            .distinct()
        )


class Tenant(models.Model):
    """The model used to create a tenant schema."""

    PUBLIC_TENANT_NAME = "public"

    _public_tenant = None

    ready = models.BooleanField(default=False)
    tenant_name = models.CharField(max_length=63)
    account_id = models.CharField(max_length=36, default=None, null=True)
    org_id = models.CharField(max_length=36, unique=True, default=None, db_index=True, null=True)
    relations_consistency_token = models.CharField(max_length=1024, default=None, null=True)
    objects = TenantModifiedQuerySet.as_manager()

    def __str__(self):
        """Get string representation of Tenant."""
        return f"Tenant ({self.org_id})"

    @classmethod
    def _get_public_tenant(cls):
        """Get or set public tenant."""
        if cls._public_tenant is None:
            cls._public_tenant = Tenant.objects.get(tenant_name=cls.PUBLIC_TENANT_NAME)
        return cls._public_tenant

    @staticmethod
    def _resource_id_prefix() -> str:
        return f"{settings.PRINCIPAL_USER_DOMAIN}/"

    @classmethod
    def org_id_to_tenant_resource_id(cls, org_id: str) -> str:
        """Get the V2 resource ID for a tenant with the provided org_id."""
        return cls._resource_id_prefix() + org_id

    def tenant_resource_id(self) -> Optional[str]:
        """Get the V2 resource ID for this tenant; None is returned if org_id is not available."""
        if self.org_id is None:
            return None

        return Tenant.org_id_to_tenant_resource_id(org_id=self.org_id)

    @classmethod
    def tenant_resource_id_to_org_id(cls, resource_id: str) -> str:
        """Convert a tenant's resource ID to the tenant's org_id."""
        if not isinstance(resource_id, str):
            raise TypeError(f"Expected resource ID to be a string, but got: {resource_id!r}")

        prefix = cls._resource_id_prefix()

        if not resource_id.startswith(prefix):
            raise ValueError(f"Expected resource ID to start with {prefix!r}, but got: {resource_id!r}")

        return resource_id[len(prefix) :]  # noqa: E203

    class Meta:
        indexes = [
            models.Index(fields=["ready"]),
        ]


class TenantAwareModel(models.Model):
    """Abstract model for inheriting `Tenant`."""

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)

    class Meta:
        abstract = True


class User:
    """A request User. Might also represent a service account."""

    _username: Optional[str] = None

    def __init__(self, **kwargs: Any):
        """
        Initialize User with optional parameters.

        :param kwargs: Optional parameters to set on the User instance.
        """
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)

    @property
    def username(self) -> Optional[str]:
        """Return the username."""
        return self._username

    @username.setter
    def username(self, value: Optional[str]) -> None:
        """
        Set the username.

        Lower-cases the username due to case insensitivity.
        """
        self._username = value.lower() if value else None

    account: Optional[str] = None
    admin: bool = False
    internal: bool = False
    access = {}
    system: bool = False
    is_active: bool = True
    org_id: Optional[str] = None
    user_id: Optional[str] = None
    # Service account properties.
    bearer_token: str = ""
    client_id: str = ""
    is_service_account: bool = False

    def __eq__(self, other):
        """Check equality of User instances."""
        if not isinstance(other, User):
            return NotImplemented
        return (
            self.username == other.username
            and self.account == other.account
            and self.admin == other.admin
            and self.internal == other.internal
            and self.access == other.access
            and self.system == other.system
            and self.is_active == other.is_active
            and self.org_id == other.org_id
            and self.user_id == other.user_id
            and self.bearer_token == other.bearer_token
            and self.client_id == other.client_id
            and self.is_service_account == other.is_service_account
        )

    def __hash__(self):
        """Hash the User instance."""
        return hash((self.username, self.user_id, self.client_id))

    def __repr__(self):
        """Return a string representation of the User instance."""
        return (
            f"User(username={self.username!r}, account={self.account!r}, admin={self.admin!r}, "
            f"internal={self.internal!r}, system={self.system!r}, is_active={self.is_active!r}, "
            f"org_id={self.org_id!r}, user_id={self.user_id!r}, bearer_token={'***' if self.bearer_token else ''}, "
            f"client_id={self.client_id!r}, is_service_account={self.is_service_account!r})"
        )


class FilterQuerySet(models.QuerySet):
    """Queryset for filtering."""

    def public_tenant_only(self):
        """Filter queryset by returning only non-custom results."""
        return self.filter(system=True, tenant=Tenant._get_public_tenant())
