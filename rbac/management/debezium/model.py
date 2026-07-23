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

"""Debezium model for outbox messages."""

from uuid import uuid4

from django.db import models


class Outbox(models.Model):
    """Outbox Table for Debezium."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    aggregatetype = models.CharField(max_length=255)
    aggregateid = models.CharField(max_length=255)
    event_type = models.CharField(max_length=255, db_column="type")
    payload = models.JSONField()
