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
"""Tests for V2 write activation state."""

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.db import transaction

from api.models import Tenant
from management.group.model import Group
from management.relation_replicator.noop_replicator import NoopReplicator
from management.role.model import BindingMapping, Role
from management.role_binding.model import RoleBinding
from management.tenant_mapping.model import TenantMapping
from management.tenant_mapping.v2_activation import (
    V1WriteBlockedError,
    assert_v1_write_allowed,
    ensure_v2_write_activated,
    is_v2_write_activated,
    lock_tenant_version,
    TenantVersion,
)
from management.tenant_service.v2 import TenantNotBootstrappedError
from management.workspace.service import WorkspaceService
from migration_tool.in_memory_tuples import (
    InMemoryTuples,
    InMemoryRelationReplicator,
    all_of,
    resource,
    relation,
    subject,
)
from tests.management.role.test_dual_write import RbacFixture, DualWriteTestCase
from tests.util import assert_v2_tuples_consistent
from tests.v2_util import bootstrap_tenant_for_v2_test


@override_settings(ATOMIC_RETRY_DISABLED=True)
class V2ActivationTests(TestCase):
    """Tests for V2 activation functions."""

    def setUp(self):
        super().setUp()

        self.fixture = RbacFixture()
        self.bootstrapped = self.fixture.new_tenant(org_id="activation-test-org")
        self.tenant = self.bootstrapped.tenant

    def test_new_tenant_is_not_v2_activated(self):
        self.assertFalse(is_v2_write_activated(self.tenant))

    def test_ensure_v2_write_activated_sets_timestamp(self):
        with transaction.atomic():
            ensure_v2_write_activated(self.tenant)

        mapping = TenantMapping.objects.get(tenant=self.tenant)
        self.assertIsNotNone(mapping.v2_write_activated_at)

    def test_ensure_v2_write_activated_is_idempotent(self):
        with transaction.atomic():
            ensure_v2_write_activated(self.tenant)

        mapping = TenantMapping.objects.get(tenant=self.tenant)
        first_timestamp = mapping.v2_write_activated_at

        with transaction.atomic():
            ensure_v2_write_activated(self.tenant)

        mapping.refresh_from_db()
        self.assertEqual(first_timestamp, mapping.v2_write_activated_at)

    def test_is_v2_write_activated_after_activation(self):
        self.assertFalse(is_v2_write_activated(self.tenant))

        with transaction.atomic():
            ensure_v2_write_activated(self.tenant)

        self.assertTrue(is_v2_write_activated(self.tenant))

    def test_assert_v1_write_allowed_before_activation(self):
        with transaction.atomic():
            assert_v1_write_allowed(self.tenant)

    def test_assert_v1_write_blocked_after_activation(self):
        with transaction.atomic():
            ensure_v2_write_activated(self.tenant)

        with self.assertRaises(V1WriteBlockedError):
            with transaction.atomic():
                assert_v1_write_allowed(self.tenant)

    def test_unbootstrapped_tenant_assert_v1_write_raises(self):
        """assert_v1_write_allowed raises TenantNotBootstrappedError for tenants without TenantMapping."""
        unbootstrapped = self.fixture.new_unbootstrapped_tenant(org_id="unboot-org")
        self.assertFalse(is_v2_write_activated(unbootstrapped))

        with self.assertRaises(TenantNotBootstrappedError):
            with transaction.atomic():
                assert_v1_write_allowed(unbootstrapped)

    def test_unbootstrapped_tenant_v2_activation_raises(self):
        """ensure_v2_write_activated raises TenantNotBootstrappedError for tenants without TenantMapping."""
        unbootstrapped = self.fixture.new_unbootstrapped_tenant(org_id="unboot-noop-org")

        with self.assertRaises(TenantNotBootstrappedError):
            with transaction.atomic():
                ensure_v2_write_activated(unbootstrapped)

    def test_lock_version_v1(self):
        """Test that lock_tenant_version returns VERSION_1 for a V1 tenant."""
        with transaction.atomic():
            self.assertEqual(lock_tenant_version(self.tenant), TenantVersion.VERSION_1)

    def test_lock_version_v2(self):
        """Test that lock_tenant_version returns VERSION_2 for a V2 tenant."""
        with transaction.atomic():
            ensure_v2_write_activated(self.tenant)
            self.assertEqual(lock_tenant_version(self.tenant), TenantVersion.VERSION_2)

    def test_lock_version_unbootstrapped(self):
        """Test that lock_tenant_version fails for an unbootstrapped tenant."""
        unbootstrapped = self.fixture.new_unbootstrapped_tenant(org_id="unboot-org")

        with self.assertRaises(TenantNotBootstrappedError):
            with transaction.atomic():
                lock_tenant_version(unbootstrapped)


@override_settings(ATOMIC_RETRY_DISABLED=True)
class V2ActivationBindingRemovalTest(DualWriteTestCase):
    def setUp(self):
        super().setUp()
        bootstrap_tenant_for_v2_test(self.tenant, tuples=self.tuples)

        self.group, _ = self.given_group("group", ["p1"])

        self.system_role = self.given_v1_system_role("a role", ["rbac:*:*"])
        self.custom_role = self.given_v1_role("a role", default=["rbac:*:*"], **{self.ws_1_id: ["inventory:*:*"]})

    def tearDown(self):
        assert_v2_tuples_consistent(test=self, tuples=self.tuples)
        super().tearDown()

    def _expect_binding_counts(self, role: Role, v1_count: int, v2_count: int):
        self.assertEqual(v1_count, BindingMapping.objects.filter(role=role).count())

        role_bindings = list(RoleBinding.objects.filter(role__v1_source=role))
        self.assertEqual(v2_count, len(role_bindings))

        for binding in role_bindings:
            self.assertEqual(
                1,
                self.tuples.count_tuples(
                    all_of(
                        resource("rbac", "role_binding", str(binding.uuid)),
                        relation("role"),
                        subject("rbac", "role", str(binding.role.uuid)),
                    )
                ),
            )

    def test_remove_system_binding(self):
        self.given_roles_assigned_to_group(self.group, [self.system_role])

        initial_tuples = set(self.tuples)
        self._expect_binding_counts(self.system_role, 1, 1)

        ensure_v2_write_activated(self.tenant)

        self.assertSetEqual(initial_tuples, set(self.tuples))
        self._expect_binding_counts(self.system_role, 0, 1)

    def test_remove_custom_bindings(self):
        self.given_roles_assigned_to_group(self.group, [self.custom_role])

        initial_tuples = set(self.tuples)
        self._expect_binding_counts(self.custom_role, 2, 2)

        ensure_v2_write_activated(self.tenant)

        self.assertSetEqual(initial_tuples, set(self.tuples))
        self._expect_binding_counts(self.custom_role, 0, 2)

    def test_fail_missing_binding_mapping(self):
        self.given_roles_assigned_to_group(self.group, [self.system_role])
        BindingMapping.objects.filter(role=self.system_role).delete()

        with self.assertRaises(AssertionError):
            ensure_v2_write_activated(self.tenant)

    def test_fail_missing_role_binding(self):
        self.given_roles_assigned_to_group(self.group, [self.system_role])
        RoleBinding.objects.filter(role__v1_source=self.system_role).delete()

        with self.assertRaises(AssertionError):
            ensure_v2_write_activated(self.tenant)
