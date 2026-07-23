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

from management.models import Group, Role, Policy
from tests.identity_request import IdentityRequest


class GroupModelTests(IdentityRequest):
    """Test the group model."""

    def setUp(self):
        """Set up the group model tests."""
        super().setUp()

        self.group = Group.objects.create(name="groupA", tenant=self.tenant)
        self.roleA = Role.objects.create(name="roleA", tenant=self.tenant)
        self.roleB = Role.objects.create(name="roleB", tenant=self.tenant)
        self.policy = Policy(name="policyA", group=self.group, tenant=self.tenant)
        self.policy.save()
        self.policy.roles.add(self.roleA)
        self.policy.save()
        self.group.policies.add(self.policy)
        self.group.save()

    def tearDown(self):
        """Tear down group model tests."""
        Group.objects.all().delete()
        Policy.objects.all().delete()
        Role.objects.all().delete()

    def test_roles_for_group(self):
        """Test that we can get roles for a group."""
        self.assertEqual(list(self.group.roles()), [self.roleA])

    def test_role_count_for_group(self):
        """Test the role count for a group."""
        self.assertEqual(self.group.role_count(), 1)
