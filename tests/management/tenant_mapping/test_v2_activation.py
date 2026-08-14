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

from django.db import transaction
from django.test import TestCase
from management.tenant_mapping.model import TenantMapping
from management.tenant_mapping.v2_activation import (
    InvalidV2OptOutError,
    TenantVersion,
    V1WriteBlockedError,
    assert_v1_write_allowed,
    ensure_v2_write_activated,
    is_v2_opted_in,
    is_v2_write_activated,
    lock_tenant_version,
    lock_v2_opt_in_state,
    set_v2_opt_in_state,
)
from management.tenant_service.v2 import TenantNotBootstrappedError
from tests.management.role.test_dual_write import RbacFixture

from api.models import Tenant


class V2ActivationTests(TestCase):
    """Tests for V2 activation functions."""

    def setUp(self):
        self.fixture = RbacFixture()
        self.bootstrapped = self.fixture.new_tenant(org_id="activation-test-org")
        self.tenant = self.bootstrapped.tenant

    def test_new_tenant_is_not_v2_activated(self):
        self.assertFalse(is_v2_write_activated(self.tenant))

    def test_ensure_v2_write_activated_sets_timestamp(self):
        with transaction.atomic():
            ensure_v2_write_activated(self.tenant)

        mapping = TenantMapping.objects.get(tenant=self.tenant)

        timestamp = mapping.v2_write_activated_at

        self.assertIsNotNone(timestamp)
        self.assertEqual(timestamp, mapping.v2_opted_in_at)

    def test_ensure_v2_write_activated_is_idempotent(self):
        with transaction.atomic():
            ensure_v2_write_activated(self.tenant)

        mapping = TenantMapping.objects.get(tenant=self.tenant)
        first_timestamp = mapping.v2_write_activated_at

        with transaction.atomic():
            ensure_v2_write_activated(self.tenant)

        mapping.refresh_from_db()
        self.assertEqual(first_timestamp, mapping.v2_write_activated_at)
        self.assertEqual(first_timestamp, mapping.v2_opted_in_at)

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


class V2OptInTests(TestCase):
    def setUp(self):
        self.fixture = RbacFixture()
        self.bootstrapped = self.fixture.new_tenant(org_id="activation-test-org")
        self.tenant = self.bootstrapped.tenant

    def _assert_opt_in_state(self, opted_in: bool):
        self.assertEqual(is_v2_opted_in(self.tenant), opted_in)
        self.assertEqual(lock_v2_opt_in_state(self.tenant), opted_in)

    def test_opt_in_v1_tenant(self):
        set_v2_opt_in_state(self.tenant, True)

        assert_v1_write_allowed(self.tenant)

        mapping = self.tenant.tenant_mapping
        mapping.refresh_from_db()

        initial_timestamp = mapping.v2_opted_in_at

        self.assertIsNotNone(initial_timestamp)
        self.assertIsNone(mapping.v2_write_activated_at)

        self._assert_opt_in_state(True)

        # Opting-in should be idempotent.

        set_v2_opt_in_state(self.tenant, True)

        mapping.refresh_from_db()
        self.assertEqual(initial_timestamp, mapping.v2_opted_in_at)

        self._assert_opt_in_state(True)

    def test_opt_out_v1_tenant(self):
        set_v2_opt_in_state(self.tenant, True)
        set_v2_opt_in_state(self.tenant, False)

        assert_v1_write_allowed(self.tenant)

        mapping = self.tenant.tenant_mapping
        mapping.refresh_from_db()

        self.assertIsNone(mapping.v2_opted_in_at)
        self.assertIsNone(mapping.v2_write_activated_at)

        self._assert_opt_in_state(False)

    def test_v2_tenant_opted_in(self):
        ensure_v2_write_activated(self.tenant)
        self._assert_opt_in_state(True)

    def test_opt_in_v2_tenant_noop(self):
        ensure_v2_write_activated(self.tenant)

        mapping = self.tenant.tenant_mapping
        mapping.refresh_from_db()

        initial_timestamp = mapping.v2_opted_in_at

        set_v2_opt_in_state(self.tenant, True)

        mapping.refresh_from_db()
        self.assertEqual(initial_timestamp, mapping.v2_opted_in_at)
        self.assertEqual(initial_timestamp, mapping.v2_write_activated_at)

        self._assert_opt_in_state(True)

    def test_opt_out_v2_tenant_prohibited(self):
        ensure_v2_write_activated(self.tenant)

        with self.assertRaises(InvalidV2OptOutError):
            set_v2_opt_in_state(self.tenant, False)

        mapping = self.tenant.tenant_mapping
        mapping.refresh_from_db()

        self.assertIsNotNone(mapping.v2_write_activated_at)
        self.assertIsNotNone(mapping.v2_opted_in_at)

        self._assert_opt_in_state(True)
