#
# Copyright 2020 Red Hat, Inc.
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
"""Test the caching system."""

import pickle
from unittest import skipIf
from unittest.mock import call, patch

from django.conf import settings
from django.test import TestCase, override_settings
from management.cache import TenantCache
from management.models import Access, Group, Permission, Policy, Principal, ResourceDefinition, Role

from api.models import Tenant
from rbac.settings import ACCESS_CACHE_ENABLED


@skipIf(not ACCESS_CACHE_ENABLED, "Caching is disabled.")
@override_settings(MOCK_REDIS=False)
class AccessCacheTest(TestCase):
    @classmethod
    def setUpClass(self):
        """Set up the tenant."""
        super().setUpClass()
        self.tenant = Tenant.objects.create(tenant_name="acct12345", org_id="12345")
        self.tenant.ready = True
        self.tenant.save()

    def setUp(self):
        """Set up AccessCache tests."""
        super().setUp()
        self.principal_a = Principal.objects.create(username="principal_a", tenant=self.tenant)
        self.principal_b = Principal.objects.create(username="principal_b", tenant=self.tenant)
        self.group_a = Group.objects.create(name="group_a", platform_default=True, tenant=self.tenant)
        self.group_b = Group.objects.create(name="group_b", tenant=self.tenant)
        self.policy_a = Policy.objects.create(name="policy_a", tenant=self.tenant)
        self.policy_b = Policy.objects.create(name="policy_b", tenant=self.tenant)
        self.role_a = Role.objects.create(name="role_a", tenant=self.tenant)
        self.role_b = Role.objects.create(name="role_b", tenant=self.tenant)

    @classmethod
    def tearDownClass(self):
        self.tenant.delete()
        super().tearDownClass()

    @patch("management.group.model.AccessCache.delete_policy")
    def test_group_cache_add_remove_signals(self, cache):
        """Test signals attached to Groups"""
        cache.reset_mock()

        # If a Principal is added to a group
        self.group_a.principals.add(self.principal_a)

        cache.assert_called_once()
        cache.assert_called_once_with(self.principal_a.uuid)

        cache.reset_mock()
        # If a Group is added to a Principal
        self.principal_b.group.add(self.group_a)
        cache.asset_called_once()
        cache.asset_called_once_with(self.principal_b.uuid)

        cache.reset_mock()
        # If a Principal is removed from a group
        self.group_a.principals.remove(self.principal_a)
        cache.assert_called_once()
        cache.assert_called_once_with(self.principal_a.uuid)

        cache.reset_mock()
        # If a Group is removed from a Principal
        self.principal_b.group.remove(self.group_a)
        cache.asset_called_once()
        cache.asset_called_once_with(self.principal_b.uuid)

    @patch("management.group.model.AccessCache.delete_policy")
    def test_group_cache_clear_signals(self, cache):
        # If all groups are removed from a Principal
        self.group_a.principals.add(self.principal_a, self.principal_b)
        cache.reset_mock()
        self.principal_a.group.clear()
        cache.assert_called_once()
        cache.assert_called_once_with(self.principal_a.uuid)

        cache.reset_mock()
        # If all Principals are removed from a Group
        self.group_a.principals.clear()
        cache.asset_called_once()
        cache.asset_called_once_with(self.principal_b.uuid)

    @patch("management.group.model.AccessCache.delete_policy")
    def test_group_cache_delete_group_signal(self, cache):
        self.group_a.principals.add(self.principal_a)
        cache.reset_mock()
        self.group_a.delete()
        cache.assert_called_once()
        cache.assert_called_once_with(self.principal_a.uuid)

    @patch("management.policy.model.AccessCache.delete_all_policies_for_tenant")
    @patch("management.policy.model.AccessCache.delete_policy")
    def test_policy_cache_group_signals(self, cache_delete, cache_delete_all):
        """Test signals attached to Groups"""
        self.group_a.principals.add(self.principal_a)
        self.group_b.principals.add(self.principal_b)
        cache_delete.reset_mock()

        # If a policy has its group set
        self.policy_a.group = self.group_a
        self.policy_a.save()
        cache_delete_all.asset_called_once()

        cache_delete.reset_mock()
        # If a policy has its group changed
        self.policy_a.group = self.group_b
        self.policy_a.save()
        cache_delete.asset_called_once()
        cache_delete.asset_called_once_with(self.principal_b.uuid)

        cache_delete.reset_mock()
        # If a policy is deleted
        self.policy_a.delete()
        cache_delete.assert_called_once()
        cache_delete.assert_called_once_with(self.principal_b.uuid)

    @patch("management.policy.model.AccessCache.delete_all_policies_for_tenant")
    @patch("management.policy.model.AccessCache.delete_policy")
    def test_policy_cache_add_remove_roles_signals(self, cache_delete, cache_delete_all):
        """Test signals attached to Policy/Roles"""
        self.group_b.principals.add(self.principal_b)
        self.policy_a.group = self.group_a
        self.policy_a.save()
        self.policy_b.group = self.group_b
        self.policy_b.save()
        cache_delete.reset_mock()

        # If a Role is added to a platform default group's Policy
        self.policy_a.roles.add(self.role_a)
        self.policy_a.save()
        cache_delete_all.asset_called_once()

        cache_delete.reset_mock()
        # If a Policy is added to a Role
        self.role_b.policies.add(self.policy_a)
        cache_delete.asset_called_once()
        cache_delete.asset_called_once_with(self.principal_b.uuid)

        cache_delete.reset_mock()
        # If a Role is removed from a platform default group's Policy
        self.policy_a.roles.remove(self.role_a)
        self.policy_a.save()
        cache_delete_all.asset_called_once()

        cache_delete.reset_mock()
        # If a Role is removed from a Policy
        self.policy_b.roles.remove(self.role_b)
        cache_delete.assert_called_once()
        cache_delete.assert_called_once_with(self.principal_b.uuid)

        cache_delete.reset_mock()
        # If a Policy is removed from a Role
        self.role_b.policies.remove(self.policy_b)
        cache_delete.asset_called_once()
        cache_delete.asset_called_once_with(self.principal_b.uuid)

    @patch("management.policy.model.AccessCache.delete_policy")
    def test_policy_cache_clear_signals(self, cache):
        self.group_a.principals.add(self.principal_a)
        self.group_b.principals.add(self.principal_b)
        self.policy_a.group = self.group_a
        self.policy_a.save()
        self.policy_b.group = self.group_b
        self.policy_b.save()
        self.policy_a.roles.add(self.role_a)
        self.policy_b.roles.add(self.role_b)
        cache.reset_mock()

        # If all policies are removed from a role
        self.role_a.policies.clear()
        cache.assert_called_once()
        cache.assert_called_once_with(self.principal_a.uuid)

        cache.reset_mock()
        # If all Roles are removed from a Policy
        self.policy_b.roles.clear()
        cache.asset_called_once()
        cache.asset_called_once_with(self.principal_b.uuid)

    @patch("management.role.model.AccessCache.delete_policy")
    def test_policy_cache_change_delete_roles_signals(self, cache):
        self.group_a.principals.add(self.principal_a)
        self.group_b.principals.add(self.principal_b)
        self.policy_a.group = self.group_a
        self.policy_a.save()
        self.policy_b.group = self.group_b
        self.policy_b.save()
        self.policy_a.roles.add(self.role_a)
        self.policy_b.roles.add(self.role_b)
        cache.reset_mock()

        # If a role is changed
        self.role_a.version += 1
        self.role_a.save()
        cache.assert_called_once()
        cache.assert_called_once_with(self.principal_a.uuid)

        cache.reset_mock()
        # If Access is added
        self.permission = Permission.objects.create(permission="foo:*:*", tenant=self.tenant)
        self.access_a = Access.objects.create(permission=self.permission, role=self.role_a, tenant=self.tenant)
        cache.assert_called_once()
        cache.assert_called_once_with(self.principal_a.uuid)

        cache.reset_mock()
        # If ResourceDefinition is added
        self.rd_a = ResourceDefinition.objects.create(access=self.access_a, tenant=self.tenant)
        cache.assert_called_once()
        cache.assert_called_once_with(self.principal_a.uuid)

        cache.reset_mock()
        # If ResourceDefinition is destroyed
        self.rd_a.delete()
        cache.assert_called_once()
        cache.assert_called_once_with(self.principal_a.uuid)

        cache.reset_mock()
        # If Access is destroyed
        self.access_a.delete()
        cache.assert_called_once()
        cache.assert_called_once_with(self.principal_a.uuid)

        cache.reset_mock()
        # If Role is destroyed
        self.role_a.delete()
        cache.assert_called_once()
        cache.assert_called_once_with(self.principal_a.uuid)


