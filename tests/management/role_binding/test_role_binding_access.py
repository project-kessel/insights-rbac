#
# Copyright 2025 Red Hat, Inc.
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
"""Tests for role binding access permissions using Inventory API."""

from importlib import reload
from unittest.mock import MagicMock, Mock, patch

import requests
from django.test import TestCase, TransactionTestCase
from django.test.utils import override_settings
from django.urls import clear_url_caches, reverse
from kessel.inventory.v1beta2 import allowed_pb2
from rest_framework import status
from rest_framework.test import APIClient

from management.models import Workspace
from rest_framework.exceptions import ParseError

from management.permissions.role_binding_access import (
    RoleBindingKesselAccessPermission,
    RoleBindingSystemUserAccessPermission,
)
from rbac import urls
from tests.identity_request import BaseIdentityRequest


class TransactionIdentityRequest(BaseIdentityRequest, TransactionTestCase):
    """Identity request test base class that uses TransactionTestCase."""

    pass


class RoleBindingAccessTestMixin:
    """Mixin providing common setup for role binding access tests."""

    def setUp(self):
        """Set up the role binding access tests."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.tenant.save()

        self.client = APIClient()

        # Create workspace hierarchy
        self.root_workspace = Workspace.objects.create(
            name=Workspace.SpecialNames.ROOT,
            tenant=self.tenant,
            type=Workspace.Types.ROOT,
        )
        self.default_workspace = Workspace.objects.create(
            name=Workspace.SpecialNames.DEFAULT,
            tenant=self.tenant,
            type=Workspace.Types.DEFAULT,
            parent=self.root_workspace,
        )
        self.workspace = Workspace.objects.create(
            name="Test Workspace",
            description="Test workspace description",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.default_workspace,
        )

    def tearDown(self):
        """Tear down test data."""
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.STANDARD).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.DEFAULT).delete()
        Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.ROOT).delete()
        super().tearDown()

    def _get_list_url(self):
        """Get the list URL."""
        return reverse("v2_management:role-bindings-list")

    def _get_by_subject_url(self):
        """Get the by-subject URL."""
        return reverse("v2_management:role-bindings-by-subject")

    def _setup_kessel_mock(self, mock_inventory_client, allowed=allowed_pb2.Allowed.ALLOWED_TRUE):
        """
        Set up the Kessel Inventory mock with the specified allowed status.

        Args:
            mock_inventory_client: The mocked inventory_client
            allowed: The allowed status to return (default: ALLOWED_TRUE)

        Returns:
            tuple: (mock_stub, mock_response) for additional assertions
        """
        mock_stub = MagicMock()
        mock_response = MagicMock()
        mock_response.allowed = allowed
        mock_stub.CheckForUpdate.return_value = mock_response
        mock_inventory_client.return_value.__enter__.return_value = mock_stub
        return mock_stub, mock_response


@override_settings(V2_APIS_ENABLED=True)
class RoleBindingAccessIntegrationTests(RoleBindingAccessTestMixin, TransactionIdentityRequest):
    """Integration tests for role binding access with full request flow."""

    @patch("management.permissions.workspace_inventory_access.inventory_client")
    def test_access_granted_when_inventory_returns_allowed(self, mock_inventory_client):
        """Test that access is granted when Inventory API returns ALLOWED_TRUE."""
        self._setup_kessel_mock(mock_inventory_client, allowed_pb2.Allowed.ALLOWED_TRUE)

        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
        headers = request_context["request"].META

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace",
            **headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @patch("management.permissions.workspace_inventory_access.inventory_client")
    def test_access_denied_when_inventory_returns_not_allowed(self, mock_inventory_client):
        """Test that access is denied when Inventory API returns ALLOWED_FALSE."""
        self._setup_kessel_mock(mock_inventory_client, allowed_pb2.Allowed.ALLOWED_FALSE)

        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
        headers = request_context["request"].META

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace",
            **headers,
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch("management.permissions.workspace_inventory_access.inventory_client")
    def test_admin_user_goes_through_kessel_check(self, mock_inventory_client):
        """Test that admin users go through Kessel permission check."""
        self._setup_kessel_mock(mock_inventory_client)

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Admin users should go through Kessel check
        mock_inventory_client.assert_called()

    @patch("management.permissions.workspace_inventory_access.inventory_client")
    def test_check_uses_role_binding_view_relation(self, mock_inventory_client):
        """Test that the permission check uses role_binding_view relation."""
        mock_stub, _ = self._setup_kessel_mock(mock_inventory_client)

        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
        headers = request_context["request"].META

        url = self._get_by_subject_url()
        self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace",
            **headers,
        )

        mock_stub.CheckForUpdate.assert_called_once()
        call_args = mock_stub.CheckForUpdate.call_args
        request_obj = call_args[0][0]
        self.assertEqual(request_obj.relation, "role_binding_view")

    @patch("management.permissions.workspace_inventory_access.inventory_client")
    def test_check_uses_correct_workspace_id(self, mock_inventory_client):
        """Test that the permission check uses the correct workspace ID."""
        mock_stub, _ = self._setup_kessel_mock(mock_inventory_client)

        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
        headers = request_context["request"].META

        url = self._get_by_subject_url()
        self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace",
            **headers,
        )

        mock_stub.CheckForUpdate.assert_called_once()
        call_args = mock_stub.CheckForUpdate.call_args
        request_obj = call_args[0][0]
        self.assertEqual(request_obj.object.resource_id, str(self.workspace.id))
        self.assertEqual(request_obj.object.resource_type, "workspace")

    @patch("management.permissions.workspace_inventory_access.inventory_client")
    def test_access_denied_on_inventory_connectivity_error(self, mock_inventory_client):
        """Test that access is denied when Inventory API is unreachable."""
        import grpc

        mock_inventory_client.return_value.__enter__.side_effect = grpc.RpcError()

        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
        headers = request_context["request"].META

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace",
            **headers,
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch("management.permissions.workspace_inventory_access.inventory_client")
    def test_unexpected_allowed_status_returns_false(self, mock_inventory_client):
        """Test that unexpected allowed status from Inventory API returns False."""
        self._setup_kessel_mock(mock_inventory_client, allowed_pb2.Allowed.ALLOWED_UNSPECIFIED)

        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
        headers = request_context["request"].META

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace",
            **headers,
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch("management.permissions.workspace_inventory_access.inventory_client")
    @patch("management.principal.proxy.PrincipalProxy")
    @patch("management.utils.get_principal_from_request", return_value=None)
    def test_principal_id_retrieved_from_it_service(self, mock_get_principal, mock_proxy_class, mock_inventory_client):
        """Test that principal_id is retrieved from IT service when not available locally."""
        test_user_id = "it-service-user-456"
        mock_proxy = MagicMock()
        mock_proxy.request_filtered_principals.return_value = {
            "status_code": 200,
            "data": [{"user_id": test_user_id, "username": "testuser"}],
        }
        mock_proxy_class.return_value = mock_proxy

        self._setup_kessel_mock(mock_inventory_client)

        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
        headers = request_context["request"].META

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace",
            **headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @patch("management.permissions.workspace_inventory_access.inventory_client")
    def test_nul_bytes_stripped_from_resource_id(self, mock_inventory_client):
        """Test that NUL bytes are stripped from resource_id in permission check."""
        self._setup_kessel_mock(mock_inventory_client)

        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
        headers = request_context["request"].META

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id=\x00{self.workspace.id}\x00&resource_type=workspace",
            **headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_access_granted_for_tenant_resource_type_when_org_admin(self):
        """Test that access is granted for resource_type=tenant when user is org admin."""
        tenant_resource_id = self.tenant.tenant_resource_id()
        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=True)
        headers = request_context["request"].META

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={tenant_resource_id}&resource_type=tenant",
            **headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_access_denied_for_tenant_when_not_org_admin(self):
        """Test that access is denied for resource_type=tenant when user is not org admin."""
        tenant_resource_id = self.tenant.tenant_resource_id()
        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
        headers = request_context["request"].META

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={tenant_resource_id}&resource_type=tenant",
            **headers,
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_access_denied_for_tenant_when_resource_id_mismatch(self):
        """Test that access is denied when resource_type=tenant but resource_id does not match user's tenant."""
        # Use a resource_id that doesn't match the request's tenant (e.g., another org's tenant)
        other_tenant_resource_id = "localhost/other-org-12345"
        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=True)
        headers = request_context["request"].META

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={other_tenant_resource_id}&resource_type=tenant",
            **headers,
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@override_settings(V2_APIS_ENABLED=True)
class RoleBindingInvalidResourceTypeTests(RoleBindingAccessTestMixin, TransactionIdentityRequest):
    """Integration tests for invalid resource_type returning 400."""

    def test_invalid_resource_type_returns_400_on_by_subject(self):
        """GET by_subject with invalid resource_type should return 400 with allowed values."""
        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
        headers = request_context["request"].META

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=invalid",
            **headers,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response_data = response.json()
        self.assertIn("invalid", str(response_data))
        self.assertIn("tenant", str(response_data))
        self.assertIn("workspace", str(response_data))

    def test_invalid_resource_type_returns_400_on_list(self):
        """GET list with invalid resource_type should return 400 with allowed values."""
        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
        headers = request_context["request"].META

        url = self._get_list_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=invalid",
            **headers,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response_data = response.json()
        self.assertIn("invalid", str(response_data))
        self.assertIn("tenant", str(response_data))
        self.assertIn("workspace", str(response_data))

    def test_invalid_resource_type_returns_400_on_put_by_subject(self):
        """PUT by_subject with invalid resource_type should return 400 with allowed values."""
        url = self._get_by_subject_url()
        response = self.client.put(
            f"{url}?resource_id={self.workspace.id}&resource_type=invalid",
            data={"requests": []},
            content_type="application/json",
            **self.headers,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response_data = response.json()
        self.assertIn("invalid", str(response_data))

    def test_valid_resource_types_still_work(self):
        """Ensure valid resource_type values (workspace, tenant) are not rejected."""
        # Tenant resource type for org admin
        tenant_resource_id = self.tenant.tenant_resource_id()
        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=True)
        headers = request_context["request"].META

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={tenant_resource_id}&resource_type=tenant",
            **headers,
        )

        # Should NOT be 400 — tenant is a valid resource_type
        self.assertNotEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_no_resource_params_passes_through(self):
        """No resource params at all should pass through (no validation error)."""
        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
        headers = request_context["request"].META

        url = self._get_list_url()
        response = self.client.get(url, **headers)

        # No resource params — passes through without 400
        self.assertNotEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_case_insensitive_resource_type(self):
        """resource_type should be case-insensitive (normalized to lowercase)."""
        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=True)
        headers = request_context["request"].META

        tenant_resource_id = self.tenant.tenant_resource_id()
        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={tenant_resource_id}&resource_type=TENANT",
            **headers,
        )

        # TENANT uppercased should be normalized to tenant — not 400
        self.assertNotEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@override_settings(V2_APIS_ENABLED=True)
