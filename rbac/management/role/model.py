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

"""Model for role management."""

import logging
from typing import Optional, TYPE_CHECKING, Union
from uuid import uuid4

from django.conf import settings
from django.db import models
from django.db.models import signals
from django.utils import timezone
from internal.integration import sync_handlers
from management.cache import AccessCache, skip_purging_cache_for_public_tenant
from management.models import Permission, Principal
from management.rbac_fields import AutoDateTimeField
from management.relation_replicator.types import RelationTuple
from management.role.user_source import SourceKey
from migration_tool.models import (
    V2boundresource,
    V2role,
    V2rolebinding,
    role_binding_group_subject_tuple,
    role_binding_user_subject_tuple,
)

from api.models import FilterQuerySet, TenantAwareModel

if TYPE_CHECKING:
    from management.role.v2_model import RoleV2

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class Role(TenantAwareModel):
    """A role."""

    uuid = models.UUIDField(default=uuid4, editable=False, unique=True, null=False)
    name = models.CharField(max_length=150)
    display_name = models.CharField(default="", max_length=150)
    description = models.TextField(null=True)
    system = models.BooleanField(default=False)
    platform_default = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    created = models.DateTimeField(default=timezone.now)
    modified = AutoDateTimeField(default=timezone.now)
    admin_default = models.BooleanField(default=False)
    objects = FilterQuerySet.as_manager()

    @property
    def role(self):
        """Get role for self."""
        return self

    class Meta:
        ordering = ["name", "modified"]
        constraints = [
            models.UniqueConstraint(fields=["name", "tenant"], name="unique role name per tenant"),
            models.UniqueConstraint(fields=["display_name", "tenant"], name="unique role display name per tenant"),
        ]

    def save(self, *args, **kwargs):
        """Ensure that display_name is populated on save."""
        if not self.display_name:
            self.display_name = self.name
        super(Role, self).save(*args, **kwargs)

    def external_role_id(self):
        """Return external role id."""
        return self.ext_relation.ext_id if hasattr(self, "ext_relation") else None

    def external_tenant_name(self):
        """Return external tenant name."""
        return self.ext_relation.ext_tenant.name if hasattr(self, "ext_relation") else None


class Access(TenantAwareModel):
    """An access object."""

    permission = models.ForeignKey(Permission, null=True, on_delete=models.CASCADE, related_name="accesses")
    role = models.ForeignKey(Role, null=True, on_delete=models.CASCADE, related_name="access")

    def permission_application(self):
        """Return the application name from the permission."""
        return self.permission.application


class ResourceDefinition(TenantAwareModel):
    """A resource definition."""

    attributeFilter = models.JSONField(default=dict)
    access = models.ForeignKey(Access, null=True, on_delete=models.CASCADE, related_name="resourceDefinitions")

    @property
    def role(self):
        """Get role for RD."""
        if self.access:
            return self.access.role

    @property
    def application(self):
        """Get the corresponding application."""
        if self.access and self.access.permission:
            return self.access.permission.application

    @property
    def resource_type(self):
        """Get the corresponding resource type."""
        if self.access and self.access.permission:
            return self.access.permission.resource_type

    @property
    def tenant_id(self):
        """Get the tenant_id of the RD."""
        if self.tenant:
            return self.tenant.id


class ExtTenant(models.Model):
    """External tenant."""

    name = models.CharField(max_length=64, null=False, unique=True)


class ExtRoleRelation(models.Model):
    """External relation info of role."""

    ext_tenant = models.ForeignKey(ExtTenant, null=True, on_delete=models.CASCADE, related_name="ext_role_relation")
    ext_id = models.CharField(max_length=64, null=False)
    role = models.OneToOneField(Role, on_delete=models.CASCADE, null=False, related_name="ext_relation")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["ext_tenant", "ext_id"], name="unique external id per external tenant")
        ]


