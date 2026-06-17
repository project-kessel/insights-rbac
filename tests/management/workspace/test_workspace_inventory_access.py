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
"""Tests for workspace access check v2 using Inventory API."""

import random
import string
from importlib import reload
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

from django.db import transaction
from django.test import TransactionTestCase
from django.test.utils import override_settings
from django.urls import clear_url_caches, reverse
from google.protobuf import json_format
from kessel.inventory.v1beta2 import allowed_pb2
from management.models import (
    Access,
    Group,
    Permission,
    Policy,
    Principal,
    ResourceDefinition,
    Role,
    Workspace,
)
from management.permissions.workspace_access import (
    TARGET_WORKSPACE_ACCESS_DENIED_MESSAGE,
    WorkspaceAccessPermission,
)
from management.permissions.workspace_inventory_access import (
    WorkspaceInventoryAccessChecker,
)
from management.workspace.service import WorkspaceService
from management.workspace.utils.access import is_user_allowed_v2
from rest_framework import status
from rest_framework.test import APIClient
from tests.identity_request import BaseIdentityRequest

from api.models import Tenant
from rbac import urls
from tests.v2_util import bootstrap_tenant_for_v2_test


class TransactionIdentityRequest(BaseIdentityRequest, TransactionTestCase):
    """Identity request test base class that uses TransactionTestCase.

    This allows tests to use pgtransaction.atomic(retry=3) without nested transaction errors.
    Use this instead of IdentityRequest for tests that need retry functionality.
    """

    pass


