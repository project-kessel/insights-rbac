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
"""Tests for V2 Kessel fallback in V1 permission classes.

In V2-migrated orgs, service accounts and non-admin users whose permissions
are granted exclusively via V2 role bindings should be allowed to call V1
endpoints when Kessel grants access.
"""

from unittest.mock import Mock, patch

from django.test import TestCase

from api.models import User
from management.permissions.group_access import GroupAccessPermission
from management.permissions.permission_access import PermissionAccessPermission
from management.permissions.principal_access import PrincipalAccessPermission
from management.permissions.role_access import RoleAccessPermission
from management.permissions.utils import check_v2_kessel_access


class CheckV2KesselAccessTest(TestCase):
    """Test the check_v2_kessel_access helper function."""

    def _make_request(self, tenant=None):
        """Create a mock request with optional tenant."""
        req = Mock()
        req.tenant = tenant
        return req

    @patch("management.tenant_mapping.v2_activation.TenantMapping")
    def test_returns_false_for_v1_org(self, mock_tm):
        """V1 orgs should never get Kessel fallback."""
        mock_tm.objects.get.return_value = Mock(v2_write_activated_at=None)
        tenant = Mock()
        req = self._make_request(tenant=tenant)
        self.assertFalse(check_v2_kessel_access(req))

    def test_returns_false_without_tenant(self):
        """No tenant on request should return False."""
        req = self._make_request(tenant=None)
        self.assertFalse(check_v2_kessel_access(req))

    @patch("management.permissions.workspace_inventory_access.WorkspaceInventoryAccessChecker.check_resource_access")
    @patch("management.principal.proxy.get_kessel_principal_id", return_value="localhost/test-user")
    @patch("management.tenant_mapping.v2_activation.TenantMapping")
    def test_returns_true_when_kessel_grants(self, mock_tm, mock_pid, mock_check):
        """V2 org with Kessel granting access should return True."""
        mock_tm.objects.get.return_value = Mock(v2_write_activated_at="2026-01-01")
        tenant = Mock()
        tenant.tenant_resource_id.return_value = "localhost/org-123"
        req = self._make_request(tenant=tenant)

        mock_check.return_value = True
        self.assertTrue(check_v2_kessel_access(req))

        mock_check.assert_called_once_with(
            resource_type="tenant",
            resource_id="localhost/org-123",
            principal_id="localhost/test-user",
            relation="rbac_roles_read",
        )

    @patch("management.permissions.workspace_inventory_access.WorkspaceInventoryAccessChecker.check_resource_access")
    @patch("management.principal.proxy.get_kessel_principal_id", return_value="localhost/test-user")
    @patch("management.tenant_mapping.v2_activation.TenantMapping")
    def test_returns_false_when_kessel_denies(self, mock_tm, mock_pid, mock_check):
        """V2 org with Kessel denying access should return False."""
        mock_tm.objects.get.return_value = Mock(v2_write_activated_at="2026-01-01")
        tenant = Mock()
        tenant.tenant_resource_id.return_value = "localhost/org-123"
        req = self._make_request(tenant=tenant)

        mock_check.return_value = False
        self.assertFalse(check_v2_kessel_access(req))

    @patch("management.principal.proxy.get_kessel_principal_id", return_value=None)
    @patch("management.tenant_mapping.v2_activation.TenantMapping")
    def test_returns_false_without_principal_id(self, mock_tm, mock_pid):
        """V2 org where principal ID cannot be resolved should return False."""
        mock_tm.objects.get.return_value = Mock(v2_write_activated_at="2026-01-01")
        tenant = Mock()
        tenant.tenant_resource_id.return_value = "localhost/org-123"
        req = self._make_request(tenant=tenant)
        self.assertFalse(check_v2_kessel_access(req))

    @patch("management.tenant_mapping.v2_activation.TenantMapping")
    def test_returns_false_without_tenant_resource_id(self, mock_tm):
        """V2 org where tenant has no resource ID should return False."""
        mock_tm.objects.get.return_value = Mock(v2_write_activated_at="2026-01-01")
        tenant = Mock()
        tenant.tenant_resource_id.return_value = None
        req = self._make_request(tenant=tenant)
        self.assertFalse(check_v2_kessel_access(req))

    @patch("management.permissions.workspace_inventory_access.WorkspaceInventoryAccessChecker.check_resource_access")
    @patch("management.principal.proxy.get_kessel_principal_id", return_value="localhost/test-user")
    @patch("management.tenant_mapping.v2_activation.TenantMapping")
    def test_passes_custom_relation(self, mock_tm, mock_pid, mock_check):
        """Custom relation should be forwarded to Kessel."""
        mock_tm.objects.get.return_value = Mock(v2_write_activated_at="2026-01-01")
        tenant = Mock()
        tenant.tenant_resource_id.return_value = "localhost/org-123"
        req = self._make_request(tenant=tenant)

        mock_check.return_value = True
        check_v2_kessel_access(req, relation="rbac_roles_write")

        mock_check.assert_called_once_with(
            resource_type="tenant",
            resource_id="localhost/org-123",
            principal_id="localhost/test-user",
            relation="rbac_roles_write",
        )


