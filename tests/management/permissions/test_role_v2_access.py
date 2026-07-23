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
"""Tests for Role V2 access permissions using Kessel Inventory API."""

from unittest.mock import MagicMock, Mock, patch

from django.test import TestCase

from management.permissions.role_v2_access import RoleV2KesselAccessPermission


class RoleV2KesselAccessPermissionTest(TestCase):
    """Unit tests for RoleV2KesselAccessPermission."""

    def setUp(self):
        """Set up common test fixtures."""
        self.permission = RoleV2KesselAccessPermission()
        self.tenant = Mock()
        self.tenant.tenant_resource_id.return_value = "redhat/12345"

    def _make_request(self, tenant=None):
        """Build a mock request with the given tenant."""
        request = Mock()
        request.tenant = tenant if tenant is not None else self.tenant
        return request

    def _make_view(self, action):
        """Build a mock view with the given action."""
        view = Mock()
        view.action = action
        return view

    # --- Relation mapping tests ---

    def test_get_relation_returns_read_for_list(self):
        """list action should map to rbac_roles_read."""
        view = self._make_view("list")
        self.assertEqual(self.permission._get_relation(view), "rbac_roles_read")

    def test_get_relation_returns_read_for_retrieve(self):
        """retrieve action should map to rbac_roles_read."""
        view = self._make_view("retrieve")
        self.assertEqual(self.permission._get_relation(view), "rbac_roles_read")

    def test_get_relation_returns_write_for_create(self):
        """create action should map to rbac_roles_write."""
        view = self._make_view("create")
        self.assertEqual(self.permission._get_relation(view), "rbac_roles_write")

    def test_get_relation_returns_write_for_update(self):
        """update action should map to rbac_roles_write."""
        view = self._make_view("update")
        self.assertEqual(self.permission._get_relation(view), "rbac_roles_write")

    def test_get_relation_returns_write_for_bulk_destroy(self):
        """bulk_destroy action should map to rbac_roles_write."""
        view = self._make_view("bulk_destroy")
        self.assertEqual(self.permission._get_relation(view), "rbac_roles_write")

    def test_get_relation_defaults_to_read_for_unknown_action(self):
        """Unknown actions should default to rbac_roles_read."""
        view = self._make_view("unknown")
        self.assertEqual(self.permission._get_relation(view), "rbac_roles_read")

    # --- Checker integration tests ---

    @patch("management.permissions.role_v2_access.get_kessel_principal_id")
    @patch("management.permissions.role_v2_access.WorkspaceInventoryAccessChecker")
    def test_read_action_calls_checker_with_rbac_roles_read(self, mock_checker_class, mock_get_principal_id):
        """Read action should call checker with rbac_roles_read relation."""
        mock_get_principal_id.return_value = "redhat/test-user-123"

        mock_checker = MagicMock()
        mock_checker.check_resource_access.return_value = True
        mock_checker_class.return_value = mock_checker

        request = self._make_request()
        view = self._make_view("list")

        result = self.permission.has_permission(request, view)

        self.assertTrue(result)
        mock_checker.check_resource_access.assert_called_once()
        call_kwargs = mock_checker.check_resource_access.call_args[1]
        self.assertEqual(call_kwargs["relation"], "rbac_roles_read")
        self.assertEqual(call_kwargs["resource_type"], "tenant")
        self.assertEqual(call_kwargs["resource_id"], "redhat/12345")
        self.assertEqual(call_kwargs["principal_id"], "redhat/test-user-123")

    @patch("management.permissions.role_v2_access.get_kessel_principal_id")
    @patch("management.permissions.role_v2_access.WorkspaceInventoryAccessChecker")
    def test_write_action_calls_checker_with_rbac_roles_write(self, mock_checker_class, mock_get_principal_id):
        """Write action should call checker with rbac_roles_write relation."""
        mock_get_principal_id.return_value = "redhat/test-user-123"

        mock_checker = MagicMock()
        mock_checker.check_resource_access.return_value = True
        mock_checker_class.return_value = mock_checker

        request = self._make_request()
        view = self._make_view("create")

        result = self.permission.has_permission(request, view)

        self.assertTrue(result)
        mock_checker.check_resource_access.assert_called_once()
        call_kwargs = mock_checker.check_resource_access.call_args[1]
        self.assertEqual(call_kwargs["relation"], "rbac_roles_write")
        self.assertEqual(call_kwargs["resource_type"], "tenant")
        self.assertEqual(call_kwargs["resource_id"], "redhat/12345")

    @patch("management.permissions.role_v2_access.get_kessel_principal_id")
    @patch("management.permissions.role_v2_access.WorkspaceInventoryAccessChecker")
    def test_access_granted_when_checker_returns_true(self, mock_checker_class, mock_get_principal_id):
        """Permission should be granted when checker returns True."""
        mock_get_principal_id.return_value = "redhat/test-user-123"

        mock_checker = MagicMock()
        mock_checker.check_resource_access.return_value = True
        mock_checker_class.return_value = mock_checker

        request = self._make_request()
        view = self._make_view("retrieve")

        result = self.permission.has_permission(request, view)

        self.assertTrue(result)

    @patch("management.permissions.role_v2_access.get_kessel_principal_id")
    @patch("management.permissions.role_v2_access.WorkspaceInventoryAccessChecker")
    def test_access_denied_when_checker_returns_false(self, mock_checker_class, mock_get_principal_id):
        """Permission should be denied when checker returns False."""
        mock_get_principal_id.return_value = "redhat/test-user-123"

        mock_checker = MagicMock()
        mock_checker.check_resource_access.return_value = False
        mock_checker_class.return_value = mock_checker

        request = self._make_request()
        view = self._make_view("list")

        result = self.permission.has_permission(request, view)

        self.assertFalse(result)

    # --- Edge case / denial tests ---

    def test_denied_when_tenant_is_none(self):
        """Permission should be denied when request has no tenant."""
        request = Mock(spec=[])
        request.tenant = None

        view = self._make_view("list")

        result = self.permission.has_permission(request, view)

        self.assertFalse(result)

    def test_denied_when_tenant_missing_from_request(self):
        """Permission should be denied when request has no tenant attribute."""
        request = Mock(spec=[])
        view = self._make_view("list")

        result = self.permission.has_permission(request, view)

        self.assertFalse(result)

    def test_denied_when_tenant_resource_id_is_none(self):
        """Permission should be denied when tenant has no resource ID."""
        tenant = Mock()
        tenant.tenant_resource_id.return_value = None

        request = self._make_request(tenant=tenant)
        view = self._make_view("list")

        result = self.permission.has_permission(request, view)

        self.assertFalse(result)

    @patch("management.permissions.role_v2_access.get_kessel_principal_id")
    def test_denied_when_principal_id_is_none(self, mock_get_principal_id):
        """Permission should be denied when principal ID cannot be determined."""
        mock_get_principal_id.return_value = None

        request = self._make_request()
        view = self._make_view("list")

        result = self.permission.has_permission(request, view)

        self.assertFalse(result)

    @patch("management.permissions.role_v2_access.get_kessel_principal_id")
    @patch("management.permissions.role_v2_access.WorkspaceInventoryAccessChecker")
    def test_denied_on_inventory_connectivity_error(self, mock_checker_class, mock_get_principal_id):
        """Permission should be denied when Inventory API is unreachable (fail closed).

        WorkspaceInventoryAccessChecker.check_resource_access returns False on
        gRPC connectivity errors internally, so the permission class sees False.
        """
        mock_get_principal_id.return_value = "redhat/test-user-123"

        mock_checker = MagicMock()
        mock_checker.check_resource_access.return_value = False
        mock_checker_class.return_value = mock_checker

        request = self._make_request()
        view = self._make_view("list")

        result = self.permission.has_permission(request, view)

        self.assertFalse(result)
        mock_checker.check_resource_access.assert_called_once()

    # --- Verify all write actions ---

    @patch("management.permissions.role_v2_access.get_kessel_principal_id")
    @patch("management.permissions.role_v2_access.WorkspaceInventoryAccessChecker")
    def test_update_action_uses_write_relation(self, mock_checker_class, mock_get_principal_id):
        """update action should use rbac_roles_write relation."""
        mock_get_principal_id.return_value = "redhat/test-user-123"

        mock_checker = MagicMock()
        mock_checker.check_resource_access.return_value = True
        mock_checker_class.return_value = mock_checker

        request = self._make_request()
        view = self._make_view("update")

        self.permission.has_permission(request, view)

        call_kwargs = mock_checker.check_resource_access.call_args[1]
        self.assertEqual(call_kwargs["relation"], "rbac_roles_write")

    @patch("management.permissions.role_v2_access.get_kessel_principal_id")
    @patch("management.permissions.role_v2_access.WorkspaceInventoryAccessChecker")
    def test_bulk_destroy_action_uses_write_relation(self, mock_checker_class, mock_get_principal_id):
        """bulk_destroy action should use rbac_roles_write relation."""
        mock_get_principal_id.return_value = "redhat/test-user-123"

        mock_checker = MagicMock()
        mock_checker.check_resource_access.return_value = True
        mock_checker_class.return_value = mock_checker

        request = self._make_request()
        view = self._make_view("bulk_destroy")

        self.permission.has_permission(request, view)

        call_kwargs = mock_checker.check_resource_access.call_args[1]
        self.assertEqual(call_kwargs["relation"], "rbac_roles_write")

    @patch("management.permissions.role_v2_access.get_kessel_principal_id")
    @patch("management.permissions.role_v2_access.WorkspaceInventoryAccessChecker")
    def test_retrieve_action_uses_read_relation(self, mock_checker_class, mock_get_principal_id):
        """retrieve action should use rbac_roles_read relation."""
        mock_get_principal_id.return_value = "redhat/test-user-123"

        mock_checker = MagicMock()
        mock_checker.check_resource_access.return_value = True
        mock_checker_class.return_value = mock_checker

        request = self._make_request()
        view = self._make_view("retrieve")

        self.permission.has_permission(request, view)

        call_kwargs = mock_checker.check_resource_access.call_args[1]
        self.assertEqual(call_kwargs["relation"], "rbac_roles_read")
