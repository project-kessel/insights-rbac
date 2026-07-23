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
"""Test the Management queryset helpers."""

import uuid
from unittest.mock import Mock, patch

from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.urls import reverse
from management.group.model import Group
from management.policy.model import Policy
from management.principal.model import Principal
from management.role.model import Role
from management.models import Permission, Access
from management.querysets import (
    PRINCIPAL_SCOPE,
    SCOPE_KEY,
    get_group_queryset,
    get_policy_queryset,
    get_role_queryset,
    get_access_queryset,
    _filter_default_groups,
    _check_user_username_is_org_admin,
)
from management.utils import APPLICATION_KEY
from rest_framework import serializers

from api.models import Tenant, User
from tests.identity_request import IdentityRequest


class QuerySetTest(TestCase):
    """Test the Management queryset helper functions."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.tenant = Tenant.objects.get(tenant_name="acct1111111")
        except:
            cls.tenant = Tenant(tenant_name="acct1111111", account_id="1111111", org_id="100001", ready=True)
            cls.tenant.save()

    @classmethod
    def tearDownClass(cls):
        cls.tenant.delete()

    def tearDown(self):
        """Clean up after each test."""
        # Clean up database objects
        Group.objects.all().delete()
        Principal.objects.all().delete()
        Role.objects.all().delete()
        Policy.objects.all().delete()
        Permission.objects.all().delete()
        Access.objects.all().delete()

        # Clear principal cache to avoid stale data between tests
        from management.utils import PRINCIPAL_CACHE

        PRINCIPAL_CACHE.delete_all_principals_for_tenant("100001")
        PRINCIPAL_CACHE.delete_all_principals_for_tenant(self.tenant.org_id)

    def _create_groups(self):
        """Setup groups for tests."""
        Group.objects.create(name="group1", tenant=self.tenant)
        Group.objects.create(name="group2", tenant=self.tenant)
        Group.objects.create(name="group3", tenant=self.tenant)
        Group.objects.create(name="group4", tenant=self.tenant)
        Group.objects.create(name="group5", tenant=self.tenant)

    def _create_roles(self):
        """Setup roles for tests."""
        Role.objects.create(name="role1", tenant=self.tenant)
        Role.objects.create(name="role2", tenant=self.tenant)
        Role.objects.create(name="role3", tenant=self.tenant)
        Role.objects.create(name="role4", tenant=self.tenant)
        Role.objects.create(name="role5", tenant=self.tenant)

    def _create_policies(self):
        """Setup policies for tests."""
        Policy.objects.create(name="policy1", tenant=self.tenant)
        Policy.objects.create(name="policy2", tenant=self.tenant)
        Policy.objects.create(name="policy3", tenant=self.tenant)
        Policy.objects.create(name="policy4", tenant=self.tenant)
        Policy.objects.create(name="policy5", tenant=self.tenant)

    def test_get_group_queryset_admin(self):
        """Test get_group_queryset as an admin."""
        self._create_groups()
        user = Mock(spec=User, admin=True)
        req = Mock(user=user, tenant=self.tenant, query_params={})
        queryset = get_group_queryset(req)
        self.assertEqual(queryset.count(), 5)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "test_user",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_get_user_group_queryset_admin(self, mock_request):
        """Test get_group_queryset as an admin."""
        self._create_groups()
        principal = Principal.objects.create(username="test_user", tenant=self.tenant)
        group = Group.objects.first()
        group.principals.add(principal)
        user = Mock(spec=User, admin=True, account="00001", username="test_user")
        req = Mock(user=user, tenant=self.tenant, query_params={"username": "test_user"})
        queryset = get_group_queryset(req)
        self.assertEqual(queryset.count(), 1)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "test_user",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_get_group_queryset_get_users_own_groups(self, mock_request):
        """Test get_group_queryset to get a users own groups."""
        self._create_groups()
        principal = Principal.objects.create(username="test_user", tenant=self.tenant)
        group = Group.objects.first()
        group.principals.add(principal)
        user = Mock(spec=User, admin=False, account="00001", username="test_user")
        req = Mock(
            user=user,
            method="GET",
            tenant=self.tenant,
            query_params={"username": "test_user"},
            path=reverse("v1_management:group-list"),
        )
        queryset = get_group_queryset(req)
        self.assertEqual(queryset.count(), 1)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "test_user2",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_get_group_queryset_get_users_other_users_groups(self, mock_request):
        """Test get_group_queryset to get a users other users groups."""
        self._create_groups()
        principal = Principal.objects.create(username="test_user", tenant=self.tenant)
        principal2 = Principal.objects.create(username="test_user2", tenant=self.tenant)
        group = Group.objects.first()
        group.principals.add(principal)
        user = Mock(spec=User, admin=False, account="00001", username="test_user")
        req = Mock(user=user, method="GET", tenant=self.tenant, query_params={"username": "test_user2"})
        queryset = get_group_queryset(req)
        self.assertEqual(queryset.count(), 0)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "test_user",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_get_user_group_queryset_admin_default_org_admin(self, mock_request):
        """Test get_group_queryset as an org admin searching for admin default groups."""
        principal = Principal.objects.create(username="test_user", tenant=self.tenant)
        group = Group.objects.create(name="group_admin_default", tenant=self.tenant, admin_default=True)
        group.principals.add(principal)
        user = Mock(spec=User, admin=True, account="00001", username="test_user")
        req = Mock(user=user, tenant=self.tenant, query_params={"username": "test_user"})
        queryset = get_group_queryset(req)
        self.assertEqual(queryset.count(), 1)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": False,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "test_user",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_get_user_group_queryset_admin_default_non_org_admin(self, mock_request):
        """Test get_group_queryset not as an org admin, searching for admin default groups."""
        principal = Principal.objects.create(username="test_user", tenant=self.tenant)
        group = Group.objects.create(name="group_admin_default", tenant=self.tenant, admin_default=True)
        user = Mock(spec=User, admin=False, account="00001", username="test_user")
        req = Mock(user=user, tenant=self.tenant, query_params={"username": "test_user"})
        queryset = get_group_queryset(req)
        self.assertEqual(queryset.count(), 0)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "test_user",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_get_user_group_queryset_admin_and_platform_default(self, mock_request):
        """Test get_group_queryset not as an org admin, searching for groups that are admin and platform default."""
        principal = Principal.objects.create(username="test_user", tenant=self.tenant)
        group = Group.objects.create(
            name="group_admin_default", tenant=self.tenant, admin_default=True, platform_default=True
        )
        user = Mock(spec=User, admin=False, account="00001", username="test_user")
        req = Mock(user=user, tenant=self.tenant, query_params={"username": "test_user"})
        queryset = get_group_queryset(req)
        self.assertEqual(queryset.count(), 1)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": False,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "test_user",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_get_user_group_queryset_admin_default_rbac_admin(self, mock_request):
        """Test get_group_queryset not as an org admin, but as an RBAC admin searching for admin default groups."""
        principal = Principal.objects.create(username="test_user", tenant=self.tenant)
        group = Group.objects.create(name="group_admin_default", tenant=self.tenant, admin_default=True)
        permission = Permission.objects.create(permission="rbac:*:*", tenant=self.tenant)
        rbac_admin_role = Role.objects.create(name="RBAC admin role", tenant=self.tenant)
        access = Access.objects.create(permission=permission, role=rbac_admin_role, tenant=self.tenant)
        user = Mock(spec=User, admin=False, account="00001", username="test_user", access=access)
        req = Mock(user=user, tenant=self.tenant, query_params={"username": "test_user"})
        queryset = get_group_queryset(req)
        self.assertEqual(queryset.count(), 0)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "test_user",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_get_user_group_queryset_exclude_username(self, mock_request):
        """Test get_group_queryset to get the groups where can be user manually added."""
        # Create 5 groups and add principal to 1 of tem
        self._create_groups()
        principal = Principal.objects.create(username="test_user", tenant=self.tenant)
        group = Group.objects.first()
        group.principals.add(principal)

        # Create default group and admin default group and add principal to the default group
        admin_default_group = Group.objects.create(name="group_admin_default", tenant=self.tenant, admin_default=True)
        default_group = Group.objects.create(name="group_default", tenant=self.tenant, platform_default=True)
        default_group.principals.add(principal)

        # Check that 7 groups are created
        self.assertEqual(Group.objects.count(), 7)

        user = Mock(spec=User, admin=False, account="00001", username="test_user")
        req = Mock(
            user=user,
            method="GET",
            tenant=self.tenant,
            query_params={"exclude_username": "test_user"},
            path=reverse("v1_management:group-list"),
        )
        queryset = get_group_queryset(req)

        # Check that principal can be added into 4 groups
        # (excluded 2 groups where the principal is already a member
        # and excluded default groups where cannot be added manually)
        self.assertEqual(queryset.count(), 4)

    def test_get_role_queryset_admin(self):
        """Test get_role_queryset as an admin."""
        self._create_roles()
        user = Mock(spec=User, admin=True)
        req = Mock(user=user, tenant=self.tenant, query_params={})
        queryset = get_role_queryset(req)
        self.assertEqual(queryset.count(), 5)
        self.assertIsNotNone(queryset.last().accessCount)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "test_user2",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_get_role_queryset_non_admin_username(self, mock_request):
        """Test get_role_queryset as a non-admin supplying a username."""
        roles = self._setup_roles_for_role_username_queryset_tests()

        user = Mock(spec=User, admin=False, username="test_user2", access={})
        req = Mock(user=user, method="GET", tenant=self.tenant, query_params={"username": "test_user2"})
        with self.assertRaises(PermissionDenied):
            get_role_queryset(req)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "test_user2",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_get_role_queryset_non_admin_username_with_perms_diff_user(self, mock_request):
        """Test get_role_queryset as a non-admin supplying a username."""
        roles = self._setup_roles_for_role_username_queryset_tests()

        user = Mock(
            spec=User,
            admin=False,
            username="test_user3",
            access={"role": {"read": ["*"]}, "principal": {"read": ["*"]}},
        )
        req = Mock(user=user, method="GET", query_params={"username": "test_user2"}, tenant=self.tenant)
        get_role_queryset(req)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "test_user2",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_get_role_queryset_non_admin_username_with_perms(self, mock_request):
        """Test get_role_queryset as a non-admin supplying a username."""
        roles = self._setup_roles_for_role_username_queryset_tests()

        user = Mock(
            spec=User,
            admin=False,
            username="test_user2",
            access={"role": {"read": ["*"]}, "principal": {"read": ["*"]}},
        )
        req = Mock(user=user, method="GET", query_params={"username": "test_user2"}, tenant=self.tenant)
        get_role_queryset(req)

    def test_get_role_queryset_non_admin_username_different(self):
        """Test get_role_queryset as a non-admin supplying a different username."""
        roles = self._setup_roles_for_role_username_queryset_tests()

        user = Mock(spec=User, admin=False, username="test_user", access={})
        req = Mock(user=user, tenant=self.tenant, method="GET", query_params={"username": "test_user2"})
        queryset = get_role_queryset(req)
        self.assertEqual(list(queryset), [])
        self.assertEqual(queryset.count(), 0)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "test_user2",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_get_role_queryset_admin_username(self, mock_request):
        """Test get_role_queryset as an admin supplying a username."""
        roles = self._setup_roles_for_role_username_queryset_tests()

        user = Mock(spec=User, admin=True, account="00001", username="test_user2")
        req = Mock(user=user, method="GET", tenant=self.tenant, query_params={"username": "test_user2"})
        queryset = get_role_queryset(req)
        role = queryset.last()
        self.assertEqual(list(queryset), [roles.first()])
        self.assertEqual(queryset.count(), 1)
        self.assertTrue(hasattr(role, "accessCount"))
        self.assertTrue(hasattr(role, "policyCount"))

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "test_user2",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_get_role_queryset_principal_scope(self, mock_request):
        """Test get_role_queryset with principal scope."""
        roles = self._setup_roles_for_role_username_queryset_tests()

        user = Mock(spec=User, admin=True, account="00001", username="test_user2")
        req = Mock(
            user=user,
            method="GET",
            tenant=self.tenant,
            query_params={SCOPE_KEY: PRINCIPAL_SCOPE, "username": "test_user2"},
        )
        queryset = get_role_queryset(req)
        role = queryset.last()
        self.assertEqual(list(queryset), [roles.first()])
        self.assertEqual(queryset.count(), 1)
        self.assertTrue(hasattr(role, "accessCount"))
        self.assertTrue(hasattr(role, "policyCount"))

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "test_user2",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_get_role_queryset_admin_username_different(self, mock_request):
        """Test get_role_queryset as an admin supplying a different username."""
        roles = self._setup_roles_for_role_username_queryset_tests()
        user = Mock(spec=User, admin=True, account="00001", username="admin")
        req = Mock(user=user, method="GET", tenant=self.tenant, query_params={"username": "test_user2"})
        queryset = get_role_queryset(req)
        self.assertEqual(list(queryset), [roles.first()])
        self.assertEqual(queryset.count(), 1)

    def test_get_role_queryset_get_all(self):
        """Test get_role_queryset as a user with all access."""
        self._create_roles()
        access = {"role": {"read": ["*"]}}
        user = Mock(spec=User, admin=False, access=access)
        req = Mock(user=user, method="GET", tenant=self.tenant, query_params={})
        queryset = get_role_queryset(req)
        self.assertEqual(queryset.count(), 5)

    def test_get_role_queryset_get_some(self):
        """Test get_role_queryset as a user with one role access."""
        self._create_roles()
        access = {"role": {"read": [Role.objects.first().uuid]}}
        user = Mock(spec=User, admin=False, access=access)
        req = Mock(user=user, method="GET", tenant=self.tenant, query_params={})
        queryset = get_role_queryset(req)
        self.assertEqual(queryset.count(), 1)

    def test_get_role_queryset_get_none(self):
        """Test get_role_queryset as a user with no access."""
        self._create_roles()
        access = {"role": {"read": []}}
        user = Mock(spec=User, admin=False, access=access)
        req = Mock(user=user, method="GET", tenant=self.tenant, query_params={})
        queryset = get_role_queryset(req)
        self.assertEqual(queryset.count(), 0)

    def test_get_role_queryset_post_all(self):
        """Test get_role_queryset as a user with all access."""
        self._create_roles()
        access = {"role": {"write": ["*"]}}
        user = Mock(spec=User, admin=False, access=access)
        req = Mock(user=user, method="PUT", tenant=self.tenant, query_params={})
        queryset = get_role_queryset(req)
        self.assertEqual(queryset.count(), 5)

    def test_get_role_queryset_put_some(self):
        """Test get_role_queryset as a user with one role access."""
        self._create_roles()
        access = {"role": {"write": [Role.objects.first().uuid]}}
        user = Mock(spec=User, admin=False, access=access)
        req = Mock(user=user, method="PUT", tenant=self.tenant, query_params={})
        queryset = get_role_queryset(req)
        self.assertEqual(queryset.count(), 1)

    def test_get_role_queryset_put_none(self):
        """Test get_role_queryset as a user with no access."""
        self._create_roles()
        access = {"role": {"write": []}}
        user = Mock(spec=User, admin=False, access=access)
        req = Mock(user=user, method="PUT", tenant=self.tenant, query_params={})
        queryset = get_role_queryset(req)
        self.assertEqual(queryset.count(), 0)

    def test_get_policy_queryset_admin(self):
        """Test get_policy_queryset as an admin."""
        self._create_policies()
        user = Mock(spec=User, admin=True)
        req = Mock(user=user, tenant=self.tenant, query_params={})
        queryset = get_policy_queryset(req)
        self.assertEqual(queryset.count(), 5)

    def test_get_policy_queryset_get_all(self):
        """Test get_policy_queryset as a user with all access."""
        self._create_policies()
        access = {"policy": {"read": ["*"]}}
        user = Mock(spec=User, admin=False, access=access)
        req = Mock(user=user, method="GET", tenant=self.tenant, query_params={})
        queryset = get_policy_queryset(req)
        self.assertEqual(queryset.count(), 5)

    def test_get_policy_queryset_get_some(self):
        """Test get_policy_queryset as a user with one role access."""
        self._create_policies()
        access = {"policy": {"read": [Policy.objects.first().uuid]}}
        user = Mock(spec=User, admin=False, access=access)
        req = Mock(user=user, method="GET", tenant=self.tenant, query_params={})
        queryset = get_policy_queryset(req)
        self.assertEqual(queryset.count(), 1)

    def test_get_policy_queryset_get_none(self):
        """Test get_policy_queryset as a user with no access."""
        self._create_policies()
        access = {"policy": {"read": []}}
        user = Mock(spec=User, admin=False, access=access)
        req = Mock(user=user, method="GET", tenant=self.tenant, query_params={})
        queryset = get_policy_queryset(req)
        self.assertEqual(queryset.count(), 0)

    def test_get_policy_queryset_post_all(self):
        """Test get_policy_queryset as a user with all access."""
        self._create_policies()
        access = {"policy": {"write": ["*"]}}
        user = Mock(spec=User, admin=False, access=access)
        req = Mock(user=user, method="PUT", tenant=self.tenant, query_params={})
        queryset = get_policy_queryset(req)
        self.assertEqual(queryset.count(), 5)

    def test_get_policy_queryset_put_some(self):
        """Test get_policy_queryset as a user with one role access."""
        self._create_policies()
        access = {"policy": {"write": [Policy.objects.first().uuid]}}
        user = Mock(spec=User, admin=False, access=access)
        req = Mock(user=user, method="PUT", tenant=self.tenant, query_params={})
        queryset = get_policy_queryset(req)
        self.assertEqual(queryset.count(), 1)

    def test_get_policy_queryset_put_none(self):
        """Test get_policy_queryset as a user with no access."""
        self._create_policies()
        access = {"policy": {"write": []}}
        user = Mock(spec=User, admin=False, access=access)
        req = Mock(user=user, method="PUT", tenant=self.tenant, query_params={})
        queryset = get_policy_queryset(req)
        self.assertEqual(queryset.count(), 0)

    def test_get_policy_queryset_scope_put_none(self):
        """Test get_policy_queryset for a principal scope with put."""
        self._create_policies()
        access = {"policy": {"write": []}}
        user = Mock(spec=User, admin=False, access=access)
        req = Mock(user=user, method="PUT", tenant=self.tenant, query_params={SCOPE_KEY: PRINCIPAL_SCOPE})
        queryset = get_policy_queryset(req)
        self.assertEqual(queryset.count(), 0)

    def test_get_policy_queryset_bad_scope(self):
        """Test get_policy_queryset with a bad scope."""
        self._create_policies()
        access = {"policy": {"read": ["*"]}}
        user = Mock(spec=User, admin=False, access=access)
        req = Mock(user=user, method="GET", tenant=self.tenant, query_params={SCOPE_KEY: "bad"})
        with self.assertRaises(serializers.ValidationError):
            get_policy_queryset(req)

    def test_get_access_queryset_org_admin(self):
        """Test get_access_queryset with an org admin user"""
        user_id = "1234567890"
        user_data = {"username": "test_user", "email": "admin@example.com", "user_id": user_id}
        customer = {"account_id": "10001"}
        request_context = IdentityRequest._create_request_context(customer, user_data, is_org_admin=True)
        encoded_req = request_context["request"]

        self._setup_group_for_org_admin_tests()

        user = Mock(
            spec=User, account="00001", username="test_user", user_id=user_id, admin=True, is_service_account=False
        )
        req = Mock(user=user, method="GET", tenant=self.tenant, query_params={APPLICATION_KEY: "app"})
        req.META = encoded_req.META

        queryset = get_access_queryset(req)
        self.assertEqual(queryset.count(), 1)

    def test_get_access_queryset_non_org_admin(self):
        """Test get_access_queryset with a non 'org admin' user"""
        user_id = "1234567890"
        user_data = {"username": "test_user", "email": "admin@example.com", "user_id": user_id}
        customer = {"account_id": "10001"}
        request_context = IdentityRequest._create_request_context(customer, user_data, is_org_admin=False)
        encoded_req = request_context["request"]

        self._setup_group_for_org_admin_tests()

        user = Mock(
            spec=User, account="00001", username="test_user", user_id=user_id, admin=False, is_service_account=False
        )
        req = Mock(user=user, method="GET", tenant=self.tenant, query_params={APPLICATION_KEY: "app"})
        req.META = encoded_req.META

        queryset = get_access_queryset(req)
        self.assertEqual(queryset.count(), 0)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "org_id": "100001",
                    "is_org_admin": True,
                    "is_internal": False,
                    "id": 52567473,
                    "username": "test_user",
                    "account_number": "1111111",
                    "is_active": True,
                }
            ],
        },
    )
    def test_get_access_queryset_non_org_admin_rbac_admin(self, mock_request):
        """Test get_access_queryset with a non 'org admin' but rbac admin user"""
        user_id = "1234567890"
        user_data = {"username": "test_user", "email": "admin@example.com", "user_id": user_id}
        customer = {"account_id": "10001"}
        request_context = IdentityRequest._create_request_context(customer, user_data, is_org_admin=False)
        encoded_req = request_context["request"]

        self._setup_group_for_org_admin_tests()

        permission = Permission.objects.create(permission="rbac:*:*", tenant=self.tenant)
        rbac_admin_role = Role.objects.create(name="RBAC admin role", tenant=self.tenant)
        access = Access.objects.create(permission=permission, role=rbac_admin_role, tenant=self.tenant)
        user = Mock(
            spec=User,
            account="00001",
            username="test_user",
            user_id=user_id,
            admin=False,
            access=access,
            is_service_account=False,
        )
        req = Mock(user=user, method="GET", tenant=self.tenant, query_params={APPLICATION_KEY: "app"})
        req.META = encoded_req.META

        queryset = get_access_queryset(req)
        self.assertEqual(queryset.count(), 0)

    def _setup_group_for_org_admin_tests(self):
        role = Role.objects.create(name="role_admin_default", tenant=self.tenant)
        policy = Policy.objects.create(name="policy_admin_default", tenant=self.tenant)
        group = Group.objects.create(name="group_admin_default", tenant=self.tenant, admin_default=True)
        policy.roles.add(role)
        group.policies.add(policy)
        permission = Permission.objects.create(permission="app:*:*", tenant=self.tenant)
        access = Access.objects.create(permission=permission, role=role, tenant=self.tenant)

    def _setup_roles_for_role_username_queryset_tests(self):
        self._create_groups()
        self._create_policies()
        self._create_roles()

        principal = Principal.objects.create(username="test_user2", tenant=self.tenant)
        group = Group.objects.first()
        policy = Policy.objects.first()
        roles = Role.objects.all()

        policy.roles.add(roles.first())
        group.principals.add(principal)
        group.policies.add(policy)
        return roles

    def test_filter_default_groups(self):
        """Test that filtering the default groups works when the conditions are met"""
        # Create two "default" groups.
        Group.objects.create(name="default-group-one", tenant=self.tenant, admin_default=True, platform_default=True)
        Group.objects.create(name="default-group-two", tenant=self.tenant, admin_default=True, platform_default=True)
        # Create another two regular groups.
        Group.objects.create(name="group-one", tenant=self.tenant, admin_default=False, platform_default=False)
        Group.objects.create(name="group-two", tenant=self.tenant, admin_default=False, platform_default=False)

        # Define the query parameters that should trigger the filtering of the default groups.
        query_parameters_test_case = [
            {"username": f"service-account-{uuid.uuid4()}"},
            {"exclude_username": True},
        ]

        for qptc in query_parameters_test_case:
            request = Mock()
            request.query_params: dict[str, str] = qptc

            query_set = Group.objects.all()
            returned_query_set = _filter_default_groups(request=request, queryset=query_set)

            self.assertFalse(
                len(query_set) == len(returned_query_set), "the filtering should have removed the default groups"
            )

            for group in returned_query_set:
                self.assertFalse(
                    group.admin_default,
                    "the group should not be an admin default one since it should have been filtered by the"
                    f' function under test when the query parameter "{qptc}" is specified',
                )
                self.assertFalse(
                    group.platform_default,
                    "the group should not be a platform default one since it should have been filtered by the"
                    f' function under test when the query parameter "{qptc}" is specified',
                )

    def test_dont_filter_default_groups(self):
        """Test that default groups are not filtered when the conditions are not met"""
        # Create two "default" groups.
        Group.objects.create(name="default-group-one", tenant=self.tenant, admin_default=True, platform_default=True)
        Group.objects.create(name="default-group-two", tenant=self.tenant, admin_default=True, platform_default=True)
        # Create another two regular groups.
        Group.objects.create(name="group-one", tenant=self.tenant, admin_default=False, platform_default=False)
        Group.objects.create(name="group-two", tenant=self.tenant, admin_default=False, platform_default=False)

        # Create a mocked request.
        request = Mock()
        request.query_params = {}

        query_set = Group.objects.all()
        returned_query_set = _filter_default_groups(request=request, queryset=query_set)

        self.assertEqual(
            query_set,
            returned_query_set,
            "the query set returned by the function under test should have been left untouched since the"
            " conditions for filtering the default groups are not met",
        )

    @patch("management.querysets.get_admin_from_proxy")
    def test_check_user_username_is_org_admin_service_account(self, get_admin_from_proxy: Mock):
        """Test that the function under test correctly identifies service accounts as non-org-admins"""
        # Call the function under test.
        self.assertEqual(
            False,
            _check_user_username_is_org_admin(request=Mock(), username=f"service-account-{uuid.uuid4()}"),
            "the service account username should have been flagged as non organization administrator",
        )

    @patch("management.querysets.get_admin_from_proxy")
    def test_check_user_username_is_org_admin_user_principal(self, get_admin_from_proxy: Mock):
        """Test that the function under test correctly identifies service accounts as non-org-admins"""
        # Return a mocked value for the dependant function, different to the value the function returns when the
        # username is identified as a service account username.
        get_admin_from_proxy.return_value = True

        # Create the mocked parameters to assert that the underlying function got called correctly.
        request = Mock()
        username = "user-1"

        # Call the function under test.
        self.assertEqual(
            True,
            _check_user_username_is_org_admin(request=request, username=username),
            "the user principal should have been identified as such, and the underlying function should have been called",
        )

        # Make sure the underlying function gets called.
        get_admin_from_proxy.assert_called_with(request=request, username=username)