def _make_v2_request(*, method="GET", admin=False, access=None, tenant=None, query_params=None, username="svc-acct"):
    """Create a mock request for V2 fallback tests.

    Returns a request where V1 access is empty (simulating a V2-only user)
    and the org is V2-migrated.
    """
    if access is None:
        access = {
            "group": {"read": [], "write": []},
            "role": {"read": [], "write": []},
            "principal": {"read": [], "write": []},
            "permission": {"read": [], "write": []},
            "policy": {"read": [], "write": []},
        }
    user = Mock(spec=User, admin=admin, access=access, username=username)
    req = Mock(user=user, method=method, query_params=query_params or {})
    req.tenant = tenant
    return req


_PATCH_PRINCIPAL = "management.permissions.principal_access.check_v2_kessel_access"
_PATCH_PERMISSION = "management.permissions.permission_access.check_v2_kessel_access"
_PATCH_ROLE = "management.permissions.role_access.check_v2_kessel_access"
_PATCH_GROUP = "management.permissions.group_access.check_v2_kessel_access"


class PrincipalAccessV2FallbackTest(TestCase):
    """Test PrincipalAccessPermission with V2 Kessel fallback."""

    def test_admin_bypass_unchanged(self):
        """Admin users should still be allowed without Kessel check."""
        req = _make_v2_request(admin=True)
        self.assertTrue(PrincipalAccessPermission().has_permission(req, None))

    def test_v1_access_still_works(self):
        """Users with V1 principal:read should still be allowed."""
        access = {
            "principal": {"read": ["*"], "write": []},
        }
        req = _make_v2_request(access=access)
        self.assertTrue(PrincipalAccessPermission().has_permission(req, None))

    @patch(_PATCH_PRINCIPAL, return_value=True)
    def test_v2_kessel_grants_read(self, mock_kessel):
        """V2 user with no V1 access should be allowed when Kessel grants."""
        req = _make_v2_request()
        self.assertTrue(PrincipalAccessPermission().has_permission(req, None))
        mock_kessel.assert_called_once_with(req)

    @patch(_PATCH_PRINCIPAL, return_value=False)
    def test_v2_kessel_denies_read(self, mock_kessel):
        """V2 user should be denied when Kessel denies."""
        req = _make_v2_request()
        self.assertFalse(PrincipalAccessPermission().has_permission(req, None))

    @patch(_PATCH_PRINCIPAL, return_value=True)
    def test_non_get_not_affected(self, mock_kessel):
        """Non-GET methods should not trigger Kessel fallback."""
        req = _make_v2_request(method="POST")
        self.assertFalse(PrincipalAccessPermission().has_permission(req, None))
        mock_kessel.assert_not_called()


class PermissionAccessV2FallbackTest(TestCase):
    """Test PermissionAccessPermission with V2 Kessel fallback."""

    def test_admin_bypass_unchanged(self):
        """Admin users should still be allowed."""
        req = _make_v2_request(admin=True)
        self.assertTrue(PermissionAccessPermission().has_permission(req, None))

    def test_v1_access_still_works(self):
        """Users with V1 permission:read should still be allowed."""
        access = {
            "permission": {"read": ["*"], "write": []},
        }
        req = _make_v2_request(access=access)
        self.assertTrue(PermissionAccessPermission().has_permission(req, None))

    @patch(_PATCH_PERMISSION, return_value=True)
    def test_v2_kessel_grants_read(self, mock_kessel):
        """V2 user with no V1 access should be allowed when Kessel grants."""
        req = _make_v2_request()
        self.assertTrue(PermissionAccessPermission().has_permission(req, None))
        mock_kessel.assert_called_once_with(req)

    @patch(_PATCH_PERMISSION, return_value=False)
    def test_v2_kessel_denies_read(self, mock_kessel):
        """V2 user should be denied when Kessel denies."""
        req = _make_v2_request()
        self.assertFalse(PermissionAccessPermission().has_permission(req, None))


