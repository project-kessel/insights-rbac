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
"""Tests for the Relations API client utility functions."""

from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from grpc import RpcError
from kessel.inventory.v1beta2 import reporter_reference_pb2

from management.role_binding.util.inventory_api_client import (
    lookup_binding_subjects,
    parse_resource_type,
)

# Valid UUIDs for testing
VALID_UUID_1 = "019d1b5e-f95c-7e23-b10b-ae7f44e245bf"
VALID_UUID_2 = "123e4567-e89b-12d3-a456-426614174000"
VALID_UUID_3 = "550e8400-e29b-41d4-a716-446655440000"


def subject_payload(resource_id):
    """Build a StreamedListSubjectsResponse-shaped dict for a given resource id."""
    return {"subject": {"resource": {"resourceId": resource_id}}}


class ParseResourceTypeTests(TestCase):
    """Tests for the parse_resource_type function."""

    def test_parses_type_with_namespace_prefix(self):
        """Test parsing resource type with namespace/name format."""
        namespace, name = parse_resource_type("rbac/workspace")
        self.assertEqual(namespace, "rbac")
        self.assertEqual(name, "workspace")

    def test_parses_type_without_namespace_prefix(self):
        """Test parsing resource type without namespace defaults to rbac."""
        namespace, name = parse_resource_type("workspace")
        self.assertEqual(namespace, "rbac")
        self.assertEqual(name, "workspace")

    def test_parses_custom_namespace(self):
        """Test parsing resource type with custom namespace."""
        namespace, name = parse_resource_type("inventory/host")
        self.assertEqual(namespace, "inventory")
        self.assertEqual(name, "host")

    def test_handles_multiple_slashes(self):
        """Test that only the first slash is used as delimiter."""
        namespace, name = parse_resource_type("ns/type/with/slashes")
        self.assertEqual(namespace, "ns")
        self.assertEqual(name, "type/with/slashes")

    def test_handles_empty_name_after_slash(self):
        """Test handling of namespace with empty name."""
        namespace, name = parse_resource_type("rbac/")
        self.assertEqual(namespace, "rbac")
        self.assertEqual(name, "")


