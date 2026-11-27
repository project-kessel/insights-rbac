#
# Copyright 2026 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#

"""Durable progress for one-time tenant backfills."""

from django.db import models
from django.utils import timezone
from uuid_utils.compat import uuid7


class TenantBackfillProgress(models.Model):
    """The last consumer-acknowledged tenant in a global, ordered backfill."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    job_name = models.CharField(max_length=100, unique=True)
    last_tenant_id = models.BigIntegerField(default=0)
    processed_count = models.BigIntegerField(default=0)
    upper_tenant_id = models.BigIntegerField(default=0)
    total_count = models.BigIntegerField(default=0)
    updated_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True)