@override_settings(MOCK_REDIS=False)
class TenantCacheTest(TestCase):
    @classmethod
    def setUpClass(self):
        """Set up the tenant."""
        super().setUpClass()
        self.tenant = Tenant.objects.create(tenant_name="acct67890")

    @classmethod
    def tearDownClass(self):
        self.tenant.delete()
        super().tearDownClass()

    @patch("management.cache.TenantCache.connection")
    @patch("management.cache.BasicCache.redis_health_check")
    def test_tenant_cache_functions_success(self, redis_health_check, redis_connection):
        tenant_name = self.tenant.tenant_name
        tenant_org_id = self.tenant.org_id
        key = f"rbac::tenant::tenant={tenant_org_id}"
        dump_content = pickle.dumps(self.tenant)

        # Save tenant to cache
        tenant_cache = TenantCache()
        tenant_cache.save_tenant(self.tenant)
        self.assertTrue(call().__enter__().set(key, dump_content) in redis_connection.pipeline.mock_calls)

        redis_connection.get.return_value = dump_content
        redis_health_check.return_value = True
        # Get tenant from cache
        tenant = tenant_cache.get_tenant(tenant_org_id)
        redis_health_check.assert_called_once()
        redis_connection.get.assert_called_once_with(key)
        self.assertEqual(tenant, self.tenant)

        # Delete tenant from cache
        tenant_cache.delete_tenant(tenant_org_id)
        redis_connection.delete.assert_called_once_with(key)

    @patch("management.cache.TenantCache.connection")
    @patch("management.cache.BasicCache.redis_health_check")
    def test_tenant_cache_functions_failure(self, redis_health_check, redis_connection):
        tenant_name = self.tenant.tenant_name
        tenant_org_id = self.tenant.org_id
        key = f"rbac::tenant::tenant={tenant_org_id}"
        dump_content = pickle.dumps(self.tenant)

        # Save tenant to cache
        tenant_cache = TenantCache()
        tenant_cache.save_tenant(self.tenant)
        self.assertTrue(call().__enter__().set(key, dump_content) in redis_connection.pipeline.mock_calls)

        redis_connection.get.return_value = dump_content
        redis_health_check.return_value = False
        # Get tenant from cache (should fail because redis_health_check failed)
        tenant = tenant_cache.get_tenant(tenant_org_id)
        redis_health_check.assert_called_once()
        self.assertNotEqual(tenant, self.tenant)