class RoleBindingSystemUserPermissionTests(RoleBindingAccessTestMixin, TransactionIdentityRequest):
    """Unit tests for RoleBindingSystemUserAccessPermission."""

    def test_system_user_admin_passes_through(self):
        """Test that system user with admin=True passes through to Kessel check."""
        permission = RoleBindingSystemUserAccessPermission()

        mock_request = Mock()
        mock_request.user.system = True
        mock_request.user.admin = True
        mock_request.query_params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        # Should return True to pass through to next permission class (Kessel check)
        self.assertTrue(result)

    def test_system_user_non_admin_denied(self):
        """Test that system user without admin=True is denied."""
        permission = RoleBindingSystemUserAccessPermission()

        mock_request = Mock()
        mock_request.user.system = True
        mock_request.user.admin = False
        mock_request.query_params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        self.assertFalse(result)

    def test_regular_admin_passes_through(self):
        """Test that regular admin user passes through to Kessel check."""
        permission = RoleBindingSystemUserAccessPermission()

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = True
        mock_request.query_params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        # Should return True to pass through to next permission class (Kessel check)
        self.assertTrue(result)

    def test_non_admin_non_system_passes_through(self):
        """Test that non-admin, non-system user passes through to next permission."""
        permission = RoleBindingSystemUserAccessPermission()

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.query_params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        # Should pass through (return True) to let next permission class check
        self.assertTrue(result)

    def test_handles_user_without_admin_attribute(self):
        """Test that permission handles user without admin attribute safely."""
        permission = RoleBindingSystemUserAccessPermission()

        mock_request = Mock(spec=[])
        mock_request.user = Mock(spec=[])

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        self.assertTrue(result)

    def test_handles_user_without_system_attribute(self):
        """Test that permission handles user without system attribute safely."""
        permission = RoleBindingSystemUserAccessPermission()

        mock_request = Mock()
        mock_request.user = Mock(spec=["admin"])
        mock_request.user.admin = False

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        self.assertTrue(result)