class BindingMapping(models.Model):
    """V2 binding Mapping definition."""

    # JSON encoding of migration_tool.models.V2rolebinding
    mappings = models.JSONField(default=dict)
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="binding_mappings")
    v2_role = models.ForeignKey(
        "management.RoleV2",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="binding_mappings",
    )
    resource_type_namespace = models.CharField(max_length=256, null=False)
    resource_type_name = models.CharField(max_length=256, null=False)
    resource_id = models.CharField(max_length=256, null=False)

    class Meta:
        indexes = [
            models.Index(
                fields=["role", "resource_type_namespace", "resource_type_name", "resource_id"],
                name="bm_role_resource_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["v2_role", "resource_type_namespace", "resource_type_name", "resource_id"],
                name="unique_bindingmapping_v2role_resource",
            ),
        ]

    def save(self, *args, **kwargs):
        """Validate and save this BindingMapping."""
        users = self.mappings.get("users", None)

        if (users is not None) and not isinstance(users, dict):
            raise TypeError("users must be a dict. Support for representing users as a list has been removed.")

        super().save(*args, **kwargs)

    @classmethod
    def for_role_binding(cls, role_binding: V2rolebinding, v1_role: Union[Role, str], v2_role: Optional["RoleV2"]):
        """Create a new BindingMapping for a V2rolebinding."""
        mappings = role_binding.as_minimal_dict()
        resource = role_binding.resource
        resource_type_namespace = resource.resource_type[0]
        resource_type_name = resource.resource_type[1]
        resource_id = resource.resource_id
        role_arg: dict[str, Union[Role, str]] = (
            {"role": v1_role} if isinstance(v1_role, Role) else {"role_id": v1_role}
        )
        return cls(
            mappings=mappings,
            **role_arg,
            v2_role=v2_role,
            resource_type_namespace=resource_type_namespace,
            resource_type_name=resource_type_name,
            resource_id=resource_id,
        )

    def as_tuples(self) -> list[RelationTuple]:
        """Create tuples from BindingMapping model."""
        v2_role_binding = self.get_role_binding()
        return v2_role_binding.as_tuples()

    def is_unassigned(self):
        """Return true if mapping is not assigned to any groups or users."""
        return len(self.mappings.get("groups", [])) == 0 and len(self.mappings.get("users", {})) == 0

    def unassign_group(self, group_uuid) -> Optional[RelationTuple]:
        """
        Completely unassign this group from the mapping, even if it is assigned more than once.

        Returns the Relationship for this Group.
        """
        relationship = None
        while True:
            relationship = self.pop_group_from_bindings(group_uuid)
            if relationship is not None:
                break
        return relationship

    def pop_group_from_bindings(self, group_uuid: str) -> Optional[RelationTuple]:
        """
        Pop the group from mappings.

        The group may still be bound to the role in other ways, so the group may still be included in the binding
        more than once after this method returns.

        If the group is no longer assigned at all, the Relationship is returned to be removed.

        If you wish to remove the group entirely (and know it is safe to do so!), use [unassign_group].
        """
        if group_uuid in self.mappings["groups"]:
            self.mappings["groups"].remove(group_uuid)
        if group_uuid in self.mappings["groups"]:
            return None
        return role_binding_group_subject_tuple(self.mappings["id"], group_uuid)

    def assign_group_to_bindings(self, group_uuid: str) -> Optional[RelationTuple]:
        """
        Assign group to mappings.

        If the group entry already exists, skip it.
        """
        if group_uuid in self.mappings["groups"]:
            return None
        self.mappings["groups"].append(group_uuid)
        return role_binding_group_subject_tuple(self.mappings["id"], group_uuid)

    # TODO: This can be deleted after the migration
    def add_group_to_bindings(self, group_uuid: str) -> RelationTuple:
        """
        Add group to mappings.

        This adds an additional entry for the group, even if the group is already assigned, to account for multiple
        possible sources that may have assigned the group for the same role and resource.
        """
        self.mappings["groups"].append(group_uuid)
        return role_binding_group_subject_tuple(self.mappings["id"], group_uuid)

    def unassign_user_from_bindings(self, user_id: str, source: SourceKey) -> Optional[RelationTuple]:
        """Unassign user from mappings."""
        self._require_source(source)
        self.mappings["users"].pop(str(source), None)
        users_list = self.mappings["users"].values()
        if user_id in users_list:
            logging.info(
                f"[Dual Write] user {user_id} still in mappings of bindingmapping {self.pk}, "
                "therefore, no relation to remove. "
            )
            return None
        return role_binding_user_subject_tuple(self.mappings["id"], user_id)

    def assign_user_to_bindings(self, user_id: str, source: SourceKey) -> RelationTuple:
        """Assign user to mappings."""
        self._require_source(source)
        self.mappings["users"][str(source)] = user_id
        return role_binding_user_subject_tuple(self.mappings["id"], user_id)

    def update_mappings_from_role_binding(self, role_binding: V2rolebinding):
        """Set mappings."""
        # Validate resource and v1 role match
        resource = role_binding.resource
        if (
            resource.resource_type[0] != self.resource_type_namespace
            or resource.resource_type[1] != self.resource_type_name
            or resource.resource_id != self.resource_id
        ):
            raise Exception(
                "Resource mismatch."
                f"Expected: {self.resource_type_namespace}:{self.resource_type_name}:{self.resource_id} "
                f"but got: {resource.resource_type[0]}:{resource.resource_type[1]}:{resource.resource_id} "
            )

        self.mappings = role_binding.as_minimal_dict()

    def get_role_binding(self) -> V2rolebinding:
        """Get role binding."""
        args = {**self.mappings}
        args["resource"] = V2boundresource(
            resource_type=(self.resource_type_namespace, self.resource_type_name), resource_id=self.resource_id
        )
        args["role"] = V2role(
            id=args["role"]["id"],
            is_system=args["role"]["is_system"],
            permissions=frozenset(args["role"]["permissions"]),
        )
        return V2rolebinding(**args)

    @staticmethod
    def _require_source(source) -> SourceKey:
        if not isinstance(source, SourceKey):
            raise TypeError(f"Expected SourceKey, but got: {source!r}")

        return source