class LookupBindingSubjectsTests(TestCase):
    """Tests for the lookup_binding_subjects function."""

    @override_settings(INVENTORY_API_SERVER=None)
    def test_returns_none_when_inventory_api_not_configured(self):
        """Test that None is returned when INVENTORY_API_SERVER is not set."""
        result = lookup_binding_subjects("workspace", "ws-123")
        self.assertIsNone(result)

    @override_settings(INVENTORY_API_SERVER="")
    def test_returns_none_when_inventory_api_empty_string(self):
        """Test that None is returned when INVENTORY_API_SERVER is empty string."""
        result = lookup_binding_subjects("workspace", "ws-123")
        self.assertIsNone(result)

    @override_settings(INVENTORY_API_SERVER="localhost:9000")
    @patch("management.role_binding.util.inventory_api_client._jwt_manager")
    @patch("management.role_binding.util.inventory_api_client.create_client_channel_inventory")
    def test_returns_subject_ids_from_response(self, mock_create_channel, mock_jwt_manager):
        """Test that subject IDs are extracted from successful response."""
        mock_jwt_manager.get_jwt_from_redis.return_value = "test-token"

        mock_stub = MagicMock()
        mock_response_1 = MagicMock()
        mock_response_2 = MagicMock()

        # Simulate json_format.MessageToDict output
        with patch("management.role_binding.util.inventory_api_client.json_format") as mock_json_format:
            mock_json_format.MessageToDict.side_effect = [
                subject_payload(VALID_UUID_1),
                subject_payload(VALID_UUID_2),
            ]

            mock_stub.StreamedListSubjects.return_value = [mock_response_1, mock_response_2]
            mock_create_channel.return_value.__enter__.return_value = mock_stub

            with patch(
                "management.role_binding.util.inventory_api_client.inventory_service_pb2_grpc.KesselInventoryServiceStub",
                return_value=mock_stub,
            ):
                result = lookup_binding_subjects("workspace", "ws-123")

        self.assertIsNotNone(result)
        self.assertEqual(set(result), {VALID_UUID_1, VALID_UUID_2})

    @override_settings(INVENTORY_API_SERVER="localhost:9000")
    @patch("management.role_binding.util.inventory_api_client._jwt_manager")
    @patch("management.role_binding.util.inventory_api_client.create_client_channel_inventory")
    def test_returns_empty_list_when_no_subjects_found(self, mock_create_channel, mock_jwt_manager):
        """Test that empty list is returned when no subjects are found."""
        mock_jwt_manager.get_jwt_from_redis.return_value = "test-token"

        mock_stub = MagicMock()
        mock_stub.StreamedListSubjects.return_value = []
        mock_create_channel.return_value.__enter__.return_value = mock_stub

        with patch(
            "management.role_binding.util.inventory_api_client.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            result = lookup_binding_subjects("workspace", "ws-123")

        self.assertIsNotNone(result)
        self.assertEqual(result, [])

    @override_settings(INVENTORY_API_SERVER="localhost:9000")
    @patch("management.role_binding.util.inventory_api_client._jwt_manager")
    @patch("management.role_binding.util.inventory_api_client.create_client_channel_inventory")
    def test_returns_none_on_grpc_error(self, mock_create_channel, mock_jwt_manager):
        """Test that None is returned when gRPC call fails."""
        mock_jwt_manager.get_jwt_from_redis.return_value = "test-token"

        mock_create_channel.return_value.__enter__.side_effect = RpcError()

        result = lookup_binding_subjects("workspace", "ws-123")

        self.assertIsNone(result)

    @override_settings(INVENTORY_API_SERVER="localhost:9000")
    @patch("management.role_binding.util.inventory_api_client._jwt_manager")
    @patch("management.role_binding.util.inventory_api_client.create_client_channel_inventory")
    def test_returns_none_on_generic_exception(self, mock_create_channel, mock_jwt_manager):
        """Test that None is returned when an unexpected exception occurs."""
        mock_jwt_manager.get_jwt_from_redis.return_value = "test-token"

        mock_create_channel.return_value.__enter__.side_effect = Exception("Unexpected error")

        result = lookup_binding_subjects("workspace", "ws-123")

        self.assertIsNone(result)

    @override_settings(INVENTORY_API_SERVER="localhost:9000")
    @patch("management.role_binding.util.inventory_api_client._jwt_manager")
    @patch("management.role_binding.util.inventory_api_client.create_client_channel_inventory")
    def test_deduplicates_subject_ids(self, mock_create_channel, mock_jwt_manager):
        """Test that duplicate subject IDs are deduplicated."""
        mock_jwt_manager.get_jwt_from_redis.return_value = "test-token"

        mock_stub = MagicMock()
        mock_response_1 = MagicMock()
        mock_response_2 = MagicMock()
        mock_response_3 = MagicMock()

        with patch("management.role_binding.util.inventory_api_client.json_format") as mock_json_format:
            mock_json_format.MessageToDict.side_effect = [
                subject_payload(VALID_UUID_1),
                subject_payload(VALID_UUID_2),
                subject_payload(VALID_UUID_1),  # Duplicate
            ]

            mock_stub.StreamedListSubjects.return_value = [mock_response_1, mock_response_2, mock_response_3]
            mock_create_channel.return_value.__enter__.return_value = mock_stub

            with patch(
                "management.role_binding.util.inventory_api_client.inventory_service_pb2_grpc.KesselInventoryServiceStub",
                return_value=mock_stub,
            ):
                result = lookup_binding_subjects("workspace", "ws-123")

        self.assertIsNotNone(result)
        self.assertEqual(len(result), 2)
        self.assertEqual(set(result), {VALID_UUID_1, VALID_UUID_2})

    @override_settings(INVENTORY_API_SERVER="localhost:9000")
    @patch("management.role_binding.util.inventory_api_client._jwt_manager")
    @patch("management.role_binding.util.inventory_api_client.create_client_channel_inventory")
    def test_skips_responses_without_subject_id(self, mock_create_channel, mock_jwt_manager):
        """Test that responses without subject ID are skipped."""
        mock_jwt_manager.get_jwt_from_redis.return_value = "test-token"

        mock_stub = MagicMock()
        mock_response_1 = MagicMock()
        mock_response_2 = MagicMock()
        mock_response_3 = MagicMock()

        with patch("management.role_binding.util.inventory_api_client.json_format") as mock_json_format:
            mock_json_format.MessageToDict.side_effect = [
                subject_payload(VALID_UUID_1),
                {"subject": {"resource": {}}},  # Missing resourceId
                {"other_field": "value"},  # Missing subject entirely
            ]

            mock_stub.StreamedListSubjects.return_value = [mock_response_1, mock_response_2, mock_response_3]
            mock_create_channel.return_value.__enter__.return_value = mock_stub

            with patch(
                "management.role_binding.util.inventory_api_client.inventory_service_pb2_grpc.KesselInventoryServiceStub",
                return_value=mock_stub,
            ):
                result = lookup_binding_subjects("workspace", "ws-123")

        self.assertIsNotNone(result)
        self.assertEqual(result, [VALID_UUID_1])

    @override_settings(INVENTORY_API_SERVER="localhost:9000")
    @patch("management.role_binding.util.inventory_api_client._jwt_manager")
    @patch("management.role_binding.util.inventory_api_client.create_client_channel_inventory")
    def test_uses_default_parameters(self, mock_create_channel, mock_jwt_manager):
        """Test that default parameters are correctly passed to the API."""
        mock_jwt_manager.get_jwt_from_redis.return_value = "test-token"

        mock_stub = MagicMock()
        mock_stub.StreamedListSubjects.return_value = []
        mock_create_channel.return_value.__enter__.return_value = mock_stub

        with patch(
            "management.role_binding.util.inventory_api_client.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            with patch(
                "management.role_binding.util.inventory_api_client.streamed_list_subjects_request_pb2"
            ) as mock_request_pb2:
                with patch(
                    "management.role_binding.util.inventory_api_client.resource_reference_pb2"
                ) as mock_resource_reference_pb2:
                    with patch(
                        "management.role_binding.util.inventory_api_client.representation_type_pb2"
                    ) as mock_representation_type_pb2:
                        mock_request = MagicMock()
                        mock_request_pb2.StreamedListSubjectsRequest.return_value = mock_request

                        lookup_binding_subjects("workspace", "ws-123")

                        # Verify default resource type and subject type are correctly passed
                        mock_resource_reference_pb2.ResourceReference.assert_any_call(
                            resource_type="workspace",
                            resource_id="ws-123",
                            reporter=reporter_reference_pb2.ReporterReference(type="rbac"),
                        )
                        mock_representation_type_pb2.RepresentationType.assert_any_call(
                            resource_type="role_binding", reporter_type="rbac"
                        )

    @override_settings(INVENTORY_API_SERVER="localhost:9000")
    @patch("management.role_binding.util.inventory_api_client._jwt_manager")
    @patch("management.role_binding.util.inventory_api_client.create_client_channel_inventory")
    def test_uses_custom_parameters(self, mock_create_channel, mock_jwt_manager):
        """Test that custom parameters are correctly passed to the API."""
        mock_jwt_manager.get_jwt_from_redis.return_value = "test-token"

        mock_stub = MagicMock()
        mock_stub.StreamedListSubjects.return_value = []
        mock_create_channel.return_value.__enter__.return_value = mock_stub

        with patch(
            "management.role_binding.util.inventory_api_client.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            with patch(
                "management.role_binding.util.inventory_api_client.streamed_list_subjects_request_pb2"
            ) as mock_request_pb2:
                with patch(
                    "management.role_binding.util.inventory_api_client.representation_type_pb2"
                ) as mock_representation_type_pb2:
                    mock_request = MagicMock()
                    mock_request_pb2.StreamedListSubjectsRequest.return_value = mock_request

                    lookup_binding_subjects(
                        resource_type="inventory/host",
                        resource_id="host-456",
                        relation="custom_relation",
                        subject_namespace="custom_ns",
                        subject_name="custom_type",
                    )

                    # Verify custom subject type is used
                    mock_representation_type_pb2.RepresentationType.assert_any_call(
                        resource_type="custom_type", reporter_type="custom_ns"
                    )

    @override_settings(INVENTORY_API_SERVER="localhost:9000")
    @patch("management.role_binding.util.inventory_api_client._jwt_manager")
    @patch("management.role_binding.util.inventory_api_client.create_client_channel_inventory")
    def test_includes_auth_token_when_available(self, mock_create_channel, mock_jwt_manager):
        """Test that authorization header is included when token is available."""
        mock_jwt_manager.get_jwt_from_redis.return_value = "test-token"

        mock_stub = MagicMock()
        mock_stub.StreamedListSubjects.return_value = []
        mock_create_channel.return_value.__enter__.return_value = mock_stub

        with patch(
            "management.role_binding.util.inventory_api_client.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            lookup_binding_subjects("workspace", "ws-123")

            # Verify metadata includes auth header
            call_kwargs = mock_stub.StreamedListSubjects.call_args[1]
            self.assertIn("metadata", call_kwargs)
            metadata = call_kwargs["metadata"]
            self.assertIn(("authorization", "Bearer test-token"), metadata)

    @override_settings(INVENTORY_API_SERVER="localhost:9000")
    @patch("management.role_binding.util.inventory_api_client._jwt_manager")
    @patch("management.role_binding.util.inventory_api_client.create_client_channel_inventory")
    def test_no_auth_metadata_when_token_not_available(self, mock_create_channel, mock_jwt_manager):
        """Test that no auth metadata is sent when token is not available."""
        mock_jwt_manager.get_jwt_from_redis.return_value = None

        mock_stub = MagicMock()
        mock_stub.StreamedListSubjects.return_value = []
        mock_create_channel.return_value.__enter__.return_value = mock_stub

        with patch(
            "management.role_binding.util.inventory_api_client.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            lookup_binding_subjects("workspace", "ws-123")

            # Verify metadata is empty
            call_kwargs = mock_stub.StreamedListSubjects.call_args[1]
            self.assertIn("metadata", call_kwargs)
            metadata = call_kwargs["metadata"]
            self.assertEqual(metadata, [])

    @override_settings(INVENTORY_API_SERVER="localhost:9000")
    @patch("management.role_binding.util.inventory_api_client._jwt_manager")
    @patch("management.role_binding.util.inventory_api_client.create_client_channel_inventory")
    def test_parses_namespace_from_resource_type(self, mock_create_channel, mock_jwt_manager):
        """Test that namespace is correctly parsed from resource_type with slash."""
        mock_jwt_manager.get_jwt_from_redis.return_value = "test-token"

        mock_stub = MagicMock()
        mock_stub.StreamedListSubjects.return_value = []
        mock_create_channel.return_value.__enter__.return_value = mock_stub

        with patch(
            "management.role_binding.util.inventory_api_client.inventory_service_pb2_grpc.KesselInventoryServiceStub",
            return_value=mock_stub,
        ):
            with patch(
                "management.role_binding.util.inventory_api_client.streamed_list_subjects_request_pb2"
            ) as mock_request_pb2:
                with patch(
                    "management.role_binding.util.inventory_api_client.resource_reference_pb2"
                ) as mock_resource_reference_pb2:
                    with patch(
                        "management.role_binding.util.inventory_api_client.reporter_reference_pb2"
                    ) as mock_reporter_reference_pb2:
                        mock_request = MagicMock()
                        mock_request_pb2.StreamedListSubjectsRequest.return_value = mock_request

                        lookup_binding_subjects("custom/resource", "res-789")

                        # Verify resource_type (name) and reporter (namespace) are parsed from resource_type
                        mock_resource_reference_pb2.ResourceReference.assert_any_call(
                            resource_type="resource",
                            resource_id="res-789",
                            reporter=mock_reporter_reference_pb2.ReporterReference.return_value,
                        )
                        mock_reporter_reference_pb2.ReporterReference.assert_any_call(type="custom")

    @override_settings(INVENTORY_API_SERVER="localhost:9000")
    @patch("management.role_binding.util.inventory_api_client._jwt_manager")
    @patch("management.role_binding.util.inventory_api_client.create_client_channel_inventory")
    def test_skips_non_uuid_subject_ids(self, mock_create_channel, mock_jwt_manager):
        """Test that non-UUID subject IDs are skipped."""
        mock_jwt_manager.get_jwt_from_redis.return_value = "test-token"

        mock_stub = MagicMock()
        mock_response_1 = MagicMock()
        mock_response_2 = MagicMock()
        mock_response_3 = MagicMock()

        with patch("management.role_binding.util.inventory_api_client.json_format") as mock_json_format:
            mock_json_format.MessageToDict.side_effect = [
                subject_payload(VALID_UUID_1),
                subject_payload("not-a-valid-uuid"),  # Non-UUID, should be skipped
                subject_payload(VALID_UUID_2),
            ]

            mock_stub.StreamedListSubjects.return_value = [mock_response_1, mock_response_2, mock_response_3]
            mock_create_channel.return_value.__enter__.return_value = mock_stub

            with patch(
                "management.role_binding.util.inventory_api_client.inventory_service_pb2_grpc.KesselInventoryServiceStub",
                return_value=mock_stub,
            ):
                result = lookup_binding_subjects("workspace", "ws-123")

        self.assertIsNotNone(result)
        self.assertEqual(len(result), 2)
        self.assertEqual(set(result), {VALID_UUID_1, VALID_UUID_2})
        self.assertNotIn("not-a-valid-uuid", result)

    @override_settings(INVENTORY_API_SERVER="localhost:9000")
    @patch("management.role_binding.util.inventory_api_client._jwt_manager")
    @patch("management.role_binding.util.inventory_api_client.create_client_channel_inventory")
    def test_returns_empty_list_when_all_ids_are_non_uuid(self, mock_create_channel, mock_jwt_manager):
        """Test that empty list is returned when all subject IDs are non-UUIDs."""
        mock_jwt_manager.get_jwt_from_redis.return_value = "test-token"

        mock_stub = MagicMock()
        mock_response_1 = MagicMock()
        mock_response_2 = MagicMock()

        with patch("management.role_binding.util.inventory_api_client.json_format") as mock_json_format:
            mock_json_format.MessageToDict.side_effect = [
                subject_payload("namespace/not-a-uuid"),
                subject_payload("invalid-id"),
            ]

            mock_stub.StreamedListSubjects.return_value = [mock_response_1, mock_response_2]
            mock_create_channel.return_value.__enter__.return_value = mock_stub

            with patch(
                "management.role_binding.util.inventory_api_client.inventory_service_pb2_grpc.KesselInventoryServiceStub",
                return_value=mock_stub,
            ):
                result = lookup_binding_subjects("workspace", "ws-123")

        self.assertIsNotNone(result)
        self.assertEqual(result, [])

    @override_settings(INVENTORY_API_SERVER="localhost:9000")
    @patch("management.role_binding.util.inventory_api_client.logger")
    @patch("management.role_binding.util.inventory_api_client._jwt_manager")
    @patch("management.role_binding.util.inventory_api_client.create_client_channel_inventory")
    def test_logs_warning_for_non_uuid_subject_ids(self, mock_create_channel, mock_jwt_manager, mock_logger):
        """Test that a warning is logged when non-UUID subject IDs are encountered."""
        mock_jwt_manager.get_jwt_from_redis.return_value = "test-token"

        mock_stub = MagicMock()
        mock_response = MagicMock()

        with patch("management.role_binding.util.inventory_api_client.json_format") as mock_json_format:
            mock_json_format.MessageToDict.return_value = subject_payload("namespace/not-a-uuid")

            mock_stub.StreamedListSubjects.return_value = [mock_response]
            mock_create_channel.return_value.__enter__.return_value = mock_stub

            with patch(
                "management.role_binding.util.inventory_api_client.inventory_service_pb2_grpc.KesselInventoryServiceStub",
                return_value=mock_stub,
            ):
                lookup_binding_subjects("workspace", "ws-123")

        mock_logger.warning.assert_called_with(
            "Skipping non-UUID subject_id from Inventory API: %s", "namespace/not-a-uuid"
        )
