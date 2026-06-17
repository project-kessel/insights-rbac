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

"""Models to store cross account access request."""

import datetime
from uuid import uuid4

from django.db import models
from django.utils import timezone
from management.rbac_fields import AutoDateTimeField
from management.role.user_source import SourceKey
from rest_framework.serializers import ValidationError

STATUS_LIST = ["pending", "cancelled", "approved", "denied", "expired"]


class CrossAccountRequest(models.Model):
    """Cross account access request."""

    STATUS_APPROVED = "approved"

    request_id = models.UUIDField(default=uuid4, editable=False, unique=True, null=False, primary_key=True)
    target_account = models.CharField(max_length=36, default=None, null=True)
    target_org = models.CharField(max_length=36, default=None)
    user_id = models.CharField(max_length=15, default=None)
    created = models.DateTimeField(default=timezone.now)
    start_date = models.DateTimeField(default=timezone.now)
    end_date = models.DateTimeField(null=False, blank=False, default=None)
    modified = AutoDateTimeField(default=timezone.now)
    status = models.CharField(max_length=10, default="pending")
    roles = models.ManyToManyField("management.Role", through="RequestsRoles")

    def validate_date(self, date):
        """Validate that end dates are not in the past."""
        if isinstance(date, datetime.datetime) and date.date() < timezone.now().date():
            raise ValidationError("Please verify the end dates are not in the past.")

    def validate_input_value(self):
        """Validate status is valid, and date is valid."""
        if self.status not in STATUS_LIST:
            raise ValidationError(f'Unknown status "{self.status}" specified, {STATUS_LIST} are valid inputs.')

        if self.status != "expired":
            self.validate_date(self.end_date)

        if isinstance(self.end_date, datetime.datetime) and isinstance(self.start_date, datetime.datetime):
            if self.start_date.date() > (datetime.datetime.now() + datetime.timedelta(60)).date():
                raise ValidationError("Start date must be within 60 days of today.")

            if self.start_date > self.end_date:
                raise ValidationError("Start date must be earlier than end date.")

            if self.end_date - self.start_date > datetime.timedelta(365):
                raise ValidationError("Access duration may not be longer than one year.")

    def save(self, *args, **kwargs):
        """Override save method to validate some input."""
        self.validate_input_value()

        super(CrossAccountRequest, self).save(*args, **kwargs)

    def source_key(self) -> SourceKey:
        """Return the SourceKey for this cross-account request, for use with a BindingMapping."""
        return SourceKey(self, str(self.request_id))


class RequestsRoles(models.Model):
    """Model to associate the cross account access request and role."""

    cross_account_request = models.ForeignKey(CrossAccountRequest, on_delete=models.CASCADE)
    role = models.ForeignKey(
        "management.Role", on_delete=models.CASCADE, to_field="uuid", related_name="cross_account_requests"
    )
