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

"""Model for group management."""

import logging
from typing import Optional, Union
from uuid import uuid4

from django.conf import settings
from django.db import models
from django.db.models import signals
from django.utils import timezone
from internal.integration import chrome_handlers
from internal.integration import sync_handlers
from management.cache import AccessCache, skip_purging_cache_for_public_tenant
from management.principal.model import Principal
from management.rbac_fields import AutoDateTimeField
from management.relation_replicator.types import RelationTuple
from management.role.model import Role
from migration_tool.utils import create_relationship

from api.models import FilterQuerySet, TenantAwareModel, User

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class Group(TenantAwareModel):
    """A group."""

    uuid = models.UUIDField(default=uuid4, editable=False, unique=True, null=False)
    name = models.CharField(max_length=150)
    description = models.TextField(null=True)
    principals = models.ManyToManyField(Principal, related_name="group")
    created = models.DateTimeField(default=timezone.now)
    modified = AutoDateTimeField(default=timezone.now)
    platform_default = models.BooleanField(default=False)
    system = models.BooleanField(default=False)
    admin_default = models.BooleanField(default=False)
    objects = FilterQuerySet.as_manager()

    @staticmethod
    def relationship_to_principal_for_group(
        group: "Group", principal: Union[Principal, User]
    ) -> Optional[RelationTuple]:
        """Create a relationship between a group and a principal given a Principal or User."""
        if isinstance(principal, Principal):
            id = principal.principal_resource_id()
            if id is None:
                return None
        elif (user_id := principal.user_id) is not None:
            id = Principal.user_id_to_principal_resource_id(user_id)
        else:
            return None
        return create_relationship(("rbac", "group"), str(group.uuid), ("rbac", "principal"), id, "member")

    @staticmethod
    def relationship_to_user_id_for_group(group_uuid: str, user_id: str) -> RelationTuple:
        """Create a relationship between a group and a user ID."""
        id = Principal.user_id_to_principal_resource_id(user_id)
        return create_relationship(("rbac", "group"), group_uuid, ("rbac", "principal"), id, "member")

    def relationship_to_principal(self, principal: Union[Principal, User]) -> Optional[RelationTuple]:
        """Create a relationship between a group and a principal given a Principal or User."""
        return Group.relationship_to_principal_for_group(self, principal)

    def roles(self):
        """Roles for a group."""
        return Role.objects.filter(policies__in=self.__policy_ids()).distinct()

    def roles_with_access(self):
        """Queryset for roles with access data prefetched."""
        return self.roles().prefetch_related("access")

    def role_count(self):
        """Role count for a group."""
        return self.roles().count()

    def platform_default_set():
        """Queryset for platform default group."""
        return Group.objects.filter(platform_default=True)

    def admin_default_set():
        """Queryset for admin default group."""
        return Group.objects.filter(admin_default=True)

    def __policy_ids(self):
        """Policy IDs for a group."""
        return self.policies.values_list("id", flat=True)

    class Meta:
        ordering = ["name", "modified"]
        constraints = [models.UniqueConstraint(fields=["name", "tenant"], name="unique group name per tenant")]


def group_deleted_cache_handler(sender=None, instance=None, using=None, **kwargs):
    """Signal handler to purge principal caches when a Group is deleted."""
    if skip_purging_cache_for_public_tenant(instance.tenant):
        return
    logger.info("Handling signal for deleted group %s - invalidating policy cache for users in group", instance)
    cache = AccessCache(instance.tenant.org_id)
    for principal in instance.principals.all():
        cache.delete_policy(principal.uuid)


def principals_to_groups_cache_handler(
    sender=None, instance=None, action=None, reverse=None, model=None, pk_set=None, using=None, **kwargs
):
    """Signal handler to purge caches when Group membership changes."""
    if skip_purging_cache_for_public_tenant(instance.tenant):
        return
    cache = AccessCache(instance.tenant.org_id)
    if action in ("post_add", "pre_remove"):
        logger.info("Handling signal for %s group membership change - invalidating policy cache", instance)
        if isinstance(instance, Group):
            # One or more principals was added to/removed from the group
            for principal in Principal.objects.filter(pk__in=pk_set):
                cache.delete_policy(principal.uuid)
        elif isinstance(instance, Principal):
            # One or more groups was added to/removed from the principal
            cache.delete_policy(instance.uuid)
    elif action == "pre_clear":
        logger.info("Handling signal for %s group membership clearing - invalidating policy cache", instance)
        if isinstance(instance, Group):
            # All principals are being removed from this group
            for principal in instance.principals.all():
                cache.delete_policy(principal.uuid)
        elif isinstance(instance, Principal):
            # All groups are being removed from this principal
            cache.delete_policy(instance.uuid)


