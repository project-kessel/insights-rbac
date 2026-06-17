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

"""Model for role V2 management."""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.db.models import signals
from django.utils import timezone
from management.exceptions import RequiredFieldError
from management.models import Permission, Role
from management.rbac_fields import AutoDateTimeField
from management.relation_replicator.types import RelationTuple
from management.role.queryset import RoleV2QuerySet
from management.role.relations import role_owner_relationship
from migration_tool.models import V2role, role_permission_tuple
from rest_framework import serializers
from uuid_utils.compat import uuid7

from api.models import Tenant, TenantAwareModel

logger = logging.getLogger(__name__)


class RoleV2(TenantAwareModel):
    """V2 Role model."""

    objects = RoleV2QuerySet.as_manager()

    class Types(models.TextChoices):
        CUSTOM = "custom"
        SEEDED = "seeded"
        PLATFORM = "platform"

    uuid = models.UUIDField(default=uuid7, editable=False, unique=True, null=False)
    name = models.CharField(max_length=175, null=False, blank=False)
    description = models.TextField(null=True, blank=True)
    type = models.CharField(
        choices=Types.choices, max_length=20, null=False, blank=False, db_index=True, default=Types.CUSTOM
    )
    permissions = models.ManyToManyField(Permission, related_name="v2_roles")
    children = models.ManyToManyField("self", related_name="parents", symmetrical=False)
    v1_source = models.ForeignKey(Role, null=True, blank=True, related_name="v2_roles", on_delete=models.CASCADE)
    created = models.DateTimeField(default=timezone.now)
    modified = AutoDateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name", "modified"]
        constraints = [
            models.UniqueConstraint(fields=["name", "tenant"], name="unique role v2 name per tenant"),
        ]
        indexes = [
            GinIndex(fields=["name"], name="rolev2_name_trgm_idx", opclasses=["gin_trgm_ops"]),
        ]

    def clean(self):
        """Validate required fields with domain exceptions."""
        super().clean()
        if not self.name or not self.name.strip():
            raise RequiredFieldError("name")

    def save(self, *args, **kwargs):
        """Save the model and run all validations from the model."""
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def org_id(self) -> Optional[str]:
        """Return the org_id for this role. None for seeded roles (public tenant)."""
        if self.tenant.tenant_name == Tenant.PUBLIC_TENANT_NAME:
            return None
        if self.tenant.org_id is None:
            logger.error("Non-public tenant %s has no org_id", self.tenant_id)
            return None
        return str(self.tenant.org_id)

    def as_migration_value(self) -> V2role:
        """Get the V2role representing to this role's daya."""
        if self.type == RoleV2.Types.PLATFORM:
            raise ValueError("V2roles are not supported for PLATFORM roles.")

        if self.type == RoleV2.Types.SEEDED:
            return V2role.for_system_role(id=str(self.uuid))

        if self.type == RoleV2.Types.CUSTOM:
            return V2role(
                id=str(self.uuid),
                is_system=False,
                permissions=frozenset(p.v2_string() for p in self.permissions.all()),
            )

        raise ValueError(f"Unexpected type of role: {self.type} for {self}")

    def v2_permissions(self) -> set[str]:
        """Get the set of V2 strings for the permissions of this role."""
        return set(p.v2_string() for p in self.permissions.all())

    @staticmethod
    def _permission_tuple(role: "RoleV2", permission: Permission) -> RelationTuple:
        return role_permission_tuple(role_id=str(role.uuid), permission=permission.v2_string())

    @staticmethod
    def _permission_and_owner_tuples(role: "RoleV2", cached_permissions: Optional[Iterable[Permission]]):
        tenant_resource_id = role.tenant.tenant_resource_id()

        if role.type == RoleV2.Types.CUSTOM:
            if tenant_resource_id is None:
                raise RuntimeError(
                    f"Expected custom roles to be created in a tenant with a valid resource ID; "
                    f"got tenant pk={role.tenant.pk!r}"
                )
        else:
            if tenant_resource_id is not None:
                raise RuntimeError(
                    f"Expected non-custom roles to be created in the public tenant "
                    f"(which doesn't have a valid resource ID); "
                    f"got tenant pk={role.tenant.pk!r}"
                )

        if cached_permissions is None:
            cached_permissions = role.permissions.all()

        tuples = list({RoleV2._permission_tuple(role, p) for p in cached_permissions})

        if tenant_resource_id is not None:
            tuples.append(role_owner_relationship(role_uuid=str(role.uuid), tenant_resource_id=tenant_resource_id))

        return tuples

    @staticmethod
    def tuples_for_update(
        role: "RoleV2",
        *,
        old_permissions: Iterable[Permission],
        new_permissions: Iterable[Permission],
    ) -> tuple[list[RelationTuple], list[RelationTuple]]:
        """Get the tuples that should be added and removed when updating a role.

        Args:
            role: The role being created, updated, or deleted.
            old_permissions: Permissions before mutation (empty for create).
            new_permissions: Permissions after mutation (empty for delete).

        Returns:
            ``(tuples_to_add, tuples_to_remove)`` ready for replication.
        """
        old_set = set(old_permissions)
        new_set = set(new_permissions)

        tuples_to_add = [RoleV2._permission_tuple(role, p) for p in new_set - old_set]
        tuples_to_remove = [RoleV2._permission_tuple(role, p) for p in old_set - new_set]

        return tuples_to_add, tuples_to_remove

    # Although create and delete happen to use the same tuples currently, they are semantically different operations.
    # We use separate functions for them in order to be clearer at the call site and to ensure they can diverge in
    # the future without having to update every call site.

    @staticmethod
    def tuples_for_create(
        role: "RoleV2", cached_permissions: Optional[Iterable[Permission]] = None
    ) -> list[RelationTuple]:
        """
        Get the tuples that should be added when creating a role.

        The role's tenant will be loaded (and thus should be preloaded for bulk operations).
        If cached_permissions is not provided, the permissions from the role are loaded.
        """
        return RoleV2._permission_and_owner_tuples(role=role, cached_permissions=cached_permissions)

    @staticmethod
    def tuples_for_delete(
        role: "RoleV2", cached_permissions: Optional[Iterable[Permission]] = None
    ) -> list[RelationTuple]:
        """
        Get the tuples that should be removed when deleting a role.

        The role's tenant will be loaded (and thus should be preloaded for bulk operations).
        If cached_permissions is not provided, the permissions from the role are loaded.
        """
        return RoleV2._permission_and_owner_tuples(role=role, cached_permissions=cached_permissions)