class RoleAccessV2FallbackTest(TestCase):
    """Test RoleAccessPermission with V2 Kessel fallback."""

    def test_admin_bypass_unchanged(self):
        """Admin users should still be allowed."""
        req = _make_v2_request(admin=True)
        self.assertTrue(RoleAccessPermission().has_permission(req, None))

    def test_v1_access_still_works(self):
        """Users with V1 role:read should still be allowed."""
        access = {
            "role": {"read": ["*"], "write": []},
        }
        req = _make_v2_request(access=access, query_params={})
        self.assertTrue(RoleAccessPermission().has_permission(req, None))

    @patch(_PATCH_ROLE, return_value=True)
    def test_v2_kessel_grants_read(self, mock_kessel):
        """V2 user with no V1 access should be allowed when Kessel grants."""
        req = _make_v2_request(query_params={})
        self.assertTrue(RoleAccessPermission().has_permission(req, None))
        mock_kessel.assert_called_once_with(req)

    @patch(_PATCH_ROLE, return_value=False)
    def test_v2_kessel_denies_read(self, mock_kessel):
        """V2 user should be denied when Kessel denies."""
        req = _make_v2_request(query_params={})
        self.assertFalse(RoleAccessPermission().has_permission(req, None))

    def test_system_param_bypass_unchanged(self):
        """System roles query should still bypass access checks."""
        req = _make_v2_request(query_params={"system": "true"})
        self.assertTrue(RoleAccessPermission().has_permission(req, None))

    @patch(_PATCH_ROLE, return_value=True)
    def test_write_not_affected(self, mock_kessel):
        """Write operations should not trigger Kessel fallback."""
        req = _make_v2_request(method="POST", query_params={})
        self.assertFalse(RoleAccessPermission().has_permission(req, None))
        mock_kessel.assert_not_called()


class GroupAccessV2FallbackTest(TestCase):
    """Test GroupAccessPermission with V2 Kessel fallback."""

    def setUp(self):
        self.view = Mock()
        self.view.action = "list"
        self.view.basename = "group"

    def test_admin_bypass_unchanged(self):
        """Admin users should still be allowed."""
        req = _make_v2_request(admin=True)
        self.assertTrue(GroupAccessPermission().has_permission(req, self.view))

    def test_v1_access_still_works(self):
        """Users with V1 group:read should still be allowed."""
        access = {
            "group": {"read": ["*"], "write": []},
        }
        req = _make_v2_request(access=access)
        self.assertTrue(GroupAccessPermission().has_permission(req, self.view))

    @patch(_PATCH_GROUP, return_value=True)
    def test_v2_kessel_grants_read(self, mock_kessel):
        """V2 user with no V1 access should be allowed when Kessel grants."""
        req = _make_v2_request()
        self.assertTrue(GroupAccessPermission().has_permission(req, self.view))
        mock_kessel.assert_called_once_with(req)

    @patch(_PATCH_GROUP, return_value=False)
    def test_v2_kessel_denies_read(self, mock_kessel):
        """V2 user should be denied when Kessel denies."""
        req = _make_v2_request()
        self.assertFalse(GroupAccessPermission().has_permission(req, self.view))

    def test_own_groups_bypass_unchanged(self):
        """Users querying their own groups should still be allowed."""
        req = _make_v2_request(username="test_user", query_params={"username": "test_user"})
        self.assertTrue(GroupAccessPermission().has_permission(req, self.view))

    @patch(_PATCH_GROUP, return_value=True)
    def test_write_not_affected(self, mock_kessel):
        """Write operations should not trigger Kessel fallback."""
        req = _make_v2_request(method="POST")
        self.assertFalse(GroupAccessPermission().has_permission(req, self.view))
        mock_kessel.assert_not_called()

    @patch(_PATCH_GROUP, return_value=True)
    def test_v2_kessel_grants_detail_read(self, mock_kessel):
        """V2 user should be allowed for detail (retrieve) read via Kessel."""
        view = Mock()
        view.action = "retrieve"
        view.basename = "group"
        req = _make_v2_request()
        self.assertTrue(GroupAccessPermission().has_permission(req, view))
        mock_kessel.assert_called_once_with(req)
