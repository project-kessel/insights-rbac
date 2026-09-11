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

from api.models import Tenant
from management.models import CustomRoleV2, Group, Policy, Role, RoleBinding, RoleBindingGroup
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

        # V2 role bindings for role_count tests
        self.v2_role_a = CustomRoleV2.objects.create(name="v2RoleA", tenant=self.tenant)
        self.v2_role_b = CustomRoleV2.objects.create(name="v2RoleB", tenant=self.tenant)
        self.binding_a = RoleBinding.objects.create(
            tenant=self.tenant, role=self.v2_role_a, resource_type="workspace", resource_id="ws-1"
        )
        RoleBindingGroup.objects.create(group=self.group, binding=self.binding_a)

    def tearDown(self):
        """Tear down group model tests."""
        RoleBindingGroup.objects.all().delete()
        RoleBinding.objects.all().delete()
        CustomRoleV2.objects.all().delete()
        Group.objects.all().delete()
        Policy.objects.all().delete()
        Role.objects.all().delete()

    def test_roles_for_group(self):
        """Test that we can get roles for a group."""
        self.assertEqual(list(self.group.roles()), [self.roleA])

    def test_role_count_for_group(self):
        """Test the role count for a group derives from RoleBindingGroup."""
        self.assertEqual(self.group.role_count(), 1)

    def test_role_count_multiple_roles(self):
        """Test role count with multiple distinct roles."""
        binding_b = RoleBinding.objects.create(
            tenant=self.tenant, role=self.v2_role_b, resource_type="workspace", resource_id="ws-2"
        )
        RoleBindingGroup.objects.create(group=self.group, binding=binding_b)
        self.assertEqual(self.group.role_count(), 2)

    def test_role_count_deduplicates_same_role(self):
        """Test role count deduplicates when same role is bound at multiple resources."""
        binding_dup = RoleBinding.objects.create(
            tenant=self.tenant, role=self.v2_role_a, resource_type="workspace", resource_id="ws-other"
        )
        RoleBindingGroup.objects.create(group=self.group, binding=binding_dup)
        # Same role bound at two resources → still counts as 1
        self.assertEqual(self.group.role_count(), 1)

    def test_role_count_no_bindings(self):
        """Test role count is zero when group has no role binding entries."""
        empty_group = Group.objects.create(name="emptyGroup", tenant=self.tenant)
        self.assertEqual(empty_group.role_count(), 0)

    def test_role_count_excludes_cross_tenant_bindings(self):
        """Test role count excludes bindings from a different tenant."""
        other_tenant = Tenant.objects.create(
            tenant_name="acctOther", account_id="other-acct", org_id="other-org", ready=True
        )
        other_role = CustomRoleV2.objects.create(name="otherRole", tenant=other_tenant)
        cross_binding = RoleBinding.objects.create(
            tenant=other_tenant, role=other_role, resource_type="workspace", resource_id="ws-cross"
        )
        # Link cross-tenant binding to our group
        RoleBindingGroup.objects.create(group=self.group, binding=cross_binding)
        # Should only count same-tenant binding (1), not the cross-tenant one
        self.assertEqual(self.group.role_count(), 1)
