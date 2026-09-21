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

"""Tests for the Inventory API replicator."""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from management.inventory_replicator.inventory_api_replicator import InventoryApiReplicator
from management.inventory_replicator.types import ObjectReference, ObjectType, RelationTuple, SubjectReference


def make_relation_tuple():
    """Build a valid relationship for Inventory API request tests."""
    return RelationTuple(
        resource=ObjectReference(type=ObjectType(namespace="rbac", name="group"), id="group-1"),
        relation="member",
        subject=SubjectReference(
            subject=ObjectReference(type=ObjectType(namespace="rbac", name="principal"), id="user-1")
        ),
    )


class InventoryApiReplicatorTests(TestCase):
    """Test relationship writes to the Inventory API."""

    @patch("management.inventory_replicator.inventory_api_replicator.tuple_service_pb2_grpc.KesselTupleServiceStub")
    @patch("management.inventory_replicator.inventory_api_replicator.create_client_channel_inventory")
    @patch("management.inventory_replicator.inventory_api_replicator.get_inventory_auth_metadata", return_value=[])
    def test_write_relationships_accepts_protobuf_relationships(
        self, mock_auth_metadata, mock_create_channel, mock_stub_class
    ):
        """Kafka-decoded protobuf relationships are sent to CreateTuples unchanged."""
        relationship = make_relation_tuple().as_message()
        mock_stub = MagicMock()
        mock_stub_class.return_value = mock_stub

        InventoryApiReplicator().write_relationships([relationship])

        request = mock_stub.CreateTuples.call_args.args[0]
        self.assertTrue(request.upsert)
        self.assertEqual(list(request.tuples), [relationship])

    @patch("management.inventory_replicator.inventory_api_replicator.tuple_service_pb2_grpc.KesselTupleServiceStub")
    @patch("management.inventory_replicator.inventory_api_replicator.create_client_channel_inventory")
    @patch("management.inventory_replicator.inventory_api_replicator.get_inventory_auth_metadata", return_value=[])
    def test_write_relationships_converts_relation_tuples(
        self, mock_auth_metadata, mock_create_channel, mock_stub_class
    ):
        """RelationTuple inputs are converted via as_message before sending."""
        relation_tuple = make_relation_tuple()
        expected = relation_tuple.as_message()
        mock_stub = MagicMock()
        mock_stub_class.return_value = mock_stub

        InventoryApiReplicator().write_relationships([relation_tuple])

        request = mock_stub.CreateTuples.call_args.args[0]
        self.assertTrue(request.upsert)
        self.assertEqual(list(request.tuples), [expected])

    @patch("management.inventory_replicator.inventory_api_replicator.tuple_service_pb2_grpc.KesselTupleServiceStub")
    @patch("management.inventory_replicator.inventory_api_replicator.create_client_channel_inventory")
    @patch("management.inventory_replicator.inventory_api_replicator.get_inventory_auth_metadata", return_value=[])
    def test_write_relationships_handles_mixed_input(self, mock_auth_metadata, mock_create_channel, mock_stub_class):
        """Mixed RelationTuple and protobuf Relationship inputs are handled correctly."""
        relation_tuple = make_relation_tuple()
        protobuf_relationship = make_relation_tuple().as_message()
        mock_stub = MagicMock()
        mock_stub_class.return_value = mock_stub

        InventoryApiReplicator().write_relationships([relation_tuple, protobuf_relationship])

        request = mock_stub.CreateTuples.call_args.args[0]
        self.assertTrue(request.upsert)
        tuples = list(request.tuples)
        self.assertEqual(len(tuples), 2)
        self.assertEqual(tuples[0], relation_tuple.as_message())
        self.assertEqual(tuples[1], protobuf_relationship)
