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
"""Tests for RoleBindingInventoryChecker."""

from unittest.mock import MagicMock, patch

from kessel.inventory.v1beta2.check_response_pb2 import CheckResponse
from management.inventory_checker.inventory_api_check import RoleBindingInventoryChecker
from management.models import CustomRoleV2, Group, Principal, RoleBinding
from tests.identity_request import IdentityRequest

INVENTORY_STUB_PATH = "management.inventory_checker.inventory_api_check.inventory_service_pb2_grpc.KesselInventoryServiceStub"  # noqa: E501


class RoleBindingInventoryCheckerTest(IdentityRequest):
    """Tests for the RoleBindingInventoryChecker class."""

    def setUp(self):
        """Set up test data."""
        super().setUp()

        # Create a custom role
        self.role = CustomRoleV2.objects.create(
            name="test-role",
            description="Test role description",
            tenant=self.tenant,
        )

        # Create a group
        self.group = Group.objects.create(
            name="test-group",
            tenant=self.tenant,
        )

        # Create a principal
        self.principal = Principal.objects.create(
            username="test-user",
            user_id="123456",
            tenant=self.tenant,
        )

        # Create a role binding
        self.binding = RoleBinding.objects.create(
            role=self.role,
            resource_type="workspace",
            resource_id="test-workspace-uuid",
            tenant=self.tenant,
        )

        # Add group and principal to the binding
        self.binding.update_groups([self.group])
        self.binding.update_principals([("direct", self.principal)])

        self.checker = RoleBindingInventoryChecker()

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
    def test_check_role_binding_all_relations_exist(self, mock_message_to_dict, mock_create_channel):
        """Test that check returns True when all relations exist in inventory."""
        mock_response = MagicMock(spec=CheckResponse)
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_response)
        mock_message_to_dict.return_value = {"allowed": "ALLOWED_TRUE"}

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            binding_tuples = self.binding.all_tuples()
            result = self.checker.check_role_binding(binding_tuples, str(self.binding.uuid))

            self.assertTrue(result)
            self.assertEqual(mock_stub.Check.call_count, 4)

    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    def test_check_role_binding_missing_relation(self, mock_message_to_dict, mock_create_channel):
        """Test that check returns False when a relation is missing in inventory."""
        mock_responses = [
            MagicMock(spec=CheckResponse),
            MagicMock(spec=CheckResponse),
            MagicMock(spec=CheckResponse),
            MagicMock(spec=CheckResponse),
        ]
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_responses)
        mock_message_to_dict.side_effect = [
            {"allowed": "ALLOWED_TRUE"},
            {"allowed": "ALLOWED_FALSE"},
            {"allowed": "ALLOWED_TRUE"},
            {"allowed": "ALLOWED_TRUE"},
        ]

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            binding_tuples = self.binding.all_tuples()
            result = self.checker.check_role_binding(binding_tuples, str(self.binding.uuid))
            self.assertFalse(result)

    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    def test_check_role_binding_with_only_groups(self, mock_message_to_dict, mock_create_channel):
        """Test checking a binding that has only group subjects."""
        mock_response = MagicMock(spec=CheckResponse)
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_response)
        mock_message_to_dict.return_value = {"allowed": "ALLOWED_TRUE"}

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            self.binding.update_principals([])
            binding_tuples = self.binding.all_tuples()
            result = self.checker.check_role_binding(binding_tuples, str(self.binding.uuid))

            self.assertTrue(result)
            self.assertEqual(mock_stub.Check.call_count, 3)

    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    def test_check_role_binding_with_only_principals(self, mock_message_to_dict, mock_create_channel):
        """Test checking a binding that has only principal subjects."""
        mock_response = MagicMock(spec=CheckResponse)
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_response)
        mock_message_to_dict.return_value = {"allowed": "ALLOWED_TRUE"}

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            self.binding.update_groups([])
            binding_tuples = self.binding.all_tuples()
            result = self.checker.check_role_binding(binding_tuples, str(self.binding.uuid))

            self.assertTrue(result)
            self.assertEqual(mock_stub.Check.call_count, 3)

    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    def test_check_role_binding_with_multiple_subjects(self, mock_message_to_dict, mock_create_channel):
        """Test checking a binding with multiple groups and principals."""
        group2 = Group.objects.create(
            name="test-group-2",
            tenant=self.tenant,
        )
        principal2 = Principal.objects.create(
            username="test-user-2",
            user_id="654321",
            tenant=self.tenant,
        )

        self.binding.update_groups([self.group, group2])
        self.binding.update_principals([("direct", self.principal), ("direct", principal2)])

        mock_response = MagicMock(spec=CheckResponse)
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_response)
        mock_message_to_dict.return_value = {"allowed": "ALLOWED_TRUE"}

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            binding_tuples = self.binding.all_tuples()
            result = self.checker.check_role_binding(binding_tuples, str(self.binding.uuid))

            self.assertTrue(result)
            self.assertEqual(mock_stub.Check.call_count, 6)

    def test_check_role_binding_empty_tuple_list(self):
        """Test checking with an empty tuple list returns True (no checks to fail)."""
        result = self.checker.check_role_binding([], str(self.binding.uuid))
        self.assertTrue(result)
