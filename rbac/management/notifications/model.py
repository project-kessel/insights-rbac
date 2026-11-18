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
"""Models for queued Red Hat system notifications."""

from django.db import models
from django.db.models import Q
from django.utils import timezone
from uuid_utils.compat import uuid7


class RhNotificationDelivery(models.Model):
    """
    One fan-out of queued Red Hat notification events to every ready tenant.

    Not tenant-aware: Red Hat changes are global and are sent to all ready tenants. The tenant id cursor records the
    last tenant that received every event of the delivery.
    """

    class Status(models.TextChoices):
        ACTIVE = "active"
        DONE = "done"
        FAILED = "failed"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    after_tenant_id = models.BigIntegerField(default=0)
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    created = models.DateTimeField(default=timezone.now)
    last_batch_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["status"],
                condition=Q(status="active"),
                name="single active rh notification delivery",
            )
        ]


class RhNotificationEvent(models.Model):
    """A Red Hat notification event, saved in the same transaction as the change it reports."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event_type = models.CharField(max_length=255)
    payload = models.JSONField()
    created = models.DateTimeField(default=timezone.now)
    delivery = models.ForeignKey(RhNotificationDelivery, null=True, on_delete=models.CASCADE, related_name="events")

    class Meta:
        indexes = [models.Index(fields=["delivery", "created"], name="rh_notif_event_delivery_idx")]