class TypedRoleV2Manager(models.Manager):
    """Manager for RoleV2 with a specific type."""

    _role_type: str

    def __init__(self, role_type: str):
        """Initialize the manager."""
        super().__init__()
        self._role_type = role_type

    def get_queryset(self):
        """Get the queryset for the specific type."""
        return super().get_queryset().filter(type=self._role_type)


class TypeValidatedRoleV2Mixin:
    """Mixin for role types that validates type on init and save."""

    _expected_type: str = ""

    def __init__(self, *args, **kwargs):
        """Initialize the model with type validation."""
        type_provided = "type" in kwargs
        provided_type = kwargs.get("type") if type_provided else None

        super().__init__(*args, **kwargs)

        if not self.pk:
            if type_provided and provided_type != self._expected_type:
                raise serializers.ValidationError(
                    f"Expected role to have type {self._expected_type}, but found {provided_type}"
                )
            else:
                self.type = self._expected_type
        elif self.type != self._expected_type:
            raise serializers.ValidationError(
                f"Expected role to have type {self._expected_type}, but found {self.type}"
            )

    def save(self, **kwargs):
        """Save the model with type validation."""
        if self.type and self.type != self._expected_type:
            raise serializers.ValidationError(
                f"Expected role to have type {self._expected_type}, but found {self.type}"
            )
        else:
            self.type = self._expected_type

        if (update_fields := kwargs.get("update_fields")) is not None:
            kwargs["update_fields"] = {"type", *update_fields}

        self.full_clean()
        super().save(**kwargs)


class CustomRoleV2(TypeValidatedRoleV2Mixin, RoleV2):
    """V2 Custom role model."""

    class Meta:
        proxy = True

    _expected_type = RoleV2.Types.CUSTOM
    objects = TypedRoleV2Manager(role_type=_expected_type)

    def update(self, name: str, description: str):
        """Update the role's mutable attributes."""
        self.name = name
        self.description = description


class SeededRoleV2(TypeValidatedRoleV2Mixin, RoleV2):
    """V2 Seeded role model."""

    class Meta:
        proxy = True

    _expected_type = RoleV2.Types.SEEDED
    objects = TypedRoleV2Manager(role_type=_expected_type)

    @classmethod
    def for_v1_roles(cls, roles: Iterable[Role]) -> set[SeededRoleV2]:
        """
        Retrieve the V2 equivalents of the provided V1 system roles.

        Fails if a custom role is provided or if a role cannot be found.
        """
        roles = set(roles)
        non_system_roles = {r for r in roles if not r.system}

        if non_system_roles:
            raise ValueError(
                f"Only system V1 roles have seeded V2 equivalents; found non-system roles: "
                f"{', '.join(f'pk={r.pk}' for r in non_system_roles)}"
            )

        v2_roles = set(cls.objects.filter(v1_source__in=roles).distinct())

        if len(roles) != len(v2_roles):
            missing = roles - {r.v1_source for r in v2_roles}

            if len(missing) == 0:
                raise AssertionError("Unable to determine missing V1 roles.")

            raise ValueError(
                f"Unable to find V2 roles for the following V1 roles: " f"{', '.join(f'pk={r.pk}' for r in missing)}"
            )

        return v2_roles


class PlatformRoleV2(TypeValidatedRoleV2Mixin, RoleV2):
    """V2 Platform role model."""

    class Meta:
        proxy = True

    _expected_type = RoleV2.Types.PLATFORM
    objects = TypedRoleV2Manager(role_type=_expected_type)


def validate_role_children_on_m2m_change(sender, instance, action, pk_set, **kwargs):
    """
    Signal handler to validate role children relationships on M2M changes.

    This validates BEFORE the M2M relationship is written to the database.
    """
    if action != "pre_add":
        return

    parent_type = instance.type

    if parent_type == RoleV2.Types.PLATFORM:
        if pk_set:
            invalid_children = RoleV2.objects.filter(pk__in=pk_set).exclude(type=RoleV2.Types.SEEDED)
            if invalid_children.exists():
                raise serializers.ValidationError(
                    {"children": "Platform roles can only have seeded roles as children."}
                )
    else:
        raise serializers.ValidationError({"children": f"{parent_type.capitalize()} roles cannot have children."})


# Connect the signal handler to the RoleV2.children through model
signals.m2m_changed.connect(validate_role_children_on_m2m_change, sender=RoleV2.children.through)
