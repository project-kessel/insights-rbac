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
"""Test the cross account request model."""

from api.models import CrossAccountRequest
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from management.models import Role
from api.models import Tenant
from rest_framework.serializers import ValidationError

from datetime import timedelta


class CrossAccountRequestModelTests(TestCase):
    """Test the cross account request model."""

    def setUp(self):
        """Set up the cross account request model tests."""
        super().setUp()
        self.tenant = Tenant.objects.create(tenant_name="foo", org_id="123321")

        self.ref_time = timezone.now()
        self.request = CrossAccountRequest.objects.create(
            target_org="654321", user_id="567890", end_date=self.ref_time + timedelta(10)
        )

    def tearDown(self):
        """Tear down cross account request model tests."""
        CrossAccountRequest.objects.all().delete()

    def test_request_creation_success(self):
        """Test the creation of cross account request."""
        self.assertEqual(self.request.target_org, "654321")
        self.assertEqual(self.request.user_id, "567890")
        self.assertIsNotNone(self.request.start_date)
        self.assertEqual(self.request.end_date, self.ref_time + timedelta(10))
        self.assertEqual(self.request.status, "pending")

    def test_request_creation_fail_without_target(self):
        """Test the creation of cross account request fail without target."""
        with transaction.atomic():
            self.assertRaises(
                IntegrityError,
                CrossAccountRequest.objects.create,
                user_id="567890",
                end_date=self.ref_time + timedelta(10),
            )

    def test_request_with_unknown_status_fail(self):
        """Test the creation of cross account request with unknown status fail."""
        # Can't create
        self.assertRaises(
            ValidationError,
            CrossAccountRequest.objects.create,
            target_org="654321",
            user_id="567890",
            end_date=self.ref_time + timedelta(10),
            status="unknown",
        )
        # Can't update
        self.request.status = "unknown"
        self.assertRaises(ValidationError, self.request.save)

    def test_request_with_invalid_date(self):
        """Test the start date must be earlier than end date."""
        # Can't create
        self.assertRaises(
            ValidationError,
            CrossAccountRequest.objects.create,
            target_org="654321",
            user_id="567890",
            end_date=self.ref_time - timedelta(10),
        )
        # Can't update
        self.request.end_date = self.ref_time - timedelta(10)
        self.assertRaises(ValidationError, self.request.save)

        # Omitted end date
        with transaction.atomic():
            self.assertRaises(
                IntegrityError,
                CrossAccountRequest.objects.create,
                target_org="654321",
                user_id="567890",
            )

        # End date earlier than now
        self.assertRaises(
            ValidationError,
            CrossAccountRequest.objects.create,
            target_org="7777777",
            user_id="567890",
            end_date=timezone.now() - timedelta(1),
        )

    def test_request_with_same_start_and_end_date(self):
        """Test the start date and end date can be the same."""
        self.assertEqual(CrossAccountRequest.objects.count(), 1)
        CrossAccountRequest.objects.create(
            target_org="1234", user_id="9876", start_date=self.ref_time, end_date=self.ref_time
        )
        self.assertEqual(CrossAccountRequest.objects.count(), 2)

    def test_the_request_could_be_associated_with_role(self):
        ROLE_NAME = "Test Role"
        role = Role.objects.create(name=ROLE_NAME, tenant=self.tenant)
        self.assertEqual(self.request.roles.count(), 0)
        self.assertEqual(role.cross_account_requests.count(), 0)

        # Add role
        self.request.roles.add(role)
        self.assertEqual(self.request.roles.count(), 1)
        self.assertEqual(self.request.roles.filter(name="Test Role").first(), role)