@override_settings(V2_APIS_ENABLED=True)
class RoleBindingKesselPermissionTests(RoleBindingAccessTestMixin, TransactionIdentityRequest):
    """Unit tests for RoleBindingKesselAccessPermission checker integration."""

    @patch("management.permissions.role_binding_access.get_kessel_principal_id")
    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    def test_kessel_permission_calls_checker_with_correct_relation(self, mock_checker_class, mock_get_principal_id):
        """Kessel permission should call checker with role_binding_view relation."""
        permission = RoleBindingKesselAccessPermission()

        mock_get_principal_id.return_value = "localhost/test-user-123"

        mock_checker = MagicMock()
        mock_checker.check_resource_access.return_value = True
        mock_checker_class.return_value = mock_checker

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.query_params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        self.assertTrue(result)
        mock_checker.check_resource_access.assert_called_once()
        call_kwargs = mock_checker.check_resource_access.call_args[1]
        self.assertEqual(call_kwargs["relation"], "role_binding_view")
        self.assertEqual(call_kwargs["resource_id"], str(self.workspace.id))
        self.assertEqual(call_kwargs["resource_type"], "workspace")
        self.assertEqual(call_kwargs["principal_id"], mock_get_principal_id.return_value)

    @patch("management.permissions.role_binding_access.get_kessel_principal_id")
    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    def test_kessel_permission_denies_when_checker_returns_false(self, mock_checker_class, mock_get_principal_id):
        """Kessel permission should deny when checker returns False."""
        permission = RoleBindingKesselAccessPermission()

        mock_get_principal_id.return_value = "localhost/test-user-123"

        mock_checker = MagicMock()
        mock_checker.check_resource_access.return_value = False
        mock_checker_class.return_value = mock_checker

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.query_params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        self.assertFalse(result)

    @patch("management.permissions.role_binding_access.get_kessel_principal_id")
    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    def test_kessel_permission_allows_when_checker_returns_true(self, mock_checker_class, mock_get_principal_id):
        """Kessel permission should allow when checker returns True."""
        permission = RoleBindingKesselAccessPermission()

        mock_get_principal_id.return_value = "localhost/test-user-123"

        mock_checker = MagicMock()
        mock_checker.check_resource_access.return_value = True
        mock_checker_class.return_value = mock_checker

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.query_params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        self.assertTrue(result)

    @patch("management.permissions.role_binding_access.get_kessel_principal_id")
    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    def test_kessel_permission_raises_400_for_unknown_resource_type(self, mock_checker_class, mock_get_principal_id):
        """Kessel permission should raise ParseError (400) for unknown resource types."""
        permission = RoleBindingKesselAccessPermission()

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.query_params = {
            "resource_id": "custom-resource-123",
            "resource_type": "unknown_resource",
        }

        mock_view = Mock()
        mock_view.action = "by_subject"

        with self.assertRaises(ParseError) as ctx:
            permission.has_permission(mock_request, mock_view)

        self.assertIn("unknown_resource", str(ctx.exception.detail))
        self.assertIn("tenant", str(ctx.exception.detail))
        self.assertIn("workspace", str(ctx.exception.detail))
        mock_get_principal_id.assert_not_called()
        mock_checker_class.assert_not_called()

    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    def test_kessel_permission_allows_tenant_resource_type_when_org_admin(self, mock_checker_class):
        """Tenant resource type: org admin allowed without Kessel check."""
        permission = RoleBindingKesselAccessPermission()

        mock_checker = MagicMock()
        mock_checker_class.return_value = mock_checker

        tenant_resource_id = self.tenant.tenant_resource_id()
        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = True
        mock_request.query_params = {
            "resource_id": tenant_resource_id,
            "resource_type": "tenant",
        }
        mock_request.tenant = self.tenant

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        self.assertTrue(result)
        mock_checker.check_resource_access.assert_not_called()

    def test_kessel_permission_denies_tenant_when_not_org_admin(self):
        """Tenant resource type: non-org-admin denied."""
        permission = RoleBindingKesselAccessPermission()

        tenant_resource_id = self.tenant.tenant_resource_id()
        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.query_params = {
            "resource_id": tenant_resource_id,
            "resource_type": "tenant",
        }
        mock_request.tenant = self.tenant

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        self.assertFalse(result)

    def test_kessel_permission_denies_tenant_when_resource_id_mismatch(self):
        """Tenant resource type: org admin denied when resource_id does not match tenant."""
        permission = RoleBindingKesselAccessPermission()

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = True
        mock_request.query_params = {
            "resource_id": "localhost/other-org-12345",
            "resource_type": "tenant",
        }
        mock_request.tenant = self.tenant

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        self.assertFalse(result)

    @patch("management.permissions.role_binding_access.get_kessel_principal_id")
    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    def test_kessel_permission_normalizes_resource_type_to_lowercase(self, mock_checker_class, mock_get_principal_id):
        """Kessel permission should normalize resource_type to lowercase."""
        permission = RoleBindingKesselAccessPermission()

        mock_get_principal_id.return_value = "localhost/test-user-123"

        mock_checker = MagicMock()
        mock_checker.check_resource_access.return_value = True
        mock_checker_class.return_value = mock_checker

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.query_params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "WORKSPACE",
        }

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        self.assertTrue(result)
        mock_checker.check_resource_access.assert_called_once()
        call_kwargs = mock_checker.check_resource_access.call_args[1]
        self.assertEqual(call_kwargs["resource_type"], "workspace")

    @patch("management.permissions.role_binding_access.FEATURE_FLAGS")
    @patch("management.permissions.role_binding_access.get_kessel_principal_id")
    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    def test_kessel_permission_uses_view_relation_when_feature_flag_disabled(
        self, mock_checker_class, mock_get_principal_id, mock_feature_flags
    ):
        """Kessel permission should use 'view' relation when feature flag is disabled."""
        permission = RoleBindingKesselAccessPermission()

        mock_feature_flags.is_use_role_binding_view_permission_enabled.return_value = False
        mock_get_principal_id.return_value = "localhost/test-user-123"

        mock_checker = MagicMock()
        mock_checker.check_resource_access.return_value = True
        mock_checker_class.return_value = mock_checker

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.query_params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        self.assertTrue(result)
        mock_checker.check_resource_access.assert_called_once()
        call_kwargs = mock_checker.check_resource_access.call_args[1]
        self.assertEqual(call_kwargs["relation"], "view")

    @patch("management.permissions.role_binding_access.FEATURE_FLAGS")
    @patch("management.permissions.role_binding_access.get_kessel_principal_id")
    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    def test_kessel_permission_uses_role_binding_view_relation_when_feature_flag_enabled(
        self, mock_checker_class, mock_get_principal_id, mock_feature_flags
    ):
        """Kessel permission should use 'role_binding_view' relation when feature flag is enabled."""
        permission = RoleBindingKesselAccessPermission()

        mock_feature_flags.is_use_role_binding_view_permission_enabled.return_value = True
        mock_get_principal_id.return_value = "localhost/test-user-123"

        mock_checker = MagicMock()
        mock_checker.check_resource_access.return_value = True
        mock_checker_class.return_value = mock_checker

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.query_params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        self.assertTrue(result)
        mock_checker.check_resource_access.assert_called_once()
        call_kwargs = mock_checker.check_resource_access.call_args[1]
        self.assertEqual(call_kwargs["relation"], "role_binding_view")

    def test_kessel_permission_denies_unrecognized_action(self):
        """Kessel permission should deny access for unrecognized view actions (fail-closed)."""
        permission = RoleBindingKesselAccessPermission()

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.query_params = {}

        mock_view = Mock()
        mock_view.action = "some_future_action"

        result = permission.has_permission(mock_request, mock_view)

        self.assertFalse(result)

    def test_kessel_permission_rejects_invalid_uuid_resource_id(self):
        """Workspace resource_id must be a valid UUID; invalid values return 400."""
        from rest_framework.exceptions import ParseError

        permission = RoleBindingKesselAccessPermission()

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.query_params = {
            "resource_id": "not-a-uuid",
            "resource_type": "workspace",
        }

        mock_view = Mock()
        mock_view.action = "by_subject"

        with self.assertRaises(ParseError) as ctx:
            permission.has_permission(mock_request, mock_view)

        self.assertIn("not-a-uuid", str(ctx.exception.detail))
        self.assertIn("not a valid UUID", str(ctx.exception.detail))

    def test_kessel_permission_rejects_empty_string_resource_id(self):
        """Empty string resource_id with workspace type is filtered out by _parse_query_resource."""
        permission = RoleBindingKesselAccessPermission()

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.query_params = {
            "resource_id": "",
            "resource_type": "workspace",
        }

        mock_view = Mock()
        mock_view.action = "list"

        # Empty resource_id is filtered by _parse_query_resource → pass-through
        result = permission.has_permission(mock_request, mock_view)
        self.assertTrue(result)

    def test_kessel_permission_rejects_numeric_string_resource_id(self):
        """Numeric string is not a valid UUID for workspace resource_id."""
        from rest_framework.exceptions import ParseError

        permission = RoleBindingKesselAccessPermission()

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.query_params = {
            "resource_id": "12345",
            "resource_type": "workspace",
        }

        mock_view = Mock()
        mock_view.action = "list"

        with self.assertRaises(ParseError):
            permission.has_permission(mock_request, mock_view)

    def test_kessel_permission_accepts_valid_uuid_resource_id(self):
        """Valid UUID resource_id for workspace should not raise ParseError."""
        import uuid

        permission = RoleBindingKesselAccessPermission()

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.query_params = {
            "resource_id": str(uuid.uuid4()),
            "resource_type": "workspace",
        }

        mock_view = Mock()
        mock_view.action = "by_subject"

        # Should not raise ParseError; will proceed to Kessel check (which may deny)
        with patch("management.permissions.role_binding_access.get_kessel_principal_id") as mock_principal:
            mock_principal.return_value = None
            result = permission.has_permission(mock_request, mock_view)
            # Denied because principal_id is None, but no ParseError raised
            self.assertFalse(result)

    def test_kessel_permission_allows_non_uuid_for_tenant_resource_type(self):
        """Tenant resource_type does not require UUID format for resource_id."""
        permission = RoleBindingKesselAccessPermission()

        tenant_resource_id = self.tenant.tenant_resource_id()
        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = True
        mock_request.query_params = {
            "resource_id": tenant_resource_id,
            "resource_type": "tenant",
        }
        mock_request.tenant = self.tenant

        mock_view = Mock()
        mock_view.action = "by_subject"

        # Should not raise ParseError — tenant resource_id is not a UUID
        result = permission.has_permission(mock_request, mock_view)
        self.assertTrue(result)


