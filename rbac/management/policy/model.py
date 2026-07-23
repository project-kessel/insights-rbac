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

"""Model for policy management."""

import logging
from uuid import uuid4

from django.conf import settings
from django.db import models
from django.db.models import signals
from django.utils import timezone
from internal.integration import sync_handlers
from management.cache import AccessCache, skip_purging_cache_for_public_tenant
from management.group.model import Group
from management.principal.model import Principal
from management.rbac_fields import AutoDateTimeField
from management.role.model import Role

from api.models import TenantAwareModel

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class Policy(TenantAwareModel):
    """A policy."""

    uuid = models.UUIDField(default=uuid4, editable=False, unique=True, null=False)
    name = models.CharField(max_length=150)
    description = models.TextField(null=True)
    group = models.ForeignKey(Group, null=True, on_delete=models.CASCADE, related_name="policies")
    roles = models.ManyToManyField(Role, related_name="policies")
    system = models.BooleanField(default=False)
    created = models.DateTimeField(default=timezone.now)
    modified = AutoDateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name", "modified"]
        constraints = [models.UniqueConstraint(fields=["name", "tenant"], name="unique policy name per tenant")]


def policy_changed_cache_handler(sender=None, instance=None, using=None, **kwargs):
    """Signal handler for Principal cache expiry on Policy deletion."""
    if skip_purging_cache_for_public_tenant(instance.tenant):
        return
    logger.info("Handling signal for deleted policy %s - invalidating associated user cache keys", instance)
    cache = AccessCache(instance.tenant.org_id)
    if instance.group:
        principals = instance.group.principals.all()
        if instance.group.platform_default:
            cache.delete_all_policies_for_tenant()
        for principal in principals:
            cache.delete_policy(principal.uuid)


def policy_to_roles_cache_handler(
    sender=None, instance=None, action=None, reverse=None, model=None, pk_set=None, using=None, **kwargs  # noqa: C901
):
    """Signal handler for Principal cache expiry on Policy/Role m2m change."""
    if skip_purging_cache_for_public_tenant(instance.tenant):
        return
    cache = AccessCache(instance.tenant.org_id)
    if action in ("post_add", "pre_remove"):
        logger.info("Handling signal for %s roles change - invalidating policy cache", instance)
        if isinstance(instance, Policy):
            # One or more roles was added to/removed from the policy
            if instance.group:
                if instance.group.platform_default:
                    cache.delete_all_policies_for_tenant()
                for principal in instance.group.principals.all():
                    cache.delete_policy(principal.uuid)
        elif isinstance(instance, Role):
            # One or more policies was added to/removed from the role
            for policy in Policy.objects.filter(pk__in=pk_set):
                if policy.group:
                    if policy.group.platform_default:
                        cache.delete_all_policies_for_tenant()
                    for principal in policy.group.principals.all():
                        cache.delete_policy(principal.uuid)
    elif action == "pre_clear":
        logger.info("Handling signal for %s policy-roles clearing - invalidating policy cache", instance)
        if isinstance(instance, Policy):
            # All roles are being removed from this policy
            if instance.group:
                if instance.group.platform_default:
                    cache.delete_all_policies_for_tenant()
                for principal in instance.group.principals.all():
                    cache.delete_policy(principal.uuid)
        elif isinstance(instance, Role):
            # All policies are being removed from this role
            for principal in Principal.objects.filter(group__policies__roles__pk=instance.pk):
                cache.delete_policy(principal.uuid)


def policy_changed_sync_handler(sender=None, instance=None, using=None, **kwargs):
    """Signal handler for Policy change syncs."""
    logger.info("Handling signal for altered policy %s - informing sync topic", instance)
    if instance.group:
        if instance.group.platform_default:
            sync_handlers.send_sync_message(
                event_type="platform_default_group_changed",
                payload={"group": {"name": instance.group.name, "uuid": str(instance.group.uuid)}},
            )
        else:
            sync_handlers.send_sync_message(
                event_type="non_default_group_relations_changed",
                payload={"group": {"name": instance.group.name, "uuid": str(instance.group.uuid)}},
            )


def policy_to_roles_sync_handler(
    sender=None, instance=None, action=None, reverse=None, model=None, pk_set=None, using=None, **kwargs  # noqa: C901
):
    """Signal handler for Principal cache expiry on Policy/Role m2m change."""
    if action in ("post_add", "pre_remove"):
        logger.info("Handling signal for %s roles change - informing sync topic", instance)
        if isinstance(instance, Policy):
            # One or more roles was added to/removed from the policy
            if instance.group:
                if instance.group.platform_default:
                    sync_handlers.send_sync_message(
                        event_type="platform_default_group_changed",
                        payload={"group": {"name": instance.group.name, "uuid": str(instance.group.uuid)}},
                    )
                else:
                    sync_handlers.send_sync_message(
                        event_type="non_default_group_relations_changed",
                        payload={"group": {"name": instance.group.name, "uuid": str(instance.group.uuid)}},
                    )
        elif isinstance(instance, Role):
            # One or more policies was added to/removed from the role
            sync_handlers.send_sync_message(
                event_type="role_modified", payload={"role": {"name": instance.name, "uuid": str(instance.uuid)}}
            )
    elif action == "pre_clear":
        logger.info("Handling signal for %s policy-roles clearing - informing sync topic", instance)
        if isinstance(instance, Policy):
            # All roles are being removed from this policy
            if instance.group:
                if instance.group.platform_default:
                    sync_handlers.send_sync_message(
                        event_type="platform_default_group_changed",
                        payload={"group": {"name": instance.group.name, "uuid": str(instance.group.uuid)}},
                    )
                else:
                    sync_handlers.send_sync_message(
                        event_type="non_default_group_relations_changed",
                        payload={"group": {"name": instance.group.name, "uuid": str(instance.group.uuid)}},
                    )
        elif isinstance(instance, Role):
            # All policies are being removed from this role
            sync_handlers.send_sync_message(
                event_type="role_modified", payload={"role": {"name": instance.name, "uuid": str(instance.uuid)}}
            )


if settings.ACCESS_CACHE_ENABLED and settings.ACCESS_CACHE_CONNECT_SIGNALS:
    signals.post_save.connect(policy_changed_cache_handler, sender=Policy)
    signals.pre_delete.connect(policy_changed_cache_handler, sender=Policy)
    signals.m2m_changed.connect(policy_to_roles_cache_handler, sender=Policy.roles.through)

if settings.KAFKA_ENABLED:
    signals.post_save.connect(policy_changed_sync_handler, sender=Policy)
    signals.pre_delete.connect(policy_changed_sync_handler, sender=Policy)
    signals.m2m_changed.connect(policy_to_roles_sync_handler, sender=Policy.roles.through)
