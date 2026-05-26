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
"""Tests for GroupPrincipalInventoryChecker."""

from unittest.mock import MagicMock, patch

from kessel.inventory.v1beta2.check_response_pb2 import CheckResponse
from management.group.model import Group
from management.inventory_checker.inventory_api_check import GroupPrincipalInventoryChecker
from management.principal.model import Principal
from tests.identity_request import IdentityRequest

INVENTORY_STUB_PATH = "management.inventory_checker.inventory_api_check.inventory_service_pb2_grpc.KesselInventoryServiceStub"  # noqa: E501


class GroupPrincipalInventoryCheckerTest(IdentityRequest):
    """Tests for the GroupPrincipalInventoryChecker class."""

    def setUp(self):
        """Set up test data."""
        super().setUp()

        self.group = Group.objects.create(
            name="test-group",
            tenant=self.tenant,
        )

        self.principal1 = Principal.objects.create(
            username="user1",
            tenant=self.tenant,
            user_id="user1-id",
        )
        self.principal2 = Principal.objects.create(
            username="user2",
            tenant=self.tenant,
            user_id="user2-id",
        )
        self.principal3 = Principal.objects.create(
            username="user3",
            tenant=self.tenant,
            user_id="user3-id",
        )

        self.checker = GroupPrincipalInventoryChecker()

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
    def test_check_relationships_all_exist(self, mock_message_to_dict, mock_create_channel):
        """Test that check returns all relation_exists=True when all relations exist."""
        self.group.principals.add(self.principal1, self.principal2, self.principal3)

        mock_response = MagicMock(spec=CheckResponse)
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_response)
        mock_message_to_dict.return_value = {"allowed": "ALLOWED_TRUE"}

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub) as mock_stub_class:
            relationships = [self.group.relationship_to_principal(p) for p in self.group.principals.all()]
            relationships = [r for r in relationships if r is not None]
            result = self.checker.check_relationships(relationships)

            self.assertEqual(result["group_uuid"], str(self.group.uuid))
            self.assertEqual(len(result["principal_relations"]), 3)
            self.assertTrue(all(pr["relation_exists"] for pr in result["principal_relations"]))
            self.assertEqual(mock_stub.Check.call_count, 3)
            mock_create_channel.assert_called_once()
            mock_stub_class.assert_called_once()

    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    def test_check_relationships_some_missing(self, mock_message_to_dict, mock_create_channel):
        """Test that check returns correct relation_exists values when some are missing."""
        self.group.principals.add(self.principal1, self.principal2, self.principal3)

        mock_responses = [MagicMock(spec=CheckResponse) for _ in range(3)]
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_responses)
        mock_message_to_dict.side_effect = [
            {"allowed": "ALLOWED_TRUE"},
            {"allowed": "ALLOWED_FALSE"},
            {"allowed": "ALLOWED_TRUE"},
        ]

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            relationships = [self.group.relationship_to_principal(p) for p in self.group.principals.all()]
            relationships = [r for r in relationships if r is not None]
            result = self.checker.check_relationships(relationships)

            self.assertEqual(len(result["principal_relations"]), 3)
            exists_values = [pr["relation_exists"] for pr in result["principal_relations"]]
            self.assertIn(False, exists_values)
            missing = [pr for pr in result["principal_relations"] if not pr["relation_exists"]]
            self.assertEqual(len(missing), 1)

    def test_check_relationships_empty_list(self):
        """Test that check handles empty relationships list."""
        result = self.checker.check_relationships([])
        self.assertEqual(result["group_uuid"], "")
        self.assertEqual(result["principal_relations"], [])

    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    def test_check_relationships_all_missing(self, mock_message_to_dict, mock_create_channel):
        """Test that check returns all relation_exists=False when all are missing."""
        self.group.principals.add(self.principal1, self.principal2)

        mock_responses = [MagicMock(spec=CheckResponse) for _ in range(2)]
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_responses)
        mock_message_to_dict.side_effect = [
            {"allowed": "ALLOWED_FALSE"},
            {"allowed": "ALLOWED_FALSE"},
        ]

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            relationships = [self.group.relationship_to_principal(p) for p in self.group.principals.all()]
            relationships = [r for r in relationships if r is not None]
            result = self.checker.check_relationships(relationships)

            self.assertEqual(len(result["principal_relations"]), 2)
            self.assertTrue(all(not pr["relation_exists"] for pr in result["principal_relations"]))

    def test_principal_without_user_id_filtered_out(self):
        """Test that principals without user_id produce None relationships that are filtered."""
        principal_no_uid = Principal.objects.create(
            username="no-uid-user",
            tenant=self.tenant,
            user_id=None,
        )
        self.group.principals.add(principal_no_uid)

        relationships = [self.group.relationship_to_principal(p) for p in self.group.principals.all()]
        non_none = [r for r in relationships if r is not None]

        self.assertIn(None, relationships)
        self.assertEqual(len(non_none), 0)

    @patch("management.inventory_checker.inventory_api_check.create_client_channel_inventory")
    @patch("management.inventory_checker.inventory_api_check.json_format.MessageToDict")
    def test_check_relationships_single_principal(self, mock_message_to_dict, mock_create_channel):
        """Test checking a group with a single principal."""
        self.group.principals.add(self.principal1)

        mock_response = MagicMock(spec=CheckResponse)
        mock_stub = self._setup_inventory_mocks(mock_create_channel, mock_response)
        mock_message_to_dict.return_value = {"allowed": "ALLOWED_TRUE"}

        with patch(INVENTORY_STUB_PATH, return_value=mock_stub):
            relationships = [self.group.relationship_to_principal(p) for p in self.group.principals.all()]
            relationships = [r for r in relationships if r is not None]
            result = self.checker.check_relationships(relationships)

            self.assertEqual(result["group_uuid"], str(self.group.uuid))
            self.assertEqual(len(result["principal_relations"]), 1)
            self.assertTrue(result["principal_relations"][0]["relation_exists"])
            self.assertEqual(mock_stub.Check.call_count, 1)