def role_related_obj_change_cache_handler(sender=None, instance=None, using=None, **kwargs):
    """Signal handler for invalidating Principal cache on Role object change."""
    if skip_purging_cache_for_public_tenant(instance.tenant):
        return
    logger.info(
        "Handling signal for added/removed/changed role-related object %s - "
        "invalidating associated user cache keys",
        instance,
    )
    cache = AccessCache(instance.tenant.org_id)
    if instance.role:
        for principal in Principal.objects.filter(group__policies__roles__pk=instance.role.pk):
            cache.delete_policy(principal.uuid)


def role_related_obj_change_sync_handler(sender=None, instance=None, using=None, **kwargs):
    """Signal handler for informing external sync of Role object changes."""
    logger.info(
        "Handling signal for added/removed/changed role-related object %s - " "informing sync topic",
        instance,
    )
    if instance.role:
        sync_handlers.send_sync_message(
            event_type="role_modified", payload={"role": {"name": instance.role.name, "uuid": str(instance.role.uuid)}}
        )


if settings.ACCESS_CACHE_ENABLED and settings.ACCESS_CACHE_CONNECT_SIGNALS:
    signals.pre_delete.connect(role_related_obj_change_cache_handler, sender=Role)
    signals.pre_delete.connect(role_related_obj_change_cache_handler, sender=Access)
    signals.pre_delete.connect(role_related_obj_change_cache_handler, sender=ResourceDefinition)
    signals.post_save.connect(role_related_obj_change_cache_handler, sender=Role)
    signals.post_save.connect(role_related_obj_change_cache_handler, sender=Access)
    signals.post_save.connect(role_related_obj_change_cache_handler, sender=ResourceDefinition)

if settings.KAFKA_ENABLED:
    signals.pre_delete.connect(role_related_obj_change_sync_handler, sender=Role)
    signals.pre_delete.connect(role_related_obj_change_sync_handler, sender=Access)
    signals.pre_delete.connect(role_related_obj_change_sync_handler, sender=ResourceDefinition)
    signals.post_save.connect(role_related_obj_change_sync_handler, sender=Role)
    signals.post_save.connect(role_related_obj_change_sync_handler, sender=Access)
    signals.post_save.connect(role_related_obj_change_sync_handler, sender=ResourceDefinition)
