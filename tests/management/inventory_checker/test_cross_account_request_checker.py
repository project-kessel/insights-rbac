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
"""Tests for CrossAccountRequestInventoryChecker."""

from unittest.mock import MagicMock, patch

from kessel.inventory.v1beta2.check_response_pb2 import CheckResponse
from management.inventory_checker.inventory_api_check import CrossAccountRequestInventoryChecker
from management.relation_replicator.types import RelationTuple, ObjectReference, ObjectType, SubjectReference
from tests.identity_request import IdentityRequest

INVENTORY_STUB_PATH = (
    "management.inventory_checker.inventory_api_check" ".inventory_service_pb2_grpc.KesselInventoryServiceStub"
)


def _make_tuple(resource_type, resource_id, relation, subject_type, subject_id, subject_relation=None):
    """Build a RelationTuple for testing."""
    return RelationTuple(
        resource=ObjectReference(type=ObjectType(namespace="rbac", name=resource_type), id=resource_id),
        relation=relation,
        subject=SubjectReference(
            subject=ObjectReference(type=ObjectType(namespace="rbac", name=subject_type), id=subject_id),
            relation=subject_relation,
        ),
    )


class CrossAccountRequestInventoryCheckerTest(IdentityRequest):
    """Tests for the CrossAccountRequestInventoryChecker class."""

    def setUp(self):
        """Set up test data."""
        super().setUp()
        self.checker = CrossAccountRequestInventoryChecker()
        self.request_id = "test-car-uuid-1234"

    def _setup_inventory_mocks(self, mock_create_channel, mock_stub_responses):
        """Helper to set up inventory API mocks."""
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
    def test_check_car_all_relations_exist(self, mock_message_to_dict, mock_create_channel):
        """Test that check returns True when all relations exist in inventory."""
        tuples = [
            _make_tuple("workspace", "ws-1", "binding", "role_binding", "rb-1"),
            _make_tuple("role_binding", "rb-1", "role", "role", "role-1"),
            _make_tuple("role_binding", "rb-1", "subject", "principal", "user-1"),
        ]

        mock_response = MagicMock(spec=CheckResponse)
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_response)
        mock_message_to_dict.return_value = {"allowed": "ALLOWED_TRUE"}

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            result = self.checker.check_cross_account_request(tuples, self.request_id)

            self.assertTrue(result)
            self.assertEqual(mock_stub.Check.call_count, 3)

    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    def test_check_car_missing_relation(self, mock_message_to_dict, mock_create_channel):
        """Test that check returns False when a relation is missing in inventory."""
        tuples = [
            _make_tuple("workspace", "ws-1", "binding", "role_binding", "rb-1"),
            _make_tuple("role_binding", "rb-1", "role", "role", "role-1"),
            _make_tuple("role_binding", "rb-1", "subject", "principal", "user-1"),
        ]

        mock_responses = [
            MagicMock(spec=CheckResponse),
            MagicMock(spec=CheckResponse),
            MagicMock(spec=CheckResponse),
        ]
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_responses)
        mock_message_to_dict.side_effect = [
            {"allowed": "ALLOWED_TRUE"},
            {"allowed": "ALLOWED_FALSE"},
            {"allowed": "ALLOWED_TRUE"},
        ]

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            result = self.checker.check_cross_account_request(tuples, self.request_id)
            self.assertFalse(result)

    def test_check_car_empty_tuple_list(self):
        """Test checking with an empty tuple list returns True (no checks to fail)."""
        result = self.checker.check_cross_account_request([], self.request_id)
        self.assertTrue(result)

    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    def test_check_car_multiple_roles(self, mock_message_to_dict, mock_create_channel):
        """Test checking a CAR with multiple roles produces correct number of checks."""
        tuples = [
            _make_tuple("workspace", "ws-1", "binding", "role_binding", "rb-1"),
            _make_tuple("role_binding", "rb-1", "role", "role", "role-1"),
            _make_tuple("role_binding", "rb-1", "subject", "principal", "user-1"),
            _make_tuple("tenant", "t-1", "binding", "role_binding", "rb-2"),
            _make_tuple("role_binding", "rb-2", "role", "role", "role-2"),
            _make_tuple("role_binding", "rb-2", "subject", "principal", "user-1"),
        ]

        mock_response = MagicMock(spec=CheckResponse)
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_response)
        mock_message_to_dict.return_value = {"allowed": "ALLOWED_TRUE"}

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            result = self.checker.check_cross_account_request(tuples, self.request_id)

            self.assertTrue(result)
            self.assertEqual(mock_stub.Check.call_count, 6)

    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    def test_check_car_all_false(self, mock_message_to_dict, mock_create_channel):
        """Test that check returns False when all relations are missing."""
        tuples = [
            _make_tuple("workspace", "ws-1", "binding", "role_binding", "rb-1"),
            _make_tuple("role_binding", "rb-1", "role", "role", "role-1"),
        ]

        mock_response = MagicMock(spec=CheckResponse)
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_response)
        mock_message_to_dict.return_value = {"allowed": "ALLOWED_FALSE"}

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            result = self.checker.check_cross_account_request(tuples, self.request_id)
            self.assertFalse(result)