def group_deleted_chrome_handler(sender=None, instance=None, using=None, **kwargs):
    """Signal handler to inform external services of Group deletions."""
    logger.info("Handling signal for deleted group %s - informing chrome topic", instance)
    if hasattr(instance, "tenant") and hasattr(instance.tenant, "org_id"):
        chrome_handlers.send_chrome_message(event_type="delete", uuid=instance.uuid, org_id=instance.tenant.org_id)


def group_create_and_update_chrome_handler(sender=None, instance=None, using=None, **kwargs):
    """Signal handler to inform external services of Group creations and updates."""
    is_org_id = hasattr(instance, "tenant") and hasattr(instance.tenant, "org_id")
    if isinstance(kwargs, dict) and "created" in kwargs and is_org_id:
        event_type = "update"
        if kwargs["created"]:
            event_type = "create"
        logger.info("Handling signal for %s group %s - informing chrome topic", event_type, instance)
        chrome_handlers.send_chrome_message(event_type=event_type, uuid=instance.uuid, org_id=instance.tenant.org_id)


def group_deleted_sync_handler(sender=None, instance=None, using=None, **kwargs):
    """Signal handler to inform external services of Group deletions."""
    logger.info("Handling signal for deleted group %s - informing sync topic", instance)
    sync_handlers.send_sync_message(
        event_type="group_deleted", payload={"group": {"name": instance.name, "uuid": str(instance.uuid)}}
    )


def group_created_sync_handler(sender=None, instance=None, using=None, **kwargs):
    """Signal handler to inform external services of Group creations."""
    if isinstance(kwargs, dict) and "created" in kwargs and kwargs["created"]:
        sync_handlers.send_sync_message(
            event_type="group_created", payload={"group": {"name": instance.name, "uuid": str(instance.uuid)}}
        )


def principal_group_change_sync_handler(
    sender=None, instance=None, action=None, reverse=None, model=None, pk_set=None, using=None, **kwargs
):
    """Signal handler to inform external services of Group membership changes."""
    logger.info("Handling signal for group %s membership change - informing sync topic", instance)

    if action in ["pre_remove", "post_remove"] and isinstance(instance, Group):
        if instance.tenant is not None:
            org_id = instance.tenant.org_id if hasattr(instance.tenant, "org_id") else None
            account_id = instance.tenant.account_id if hasattr(instance.tenant, "account_id") else None
            logger.info("Action %s for group: %s, OrgId: %s, AcctId: %s", action, instance.name, org_id, account_id)

    if action in ["post_add", "pre_remove", "pre_clear"]:
        if isinstance(instance, Group):
            sync_handlers.send_sync_message(
                event_type="group_membership_changed",
                payload={
                    "group": {"name": instance.name, "uuid": str(instance.uuid)},
                    "action": action.split("_")[-1],
                },
            )
        elif isinstance(instance, Principal):
            groups = instance.group.all()
            for group in groups:
                sync_handlers.send_sync_message(
                    event_type="group_membership_changed",
                    payload={"group": {"name": group.name, "uuid": str(group.uuid)}, "action": action.split("_")[-1]},
                )


if settings.ACCESS_CACHE_ENABLED and settings.ACCESS_CACHE_CONNECT_SIGNALS:
    signals.pre_delete.connect(group_deleted_cache_handler, sender=Group)
    signals.m2m_changed.connect(principals_to_groups_cache_handler, sender=Group.principals.through)

if settings.KAFKA_ENABLED:
    signals.pre_delete.connect(group_deleted_sync_handler, sender=Group)
    signals.m2m_changed.connect(principal_group_change_sync_handler, sender=Group.principals.through)
    signals.post_save.connect(group_created_sync_handler, sender=Group)
    signals.pre_delete.connect(group_deleted_chrome_handler, sender=Group)
    signals.post_save.connect(group_create_and_update_chrome_handler, sender=Group)