@override_settings(ATOMIC_RETRY_DISABLED=True)
@override_settings(V2_APIS_ENABLED=True, WORKSPACE_HIERARCHY_DEPTH_LIMIT=10)
@override_settings(WORKSPACE_ACCESS_CHECK_V2_ENABLED=True)
class WorkspaceInventoryAccessV2Tests(TransactionIdentityRequest):
    """Tests for workspace access check v2 using Inventory API."""

    def setUp(self):
        """Set up the workspace access tests."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.tenant.save()

        self.service = WorkspaceService()

        with transaction.atomic():
            bootstrap_result = bootstrap_tenant_for_v2_test(self.tenant)
            self.default_workspace = bootstrap_result.default_workspace
            self.root_workspace = bootstrap_result.root_workspace

        self.ungrouped_workspace = Workspace.objects.create(
            name="Ungrouped Hosts Workspace",
            description="Ungrouped Hosts Workspace - description",
            tenant=self.tenant,
            parent=self.default_workspace,
            type=Workspace.Types.UNGROUPED_HOSTS,
        )
        validated_data_standard_ws = {
            "name": "Standard Workspace",
            "description": "Standard Workspace - description",
            "parent_id": self.default_workspace.id,
        }
        self.standard_workspace = self.service.create(validated_data_standard_ws, self.tenant)
        validated_data_standard_sub_ws = {
            "name": "Standard Sub-workspace",
            "description": "Standard Workspace with another standard workspace parent.",
            "parent_id": self.standard_workspace.id,
        }
        self.standard_sub_workspace = self.service.create(validated_data_standard_sub_ws, self.tenant)

    def tearDown(self):
        """Tear down workspace tests."""
        Workspace.objects.update(parent=None)
        Workspace.objects.all().delete()

    def _get_random_name(self, length=10):
        """Generate a random string name."""
        return "".join(random.choices(string.ascii_letters + string.digits, k=length))

    def _setup_access_for_principal(self, username, permission, workspace_id=None, platform_default=False):
        """Set up access for a principal with the given permission."""
        group = Group(
            name=self._get_random_name(),
            platform_default=platform_default,
            tenant=self.tenant,
        )
        group.save()
        role = Role.objects.create(
            name="".join(random.choices(string.ascii_letters + string.digits, k=5)),
            description="A role for a group.",
            tenant=self.tenant,
        )
        public_tenant, _ = Tenant.objects.get_or_create(tenant_name="public")
        permission, _ = Permission.objects.get_or_create(permission=permission, tenant=public_tenant)
        access = Access.objects.create(permission=permission, role=role, tenant=self.tenant)
        if workspace_id:
            operation = "in" if isinstance(workspace_id, list) else "equal"
            ResourceDefinition.objects.create(
                attributeFilter={
                    "key": "group.id",
                    "operation": operation,
                    "value": workspace_id,
                },
                access=access,
                tenant=self.tenant,
            )

        policy = Policy.objects.create(name=self._get_random_name(), group=group, tenant=self.tenant)
        policy.roles.add(role)
        policy.save()
        group.policies.add(policy)
        group.save()
        if not platform_default:
            # Set user_id to match the hard-coded value in IdentityRequest._build_identity
            principal, _ = Principal.objects.get_or_create(username=username, tenant=self.tenant, user_id="1111111")
            group.principals.add(principal)

    def _create_mock_inventory_check_response(self, allowed=True):
        """Create a mock response for Inventory API Check."""
        mock_response = MagicMock()
        response_dict = {"allowed": "ALLOWED_TRUE" if allowed else "ALLOWED_FALSE"}
        with patch.object(json_format, "MessageToDict", return_value=response_dict):
            return mock_response

    def _create_mock_workspace_responses(self, workspace_ids, continuation_token=None):
        """
        Create mock response objects for StreamedListObjects.

        Args:
            workspace_ids: List of workspace IDs to create mock responses for
            continuation_token: Optional continuation token for the last response (use None for no token)

        Returns:
            List of mock response objects with proper structure
        """
        responses = []
        for i, ws_id in enumerate(workspace_ids):
            mock_response = MagicMock(object=MagicMock(resource_id=str(ws_id)))
            # Only set continuation token on the last response if provided (explicit None check)
            if continuation_token is not None and i == len(workspace_ids) - 1:
                mock_response.pagination = MagicMock(continuation_token=continuation_token)
            else:
                mock_response.pagination = None
            responses.append(mock_response)
        return responses

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_list_non_org_admin_default_group(self, mock_flag, mock_channel):
        """Test workspace list for non-org admin with default group access."""
        # Mock Inventory API to return allowed for default workspace
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Create mock response objects with proper structure
        mock_responses = self._create_mock_workspace_responses(
            [
                self.default_workspace.id,
                self.standard_workspace.id,
            ]
        )

        # Mock StreamedListObjects to return accessible workspaces
        mock_stub.StreamedListObjects.side_effect = lambda *args, **kwargs: iter(mock_responses)

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Setup platform default access
            self._setup_access_for_principal(
                self.user_data["username"],
                "inventory:groups:read",
                platform_default=True,
            )

            url = reverse("v2_management:workspace-list")
            client = APIClient()
            response = client.get(url, format="json", **headers)

            # Should return workspaces that the user has access to
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn("data", response.data)

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_list_custom_group_without_attribute_filter(self, mock_flag, mock_channel):
        """Test workspace list with custom group having inventory view permission without attribute filter."""
        # Mock Inventory API
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Create mock response objects with proper structure
        mock_responses = self._create_mock_workspace_responses(
            [
                self.root_workspace.id,
                self.default_workspace.id,
                self.standard_workspace.id,
                self.standard_sub_workspace.id,
            ]
        )

        # Mock StreamedListObjects to return all accessible workspaces
        mock_stub.StreamedListObjects.side_effect = lambda *args, **kwargs: iter(mock_responses)

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Setup custom group with inventory view permission (no attribute filter)
            self._setup_access_for_principal(
                self.user_data["username"],
                "inventory:groups:read",
                workspace_id=None,  # No attribute filter
                platform_default=False,
            )

            url = reverse("v2_management:workspace-list")
            client = APIClient()
            response = client.get(url, format="json", **headers)

            # Should have access to workspaces
            self.assertEqual(response.status_code, status.HTTP_200_OK)

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_list_custom_group_with_attribute_filter_group_id(self, mock_flag, mock_channel):
        """Test workspace list with custom group having attribute filter for group.id (workspace hierarchy)."""
        # Mock Inventory API
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Create mock response objects with proper structure
        mock_responses = self._create_mock_workspace_responses(
            [
                self.standard_workspace.id,
                self.standard_sub_workspace.id,
            ]
        )

        # Mock StreamedListObjects to return only specific workspace and its children
        mock_stub.StreamedListObjects.side_effect = lambda *args, **kwargs: iter(mock_responses)

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Setup custom group with attribute filter for specific workspace (group.id)
            self._setup_access_for_principal(
                self.user_data["username"],
                "inventory:groups:read",
                workspace_id=str(self.standard_workspace.id),  # Specific workspace ID
                platform_default=False,
            )

            url = reverse("v2_management:workspace-list")
            client = APIClient()
            response = client.get(url, format="json", **headers)

            # Should have access to the specific workspace and its hierarchy
            self.assertEqual(response.status_code, status.HTTP_200_OK)

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_list_admin_default_group(self, mock_flag, mock_channel):
        """Test workspace list for user in admin default group."""
        # Mock Inventory API
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Create mock response objects with proper structure
        mock_responses = self._create_mock_workspace_responses(
            [
                self.root_workspace.id,
                self.default_workspace.id,
                self.standard_workspace.id,
                self.standard_sub_workspace.id,
            ]
        )

        # Mock StreamedListObjects to return all workspaces for admin
        mock_stub.StreamedListObjects.side_effect = lambda *args, **kwargs: iter(mock_responses)

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Admin users should have access via Inventory API
            # Create request context for org admin user
            url = reverse("v2_management:workspace-list")
            client = APIClient()
            response = client.get(url, format="json", **self.headers)  # Use admin headers

            # Admin should have access to all workspaces
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn("data", response.data)

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_list_admin_default_custom_group(self, mock_flag, mock_channel):
        """Test workspace list for user in admin default custom group."""
        # Mock Inventory API
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Create mock response objects with proper structure
        mock_responses = self._create_mock_workspace_responses(
            [
                self.root_workspace.id,
                self.default_workspace.id,
                self.standard_workspace.id,
                self.standard_sub_workspace.id,
            ]
        )

        # Mock StreamedListObjects to return all workspaces
        mock_stub.StreamedListObjects.side_effect = lambda *args, **kwargs: iter(mock_responses)

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Create an admin default custom group with full permissions
            group = Group(
                name="Admin Default Custom Group",
                platform_default=True,
                admin_default=True,
                tenant=self.tenant,
            )
            group.save()

            role = Role.objects.create(
                name="Admin Custom Role",
                description="Admin custom role",
                tenant=self.tenant,
                admin_default=True,
            )

            public_tenant, _ = Tenant.objects.get_or_create(tenant_name="public")
            permission, _ = Permission.objects.get_or_create(permission="inventory:*:*", tenant=public_tenant)
            Access.objects.create(permission=permission, role=role, tenant=self.tenant)

            policy = Policy.objects.create(name="Admin Policy", group=group, tenant=self.tenant)
            policy.roles.add(role)
            policy.save()
            group.policies.add(policy)
            group.save()

            principal, _ = Principal.objects.get_or_create(
                username=self.user_data["username"],
                tenant=self.tenant,
                user_id="1111111",  # Required for Principal.user_id_to_principal_resource_id
            )
            group.principals.add(principal)

            url = reverse("v2_management:workspace-list")
            client = APIClient()
            response = client.get(url, format="json", **headers)

            # Should have full access via admin default custom group
            self.assertEqual(response.status_code, status.HTTP_200_OK)

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_access_denied(self, mock_flag, mock_channel):
        """Test workspace access is denied when Inventory API returns not allowed.

        Returns 404 (not 403) to prevent existence leakage - user cannot distinguish
        between a non-existing workspace and one they don't have access to.
        """
        # Mock Inventory API - FilterBackend uses CheckForUpdate for detail actions
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Mock CheckForUpdate to return access denied
        mock_response = MagicMock()
        mock_response.allowed = allowed_pb2.Allowed.ALLOWED_FALSE
        mock_stub.CheckForUpdate.return_value = mock_response

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Create request context for non-org admin user without permissions
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            url = reverse(
                "v2_management:workspace-detail",
                kwargs={"pk": self.standard_workspace.id},
            )
            client = APIClient()
            response = client.get(url, format="json", **headers)

            # Should return 404 (not 403) to prevent existence leakage
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

            # Verify CheckForUpdate was called for detail action
            mock_stub.CheckForUpdate.assert_called()

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_create_with_inventory_access_check(self, mock_flag, mock_channel):
        """Test workspace creation with Inventory API access check."""
        # Mock Inventory API
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        mock_response = MagicMock()
        mock_response.allowed = allowed_pb2.Allowed.ALLOWED_TRUE
        mock_stub.CheckForUpdate.return_value = mock_response

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Setup write access
            self._setup_access_for_principal(
                self.user_data["username"],
                "inventory:groups:write",
                workspace_id=str(self.default_workspace.id),
            )

            workspace_data = {
                "name": "New Workspace V2",
                "description": "Created with access check v2",
                "parent_id": str(self.default_workspace.id),
            }

            url = reverse("v2_management:workspace-list")
            client = APIClient()
            response = client.post(url, workspace_data, format="json", **headers)

            # Should be able to create workspace
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            self.assertEqual(response.data["name"], "New Workspace V2")

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_create_access_denied(self, mock_flag, mock_channel):
        """Test workspace creation is denied when Inventory API returns not allowed."""
        # Mock Inventory API to return not allowed
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        mock_response = MagicMock()
        mock_response.allowed = allowed_pb2.Allowed.ALLOWED_FALSE
        mock_stub.CheckForUpdate.return_value = mock_response

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            workspace_data = {
                "name": "New Workspace V2",
                "description": "Created with access check v2",
                "parent_id": str(self.default_workspace.id),
            }

            url = reverse("v2_management:workspace-list")
            client = APIClient()
            response = client.post(url, workspace_data, format="json", **headers)

            # Should be denied access
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_create_without_parent_id_defaults_to_default_workspace(self, mock_flag, mock_channel):
        """Test workspace creation without parent_id defaults to tenant's default workspace in V2 mode."""
        # Mock Inventory API
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        mock_response = MagicMock()
        mock_response.allowed = allowed_pb2.Allowed.ALLOWED_TRUE
        mock_stub.CheckForUpdate.return_value = mock_response

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Setup write access to default workspace
            self._setup_access_for_principal(
                self.user_data["username"],
                "inventory:groups:write",
                workspace_id=str(self.default_workspace.id),
            )

            # Create workspace WITHOUT parent_id - should default to default_workspace
            workspace_data = {
                "name": "Workspace Without Parent",
                "description": "Created without parent_id",
                # Note: No parent_id provided
            }

            url = reverse("v2_management:workspace-list")
            client = APIClient()
            response = client.post(url, workspace_data, format="json", **headers)

            # Should be able to create workspace with default parent
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            self.assertEqual(response.data["name"], "Workspace Without Parent")
            # Verify it was created under the default workspace
            self.assertEqual(str(response.data["parent_id"]), str(self.default_workspace.id))

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_create_with_invalid_parent_id(self, mock_flag, mock_channel):
        """Test workspace creation with an invalid parent_id."""
        # Mock Inventory API to return allowed (even though parent is invalid, the validation should catch it)
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        mock_response = MagicMock()
        mock_response.allowed = allowed_pb2.Allowed.ALLOWED_TRUE
        mock_stub.CheckForUpdate.return_value = mock_response

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Setup write access
            self._setup_access_for_principal(
                self.user_data["username"],
                "inventory:groups:write",
                workspace_id=str(self.default_workspace.id),
            )

            # Use a non-existent parent_id
            invalid_parent_id = str(uuid4())
            workspace_data = {
                "name": "New Workspace with Invalid Parent",
                "description": "This should fail",
                "parent_id": invalid_parent_id,
            }

            url = reverse("v2_management:workspace-list")
            client = APIClient()
            response = client.post(url, workspace_data, format="json", **headers)

            # Should return 400 BAD REQUEST due to invalid parent_id
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_create_with_missing_required_fields(self, mock_flag, mock_channel):
        """Test workspace creation with missing required fields to ensure consistent validation."""
        # Mock Inventory API to return allowed
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        mock_response = MagicMock()
        mock_response.allowed = allowed_pb2.Allowed.ALLOWED_TRUE
        mock_stub.CheckForUpdate.return_value = mock_response

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Setup write access
            self._setup_access_for_principal(
                self.user_data["username"],
                "inventory:groups:write",
                workspace_id=str(self.default_workspace.id),
            )

            # Missing required 'name' field
            invalid_workspace_data = {
                "description": "Workspace without name",
                "parent_id": str(self.default_workspace.id),
            }

            url = reverse("v2_management:workspace-list")
            client = APIClient()
            response = client.post(url, invalid_workspace_data, format="json", **headers)

            # Should return 400 BAD REQUEST due to missing 'name' field
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            # Check that the error response indicates a validation error
            self.assertIn("detail", response.data)

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_list_with_non_existent_workspace_in_attribute_filter(self, mock_flag, mock_channel):
        """Test workspace list with attribute filter containing non-existent workspace ID.

        Returns 200 with fallback workspaces since user has no real workspace access.
        """
        # Mock Inventory API
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Mock StreamedListObjects to return empty (no access to non-existent workspace)
        mock_stub.StreamedListObjects.return_value = iter([])

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Setup custom group with attribute filter for non-existent workspace
            non_existent_workspace_id = str(uuid4())
            self._setup_access_for_principal(
                self.user_data["username"],
                "inventory:groups:read",
                workspace_id=non_existent_workspace_id,
                platform_default=False,
            )

            url = reverse("v2_management:workspace-list")
            client = APIClient()
            response = client.get(url, format="json", **headers)

            # Should return 200 with fallback workspaces (root, default, ungrouped)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn("data", response.data)

            returned_ids = {str(ws["id"]) for ws in response.data["data"]}
            self.assertIn(str(self.root_workspace.id), returned_ids)
            self.assertIn(str(self.default_workspace.id), returned_ids)
            self.assertIn(str(self.ungrouped_workspace.id), returned_ids)

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_list_with_mixed_valid_invalid_workspace_ids(self, mock_flag, mock_channel):
        """Test workspace list with attribute filter containing mix of valid and invalid workspace IDs."""
        # Mock Inventory API
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Create mock response objects for only the valid workspace
        # The Inventory API will only return workspaces that actually exist and user has access to
        # Use side_effect to return fresh iterator each time StreamedListObjects is called
        mock_responses = self._create_mock_workspace_responses(
            [
                self.standard_workspace.id,
            ]
        )

        # Mock StreamedListObjects to return only valid workspaces
        mock_stub.StreamedListObjects.side_effect = lambda *args, **kwargs: iter(mock_responses)

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Setup custom group with attribute filter containing both valid and invalid workspace IDs
            non_existent_workspace_id = str(uuid4())
            workspace_ids = [str(self.standard_workspace.id), non_existent_workspace_id]
            self._setup_access_for_principal(
                self.user_data["username"],
                "inventory:groups:read",
                workspace_id=workspace_ids,  # List with mix of valid and invalid IDs
                platform_default=False,
            )

            url = reverse("v2_management:workspace-list")
            client = APIClient()
            response = client.get(url, format="json", **headers)

            # Should succeed and return only the valid workspace
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn("data", response.data)
            # Verify only valid workspaces are in the response
            returned_ids = {str(ws["id"]) for ws in response.data["data"]}
            self.assertIn(str(self.standard_workspace.id), returned_ids)
            self.assertNotIn(non_existent_workspace_id, returned_ids)

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_update_with_inventory_access_check(self, mock_flag, mock_channel):
        """Test workspace update (PUT) with Inventory API access check via FilterBackend."""
        # Mock Inventory API - FilterBackend uses CheckForUpdate for detail actions
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Mock CheckForUpdate to return access allowed
        mock_response = MagicMock()
        mock_response.allowed = allowed_pb2.Allowed.ALLOWED_TRUE
        mock_stub.CheckForUpdate.return_value = mock_response

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Setup edit access (for V1 fallback compatibility)
            self._setup_access_for_principal(
                self.user_data["username"],
                "inventory:groups:write",
                workspace_id=str(self.standard_workspace.id),
            )

            updated_data = {
                "name": "Updated Workspace Name",
                "description": "Updated description",
                "parent_id": str(self.default_workspace.id),
            }

            url = reverse(
                "v2_management:workspace-detail",
                kwargs={"pk": self.standard_workspace.id},
            )
            client = APIClient()
            response = client.put(url, updated_data, format="json", **headers)

            # Should be able to update workspace
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data["name"], "Updated Workspace Name")

            # Verify CheckForUpdate was called for detail action
            mock_stub.CheckForUpdate.assert_called()

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_update_access_denied(self, mock_flag, mock_channel):
        """Test workspace update is denied when Inventory API returns not allowed.

        Returns 404 (not 403) to prevent existence leakage - user cannot distinguish
        between a non-existing workspace and one they don't have access to.
        """
        # Mock Inventory API - FilterBackend uses CheckForUpdate for detail actions
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Mock CheckForUpdate to return access denied
        mock_response = MagicMock()
        mock_response.allowed = allowed_pb2.Allowed.ALLOWED_FALSE
        mock_stub.CheckForUpdate.return_value = mock_response

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            updated_data = {
                "name": "Updated Workspace Name",
                "description": "Updated description",
            }

            url = reverse(
                "v2_management:workspace-detail",
                kwargs={"pk": self.standard_workspace.id},
            )
            client = APIClient()
            response = client.put(url, updated_data, format="json", **headers)

            # Should return 404 (not 403) to prevent existence leakage
            # User cannot tell if workspace doesn't exist or they lack access
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

            # Verify CheckForUpdate was called for detail action
            mock_stub.CheckForUpdate.assert_called()

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_update_with_invalid_data(self, mock_flag, mock_channel):
        """Test workspace update (PUT) with invalid data to ensure validation errors are handled."""
        # Mock Inventory API - FilterBackend uses CheckForUpdate for detail actions
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Mock CheckForUpdate to return access allowed
        mock_response = MagicMock()
        mock_response.allowed = allowed_pb2.Allowed.ALLOWED_TRUE
        mock_stub.CheckForUpdate.return_value = mock_response

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Setup edit access (for V1 fallback compatibility)
            self._setup_access_for_principal(
                self.user_data["username"],
                "inventory:groups:write",
                workspace_id=str(self.standard_workspace.id),
            )

            # Invalid data: missing required 'name' field
            invalid_data = {
                "description": "Updated description without name",
            }

            url = reverse(
                "v2_management:workspace-detail",
                kwargs={"pk": self.standard_workspace.id},
            )
            client = APIClient()
            response = client.put(url, invalid_data, format="json", **headers)

            # Should return 400 BAD REQUEST due to missing 'name' field
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            # Check that the error response indicates a validation error
            self.assertIn("detail", response.data)

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_patch_with_inventory_access_check(self, mock_flag, mock_channel):
        """Test workspace partial update (PATCH) with Inventory API access check via FilterBackend."""
        # Mock Inventory API - FilterBackend uses CheckForUpdate for detail actions
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Mock CheckForUpdate to return access allowed
        mock_response = MagicMock()
        mock_response.allowed = allowed_pb2.Allowed.ALLOWED_TRUE
        mock_stub.CheckForUpdate.return_value = mock_response

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Setup edit access (for V1 fallback compatibility)
            self._setup_access_for_principal(
                self.user_data["username"],
                "inventory:groups:write",
                workspace_id=str(self.standard_workspace.id),
            )

            updated_data = {"description": "Patched description"}

            url = reverse(
                "v2_management:workspace-detail",
                kwargs={"pk": self.standard_workspace.id},
            )
            client = APIClient()
            response = client.patch(url, updated_data, format="json", **headers)

            # Should be able to patch workspace
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data["description"], "Patched description")

            # Verify CheckForUpdate was called for detail action
            mock_stub.CheckForUpdate.assert_called()

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_patch_access_denied(self, mock_flag, mock_channel):
        """Test workspace partial update (PATCH) is denied when user lacks permissions.

        Returns 404 (not 403) to prevent existence leakage - user cannot distinguish
        between a non-existing workspace and one they don't have access to.
        """
        # Mock Inventory API - FilterBackend uses CheckForUpdate for detail actions
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Mock CheckForUpdate to return access denied
        mock_response = MagicMock()
        mock_response.allowed = allowed_pb2.Allowed.ALLOWED_FALSE
        mock_stub.CheckForUpdate.return_value = mock_response

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            updated_data = {"description": "Patched description"}

            url = reverse(
                "v2_management:workspace-detail",
                kwargs={"pk": self.standard_workspace.id},
            )
            client = APIClient()
            response = client.patch(url, updated_data, format="json", **headers)

            # Should return 404 (not 403) to prevent existence leakage
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

            # Verify CheckForUpdate was called for detail action
            mock_stub.CheckForUpdate.assert_called()

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_delete_with_inventory_access_check(self, mock_flag, mock_channel):
        """Test workspace deletion with Inventory API access check via FilterBackend."""
        # Create a temporary workspace for deletion BEFORE setting up mocks
        temp_workspace = self.service.create(
            {
                "name": "Temp Workspace for Deletion",
                "description": "Will be deleted",
                "parent_id": self.standard_workspace.id,
            },
            self.tenant,
        )

        # Mock Inventory API - FilterBackend uses CheckForUpdate for detail actions
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Mock CheckForUpdate to return access allowed
        mock_response = MagicMock()
        mock_response.allowed = allowed_pb2.Allowed.ALLOWED_TRUE
        mock_stub.CheckForUpdate.return_value = mock_response

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Setup delete access (for V1 fallback compatibility)
            self._setup_access_for_principal(
                self.user_data["username"],
                "inventory:groups:write",
                workspace_id=str(temp_workspace.id),
            )

            url = reverse("v2_management:workspace-detail", kwargs={"pk": temp_workspace.id})
            client = APIClient()
            response = client.delete(url, format="json", **headers)

            # Should be able to delete workspace
            self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

            # Verify CheckForUpdate was called for detail action
            mock_stub.CheckForUpdate.assert_called()

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_delete_access_denied(self, mock_flag, mock_channel):
        """Test workspace deletion is denied when user lacks permissions.

        Returns 404 (not 403) to prevent existence leakage - user cannot distinguish
        between a non-existing workspace and one they don't have access to.
        """
        # Mock Inventory API - FilterBackend uses CheckForUpdate for detail actions
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Mock CheckForUpdate to return access denied
        mock_response = MagicMock()
        mock_response.allowed = allowed_pb2.Allowed.ALLOWED_FALSE
        mock_stub.CheckForUpdate.return_value = mock_response

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Create request context for non-org admin user without permissions
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            url = reverse(
                "v2_management:workspace-detail",
                kwargs={"pk": self.standard_workspace.id},
            )
            client = APIClient()
            response = client.delete(url, format="json", **headers)

            # Should return 404 (not 403) to prevent existence leakage
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

            # Verify CheckForUpdate was called for detail action
            mock_stub.CheckForUpdate.assert_called()

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_delete_non_existent(self, mock_flag, mock_channel):
        """Test deleting a non-existent workspace returns 404."""
        # Mock Inventory API - FilterBackend uses CheckForUpdate for detail actions
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Mock CheckForUpdate to return access allowed (doesn't matter - workspace doesn't exist)
        mock_response = MagicMock()
        mock_response.allowed = allowed_pb2.Allowed.ALLOWED_TRUE
        mock_stub.CheckForUpdate.return_value = mock_response

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Use a non-existent workspace ID
            non_existent_workspace_id = str(uuid4())

            # Setup delete access (for V1 fallback compatibility)
            self._setup_access_for_principal(
                self.user_data["username"],
                "inventory:groups:write",
                workspace_id=non_existent_workspace_id,
            )

            url = reverse(
                "v2_management:workspace-detail",
                kwargs={"pk": non_existent_workspace_id},
            )
            client = APIClient()
            response = client.delete(url, format="json", **headers)

            # Should return 404 NOT FOUND
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

            # Verify CheckForUpdate was called for detail action
            mock_stub.CheckForUpdate.assert_called()

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_list_user_without_permissions(self, mock_flag, mock_channel):
        """Test workspace list for user without any permissions returns fallback workspaces."""
        # Mock Inventory API to return no workspaces (user has no permissions)
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Mock StreamedListObjects to return empty list
        mock_stub.StreamedListObjects.return_value = iter([])

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Do NOT setup any access - user has no permissions

            url = reverse("v2_management:workspace-list")
            client = APIClient()
            response = client.get(url, format="json", **headers)

            # Should return 200 with fallback workspaces (root, default, ungrouped)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn("data", response.data)

            returned_ids = {str(ws["id"]) for ws in response.data["data"]}
            self.assertIn(str(self.root_workspace.id), returned_ids)
            self.assertIn(str(self.default_workspace.id), returned_ids)
            self.assertIn(str(self.ungrouped_workspace.id), returned_ids)

    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=False,
    )
    def test_workspace_access_falls_back_to_v1_when_v2_disabled(self, mock_flag):
        """Test that workspace access falls back to V1 logic when V2 feature flag is disabled."""
        # Create request context for non-org admin user
        request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
        headers = request_context["request"].META

        # Setup V1-style access - using platform default group
        self._setup_access_for_principal(
            self.user_data["username"],
            "inventory:groups:read",
            platform_default=True,
        )

        url = reverse("v2_management:workspace-list")
        client = APIClient()
        response = client.get(url, format="json", **headers)

        # Should return workspaces using V1 logic (no Inventory API calls)
        # The exact status depends on V1 implementation, but it should not fail
        self.assertIn(response.status_code, [status.HTTP_200_OK, status.HTTP_403_FORBIDDEN])
        # Verify that feature flag was checked and returned False
        mock_flag.assert_called()

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_admin_user_uses_inventory_api_in_v2(self, mock_flag, mock_channel):
        """Test that admin users use Inventory API in v2 mode (no bypass)."""
        # Mock Inventory API
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Mock StreamedListObjects to return accessible workspaces for admin
        mock_responses = self._create_mock_workspace_responses([self.default_workspace.id])
        mock_stub.StreamedListObjects.side_effect = lambda *args, **kwargs: iter(mock_responses)

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            # Create request context for org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=True)
            headers = request_context["request"].META

            url = reverse("v2_management:workspace-list")
            client = APIClient()
            response = client.get(url, format="json", **headers)

            # Admin should have access through Inventory API, not bypass
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn("data", response.data)

            # Verify Inventory API WAS called (admin users don't bypass in v2)
            mock_stub.StreamedListObjects.assert_called()

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_list_includes_ancestors_of_accessible_workspaces(self, mock_flag, mock_channel):
        """Test that workspace list includes ancestors of top-level accessible workspaces for ancestry needs."""
        # Mock Inventory API
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Mock StreamedListObjects to return only the standard sub-workspace
        # Since standard_sub_workspace is the only accessible workspace, it's the top-level one
        # The code should add all its ancestors (standard_workspace, default_workspace, root_workspace)
        # Use side_effect to return fresh iterator each time StreamedListObjects is called
        mock_responses = self._create_mock_workspace_responses([self.standard_sub_workspace.id])

        mock_stub.StreamedListObjects.side_effect = lambda *args, **kwargs: iter(mock_responses)

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Setup access for the sub-workspace
            self._setup_access_for_principal(
                self.user_data["username"],
                "inventory:groups:read",
                workspace_id=str(self.standard_sub_workspace.id),
                platform_default=False,
            )

            url = reverse("v2_management:workspace-list")
            client = APIClient()
            response = client.get(url, format="json", **headers)

            # Should have access to the workspace and all its ancestors
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn("data", response.data)

            # Verify that ancestors of the top-level workspace are included
            returned_ids = {str(ws["id"]) for ws in response.data["data"]}
            # Check that the accessible workspace and its ancestors are included
            self.assertIn(str(self.standard_sub_workspace.id), returned_ids)
            self.assertIn(str(self.standard_workspace.id), returned_ids)
            self.assertIn(str(self.default_workspace.id), returned_ids)
            self.assertIn(str(self.root_workspace.id), returned_ids)

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_list_returns_fallback_workspaces_when_no_access(self, mock_flag, mock_channel):
        """Test that workspace list returns fallback workspaces when user has no real workspace access in V2 mode."""
        # Mock Inventory API to return empty list (no accessible workspaces)
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Mock StreamedListObjects to return empty list
        mock_stub.StreamedListObjects.return_value = iter([])

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):

            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Setup some access (but Inventory API returns empty, simulating no actual access)
            self._setup_access_for_principal(
                self.user_data["username"],
                "inventory:groups:read",
                platform_default=False,
            )

            url = reverse("v2_management:workspace-list")
            client = APIClient()
            response = client.get(url, format="json", **headers)

            # Should return 200 with fallback workspaces (root, default, ungrouped)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn("data", response.data)

            returned_ids = {str(ws["id"]) for ws in response.data["data"]}
            self.assertIn(str(self.root_workspace.id), returned_ids)
            self.assertIn(str(self.default_workspace.id), returned_ids)
            self.assertIn(str(self.ungrouped_workspace.id), returned_ids)

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch("management.workspace.utils.access.PrincipalProxy")
    @patch(
        "management.workspace.utils.access.get_principal_for_auth",
        return_value=None,
    )
    def test_workspace_access_with_none_principal_fallback_to_it_service(
        self, mock_get_principal, mock_proxy_class, mock_channel
    ):
        """Test workspace access when get_principal_for_auth returns None, falls back to IT service."""
        # Mock PrincipalProxy to return user_id from IT service
        test_user_id = "it-service-user-456"
        mock_proxy = MagicMock()
        mock_proxy.request_filtered_principals.return_value = {
            "status_code": 200,
            "data": [{"user_id": test_user_id, "username": "testuser"}],
        }
        mock_proxy_class.return_value = mock_proxy

        # Mock Inventory API
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Create mock response for allowed access
        mock_response = MagicMock()
        mock_response.allowed = allowed_pb2.Allowed.ALLOWED_TRUE
        mock_stub.CheckForUpdate.return_value = mock_response

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            # Create a mock request with username and org_id but no user_id (to trigger IT service fallback)
            mock_request = Mock()
            mock_request.user.username = "testuser"
            mock_request.user.org_id = "test-org-123"
            mock_request.user.user_id = None  # Explicitly set to None to trigger IT service fallback
            mock_request.user.system = False  # Not a system user
            mock_request.tenant = self.tenant

            # Call is_user_allowed_v2 directly
            result = is_user_allowed_v2(mock_request, "view", str(self.standard_workspace.id))

            # Verify the function returns True (access allowed)
            self.assertTrue(result)

            # Verify PrincipalProxy was called with correct arguments
            mock_proxy.request_filtered_principals.assert_called_once_with(
                ["testuser"], org_id="test-org-123", options={"return_id": True}
            )

            # Verify the inventory stub was called with the expected principal_id
            expected_principal_id = Principal.user_id_to_principal_resource_id(test_user_id)
            mock_stub.CheckForUpdate.assert_called_once()
            call_args = mock_stub.CheckForUpdate.call_args
            self.assertIn(expected_principal_id, str(call_args))

    @patch("management.workspace.utils.access.PrincipalProxy")
    @patch(
        "management.workspace.utils.access.get_principal_for_auth",
        return_value=None,
    )
    def test_workspace_access_with_none_principal_it_service_failure(self, mock_get_principal, mock_proxy_class):
        """Test workspace access when IT service fails to return user_id."""

        # Mock PrincipalProxy to return error
        mock_proxy = MagicMock()
        mock_proxy.request_filtered_principals.return_value = {
            "status_code": 500,
            "errors": [{"detail": "Service unavailable"}],
        }
        mock_proxy_class.return_value = mock_proxy

        # Create a mock request with no user_id (to trigger IT service fallback)
        mock_request = Mock()
        mock_request.user.username = "testuser"
        mock_request.user.org_id = "test-org-123"
        mock_request.user.user_id = None  # Explicitly set to None to trigger IT service fallback
        mock_request.user.system = False  # Not a system user
        mock_request.tenant = self.tenant

        with patch("management.workspace.utils.access.logger") as mock_logger:
            # Call is_user_allowed_v2 directly - should return False
            result = is_user_allowed_v2(mock_request, "view", str(self.standard_workspace.id))

            # Verify the function returns False (access denied)
            self.assertFalse(result)

            # Verify warning was logged
            mock_logger.warning.assert_called_once_with(
                "Failed to retrieve user_id from IT service for username: %s",
                "testuser",
            )

    @patch("management.workspace.utils.access.PrincipalProxy")
    @patch(
        "management.workspace.utils.access.get_principal_for_auth",
        return_value=None,
    )
    def test_workspace_access_with_none_principal_it_service_empty_response(
        self, mock_get_principal, mock_proxy_class
    ):
        """Test workspace access when IT service returns empty data."""

        # Mock PrincipalProxy to return empty data
        mock_proxy = MagicMock()
        mock_proxy.request_filtered_principals.return_value = {
            "status_code": 200,
            "data": [],
        }
        mock_proxy_class.return_value = mock_proxy

        # Create a mock request with no user_id (to trigger IT service fallback)
        mock_request = Mock()
        mock_request.user.username = "testuser"
        mock_request.user.org_id = "test-org-123"
        mock_request.user.user_id = None  # Explicitly set to None to trigger IT service fallback
        mock_request.user.system = False  # Not a system user
        mock_request.tenant = self.tenant

        with patch("management.workspace.utils.access.logger") as mock_logger:
            # Call is_user_allowed_v2 directly - should return False
            result = is_user_allowed_v2(mock_request, "view", str(self.standard_workspace.id))

            # Verify the function returns False (access denied)
            self.assertFalse(result)

            # Verify warning was logged
            mock_logger.warning.assert_called_once_with(
                "Failed to retrieve user_id from IT service for username: %s",
                "testuser",
            )

    @patch(
        "management.workspace.utils.access.get_principal_for_auth",
        return_value=None,
    )
    def test_workspace_access_with_none_principal_and_no_username(self, mock_get_principal):
        """Test workspace access when get_principal_for_auth returns None and no username available."""

        # Create a mock request with no username and no user_id
        mock_request = Mock()
        mock_request.user.username = None
        mock_request.user.user_id = None  # Explicitly set to None to trigger fallback
        mock_request.user.system = False  # Not a system user
        mock_request.tenant = self.tenant

        with patch("management.workspace.utils.access.logger") as mock_logger:
            # Call is_user_allowed_v2 directly - should return False
            result = is_user_allowed_v2(mock_request, "view", str(self.standard_workspace.id))

            # Verify the function returns False (access denied)
            self.assertFalse(result)

            # Verify warning was logged
            mock_logger.warning.assert_called_once_with("No username available from request.user, denying access")

    @patch(
        "management.workspace.utils.access.get_principal_for_auth",
        return_value=None,
    )
    def test_workspace_access_with_none_principal_and_no_org_id(self, mock_get_principal):
        """Test workspace access when get_principal_for_auth returns None and no org_id available."""

        # Create a mock request with username but no org_id or user_id
        mock_request = Mock()
        mock_request.user.username = "testuser"
        mock_request.user.org_id = None
        mock_request.user.user_id = None  # Explicitly set to None to trigger fallback
        mock_request.user.system = False  # Not a system user
        mock_request.tenant = self.tenant

        with patch("management.workspace.utils.access.logger") as mock_logger:
            # Call is_user_allowed_v2 directly - should return False
            result = is_user_allowed_v2(mock_request, "view", str(self.standard_workspace.id))

            # Verify the function returns False (access denied)
            self.assertFalse(result)

            # Verify warning was logged
            mock_logger.warning.assert_called_once_with("No org_id available from request.user, denying access")

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch("management.workspace.utils.access.PrincipalProxy")
    @patch(
        "management.workspace.utils.access.get_principal_for_auth",
        return_value=None,
    )
    def test_workspace_access_with_none_principal_logs_debug_for_it_service(
        self, mock_get_principal, mock_proxy_class, mock_channel
    ):
        """Test that workspace access logs debug message when user_id retrieved from IT service."""

        # Mock PrincipalProxy to return user_id from IT service
        test_user_id = "it-service-user-789"
        mock_proxy = MagicMock()
        mock_proxy.request_filtered_principals.return_value = {
            "status_code": 200,
            "data": [{"user_id": test_user_id, "username": "testuser"}],
        }
        mock_proxy_class.return_value = mock_proxy

        # Mock Inventory API
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Create mock response for allowed access
        mock_response = MagicMock()
        mock_response.allowed = allowed_pb2.Allowed.ALLOWED_TRUE
        mock_stub.CheckForUpdate.return_value = mock_response

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            mock_request = Mock()
            mock_request.user.username = "testuser"
            mock_request.user.org_id = "test-org-123"
            mock_request.user.user_id = None  # Explicitly set to None to trigger IT service fallback
            mock_request.user.system = False  # Not a system user
            mock_request.tenant = self.tenant

            with patch("management.workspace.utils.access.logger") as mock_logger:
                # Call the function
                result = is_user_allowed_v2(mock_request, "view", str(self.standard_workspace.id))

                # Verify debug logging for IT service lookup
                mock_logger.debug.assert_called_once_with("Retrieved user_id from IT service via PrincipalProxy")

                # Result depends on Inventory API response, which we mocked as ALLOWED_TRUE
                self.assertTrue(result)

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_list_with_pagination_continuation_token(self, mock_flag, mock_channel):
        """Test workspace list with pagination using continuation token to fetch more than PAGE_SIZE workspaces."""
        # Mock Inventory API
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Create workspaces for two pages
        # First page has workspaces with continuation token
        # Second page has remaining workspaces without continuation token
        first_page_workspaces = [self.default_workspace.id, self.standard_workspace.id]
        second_page_workspaces = [self.standard_sub_workspace.id]
        all_expected_workspaces = first_page_workspaces + second_page_workspaces

        # Mock responses for first page with continuation token
        first_page_responses = self._create_mock_workspace_responses(
            first_page_workspaces, continuation_token="next_page_token_123"
        )

        # Mock responses for second page without continuation token (last page)
        second_page_responses = self._create_mock_workspace_responses(second_page_workspaces)

        # Use side_effect list to return different responses on each call (no conditionals)
        # Note: 2 calls expected - only FilterBackend does StreamedListObjects:
        #   1. FilterBackend page 1
        #   2. FilterBackend page 2 (follows continuation token)
        mock_stub.StreamedListObjects.side_effect = [
            iter(first_page_responses),  # FilterBackend page 1 (has continuation token)
            iter(second_page_responses),  # FilterBackend page 2
        ]

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Setup access
            self._setup_access_for_principal(
                self.user_data["username"],
                "inventory:groups:read",
                platform_default=True,
            )

            url = reverse("v2_management:workspace-list")
            client = APIClient()
            response = client.get(url, format="json", **headers)

            # Should return workspaces from both pages
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn("data", response.data)

            # Verify StreamedListObjects was called 2 times (FilterBackend pagination only)
            self.assertEqual(mock_stub.StreamedListObjects.call_count, 2)

            # Verify the second call (second FilterBackend page) included the continuation token
            second_call_args = mock_stub.StreamedListObjects.call_args_list[1]
            request_arg = second_call_args[0][0]
            self.assertEqual(request_arg.pagination.continuation_token, "next_page_token_123")

            # Verify workspaces from BOTH pages are returned in the response
            returned_workspace_ids = {str(ws["id"]) for ws in response.data["data"]}
            expected_ws_ids_str = {str(ws_id) for ws_id in all_expected_workspaces}
            # Use set operations to verify all expected workspaces are present (no loop needed)
            self.assertTrue(
                expected_ws_ids_str.issubset(returned_workspace_ids),
                f"Expected workspaces {expected_ws_ids_str} to be subset of returned {returned_workspace_ids}. "
                f"Missing: {expected_ws_ids_str - returned_workspace_ids}. Page-2 results may have been dropped.",
            )

            # Verify total count includes workspaces from both pages
            # Note: Response may include additional workspaces (root, ungrouped) added by view logic
            self.assertGreaterEqual(
                len(returned_workspace_ids),
                len(all_expected_workspaces),
                f"Expected at least {len(all_expected_workspaces)} workspaces from pagination, "
                f"but got {len(returned_workspace_ids)}",
            )

    def test_workspace_inventory_access_checker_pagination_constants(self):
        """Test that WorkspaceInventoryAccessChecker uses correct pagination constants."""

        checker = WorkspaceInventoryAccessChecker()
        # Verify PAGE_SIZE is set to 1000 for pagination
        self.assertEqual(checker.PAGE_SIZE, 1000)
        # Verify MAX_PAGES is set to prevent infinite loops
        self.assertEqual(checker.MAX_PAGES, 10000)

    @patch("management.permissions.workspace_inventory_access.logger")
    @patch("management.inventory_client.create_client_channel_inventory")
    def test_lookup_accessible_workspaces_max_items_guard(self, mock_channel, mock_logger):
        """Guard rail: stop iteration when max items (PAGE_SIZE * MAX_PAGES) is reached.

        The SDK handles pagination internally, so RBAC applies an item-count
        safety guard to prevent infinite iteration from buggy server responses.

        This test also verifies that the underlying SDK iterator is *not* fully
        consumed once the max-items limit is reached.
        """
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Use small limits for testing
        test_page_size = 2
        test_max_pages = 3
        test_max_items = test_page_size * test_max_pages  # 6 items max

        def make_response(index):
            """Create a mock response for a single item."""
            response = MagicMock()
            response.object.resource_id = f"ws-{index}"
            return response

        # Build a guarded iterator that raises if consumed past max_items.
        # This ensures the loop exits as soon as the limit is hit.
        def guarded_iterator():
            for i in range(test_max_items):
                yield make_response(i)
            raise AssertionError(
                "lookup_accessible_workspaces over-consumed the SDK iterator past the max-items guard"
            )

        with patch(
            "management.permissions.workspace_inventory_access.sdk_list_workspaces",
            return_value=guarded_iterator(),
        ):
            with patch(
                "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
                return_value=mock_stub,
            ):
                checker = WorkspaceInventoryAccessChecker()
                original_page_size = checker.PAGE_SIZE
                original_max_pages = checker.MAX_PAGES
                checker.PAGE_SIZE = test_page_size
                checker.MAX_PAGES = test_max_pages

                try:
                    workspaces = checker.lookup_accessible_workspaces("user-guard", "view")

                    # Should get exactly max_items workspaces (guard stops iteration)
                    self.assertEqual(len(workspaces), test_max_items)
                    expected_workspaces = {f"ws-{i}" for i in range(test_max_items)}
                    self.assertEqual(workspaces, expected_workspaces)

                    # Verify a warning was logged about hitting the item limit
                    warning_calls = [str(call) for call in mock_logger.warning.call_args_list]
                    max_item_warnings = [c for c in warning_calls if "maximum item limit" in c.lower()]
                    self.assertTrue(
                        max_item_warnings,
                        f"Expected a warning about hitting maximum item limit. Got warnings: {warning_calls}",
                    )
                finally:
                    checker.PAGE_SIZE = original_page_size
                    checker.MAX_PAGES = original_max_pages

    @patch("core.kafka.RBACProducer.send_kafka_message")
    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_move_uses_create_permission_for_target_in_v2(self, mock_flag, mock_channel, send_kafka_message):
        """Test that workspace move operation uses 'create' permission for target workspace in V2 mode.

        With the FilterBackend architecture:
        - Source workspace access is checked via FilterBackend (CheckForUpdate since move is a detail action)
        - Target workspace access is checked via permission class (CheckForUpdate with 'create' relation)
        """
        # Mock Inventory API
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        source_workspace_id = str(self.standard_sub_workspace.id)
        target_workspace_id = str(self.default_workspace.id)

        # Track (workspace_id, relation) tuples for precise assertions on target check
        check_calls = []

        def check_side_effect(request):
            # Capture both workspace ID and relation being checked
            workspace_id = getattr(getattr(request, "object", None), "resource_id", None)
            check_calls.append((workspace_id, request.relation))
            mock_response = MagicMock()
            mock_response.allowed = allowed_pb2.Allowed.ALLOWED_TRUE
            return mock_response

        mock_stub.CheckForUpdate.side_effect = check_side_effect

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Setup access for the user (for V1 fallback compatibility)
            self._setup_access_for_principal(
                self.user_data["username"],
                "inventory:groups:write",
                workspace_id=[
                    str(self.standard_workspace.id),
                    str(self.default_workspace.id),
                ],
            )

            # Execute move: move standard_sub_workspace to default_workspace
            url = reverse(
                "v2_management:workspace-move",
                kwargs={"pk": self.standard_sub_workspace.id},
            )
            client = APIClient()
            data = {"parent_id": str(self.default_workspace.id)}
            response = client.post(url, data, format="json", **headers)

            # Should succeed
            self.assertEqual(response.status_code, status.HTTP_200_OK)

            # Verify CheckForUpdate was called (for both source and target workspace checks)
            mock_stub.CheckForUpdate.assert_called()

            # Verify that 'create' permission was checked on target workspace via CheckForUpdate
            self.assertTrue(
                any(ws_id == target_workspace_id and rel == "create" for ws_id, rel in check_calls),
                f"Expected 'create' permission check for target workspace {target_workspace_id}, got: {check_calls}",
            )

            # Verify no 'write' permission checks occurred (doesn't exist in SpiceDB schema)
            self.assertFalse(
                any(rel == "write" for _, rel in check_calls),
                f"Should not check 'write' permission (doesn't exist in schema), got: {check_calls}",
            )

    @patch("core.kafka.RBACProducer.send_kafka_message")
    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_move_denied_when_no_create_permission_on_target_v2(
        self, mock_flag, mock_channel, send_kafka_message
    ):
        """Test that workspace move is denied when user lacks 'create' permission on target workspace in V2 mode."""
        # Mock Inventory API
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Target workspace ID used to differentiate source vs target checks
        target_workspace_id = str(self.default_workspace.id)

        # Define permission responses: (workspace_id, relation) -> allowed status
        # All checks are allowed EXCEPT 'create' on target workspace
        denied_checks = {(target_workspace_id, "create")}

        def check_side_effect(request):
            mock_response = MagicMock()
            workspace_id = getattr(getattr(request, "object", None), "resource_id", None)
            check_key = (workspace_id, request.relation)
            mock_response.allowed = (
                allowed_pb2.Allowed.ALLOWED_FALSE if check_key in denied_checks else allowed_pb2.Allowed.ALLOWED_TRUE
            )
            return mock_response

        mock_stub.CheckForUpdate.side_effect = check_side_effect

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Setup access for the user (source workspace)
            self._setup_access_for_principal(
                self.user_data["username"],
                "inventory:groups:write",
                workspace_id=str(self.standard_workspace.id),
            )

            # Execute move: try to move standard_sub_workspace to default_workspace
            url = reverse(
                "v2_management:workspace-move",
                kwargs={"pk": self.standard_sub_workspace.id},
            )
            client = APIClient()
            data = {"parent_id": target_workspace_id}
            response = client.post(url, data, format="json", **headers)

            # Should be denied (403 Forbidden)
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            self.assertEqual(response.data.get("detail"), TARGET_WORKSPACE_ACCESS_DENIED_MESSAGE)

    # ==================== V1 Move Behavior Tests ====================

    @patch("core.kafka.RBACProducer.send_kafka_message")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=False,
    )
    def test_workspace_move_admin_target_missing_v1_returns_403(self, mock_flag, send_kafka_message):
        """Admin move in V1 mode should return 403 when target workspace doesn't exist."""
        # Create non-existent target workspace ID
        non_existent_target_id = str(uuid4())

        # Use admin headers (org_admin=True by default in setUp)
        url = reverse(
            "v2_management:workspace-move",
            kwargs={"pk": self.standard_sub_workspace.id},
        )
        client = APIClient()
        data = {"parent_id": non_existent_target_id}
        response = client.post(url, data, format="json", **self.headers)

        # Should be denied (403 Forbidden) because target doesn't exist
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data.get("detail"), TARGET_WORKSPACE_ACCESS_DENIED_MESSAGE)

    @patch("core.kafka.RBACProducer.send_kafka_message")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=False,
    )
    def test_workspace_move_admin_target_exists_v1_allows_move(self, mock_flag, send_kafka_message):
        """Admin move in V1 mode should succeed when target workspace exists in the same tenant."""
        # Create a temporary workspace to move (to avoid affecting other tests)
        temp_workspace = self.service.create(
            {
                "name": "Temp Workspace for Admin Move V1",
                "description": "Will be moved",
                "parent_id": self.standard_workspace.id,
            },
            self.tenant,
        )

        # Move temp_workspace to default_workspace (which exists)
        url = reverse(
            "v2_management:workspace-move",
            kwargs={"pk": temp_workspace.id},
        )
        client = APIClient()
        data = {"parent_id": str(self.default_workspace.id)}
        response = client.post(url, data, format="json", **self.headers)

        # Should succeed
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @patch("core.kafka.RBACProducer.send_kafka_message")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=False,
    )
    def test_workspace_move_non_admin_no_write_on_target_v1_returns_403(self, mock_flag, send_kafka_message):
        """Non-admin move in V1 mode should return 403 when user lacks write access on target workspace."""
        # Use a unique user for this test to avoid conflicts with other tests
        unique_user_data = {
            "username": "test_no_target_access_user",
            "email": "no_target@example.com",
        }
        unique_customer_data = {**self.customer_data, "user_data": unique_user_data}

        # Create request context for non-org admin user
        request_context = self._create_request_context(unique_customer_data, unique_user_data, is_org_admin=False)
        headers = request_context["request"].META

        # Setup write access to default_workspace which includes standard_sub_workspace as descendant
        # This allows the user to access the source workspace
        self._setup_access_for_principal(
            unique_user_data["username"],
            "inventory:groups:write",
            workspace_id=str(self.standard_workspace.id),  # Parent of standard_sub_workspace
        )

        # Create a separate workspace that user doesn't have access to (as target)
        inaccessible_workspace = self.service.create(
            {
                "name": "Inaccessible Target Workspace",
                "description": "User has no access",
                "parent_id": self.default_workspace.id,
            },
            self.tenant,
        )

        # Try to move standard_sub_workspace to inaccessible_workspace (no write access on target)
        url = reverse(
            "v2_management:workspace-move",
            kwargs={"pk": self.standard_sub_workspace.id},
        )
        client = APIClient()
        data = {"parent_id": str(inaccessible_workspace.id)}
        response = client.post(url, data, format="json", **headers)

        # Should be denied (403 Forbidden) because user lacks write on target
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data.get("detail"), TARGET_WORKSPACE_ACCESS_DENIED_MESSAGE)

    @patch("core.kafka.RBACProducer.send_kafka_message")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=False,
    )
    def test_workspace_move_non_admin_has_write_on_both_v1_allows_move(self, mock_flag, send_kafka_message):
        """Non-admin move in V1 mode should succeed when user has write on both source and target.

        In V1 mode, granting write access to a workspace grants access to all its descendants.
        So granting access to default_workspace grants access to all workspaces under it.
        """
        # Use a unique user for this test to avoid conflicts with cached principals
        unique_user_data = {
            "username": "test_non_admin_move_v1_user",
            "email": "test_move@example.com",
        }
        unique_customer_data = {**self.customer_data, "user_data": unique_user_data}

        # Create a temporary workspace to move (to avoid affecting other tests)
        temp_workspace = self.service.create(
            {
                "name": "Temp Workspace for Non-Admin Move V1",
                "description": "Will be moved",
                "parent_id": self.standard_workspace.id,
            },
            self.tenant,
        )

        # Create request context for non-org admin user with unique username
        request_context = self._create_request_context(unique_customer_data, unique_user_data, is_org_admin=False)
        headers = request_context["request"].META

        # Setup write access to default_workspace - this grants access to all descendants
        # (standard_workspace, temp_workspace) through the V1 permission model
        self._setup_access_for_principal(
            unique_user_data["username"],
            "inventory:groups:write",
            workspace_id=str(self.default_workspace.id),
        )

        # Move temp_workspace to default_workspace
        # Source (temp_workspace): accessible as descendant of default_workspace
        # Target (default_workspace): directly accessible
        url = reverse(
            "v2_management:workspace-move",
            kwargs={"pk": temp_workspace.id},
        )
        client = APIClient()
        data = {"parent_id": str(self.default_workspace.id)}
        response = client.post(url, data, format="json", **headers)

        # Should succeed
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # ==================== Missing/Invalid parent_id Tests ====================

    @patch("core.kafka.RBACProducer.send_kafka_message")
    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_move_missing_parent_id_v2_returns_400(self, mock_flag, mock_channel, send_kafka_message):
        """Move request with missing parent_id should return 400 (not 403) in V2 mode.

        The permission layer should defer to view-level validation for missing parent_id.
        """
        # Mock Inventory API
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        mock_response = MagicMock()
        mock_response.allowed = allowed_pb2.Allowed.ALLOWED_TRUE
        mock_stub.CheckForUpdate.return_value = mock_response

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Setup access for the user
            self._setup_access_for_principal(
                self.user_data["username"],
                "inventory:groups:write",
                workspace_id=str(self.standard_workspace.id),
            )

            url = reverse(
                "v2_management:workspace-move",
                kwargs={"pk": self.standard_sub_workspace.id},
            )
            client = APIClient()
            # Send request with NO parent_id
            data = {}
            response = client.post(url, data, format="json", **headers)

            # Should return 400 BAD REQUEST (not 403)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            # Verify the error message indicates missing parent_id
            self.assertIn("parent_id", str(response.data))

    @patch("core.kafka.RBACProducer.send_kafka_message")
    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_move_invalid_uuid_parent_id_v2_returns_400(self, mock_flag, mock_channel, send_kafka_message):
        """Move request with invalid UUID parent_id should return 400 (not 403) in V2 mode.

        The permission layer should defer to view-level validation for invalid parent_id.
        """
        # Mock Inventory API
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        mock_response = MagicMock()
        mock_response.allowed = allowed_pb2.Allowed.ALLOWED_TRUE
        mock_stub.CheckForUpdate.return_value = mock_response

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Setup access for the user
            self._setup_access_for_principal(
                self.user_data["username"],
                "inventory:groups:write",
                workspace_id=str(self.standard_workspace.id),
            )

            url = reverse(
                "v2_management:workspace-move",
                kwargs={"pk": self.standard_sub_workspace.id},
            )
            client = APIClient()
            # Send request with invalid UUID parent_id
            data = {"parent_id": "not-a-valid-uuid"}
            response = client.post(url, data, format="json", **headers)

            # Should return 400 BAD REQUEST (not 403)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("core.kafka.RBACProducer.send_kafka_message")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=False,
    )
    def test_workspace_move_missing_parent_id_v1_returns_400(self, mock_flag, send_kafka_message):
        """Move request with missing parent_id should return 400 (not 403) in V1 mode.

        The permission layer should defer to view-level validation for missing parent_id.
        """
        url = reverse(
            "v2_management:workspace-move",
            kwargs={"pk": self.standard_sub_workspace.id},
        )
        client = APIClient()
        # Send request with NO parent_id
        data = {}
        response = client.post(url, data, format="json", **self.headers)

        # Should return 400 BAD REQUEST (not 403)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Verify the error message indicates missing parent_id
        self.assertIn("parent_id", str(response.data))

    @patch("core.kafka.RBACProducer.send_kafka_message")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=False,
    )
    def test_workspace_move_invalid_uuid_parent_id_v1_returns_400(self, mock_flag, send_kafka_message):
        """Move request with invalid UUID parent_id should return 400 (not 403) in V1 mode.

        The permission layer should defer to view-level validation for invalid parent_id.
        """
        url = reverse(
            "v2_management:workspace-move",
            kwargs={"pk": self.standard_sub_workspace.id},
        )
        client = APIClient()
        # Send request with invalid UUID parent_id
        data = {"parent_id": "not-a-valid-uuid"}
        response = client.post(url, data, format="json", **self.headers)

        # Should return 400 BAD REQUEST (not 403)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_system_user_with_admin_bypasses_v2_access_checks(self):
        """Test that system users (s2s communication) with admin=True bypass v2 access checks."""

        # Create a mock request for a system user with admin privileges
        mock_request = Mock()
        mock_request.user.system = True
        mock_request.user.admin = True
        mock_request.tenant = self.tenant

        # Call is_user_allowed_v2 directly - should return True without any external API calls
        result = is_user_allowed_v2(mock_request, "view", str(self.standard_workspace.id))

        # Verify the function returns True (access allowed for system admin)
        self.assertTrue(result)

    def test_system_user_without_admin_denied_in_v2_access_checks(self):
        """Test that system users (s2s communication) without admin=True are denied access in v2."""

        # Create a mock request for a system user without admin privileges
        mock_request = Mock()
        mock_request.user.system = True
        mock_request.user.admin = False
        mock_request.tenant = self.tenant

        # Call is_user_allowed_v2 directly - should return False
        result = is_user_allowed_v2(mock_request, "view", str(self.standard_workspace.id))

        # Verify the function returns False (access denied for non-admin system user)
        self.assertFalse(result)

    def test_system_user_without_admin_attribute_denied_in_v2_access_checks(self):
        """Test that system users without an explicit admin attribute are denied by default.

        This validates the default-deny behavior in is_system_user_admin when the
        admin attribute is missing from the user object.
        """
        # Create a mock request for a system user WITHOUT an admin attribute
        mock_request = Mock()
        mock_request.user = SimpleNamespace(system=True)  # No admin attribute
        mock_request.tenant = self.tenant

        # Call is_user_allowed_v2 directly - should return False (default-deny)
        result = is_user_allowed_v2(mock_request, "view", str(self.standard_workspace.id))

        # Verify the function returns False (access denied when admin attribute is missing)
        self.assertFalse(result)

    def test_system_user_admin_bypasses_v2_for_any_operation(self):
        """Test that system admin users bypass v2 access checks for all operations."""

        operations = ["view", "create", "edit", "move", "delete"]

        for operation in operations:
            mock_request = Mock()
            mock_request.user.system = True
            mock_request.user.admin = True
            mock_request.tenant = self.tenant

            # All operations should be allowed for system admin users
            result = is_user_allowed_v2(mock_request, operation, str(self.standard_workspace.id))
            self.assertTrue(result, f"System admin should be allowed for operation: {operation}")

    @patch("management.permissions.workspace_access.is_user_allowed_v2")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_permission_class_system_admin_bypasses_v2_for_list(self, mock_flag, mock_is_user_allowed_v2):
        """Test that WorkspaceAccessPermission bypasses v2 checks for system admin users on list action."""

        permission = WorkspaceAccessPermission()

        # Create mock request for system admin user
        mock_request = Mock()
        mock_request.user.system = True
        mock_request.user.admin = True
        mock_request.tenant = self.tenant
        mock_request.method = "GET"

        # Mock view for list action
        mock_view = Mock()
        mock_view.action = "list"
        mock_view.kwargs = {}

        # Call has_permission
        result = permission.has_permission(mock_request, mock_view)

        # System admin should be allowed
        self.assertTrue(result)
        # V2 access check should NOT be called (bypassed for system users)
        mock_is_user_allowed_v2.assert_not_called()

    @patch("management.permissions.workspace_access.is_user_allowed_v2")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_permission_class_system_non_admin_denied(self, mock_flag, mock_is_user_allowed_v2):
        """Test that WorkspaceAccessPermission denies access for system users without admin."""

        permission = WorkspaceAccessPermission()

        # Create mock request for system non-admin user
        mock_request = Mock()
        mock_request.user.system = True
        mock_request.user.admin = False
        mock_request.tenant = self.tenant
        mock_request.method = "GET"

        # Mock view for list action
        mock_view = Mock()
        mock_view.action = "list"
        mock_view.kwargs = {}

        # Call has_permission
        result = permission.has_permission(mock_request, mock_view)

        # System non-admin should be denied
        self.assertFalse(result)
        # V2 access check should NOT be called (system user check happens first)
        mock_is_user_allowed_v2.assert_not_called()

    @patch("management.workspace.model.Workspace.objects")
    @patch("management.permissions.workspace_access.is_user_allowed_v2")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_permission_class_system_admin_move_checks_target_exists(
        self, mock_flag, mock_is_user_allowed_v2, mock_workspace_objects
    ):
        """Test that system admin users performing move use v1 target existence check, not v2."""

        permission = WorkspaceAccessPermission()

        target_workspace_id = str(self.default_workspace.id)

        # Create mock request for system admin user with move action
        mock_request = Mock()
        mock_request.user.system = True
        mock_request.user.admin = True
        mock_request.tenant = self.tenant
        mock_request.method = "POST"
        mock_request.data = {"parent_id": target_workspace_id}

        # Mock view for move action
        mock_view = Mock()
        mock_view.action = "move"
        mock_view.kwargs = {"pk": str(self.standard_sub_workspace.id)}

        # Mock workspace existence check
        mock_workspace_objects.filter.return_value.exists.return_value = True

        # Call has_permission
        result = permission.has_permission(mock_request, mock_view)

        # System admin should be allowed
        self.assertTrue(result)
        # V2 access check should NOT be called (bypassed for system users)
        mock_is_user_allowed_v2.assert_not_called()
        # V1 target existence check should be called
        mock_workspace_objects.filter.assert_called_once_with(id=target_workspace_id, tenant=self.tenant)

    @patch("management.workspace.model.Workspace.objects")
    @patch("management.permissions.workspace_access.is_user_allowed_v2")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_permission_class_system_admin_move_denied_when_workspace_missing(
        self, mock_flag, mock_is_user_allowed_v2, mock_workspace_objects
    ):
        """Test that system admin move is denied when target workspace does not exist.

        This tests the deny branch of the v1 existence logic for system users,
        asserting that v2 is never called even in the deny case.
        """

        permission = WorkspaceAccessPermission()

        nonexistent_workspace_id = str(uuid4())

        # Create mock request for system admin user with move action
        mock_request = Mock()
        mock_request.user.system = True
        mock_request.user.admin = True
        mock_request.tenant = self.tenant
        mock_request.method = "POST"
        mock_request.data = {"parent_id": nonexistent_workspace_id}

        # Mock view for move action
        mock_view = Mock()
        mock_view.action = "move"
        mock_view.kwargs = {"pk": str(self.standard_sub_workspace.id)}

        # Mock workspace existence check to return False (workspace doesn't exist)
        mock_workspace_objects.filter.return_value.exists.return_value = False

        # Call has_permission
        result = permission.has_permission(mock_request, mock_view)

        # System admin should be denied because workspace doesn't exist
        self.assertFalse(result)
        # V2 access check should NOT be called (bypassed for system users)
        mock_is_user_allowed_v2.assert_not_called()
        # V1 target existence check should be called
        mock_workspace_objects.filter.assert_called_once_with(id=nonexistent_workspace_id, tenant=self.tenant)
        # Permission message should be set
        self.assertEqual(permission.message, TARGET_WORKSPACE_ACCESS_DENIED_MESSAGE)

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_list_type_standard_returns_empty_without_real_access(self, mock_flag, mock_channel):
        """Test that type=standard returns 200 with empty list when user has no real workspace access in V2 mode."""
        # Mock Inventory API to return empty list (no accessible workspaces)
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Mock StreamedListObjects to return empty iterator (user has no real access)
        mock_stub.StreamedListObjects.return_value = iter([])

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            url = reverse("v2_management:workspace-list")
            client = APIClient()

            # Query with type=standard - should return 200 with empty data
            # (fallback workspaces are root/default/ungrouped, none are type=standard)
            response = client.get(f"{url}?type=standard", format="json", **headers)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn("data", response.data)
            self.assertEqual(len(response.data["data"]), 0)

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_list_type_all_returns_fallback_without_real_access(self, mock_flag, mock_channel):
        """Test that type=all returns 200 with fallback workspaces when user has no real workspace access in V2 mode."""
        # Mock Inventory API to return empty list (no accessible workspaces)
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Mock StreamedListObjects to return empty iterator (user has no real access)
        mock_stub.StreamedListObjects.return_value = iter([])

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            url = reverse("v2_management:workspace-list")
            client = APIClient()

            # Query with type=all - should return 200 with fallback workspaces
            response = client.get(f"{url}?type=all", format="json", **headers)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn("data", response.data)

            returned_ids = {str(ws["id"]) for ws in response.data["data"]}
            self.assertIn(str(self.root_workspace.id), returned_ids)
            self.assertIn(str(self.default_workspace.id), returned_ids)
            self.assertIn(str(self.ungrouped_workspace.id), returned_ids)

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_list_type_standard_returns_200_with_real_access(self, mock_flag, mock_channel):
        """Test that type=standard returns 200 when user has real workspace access in V2 mode."""
        # Mock Inventory API to return accessible workspaces
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Create mock response objects with standard workspace access
        mock_responses = self._create_mock_workspace_responses(
            [
                self.standard_workspace.id,
            ]
        )

        # Mock StreamedListObjects to return accessible workspaces
        mock_stub.StreamedListObjects.side_effect = lambda *args, **kwargs: iter(mock_responses)

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Setup access for principal
            self._setup_access_for_principal(
                self.user_data["username"],
                "inventory:groups:read",
                workspace_id=str(self.standard_workspace.id),
            )

            url = reverse("v2_management:workspace-list")
            client = APIClient()

            # Query with type=standard - should return 200 since user has real access
            response = client.get(f"{url}?type=standard", format="json", **headers)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn("data", response.data)
            # Verify only standard workspaces are returned
            for workspace in response.data.get("data", []):
                self.assertEqual(workspace.get("type"), "standard")

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_list_type_all_returns_200_with_real_access(self, mock_flag, mock_channel):
        """Test that type=all returns 200 when user has real workspace access in V2 mode."""
        # Mock Inventory API to return accessible workspaces
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Create mock response objects with all workspace types
        mock_responses = self._create_mock_workspace_responses(
            [
                self.root_workspace.id,
                self.default_workspace.id,
                self.standard_workspace.id,
            ]
        )

        # Mock StreamedListObjects to return accessible workspaces
        mock_stub.StreamedListObjects.side_effect = lambda *args, **kwargs: iter(mock_responses)

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            # Setup access for principal
            self._setup_access_for_principal(
                self.user_data["username"],
                "inventory:groups:read",
                platform_default=True,
            )

            url = reverse("v2_management:workspace-list")
            client = APIClient()

            # Query with type=all - should return 200 since user has real access
            response = client.get(f"{url}?type=all", format="json", **headers)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn("data", response.data)

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_list_no_type_returns_fallback_without_real_access(self, mock_flag, mock_channel):
        """Test that default list (no type filter) returns fallback workspaces when user has no real workspace access."""
        # Mock Inventory API to return empty list (no accessible workspaces)
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Mock StreamedListObjects to return empty iterator (user has no real access)
        mock_stub.StreamedListObjects.return_value = iter([])

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            url = reverse("v2_management:workspace-list")
            client = APIClient()

            # Query without type filter - should return 200 with fallback workspaces
            response = client.get(url, format="json", **headers)

            # Default type filter is 'standard', so fallback workspaces (root, default, ungrouped)
            # are filtered out, resulting in empty data
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn("data", response.data)

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_list_type_root_returns_root_without_real_access(self, mock_flag, mock_channel):
        """Test that type=root returns root workspace when user has no real workspace access in V2 mode."""
        # Mock Inventory API to return empty list (no accessible workspaces)
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Mock StreamedListObjects to return empty iterator (user has no real access)
        mock_stub.StreamedListObjects.return_value = iter([])

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            url = reverse("v2_management:workspace-list")
            client = APIClient()

            # Query with type=root - should return root workspace from fallback
            response = client.get(f"{url}?type=root", format="json", **headers)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn("data", response.data)

            returned_ids = {str(ws["id"]) for ws in response.data["data"]}
            self.assertIn(str(self.root_workspace.id), returned_ids)

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch(
        "feature_flags.FEATURE_FLAGS.is_workspace_access_check_v2_enabled",
        return_value=True,
    )
    def test_workspace_list_type_default_returns_default_without_real_access(self, mock_flag, mock_channel):
        """Test that type=default returns default workspace when user has no real workspace access in V2 mode."""
        # Mock Inventory API to return empty list (no accessible workspaces)
        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()

        # Mock StreamedListObjects to return empty iterator (user has no real access)
        mock_stub.StreamedListObjects.return_value = iter([])

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            # Create request context for non-org admin user
            request_context = self._create_request_context(self.customer_data, self.user_data, is_org_admin=False)
            headers = request_context["request"].META

            url = reverse("v2_management:workspace-list")
            client = APIClient()

            # Query with type=default - should return default workspace from fallback
            response = client.get(f"{url}?type=default", format="json", **headers)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertIn("data", response.data)

            returned_ids = {str(ws["id"]) for ws in response.data["data"]}
            self.assertIn(str(self.default_workspace.id), returned_ids)

    @patch("management.inventory_client.create_client_channel_inventory")
    def test_lookup_accessible_workspaces_passes_consistency_token_to_request(self, mock_channel):
        """Test that consistency_token parameter is passed through sdk_list_workspaces to the gRPC call."""

        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()
        mock_stub.StreamedListObjects.return_value = iter([])

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            checker = WorkspaceInventoryAccessChecker()
            checker.lookup_accessible_workspaces(
                principal_id="localhost/testuser",
                relation="view",
                consistency_token="my-token-xyz",
            )

            mock_stub.StreamedListObjects.assert_called_once()
            call_args = mock_stub.StreamedListObjects.call_args
            request_proto = call_args[0][0]

            self.assertTrue(request_proto.HasField("consistency"))
            self.assertEqual(request_proto.consistency.at_least_as_fresh.token, "my-token-xyz")

    @patch("management.inventory_client.create_client_channel_inventory")
    def test_lookup_accessible_workspaces_no_consistency_when_token_is_none(self, mock_channel):
        """Test that consistency field is not set when consistency_token is None."""

        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()
        mock_stub.StreamedListObjects.return_value = iter([])

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            checker = WorkspaceInventoryAccessChecker()
            checker.lookup_accessible_workspaces(
                principal_id="localhost/testuser",
                relation="view",
                consistency_token=None,
            )

            mock_stub.StreamedListObjects.assert_called_once()
            call_args = mock_stub.StreamedListObjects.call_args
            request_proto = call_args[0][0]

            self.assertFalse(request_proto.HasField("consistency"))

    @patch("management.inventory_client.create_client_channel_inventory")
    @patch("management.workspace.utils.access.get_principal_for_auth")
    def test_is_user_allowed_v2_reloads_tenant_for_fresh_consistency_token(self, mock_get_principal, mock_channel):
        """Test that is_user_allowed_v2 reloads tenant from DB to get the latest consistency token.

        The tenant on request may be cached (via Redis TenantCache) with a stale
        consistency token. The Kafka consumer updates the token in the DB, so
        is_user_allowed_v2 must refresh_from_db before passing it to StreamedListObjects.
        """
        # Mock principal with user_id to skip IT service lookup
        mock_principal = Mock()
        mock_principal.user_id = "1111111"
        mock_get_principal.return_value = mock_principal

        mock_stub = MagicMock()
        mock_channel.return_value.__enter__.return_value = MagicMock()
        mock_stub.StreamedListObjects.return_value = iter([])

        with patch(
            "kessel.inventory.v1beta2.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            # Set a stale token on the in-memory tenant (simulates cached tenant)
            self.tenant.relations_consistency_token = "stale-token"
            # Do NOT save — this is the in-memory (cached) state

            # Update the DB directly with a fresh token (simulates Kafka consumer updating it)
            Tenant.objects.filter(pk=self.tenant.pk).update(relations_consistency_token="fresh-db-token")

            # Build a mock request whose tenant is the stale cached object
            mock_request = Mock()
            mock_request.user.username = "testuser"
            mock_request.user.user_id = "1111111"
            mock_request.user.org_id = self.tenant.org_id
            mock_request.user.system = False
            mock_request.tenant = self.tenant
            mock_request.path = "/api/v2/workspaces/"
            mock_request.method = "GET"
            mock_request.req_id = "test-req-id"

            is_user_allowed_v2(mock_request, "view", None)

            # Verify StreamedListObjects was called
            mock_stub.StreamedListObjects.assert_called_once()
            call_args = mock_stub.StreamedListObjects.call_args
            request_proto = call_args[0][0]

            # The consistency token should be the fresh DB value, not the stale cached one
            self.assertTrue(request_proto.HasField("consistency"))
            self.assertEqual(
                request_proto.consistency.at_least_as_fresh.token,
                "fresh-db-token",
            )
