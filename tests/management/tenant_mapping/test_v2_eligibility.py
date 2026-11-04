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
"""Tests for V2 eligibility."""

from django.test import TestCase, override_settings
from management.group.model import Group
from management.tenant_mapping.v2_activation import set_v2_opt_in_state
from management.tenant_mapping.v2_eligibility import (
    OptInEligibleState,
    OptInIneligibleRole,
    OptInIneligibleState,
    check_v2_eligibility,
)
from tests.management.role.test_dual_write import RbacFixture
from tests.v2_util import bootstrap_tenant_for_v2_test

from api.models import Tenant


@override_settings(ATOMIC_RETRY_DISABLED=True)
@override_settings(V2_MIGRATION_APP_EXCLUDE_LIST=["ineligible", "bad"])
class V2EligibilityTestCase(TestCase):
    def setUp(self):
        super().setUp()

        self.fixture = RbacFixture()
        self.tenant = self.fixture.new_tenant("some-org").tenant

    def test_eligible(self):
        group, _ = self.fixture.new_group("g", self.tenant, ["p1"])

        system_role = self.fixture.new_system_role("system", ["app:*:*"])
        self.fixture.add_role_to_group(system_role, group)

        self.fixture.new_custom_role("eligible", self.fixture.workspace_access(["app:*:*"]), self.tenant)

        self.assertIsInstance(check_v2_eligibility(self.tenant), OptInEligibleState)

    def test_eligible_opted_in(self):
        # Some tenants will have opted-in while having ineligible roles. We should still say they are eligible to opt in
        # again, since opting in is idempotent.
        self.fixture.new_custom_role("custom", self.fixture.workspace_access(["ineligible:*:*"]), self.tenant)
        set_v2_opt_in_state(self.tenant, True)

        self.assertIsInstance(check_v2_eligibility(self.tenant), OptInEligibleState)

    def test_ineligible_not_bootstrapped(self):
        new_tenant = self.fixture.new_unbootstrapped_tenant("new-org")

        result = check_v2_eligibility(new_tenant)

        self.assertIsInstance(result, OptInIneligibleState)
        self.assertFalse(result.bootstrapped)
        self.assertCountEqual([], result.ineligible_groups)
        self.assertCountEqual([], result.ineligible_custom_roles)

    def test_ineligible_roles(self):
        self.fixture.new_custom_role("eligible", self.fixture.workspace_access(["app:*:*"]), self.tenant)

        ineligible_a = self.fixture.new_custom_role(
            "ineligible a", self.fixture.workspace_access(["app:*:*", "ineligible:*:*", "bad:*:*"]), self.tenant
        )

        ineligible_b = self.fixture.new_custom_role(
            "ineligible b", self.fixture.workspace_access(["ineligible:*:*"]), self.tenant
        )

        result = check_v2_eligibility(self.tenant)

        self.assertIsInstance(result, OptInIneligibleState)
        self.assertTrue(result.bootstrapped)
        self.assertCountEqual([], result.ineligible_groups)

        self.assertCountEqual(
            [
                OptInIneligibleRole(role=ineligible_a, ineligible_applications=frozenset({"ineligible", "bad"})),
                OptInIneligibleRole(role=ineligible_b, ineligible_applications=frozenset({"ineligible"})),
            ],
            result.ineligible_custom_roles,
        )

    def test_ineligible_groups(self):
        eligible = self.fixture.new_system_role("eligible", ["app:*:*"])

        ineligible_a = self.fixture.new_system_role("ineligible a", ["app:*:*", "ineligible:*:*", "bad:*:*"])
        ineligible_b = self.fixture.new_system_role("ineligible b", ["ineligible:*:*"])

        ineligible_custom = self.fixture.new_custom_role(
            "custom", self.fixture.workspace_access(["ineligible:*:*"]), self.tenant
        )

        # A group with only an eligible system role.
        group_a = self.fixture.new_group("group a", self.tenant, ["p1"])[0]
        self.fixture.add_role_to_group(eligible, group_a)

        # A group with only an eligible custom role.
        group_b = self.fixture.new_group("group b", self.tenant, ["p1"])[0]
        self.fixture.add_role_to_group(ineligible_custom, group_b)

        # A group with a single ineligible system role.
        group_c = self.fixture.new_group("group c", self.tenant, ["p1"])[0]
        self.fixture.add_role_to_group(ineligible_a, group_c)

        # A group with an eligible system role and two ineligible system roles.
        group_d = self.fixture.new_group("group d", self.tenant, ["p1"])[0]
        self.fixture.add_role_to_group(eligible, group_d)
        self.fixture.add_role_to_group(ineligible_a, group_d)
        self.fixture.add_role_to_group(ineligible_b, group_d)

        # A group with an eligible system role, an ineligible system role, and an ineligible custom role.
        group_e = self.fixture.new_group("group e", self.tenant, ["p1"])[0]
        self.fixture.add_role_to_group(eligible, group_e)
        self.fixture.add_role_to_group(ineligible_a, group_e)
        self.fixture.add_role_to_group(ineligible_custom, group_e)

        result = check_v2_eligibility(self.tenant)

        self.assertIsInstance(result, OptInIneligibleState)
        self.assertTrue(result.bootstrapped)
        self.assertCountEqual([ineligible_custom], [r.role for r in result.ineligible_custom_roles])

        self.assertCountEqual([group_c, group_d, group_e], [g.group for g in result.ineligible_groups])

        def assert_roles_for(group: Group, roles: list[OptInIneligibleRole]):
            found_entries = [g for g in result.ineligible_groups if g.group == group]
            self.assertEqual(len(found_entries), 1)

            self.assertCountEqual(roles, found_entries[0].ineligible_system_roles)

        assert_roles_for(group_c, [OptInIneligibleRole(ineligible_a, frozenset({"ineligible", "bad"}))])

        assert_roles_for(
            group_d,
            [
                OptInIneligibleRole(ineligible_a, frozenset({"ineligible", "bad"})),
                OptInIneligibleRole(ineligible_b, frozenset({"ineligible"})),
            ],
        )

        assert_roles_for(group_e, [OptInIneligibleRole(ineligible_a, frozenset({"ineligible", "bad"}))])
