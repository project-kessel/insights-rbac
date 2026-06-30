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

from management.inventory_checker.inventory_api_check import CustomRolePermissionChecker
from management.models import CustomRoleV2, Permission
from tests.identity_request import IdentityRequest

RELATIONS_CHANNEL_PATH = "management.inventory_checker.inventory_api_check.create_client_channel_relation"
RELATIONS_STUB_PATH = (
    "management.inventory_checker.inventory_api_check.relation_tuples_pb2_grpc.KesselTupleServiceStub"  # noqa: E501
)


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

    def test_check_custom_role_permissions_no_permissions(self):
        """Test that check returns True when role has no permissions (empty tuple list)."""
        # Role has no permissions
        permission_tuples = []
        result = self.checker.check_custom_role_permissions(permission_tuples, str(self.role.uuid))
        self.assertTrue(result)

    def _setup_relations_mocks(self, mock_create_channel, read_tuples_responses):
        """Helper to set up Relations API mocks for ReadTuples.

        Args:
            mock_create_channel: Mock for create_client_channel_relation
            read_tuples_responses: List of iterables -- one per ReadTuples call.
                Each iterable yields response objects (non-empty = tuple exists).

        Returns:
            mock_stub: The mocked KesselTupleServiceStub
        """
        mock_stub = MagicMock()
        mock_stub.ReadTuples.side_effect = read_tuples_responses

        mock_channel = MagicMock()
        mock_channel.__enter__.return_value = mock_channel
        mock_channel.__exit__.return_value = None
        mock_create_channel.return_value = mock_channel

        return mock_stub

    @patch(RELATIONS_CHANNEL_PATH)
    def test_wildcard_permissions_all_exist(self, mock_create_channel):
        """Test that wildcard permission tuples are verified via ReadTuples and pass when all exist."""
        self.role.permissions.add(self.perm1, self.perm2, self.perm3)

        mock_tuple_response = MagicMock()
        read_responses = [[mock_tuple_response], [mock_tuple_response], [mock_tuple_response]]
        mock_stub = self._setup_relations_mocks(mock_create_channel, read_responses)

        with patch(RELATIONS_STUB_PATH, return_value=mock_stub):
            permission_tuples = [CustomRoleV2._permission_tuple(self.role, p) for p in self.role.permissions.all()]
            result = self.checker.check_custom_role_permissions(permission_tuples, str(self.role.uuid))

            self.assertTrue(result)
            self.assertEqual(mock_stub.ReadTuples.call_count, 3)

    @patch(RELATIONS_CHANNEL_PATH)
    def test_wildcard_permissions_one_missing(self, mock_create_channel):
        """Test that wildcard check returns False when one permission relation is missing.

        All tuples are checked (no early return) so operators see all missing relations.
        """
        self.role.permissions.add(self.perm1, self.perm2, self.perm3)

        mock_tuple_response = MagicMock()
        read_responses = [[mock_tuple_response], [], [mock_tuple_response]]
        mock_stub = self._setup_relations_mocks(mock_create_channel, read_responses)

        with patch(RELATIONS_STUB_PATH, return_value=mock_stub):
            permission_tuples = [CustomRoleV2._permission_tuple(self.role, p) for p in self.role.permissions.all()]
            result = self.checker.check_custom_role_permissions(permission_tuples, str(self.role.uuid))

            self.assertFalse(result)
            self.assertEqual(mock_stub.ReadTuples.call_count, 3)

    @patch(RELATIONS_CHANNEL_PATH)
    def test_wildcard_permissions_all_missing(self, mock_create_channel):
        """Test that wildcard check returns False when all permission relations are missing."""
        self.role.permissions.add(self.perm1, self.perm2)

        read_responses = [[], []]
        mock_stub = self._setup_relations_mocks(mock_create_channel, read_responses)

        with patch(RELATIONS_STUB_PATH, return_value=mock_stub):
            permission_tuples = [CustomRoleV2._permission_tuple(self.role, p) for p in self.role.permissions.all()]
            result = self.checker.check_custom_role_permissions(permission_tuples, str(self.role.uuid))

            self.assertFalse(result)

    @patch(RELATIONS_CHANNEL_PATH)
    def test_wildcard_single_permission_exists(self, mock_create_channel):
        """Test wildcard check with a single permission that exists."""
        self.role.permissions.add(self.perm1)

        mock_tuple_response = MagicMock()
        read_responses = [[mock_tuple_response]]
        mock_stub = self._setup_relations_mocks(mock_create_channel, read_responses)

        with patch(RELATIONS_STUB_PATH, return_value=mock_stub):
            permission_tuples = [CustomRoleV2._permission_tuple(self.role, p) for p in self.role.permissions.all()]
            result = self.checker.check_custom_role_permissions(permission_tuples, str(self.role.uuid))

            self.assertTrue(result)
            self.assertEqual(mock_stub.ReadTuples.call_count, 1)