@override_settings(MOCK_REDIS=True)
class MockRedisTest(TestCase):
    """Test that MOCK_REDIS=True makes all cache operations no-ops."""

    def test_basic_cache_use_caching_disabled(self):
        """BasicCache.use_caching is False when MOCK_REDIS=True."""
        from management.cache import BasicCache

        cache = BasicCache()
        self.assertFalse(cache.use_caching)

    def test_redis_health_check_returns_false(self):
        """redis_health_check returns False without connecting."""
        from management.cache import BasicCache

        cache = BasicCache()
        result = cache.redis_health_check()
        self.assertFalse(result)

    def test_get_cached_returns_none(self):
        """get_cached returns None without connecting."""
        from management.cache import TenantCache

        cache = TenantCache()
        result = cache.get_tenant("test_org")
        self.assertIsNone(result)

    def test_save_is_noop(self):
        """save does not attempt Redis connection."""
        from management.cache import TenantCache

        from api.models import Tenant

        tenant = Tenant(tenant_name="mock_test", org_id="mock_org")
        cache = TenantCache()
        # Should not raise — no Redis connection attempted
        cache.save_tenant(tenant)

    def test_delete_cached_is_noop(self):
        """delete_cached does not attempt Redis connection."""
        from management.cache import TenantCache

        cache = TenantCache()
        # Should not raise — no Redis connection attempted
        cache.delete_tenant("mock_org")

    def test_connection_property_returns_none_when_mock(self):
        """Accessing connection returns None when MOCK_REDIS=True."""
        from management.cache import BasicCache

        cache = BasicCache()
        self.assertIsNone(cache.connection)

    def test_access_cache_delete_all_is_noop(self):
        """delete_all_policies_for_tenant is no-op when MOCK_REDIS=True."""
        from management.cache import AccessCache

        cache = AccessCache("test_tenant")
        # Should not raise — no Redis connection attempted
        cache.delete_all_policies_for_tenant()

    def test_principal_cache_delete_all_is_noop(self):
        """delete_all_principals_for_tenant is no-op when MOCK_REDIS=True."""
        from management.cache import PrincipalCache

        cache = PrincipalCache()
        # Should not raise — no Redis connection attempted
        cache.delete_all_principals_for_tenant("mock_org")
