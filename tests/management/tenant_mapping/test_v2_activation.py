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

from django.db import transaction
from django.test import TestCase
from management.group.definer import seed_group
from management.group.model import Group
from management.group.platform import DefaultGroupNotAvailableError, GlobalPolicyIdService
from management.permission.scope_service import CONCRETE_SCOPES
from management.role.definer import seed_roles
from management.role_binding.model import RoleBinding
from management.role_binding.service import RoleBindingService
from management.tenant_mapping.model import DefaultAccessType, TenantMapping
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
from tests.identity_request import IdentityRequest
from tests.management.role.test_dual_write import RbacFixture

from api.models import Tenant

CREATE_DEFAULT_BINDINGS = "management.role_binding.service.RoleBindingService.create_default_bindings"


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


class V2OptInDefaultBindingsTests(IdentityRequest):
    """Tests that opting-in to V2 creates the tenant's default role bindings."""

    def setUp(self):
        GlobalPolicyIdService.clear_shared()
        Tenant._public_tenant = None

        # Platform roles and public default groups are required for default binding creation.
        seed_roles()
        seed_group()

        self.fixture = RbacFixture()
        self.bootstrapped = self.fixture.new_tenant(org_id="opt-in-bindings-org")
        self.tenant = self.bootstrapped.tenant
        self.mapping = self.bootstrapped.mapping

    def _count_default_bindings(self, access_type: DefaultAccessType) -> int:
        binding_uuids = [self.mapping.default_role_binding_uuid_for(access_type, s) for s in CONCRETE_SCOPES]
        return RoleBinding.objects.filter(uuid__in=binding_uuids).count()

    def test_opt_in_creates_default_bindings(self):
        """Opting-in creates ADMIN and USER default bindings for every concrete scope."""
        self.assertEqual(self._count_default_bindings(DefaultAccessType.ADMIN), 0)
        self.assertEqual(self._count_default_bindings(DefaultAccessType.USER), 0)

        set_v2_opt_in_state(self.tenant, True)

        self.assertEqual(self._count_default_bindings(DefaultAccessType.ADMIN), len(CONCRETE_SCOPES))
        self.assertEqual(self._count_default_bindings(DefaultAccessType.USER), len(CONCRETE_SCOPES))

    def test_opt_in_skips_user_bindings_with_custom_default_group(self):
        """A tenant with a custom default group only gets ADMIN default bindings."""
        Group.objects.create(name="Custom default", platform_default=True, tenant=self.tenant)

        set_v2_opt_in_state(self.tenant, True)

        self.assertEqual(self._count_default_bindings(DefaultAccessType.ADMIN), len(CONCRETE_SCOPES))
        self.assertEqual(self._count_default_bindings(DefaultAccessType.USER), 0)

    def test_repeated_opt_in_does_not_recreate_bindings(self):
        """Opting-in an already opted-in tenant does not invoke binding creation again."""
        set_v2_opt_in_state(self.tenant, True)

        with patch(CREATE_DEFAULT_BINDINGS) as mock_create_bindings:
            set_v2_opt_in_state(self.tenant, True)

        mock_create_bindings.assert_not_called()
        self.assertEqual(self._count_default_bindings(DefaultAccessType.ADMIN), len(CONCRETE_SCOPES))
        self.assertEqual(self._count_default_bindings(DefaultAccessType.USER), len(CONCRETE_SCOPES))

    def test_opt_in_succeeds_when_platform_roles_unavailable(self):
        """A missing platform role is logged but does not abort the opt-in."""
        with patch(
            "management.role_binding.service.RoleBindingService._create_all_default_bindings",
            side_effect=DefaultGroupNotAvailableError(),
        ):
            set_v2_opt_in_state(self.tenant, True)

        self.mapping.refresh_from_db()
        self.assertIsNotNone(self.mapping.v2_opted_in_at)
        self.assertEqual(self._count_default_bindings(DefaultAccessType.ADMIN), 0)
        self.assertEqual(self._count_default_bindings(DefaultAccessType.USER), 0)

    def test_opt_in_fails_when_public_default_group_missing(self):
        """A missing public default group is not an expected state, so opt-in fails and is rolled back."""
        # Warm the process-lifetime policy UUID cache first, so resolving the platform role
        # succeeds and creation gets as far as looking the group itself up.
        policy_service = GlobalPolicyIdService.shared()
        policy_service.platform_default_policy_uuid()
        policy_service.admin_default_policy_uuid()

        Group.admin_default_set().public_tenant_only().delete()

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                set_v2_opt_in_state(self.tenant, True)

        self.mapping.refresh_from_db()
        self.assertIsNone(self.mapping.v2_opted_in_at)
        self.assertEqual(self._count_default_bindings(DefaultAccessType.ADMIN), 0)
        self.assertEqual(self._count_default_bindings(DefaultAccessType.USER), 0)

    def test_opt_in_already_v2_activated_tenant_does_not_create_bindings(self):
        """A V2-activated tenant is already opted-in, so the opt-in call is a no-op."""
        with transaction.atomic():
            ensure_v2_write_activated(self.tenant)

        with patch(CREATE_DEFAULT_BINDINGS) as mock_create_bindings:
            set_v2_opt_in_state(self.tenant, True)

        mock_create_bindings.assert_not_called()

    def test_unbootstrapped_tenant_opt_in_does_not_create_bindings(self):
        """An unbootstrapped tenant fails before any binding creation is attempted."""
        unbootstrapped = self.fixture.new_unbootstrapped_tenant(org_id="opt-in-unboot-org")

        with patch(CREATE_DEFAULT_BINDINGS) as mock_create_bindings:
            with self.assertRaises(TenantNotBootstrappedError):
                set_v2_opt_in_state(unbootstrapped, True)

        mock_create_bindings.assert_not_called()

    def test_opt_out_keeps_default_bindings(self):
        """Opting out does not delete the default bindings created on opt-in."""
        set_v2_opt_in_state(self.tenant, True)
        set_v2_opt_in_state(self.tenant, False)

        self.mapping.refresh_from_db()
        self.assertIsNone(self.mapping.v2_opted_in_at)
        self.assertEqual(self._count_default_bindings(DefaultAccessType.ADMIN), len(CONCRETE_SCOPES))
        self.assertEqual(self._count_default_bindings(DefaultAccessType.USER), len(CONCRETE_SCOPES))

    def test_list_read_recovers_bindings_after_incomplete_seeding(self):
        """A list read retries default binding creation when initial opt-in skipped it due to missing roles."""
        # Opt in while platform roles are unavailable — bindings silently skipped
        with patch(
            "management.role_binding.service.RoleBindingService._create_all_default_bindings",
            side_effect=DefaultGroupNotAvailableError(),
        ):
            set_v2_opt_in_state(self.tenant, True)

        self.mapping.refresh_from_db()
        self.assertIsNotNone(self.mapping.v2_opted_in_at)
        self.assertEqual(self._count_default_bindings(DefaultAccessType.ADMIN), 0)

        # Now roles and groups are seeded (setUp already did that).
        # A list read should lazily create the missing default bindings.
        service = RoleBindingService(self.tenant)
        service.get_role_bindings_for_list({})

        self.assertEqual(self._count_default_bindings(DefaultAccessType.ADMIN), len(CONCRETE_SCOPES))
        self.assertEqual(self._count_default_bindings(DefaultAccessType.USER), len(CONCRETE_SCOPES))
