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
"""Tests for CustomRolePermissionChecker."""

from unittest.mock import MagicMock, patch

from kessel.inventory.v1beta2.check_response_pb2 import CheckResponse
from management.inventory_checker.inventory_api_check import CustomRolePermissionChecker
from management.models import CustomRoleV2, Permission
from tests.identity_request import IdentityRequest

INVENTORY_STUB_PATH = "management.inventory_checker.inventory_api_check.inventory_service_pb2_grpc.KesselInventoryServiceStub"  # noqa: E501


class CustomRolePermissionCheckerTest(IdentityRequest):
    """Tests for the CustomRolePermissionChecker class."""

    def setUp(self):
        """Set up test data."""
        super().setUp()

        # Create a custom role
        self.role = CustomRoleV2.objects.create(
            name="test-role",
            description="Test role description",
            tenant=self.tenant,
        )

        # Create permissions
        self.perm1 = Permission.objects.create(
            permission="inventory:hosts:read",
            tenant=self.tenant,
        )
        self.perm2 = Permission.objects.create(
            permission="inventory:hosts:write",
            tenant=self.tenant,
        )
        self.perm3 = Permission.objects.create(
            permission="inventory:groups:read",
            tenant=self.tenant,
        )

        self.checker = CustomRolePermissionChecker()

    def _setup_inventory_mocks(self, mock_create_channel, mock_stub_responses):
        """Helper to set up inventory API mocks.

        Args:
            mock_create_channel: Mock for create_client_channel_inventory
            mock_stub_responses: Either a single response or list of responses for stub.Check

        Returns:
            mock_stub: The mocked KesselInventoryServiceStub
        """
        mock_stub = MagicMock()
        if isinstance(mock_stub_responses, list):
            mock_stub.Check.side_effect = mock_stub_responses
        else:
            mock_stub.Check.return_value = mock_stub_responses

        mock_channel = MagicMock()
        mock_channel.__enter__.return_value = mock_channel
        mock_channel.__exit__.return_value = None
        mock_create_channel.return_value = mock_channel

        return mock_stub

    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    def test_check_custom_role_permissions_all_exist(self, mock_message_to_dict, mock_create_channel):
        """Test that check returns True when all permission relations exist in inventory."""
        # Add permissions to role
        self.role.permissions.add(self.perm1, self.perm2, self.perm3)

        mock_response = MagicMock(spec=CheckResponse)
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_response)
        mock_message_to_dict.return_value = {"allowed": "ALLOWED_TRUE"}

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            permission_tuples = [CustomRoleV2._permission_tuple(self.role, p) for p in self.role.permissions.all()]
            result = self.checker.check_custom_role_permissions(permission_tuples, str(self.role.uuid))

            self.assertTrue(result)
            self.assertEqual(mock_stub.Check.call_count, 3)

    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    def test_check_custom_role_permissions_missing_relation(self, mock_message_to_dict, mock_create_channel):
        """Test that check returns False when a permission relation is missing in inventory."""
        # Add permissions to role
        self.role.permissions.add(self.perm1, self.perm2, self.perm3)

        mock_responses = [
            MagicMock(spec=CheckResponse),
            MagicMock(spec=CheckResponse),
            MagicMock(spec=CheckResponse),
        ]
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_responses)
        # Second permission is missing
        mock_message_to_dict.side_effect = [
            {"allowed": "ALLOWED_TRUE"},
            {"allowed": "ALLOWED_FALSE"},
            {"allowed": "ALLOWED_TRUE"},
        ]

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            permission_tuples = [CustomRoleV2._permission_tuple(self.role, p) for p in self.role.permissions.all()]
            result = self.checker.check_custom_role_permissions(permission_tuples, str(self.role.uuid))
            self.assertFalse(result)

    def test_check_custom_role_permissions_no_permissions(self):
        """Test that check returns True when role has no permissions (empty tuple list)."""
        # Role has no permissions
        permission_tuples = []
        result = self.checker.check_custom_role_permissions(permission_tuples, str(self.role.uuid))
        self.assertTrue(result)

    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    def test_check_custom_role_permissions_single_permission(self, mock_message_to_dict, mock_create_channel):
        """Test checking a role with a single permission."""
        # Add one permission
        self.role.permissions.add(self.perm1)

        mock_response = MagicMock(spec=CheckResponse)
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_response)
        mock_message_to_dict.return_value = {"allowed": "ALLOWED_TRUE"}

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            permission_tuples = [CustomRoleV2._permission_tuple(self.role, p) for p in self.role.permissions.all()]
            result = self.checker.check_custom_role_permissions(permission_tuples, str(self.role.uuid))

            self.assertTrue(result)
            self.assertEqual(mock_stub.Check.call_count, 1)

    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    def test_check_custom_role_permissions_all_missing(self, mock_message_to_dict, mock_create_channel):
        """Test that check returns False when all permission relations are missing."""
        # Add permissions to role
        self.role.permissions.add(self.perm1, self.perm2)

        mock_responses = [
            MagicMock(spec=CheckResponse),
            MagicMock(spec=CheckResponse),
        ]
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_responses)
        # All permissions are missing
        mock_message_to_dict.side_effect = [
            {"allowed": "ALLOWED_FALSE"},
            {"allowed": "ALLOWED_FALSE"},
        ]

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            permission_tuples = [CustomRoleV2._permission_tuple(self.role, p) for p in self.role.permissions.all()]
            result = self.checker.check_custom_role_permissions(permission_tuples, str(self.role.uuid))
            self.assertFalse(result)