@override_settings(V2_APIS_ENABLED=True)
class RoleBindingInvalidUuidIntegrationTests(RoleBindingAccessTestMixin, TransactionIdentityRequest):
    """Integration tests verifying 400 response for invalid workspace resource_id."""

    def test_list_with_invalid_uuid_returns_400(self):
        """GET /v2/role-bindings/?resource_id=invalid&resource_type=workspace returns 400."""
        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
        request_context["request"].META["QUERY_STRING"] = "resource_id=not-a-uuid&resource_type=workspace"

        url = self._get_list_url()
        response = self.client.get(
            f"{url}?resource_id=not-a-uuid&resource_type=workspace",
            **request_context["request"].META,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not a valid UUID", str(response.data))

    def test_by_subject_with_invalid_uuid_returns_400(self):
        """GET /v2/role-bindings/by-subject/?resource_id=invalid&resource_type=workspace returns 400."""
        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id=abc-xyz&resource_type=workspace",
            **request_context["request"].META,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not a valid UUID", str(response.data))


@override_settings(V2_APIS_ENABLED=True)
class RoleBindingPrincipalLookupTests(RoleBindingAccessTestMixin, TransactionIdentityRequest):
    """Unit tests for principal lookup logic via get_kessel_principal_id utility."""

    @patch("management.utils.get_principal_from_request", return_value=None)
    @patch("management.principal.proxy.PrincipalProxy")
    def test_principal_lookup_fails_when_it_service_returns_non_200(self, mock_proxy_class, mock_get_principal):
        """Test that access is denied when IT service returns non-200 status."""
        permission = RoleBindingKesselAccessPermission()

        mock_proxy = MagicMock()
        mock_proxy.request_filtered_principals.return_value = {
            "status_code": 500,
            "data": [],
        }
        mock_proxy_class.return_value = mock_proxy

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.user.user_id = None
        mock_request.user.username = "testuser"
        mock_request.user.org_id = "test-org"
        mock_request.query_params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        self.assertFalse(result)

    @patch("management.utils.get_principal_from_request", return_value=None)
    @patch("management.principal.proxy.PrincipalProxy")
    def test_principal_lookup_fails_when_it_service_returns_empty_data(self, mock_proxy_class, mock_get_principal):
        """Test that access is denied when IT service returns empty data."""
        permission = RoleBindingKesselAccessPermission()

        mock_proxy = MagicMock()
        mock_proxy.request_filtered_principals.return_value = {
            "status_code": 200,
            "data": [],
        }
        mock_proxy_class.return_value = mock_proxy

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.user.user_id = None
        mock_request.user.username = "testuser"
        mock_request.user.org_id = "test-org"
        mock_request.query_params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        self.assertFalse(result)

    @patch("management.utils.get_principal_from_request", return_value=None)
    @patch("management.principal.proxy.PrincipalProxy")
    def test_principal_lookup_fails_when_it_service_response_missing_user_id(
        self, mock_proxy_class, mock_get_principal
    ):
        """Test that access is denied when IT service response lacks user_id."""
        permission = RoleBindingKesselAccessPermission()

        mock_proxy = MagicMock()
        mock_proxy.request_filtered_principals.return_value = {
            "status_code": 200,
            "data": [{"username": "testuser"}],
        }
        mock_proxy_class.return_value = mock_proxy

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.user.user_id = None
        mock_request.user.username = "testuser"
        mock_request.user.org_id = "test-org"
        mock_request.query_params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        self.assertFalse(result)

    @patch("management.utils.get_principal_from_request", return_value=None)
    def test_principal_lookup_fails_when_request_user_missing_username(self, mock_get_principal):
        """Test that access is denied when request.user is missing username."""
        permission = RoleBindingKesselAccessPermission()

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.user.user_id = None
        mock_request.user.username = None
        mock_request.user.org_id = "test-org"
        mock_request.query_params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        self.assertFalse(result)

    @patch("management.utils.get_principal_from_request", return_value=None)
    def test_principal_lookup_fails_when_request_user_missing_org_id(self, mock_get_principal):
        """Test that access is denied when request.user is missing org_id."""
        permission = RoleBindingKesselAccessPermission()

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.user.user_id = None
        mock_request.user.username = "testuser"
        mock_request.user.org_id = None
        mock_request.query_params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        self.assertFalse(result)

    @patch("management.utils.get_principal_from_request", return_value=None)
    @patch("management.principal.proxy.PrincipalProxy")
    def test_principal_lookup_fails_when_it_service_raises_request_exception(
        self, mock_proxy_class, mock_get_principal
    ):
        """Test that access is denied when PrincipalProxy raises a network error."""
        permission = RoleBindingKesselAccessPermission()

        mock_proxy = MagicMock()
        mock_proxy.request_filtered_principals.side_effect = requests.exceptions.ConnectionError("Network error")
        mock_proxy_class.return_value = mock_proxy

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.user.user_id = None
        mock_request.user.username = "testuser"
        mock_request.user.org_id = "test-org"
        mock_request.query_params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        self.assertFalse(result)

    @patch("management.utils.get_principal_from_request")
    @patch("management.principal.proxy.PrincipalProxy")
    @patch("management.permissions.workspace_inventory_access.inventory_client")
    def test_principal_lookup_fast_path_from_principal(
        self, mock_inventory_client, mock_proxy_class, mock_get_principal
    ):
        """Test fast path when get_principal_from_request returns a Principal with user_id."""
        permission = RoleBindingKesselAccessPermission()

        mock_principal = Mock()
        mock_principal.user_id = "principal-user-123"
        mock_get_principal.return_value = mock_principal

        self._setup_kessel_mock(mock_inventory_client)

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.query_params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        self.assertTrue(result)
        mock_get_principal.assert_called_once()
        mock_proxy_class.assert_not_called()

    @patch("management.utils.get_principal_from_request", return_value=None)
    @patch("management.principal.proxy.PrincipalProxy")
    @patch("management.permissions.workspace_inventory_access.inventory_client")
    def test_principal_lookup_fast_path_from_request_user(
        self, mock_inventory_client, mock_proxy_class, mock_get_principal
    ):
        """Test fast path when request.user.user_id is set."""
        permission = RoleBindingKesselAccessPermission()

        self._setup_kessel_mock(mock_inventory_client)

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.user.user_id = "request-user-123"
        mock_request.query_params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        self.assertTrue(result)
        mock_proxy_class.assert_not_called()


@override_settings(V2_APIS_ENABLED=True)
class RoleBindingServiceAccountTests(RoleBindingAccessTestMixin, TransactionIdentityRequest):
    """Tests for service account access to role bindings."""

    @patch("management.permissions.workspace_inventory_access.inventory_client")
    def test_service_account_goes_through_kessel_check(self, mock_inventory_client):
        """Test that service accounts go through Kessel permission check."""
        self._setup_kessel_mock(mock_inventory_client)

        # Create request context with service account user
        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
        # Simulate service account by modifying the user
        request_context["request"].user.username = "service-account-12345678-1234-1234-1234-123456789012"
        headers = request_context["request"].META

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace",
            **headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Service accounts should go through Kessel check
        mock_inventory_client.assert_called()

    @patch("management.permissions.workspace_inventory_access.inventory_client")
    def test_service_account_denied_when_kessel_returns_false(self, mock_inventory_client):
        """Test that service accounts are denied when Kessel returns ALLOWED_FALSE."""
        self._setup_kessel_mock(mock_inventory_client, allowed_pb2.Allowed.ALLOWED_FALSE)

        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
        request_context["request"].user.username = "service-account-12345678-1234-1234-1234-123456789012"
        headers = request_context["request"].META

        url = self._get_by_subject_url()
        response = self.client.get(
            f"{url}?resource_id={self.workspace.id}&resource_type=workspace",
            **headers,
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch("management.permissions.role_binding_access.get_kessel_principal_id")
    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    def test_service_account_permission_uses_kessel_checker(self, mock_checker_class, mock_get_principal_id):
        """Test that service account permission check uses WorkspaceInventoryAccessChecker."""
        permission = RoleBindingKesselAccessPermission()

        # Return a principal_id formatted for service account
        mock_get_principal_id.return_value = "localhost/sa-user-123"

        mock_checker = MagicMock()
        mock_checker.check_resource_access.return_value = True
        mock_checker_class.return_value = mock_checker

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.user.username = "service-account-12345678-1234-1234-1234-123456789012"
        mock_request.query_params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        self.assertTrue(result)
        mock_checker.check_resource_access.assert_called_once()
        call_kwargs = mock_checker.check_resource_access.call_args[1]
        self.assertEqual(call_kwargs["relation"], "role_binding_view")
        self.assertEqual(call_kwargs["principal_id"], "localhost/sa-user-123")

    @patch("management.permissions.role_binding_access.get_kessel_principal_id")
    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    def test_service_account_denied_when_principal_id_not_found(self, mock_checker_class, mock_get_principal_id):
        """Test that service account is denied when principal_id cannot be determined."""
        permission = RoleBindingKesselAccessPermission()

        # Principal ID cannot be determined
        mock_get_principal_id.return_value = None

        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.user.username = "service-account-12345678-1234-1234-1234-123456789012"
        mock_request.query_params = {
            "resource_id": str(self.workspace.id),
            "resource_type": "workspace",
        }

        mock_view = Mock()
        mock_view.action = "by_subject"

        result = permission.has_permission(mock_request, mock_view)

        self.assertFalse(result)
        mock_checker_class.assert_not_called()


@override_settings(V2_APIS_ENABLED=True)
class RoleBindingBatchCreatePermissionTests(RoleBindingAccessTestMixin, TransactionIdentityRequest):
    """Tests for batch_create action Kessel access checks."""

    def _make_batch_request(self, resources):
        """Build a mock request with batch create body."""
        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.query_params = {}
        mock_request.data = {
            "requests": [
                {
                    "resource": {"id": str(r["id"]), "type": r["type"]},
                    "subject": {"id": "some-group-id", "type": "group"},
                    "role": {"id": "some-role-id"},
                }
                for r in resources
            ]
        }
        return mock_request

    def _make_batch_view(self):
        """Build a mock view with batch_create action."""
        mock_view = Mock()
        mock_view.action = "batch_create"
        return mock_view

    @patch("management.permissions.role_binding_access.FEATURE_FLAGS")
    @patch("management.permissions.role_binding_access.get_kessel_principal_id")
    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    def test_batch_create_uses_create_relation_when_flag_disabled(
        self, mock_checker_class, mock_get_principal_id, mock_feature_flags
    ):
        """batch_create should use 'create' relation when feature flag is disabled (MVP)."""
        permission = RoleBindingKesselAccessPermission()
        mock_feature_flags.is_use_role_binding_view_permission_enabled.return_value = False
        mock_get_principal_id.return_value = "localhost/test-user-123"

        mock_checker = MagicMock()
        mock_checker.check_resource_access.return_value = True
        mock_checker_class.return_value = mock_checker

        mock_request = self._make_batch_request([{"id": self.workspace.id, "type": "workspace"}])

        result = permission.has_permission(mock_request, self._make_batch_view())

        self.assertTrue(result)
        mock_checker.check_resource_access.assert_called_once()
        call_kwargs = mock_checker.check_resource_access.call_args[1]
        self.assertEqual(call_kwargs["relation"], "create")

    @patch("management.permissions.role_binding_access.FEATURE_FLAGS")
    @patch("management.permissions.role_binding_access.get_kessel_principal_id")
    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    def test_batch_create_uses_role_binding_grant_when_flag_enabled(
        self, mock_checker_class, mock_get_principal_id, mock_feature_flags
    ):
        """batch_create should use 'role_binding_grant' relation when feature flag is enabled (POST-MVP)."""
        permission = RoleBindingKesselAccessPermission()
        mock_feature_flags.is_use_role_binding_view_permission_enabled.return_value = True
        mock_get_principal_id.return_value = "localhost/test-user-123"

        mock_checker = MagicMock()
        mock_checker.check_resource_access.return_value = True
        mock_checker_class.return_value = mock_checker

        mock_request = self._make_batch_request([{"id": self.workspace.id, "type": "workspace"}])

        result = permission.has_permission(mock_request, self._make_batch_view())

        self.assertTrue(result)
        call_kwargs = mock_checker.check_resource_access.call_args[1]
        self.assertEqual(call_kwargs["relation"], "role_binding_grant")

    @patch("management.permissions.role_binding_access.FEATURE_FLAGS")
    @patch("management.permissions.role_binding_access.get_kessel_principal_id")
    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    def test_batch_create_checks_each_unique_resource(
        self, mock_checker_class, mock_get_principal_id, mock_feature_flags
    ):
        """batch_create should check each unique resource and deduplicate."""
        permission = RoleBindingKesselAccessPermission()
        mock_feature_flags.is_use_role_binding_view_permission_enabled.return_value = False
        mock_get_principal_id.return_value = "localhost/test-user-123"

        mock_checker = MagicMock()
        mock_checker.check_resource_access.return_value = True
        mock_checker_class.return_value = mock_checker

        # Two items targeting the same workspace — should only check once
        mock_request = self._make_batch_request(
            [
                {"id": self.workspace.id, "type": "workspace"},
                {"id": self.workspace.id, "type": "workspace"},
            ]
        )

        result = permission.has_permission(mock_request, self._make_batch_view())

        self.assertTrue(result)
        self.assertEqual(mock_checker.check_resource_access.call_count, 1)

    @patch("management.permissions.role_binding_access.FEATURE_FLAGS")
    @patch("management.permissions.role_binding_access.get_kessel_principal_id")
    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    def test_batch_create_checks_multiple_distinct_resources(
        self, mock_checker_class, mock_get_principal_id, mock_feature_flags
    ):
        """batch_create should check each distinct resource separately."""
        permission = RoleBindingKesselAccessPermission()
        mock_feature_flags.is_use_role_binding_view_permission_enabled.return_value = False
        mock_get_principal_id.return_value = "localhost/test-user-123"

        mock_checker = MagicMock()
        mock_checker.check_resource_access.return_value = True
        mock_checker_class.return_value = mock_checker

        second_workspace = Workspace.objects.create(
            name="Second Workspace",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.default_workspace,
        )

        mock_request = self._make_batch_request(
            [
                {"id": self.workspace.id, "type": "workspace"},
                {"id": second_workspace.id, "type": "workspace"},
            ]
        )

        result = permission.has_permission(mock_request, self._make_batch_view())

        self.assertTrue(result)
        self.assertEqual(mock_checker.check_resource_access.call_count, 2)

        second_workspace.delete()

    @patch("management.permissions.role_binding_access.FEATURE_FLAGS")
    @patch("management.permissions.role_binding_access.get_kessel_principal_id")
    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    def test_batch_create_denied_when_any_resource_fails(
        self, mock_checker_class, mock_get_principal_id, mock_feature_flags
    ):
        """batch_create should deny entire batch if any resource check fails."""
        permission = RoleBindingKesselAccessPermission()
        mock_feature_flags.is_use_role_binding_view_permission_enabled.return_value = False
        mock_get_principal_id.return_value = "localhost/test-user-123"

        mock_checker = MagicMock()
        # First resource allowed, second denied
        mock_checker.check_resource_access.side_effect = [True, False]
        mock_checker_class.return_value = mock_checker

        second_workspace = Workspace.objects.create(
            name="Second Workspace",
            tenant=self.tenant,
            type=Workspace.Types.STANDARD,
            parent=self.default_workspace,
        )

        mock_request = self._make_batch_request(
            [
                {"id": self.workspace.id, "type": "workspace"},
                {"id": second_workspace.id, "type": "workspace"},
            ]
        )

        result = permission.has_permission(mock_request, self._make_batch_view())

        self.assertFalse(result)

        second_workspace.delete()

    def test_batch_create_denied_when_body_missing_requests(self):
        """batch_create should deny when request body has no 'requests' key."""
        permission = RoleBindingKesselAccessPermission()

        mock_request = Mock()
        mock_request.data = {}
        mock_request.query_params = {}

        result = permission.has_permission(mock_request, self._make_batch_view())

        self.assertFalse(result)

    def test_batch_create_denied_when_requests_empty(self):
        """batch_create should deny when 'requests' is empty."""
        permission = RoleBindingKesselAccessPermission()

        mock_request = Mock()
        mock_request.data = {"requests": []}
        mock_request.query_params = {}

        result = permission.has_permission(mock_request, self._make_batch_view())

        self.assertFalse(result)

    def test_batch_create_denied_when_resource_missing_id(self):
        """batch_create should deny when a resource item is missing id."""
        permission = RoleBindingKesselAccessPermission()

        mock_request = Mock()
        mock_request.data = {"requests": [{"resource": {"type": "workspace"}, "subject": {}, "role": {}}]}
        mock_request.query_params = {}

        result = permission.has_permission(mock_request, self._make_batch_view())

        self.assertFalse(result)

    def test_batch_create_denied_when_resource_missing_type(self):
        """batch_create should deny when a resource item is missing type."""
        permission = RoleBindingKesselAccessPermission()

        mock_request = Mock()
        mock_request.data = {"requests": [{"resource": {"id": str(self.workspace.id)}, "subject": {}, "role": {}}]}
        mock_request.query_params = {}

        result = permission.has_permission(mock_request, self._make_batch_view())

        self.assertFalse(result)

    @patch("management.permissions.role_binding_access.get_kessel_principal_id")
    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    def test_batch_create_raises_400_for_unknown_resource_type(self, mock_checker_class, mock_get_principal_id):
        """batch_create should raise ParseError (400) for unknown resource types."""
        permission = RoleBindingKesselAccessPermission()
        mock_get_principal_id.return_value = "localhost/test-user-123"

        mock_request = self._make_batch_request([{"id": "some-id", "type": "unknown_type"}])

        with self.assertRaises(ParseError) as ctx:
            permission.has_permission(mock_request, self._make_batch_view())

        self.assertIn("unknown_type", str(ctx.exception.detail))
        self.assertIn("tenant", str(ctx.exception.detail))
        self.assertIn("workspace", str(ctx.exception.detail))
        mock_checker_class.return_value.check_resource_access.assert_not_called()

    def test_batch_create_tenant_resource_requires_org_admin(self):
        """batch_create with tenant resource should require org admin."""
        permission = RoleBindingKesselAccessPermission()

        tenant_resource_id = self.tenant.tenant_resource_id()
        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.query_params = {}
        mock_request.data = {
            "requests": [
                {
                    "resource": {"id": tenant_resource_id, "type": "tenant"},
                    "subject": {"id": "some-group-id", "type": "group"},
                    "role": {"id": "some-role-id"},
                }
            ]
        }
        mock_request.tenant = self.tenant

        result = permission.has_permission(mock_request, self._make_batch_view())

        self.assertFalse(result)

    def test_batch_create_tenant_resource_allowed_for_org_admin(self):
        """batch_create with tenant resource should allow org admin."""
        permission = RoleBindingKesselAccessPermission()

        tenant_resource_id = self.tenant.tenant_resource_id()
        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = True
        mock_request.query_params = {}
        mock_request.data = {
            "requests": [
                {
                    "resource": {"id": tenant_resource_id, "type": "tenant"},
                    "subject": {"id": "some-group-id", "type": "group"},
                    "role": {"id": "some-role-id"},
                }
            ]
        }
        mock_request.tenant = self.tenant

        result = permission.has_permission(mock_request, self._make_batch_view())

        self.assertTrue(result)

    @patch("management.permissions.role_binding_access.get_kessel_principal_id")
    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    def test_batch_create_tenant_only_does_not_call_kessel(self, mock_checker_class, mock_get_principal_id):
        """Tenant-only batch should short-circuit without invoking Kessel."""
        permission = RoleBindingKesselAccessPermission()

        tenant_resource_id = self.tenant.tenant_resource_id()
        mock_request = Mock()
        mock_request.user.system = False
        mock_request.user.admin = True
        mock_request.query_params = {}
        mock_request.data = {
            "requests": [
                {
                    "resource": {"id": tenant_resource_id, "type": "tenant"},
                    "subject": {"id": "some-group-id", "type": "group"},
                    "role": {"id": "some-role-id"},
                }
            ]
        }
        mock_request.tenant = self.tenant

        result = permission.has_permission(mock_request, self._make_batch_view())

        self.assertTrue(result)
        mock_get_principal_id.assert_not_called()
        mock_checker_class.assert_not_called()

    def test_batch_create_denied_when_body_is_list(self):
        """batch_create should deny when request body is a list instead of dict."""
        permission = RoleBindingKesselAccessPermission()

        mock_request = Mock()
        mock_request.data = [{"resource": {"id": "ws-1", "type": "workspace"}}]
        mock_request.query_params = {}

        result = permission.has_permission(mock_request, self._make_batch_view())

        self.assertFalse(result)

    @patch("management.permissions.role_binding_access.get_kessel_principal_id")
    def test_batch_create_rejects_invalid_uuid_workspace_resource(self, mock_get_principal_id):
        """batch_create should raise ParseError for workspace resource with invalid UUID."""
        from rest_framework.exceptions import ParseError

        permission = RoleBindingKesselAccessPermission()
        mock_get_principal_id.return_value = "localhost/test-user-123"

        mock_request = self._make_batch_request([{"id": "not-a-uuid", "type": "workspace"}])

        with self.assertRaises(ParseError) as ctx:
            permission.has_permission(mock_request, self._make_batch_view())

        self.assertIn("not a valid UUID", str(ctx.exception.detail))


@override_settings(V2_APIS_ENABLED=True)
class RoleBindingBySubjectWritePermissionTests(RoleBindingAccessTestMixin, TransactionIdentityRequest):
    """Tests for PUT by_subject action Kessel access checks."""

    def _make_by_subject_put_view(self):
        """Build a mock view for PUT by_subject."""
        mock_view = Mock()
        mock_view.action = "by_subject"
        return mock_view

    def _make_put_request(self, resource_id, resource_type):
        """Build a mock PUT request with query params."""
        mock_request = Mock()
        mock_request.method = "PUT"
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.query_params = {
            "resource_id": str(resource_id),
            "resource_type": resource_type,
        }
        return mock_request

    @patch("management.permissions.role_binding_access.FEATURE_FLAGS")
    @patch("management.permissions.role_binding_access.get_kessel_principal_id")
    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    def test_put_by_subject_uses_edit_relation_when_flag_disabled(
        self, mock_checker_class, mock_get_principal_id, mock_feature_flags
    ):
        """PUT by_subject should use 'edit' relation when feature flag is disabled (MVP)."""
        permission = RoleBindingKesselAccessPermission()
        mock_feature_flags.is_use_role_binding_view_permission_enabled.return_value = False
        mock_get_principal_id.return_value = "localhost/test-user-123"

        mock_checker = MagicMock()
        mock_checker.check_resource_access.return_value = True
        mock_checker_class.return_value = mock_checker

        mock_request = self._make_put_request(self.workspace.id, "workspace")

        result = permission.has_permission(mock_request, self._make_by_subject_put_view())

        self.assertTrue(result)
        mock_checker.check_resource_access.assert_called_once()
        call_kwargs = mock_checker.check_resource_access.call_args[1]
        self.assertEqual(call_kwargs["relation"], "edit")

    @patch("management.permissions.role_binding_access.FEATURE_FLAGS")
    @patch("management.permissions.role_binding_access.get_kessel_principal_id")
    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    def test_put_by_subject_uses_grant_and_revoke_when_flag_enabled(
        self, mock_checker_class, mock_get_principal_id, mock_feature_flags
    ):
        """PUT by_subject should check both 'role_binding_grant' AND 'role_binding_revoke' (POST-MVP)."""
        permission = RoleBindingKesselAccessPermission()
        mock_feature_flags.is_use_role_binding_view_permission_enabled.return_value = True
        mock_get_principal_id.return_value = "localhost/test-user-123"

        mock_checker = MagicMock()
        mock_checker.check_resource_access.return_value = True
        mock_checker_class.return_value = mock_checker

        mock_request = self._make_put_request(self.workspace.id, "workspace")

        result = permission.has_permission(mock_request, self._make_by_subject_put_view())

        self.assertTrue(result)
        self.assertEqual(mock_checker.check_resource_access.call_count, 2)
        relations_checked = [call[1]["relation"] for call in mock_checker.check_resource_access.call_args_list]
        self.assertIn("role_binding_grant", relations_checked)
        self.assertIn("role_binding_revoke", relations_checked)

    @patch("management.permissions.role_binding_access.FEATURE_FLAGS")
    @patch("management.permissions.role_binding_access.get_kessel_principal_id")
    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    def test_put_by_subject_denied_when_grant_fails(
        self, mock_checker_class, mock_get_principal_id, mock_feature_flags
    ):
        """PUT by_subject should deny when role_binding_grant fails (POST-MVP)."""
        permission = RoleBindingKesselAccessPermission()
        mock_feature_flags.is_use_role_binding_view_permission_enabled.return_value = True
        mock_get_principal_id.return_value = "localhost/test-user-123"

        mock_checker = MagicMock()
        mock_checker.check_resource_access.return_value = False
        mock_checker_class.return_value = mock_checker

        mock_request = self._make_put_request(self.workspace.id, "workspace")

        result = permission.has_permission(mock_request, self._make_by_subject_put_view())

        self.assertFalse(result)
        # Should stop after first failed check (grant), not check revoke
        self.assertEqual(mock_checker.check_resource_access.call_count, 1)

    @patch("management.permissions.role_binding_access.FEATURE_FLAGS")
    @patch("management.permissions.role_binding_access.get_kessel_principal_id")
    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    def test_put_by_subject_denied_when_grant_passes_but_revoke_fails(
        self, mock_checker_class, mock_get_principal_id, mock_feature_flags
    ):
        """PUT by_subject should deny when grant passes but revoke fails (POST-MVP)."""
        permission = RoleBindingKesselAccessPermission()
        mock_feature_flags.is_use_role_binding_view_permission_enabled.return_value = True
        mock_get_principal_id.return_value = "localhost/test-user-123"

        mock_checker = MagicMock()
        # Grant passes, revoke fails
        mock_checker.check_resource_access.side_effect = [True, False]
        mock_checker_class.return_value = mock_checker

        mock_request = self._make_put_request(self.workspace.id, "workspace")

        result = permission.has_permission(mock_request, self._make_by_subject_put_view())

        self.assertFalse(result)
        self.assertEqual(mock_checker.check_resource_access.call_count, 2)

    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    def test_put_by_subject_raises_400_when_unknown_resource_type(self, mock_checker_class):
        """PUT by_subject should raise ParseError (400) when resource_type is unknown."""
        permission = RoleBindingKesselAccessPermission()

        mock_request = self._make_put_request(self.workspace.id, "unknown_type")

        with self.assertRaises(ParseError) as ctx:
            permission.has_permission(mock_request, self._make_by_subject_put_view())

        self.assertIn("unknown_type", str(ctx.exception.detail))
        self.assertIn("tenant", str(ctx.exception.detail))
        self.assertIn("workspace", str(ctx.exception.detail))
        mock_checker_class.assert_not_called()

    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    @patch("management.permissions.role_binding_access.FEATURE_FLAGS")
    def test_put_by_subject_tenant_flag_enabled_denies_non_admin(self, mock_feature_flags, mock_checker_class):
        """PUT by_subject tenant should deny non-admins when flag is enabled and not call Kessel."""
        permission = RoleBindingKesselAccessPermission()
        mock_feature_flags.is_use_role_binding_view_permission_enabled.return_value = True

        tenant_resource_id = self.tenant.tenant_resource_id()
        mock_request = self._make_put_request(tenant_resource_id, "tenant")
        mock_request.tenant = self.tenant

        result = permission.has_permission(mock_request, self._make_by_subject_put_view())

        self.assertFalse(result)
        mock_checker_class.assert_not_called()

    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    @patch("management.permissions.role_binding_access.FEATURE_FLAGS")
    def test_put_by_subject_tenant_flag_enabled_allows_admin(self, mock_feature_flags, mock_checker_class):
        """PUT by_subject tenant should allow admins when flag is enabled and not call Kessel."""
        permission = RoleBindingKesselAccessPermission()
        mock_feature_flags.is_use_role_binding_view_permission_enabled.return_value = True

        tenant_resource_id = self.tenant.tenant_resource_id()
        mock_request = self._make_put_request(tenant_resource_id, "tenant")
        mock_request.user.admin = True
        mock_request.tenant = self.tenant

        result = permission.has_permission(mock_request, self._make_by_subject_put_view())

        self.assertTrue(result)
        mock_checker_class.assert_not_called()

    def test_put_by_subject_denied_when_missing_resource_params(self):
        """PUT by_subject should deny when resource_id/resource_type are missing."""
        permission = RoleBindingKesselAccessPermission()

        mock_request = Mock()
        mock_request.method = "PUT"
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.query_params = {}

        result = permission.has_permission(mock_request, self._make_by_subject_put_view())

        self.assertFalse(result)

    @patch("management.permissions.role_binding_access.FEATURE_FLAGS")
    @patch("management.permissions.role_binding_access.get_kessel_principal_id")
    @patch("management.permissions.role_binding_access.WorkspaceInventoryAccessChecker")
    def test_put_by_subject_denied_when_edit_check_fails(
        self, mock_checker_class, mock_get_principal_id, mock_feature_flags
    ):
        """PUT by_subject should deny when edit check fails (MVP)."""
        permission = RoleBindingKesselAccessPermission()
        mock_feature_flags.is_use_role_binding_view_permission_enabled.return_value = False
        mock_get_principal_id.return_value = "localhost/test-user-123"

        mock_checker = MagicMock()
        mock_checker.check_resource_access.return_value = False
        mock_checker_class.return_value = mock_checker

        mock_request = self._make_put_request(self.workspace.id, "workspace")

        result = permission.has_permission(mock_request, self._make_by_subject_put_view())

        self.assertFalse(result)
        call_kwargs = mock_checker.check_resource_access.call_args[1]
        self.assertEqual(call_kwargs["relation"], "edit")

    def test_put_by_subject_tenant_resource_requires_org_admin(self):
        """PUT by_subject with tenant resource should require org admin."""
        permission = RoleBindingKesselAccessPermission()

        tenant_resource_id = self.tenant.tenant_resource_id()
        mock_request = Mock()
        mock_request.method = "PUT"
        mock_request.user.system = False
        mock_request.user.admin = False
        mock_request.query_params = {
            "resource_id": tenant_resource_id,
            "resource_type": "tenant",
        }
        mock_request.tenant = self.tenant

        result = permission.has_permission(mock_request, self._make_by_subject_put_view())

        self.assertFalse(result)

    @patch("management.permissions.role_binding_access.FEATURE_FLAGS")
    def test_put_by_subject_tenant_resource_allowed_for_org_admin(self, mock_feature_flags):
        """PUT by_subject with tenant resource should allow org admin."""
        permission = RoleBindingKesselAccessPermission()
        mock_feature_flags.is_use_role_binding_view_permission_enabled.return_value = False

        tenant_resource_id = self.tenant.tenant_resource_id()
        mock_request = Mock()
        mock_request.method = "PUT"
        mock_request.user.system = False
        mock_request.user.admin = True
        mock_request.query_params = {
            "resource_id": tenant_resource_id,
            "resource_type": "tenant",
        }
        mock_request.tenant = self.tenant

        result = permission.has_permission(mock_request, self._make_by_subject_put_view())

        self.assertTrue(result)
