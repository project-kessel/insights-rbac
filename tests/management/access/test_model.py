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
"""Test the group model."""

from django.test import TestCase

# from unittest.mock import Mock

from management.models import Access, Permission
from tests.identity_request import IdentityRequest


class AccessModelTests(IdentityRequest):
    """Test the access model."""

    def setUp(self):
        """Set up the access model tests."""
        super().setUp()

        self.permission = Permission.objects.create(permission="app:*:*", tenant=self.tenant)
        self.access = Access.objects.create(permission=self.permission, tenant=self.tenant)

    def tearDown(self):
        """Tear down access model tests."""
        Access.objects.all().delete()

    def test_permission_application(self):
        """Test we get back the application name of the permission."""
        self.assertEqual(self.access.permission_application(), "app")

    def test_perm_and_permission_are_synced(self):
        """Test the permission field is populated when creating Access."""
        self.assertEqual(self.access.permission.permission, "app:*:*")
