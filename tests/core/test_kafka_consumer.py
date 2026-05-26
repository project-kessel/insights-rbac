#
# Copyright 2024 Red Hat, Inc.
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

"""Tests for RBAC Kafka consumer."""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import grpc

# Ensure the rbac module can be found when running in different environments
# This MUST be done before any rbac imports
if "/var/workdir" in str(Path(__file__).parent):
    # Running in container environment, add parent directory to path
    sys.path.insert(0, "/var/workdir")
else:
    # Running in local environment
    project_root = Path(__file__).parent.parent.parent
    rbac_source_dir = str(project_root / "rbac")

    # Only add the rbac source directory if it's not already in the path
    if rbac_source_dir not in sys.path:
        sys.path.insert(0, rbac_source_dir)

    # Also ensure the project root is early in sys.path for rbac package imports
    if str(project_root) not in sys.path:
        sys.path.insert(1, str(project_root))

from core.kafka_consumer import (
    DebeziumMessage,
    MessageValidator,
    RBACKafkaConsumer,
    ReplicationMessage,
    RetryConfig,
)
from django.test import TestCase
from django.test.utils import override_settings
from kafka.errors import KafkaError


class MessageValidatorTests(TestCase):
    """Tests for MessageValidator class."""

    def setUp(self):
        """Set up test fixtures."""
        self.validator = MessageValidator()

    def test_validate_parsed_message_valid(self):
        """Test validation of valid parsed message."""
        message = {
            "aggregatetype": "relations",
            "aggregateid": "test-id-123",
            "type": "create_group",
            "payload": {"relations_to_add": [], "relations_to_remove": []},
        }

        self.assertTrue(self.validator.validate_parsed_message(message))

    def test_validate_parsed_message_missing_field(self):
        """Test validation fails when required field is missing."""
        message = {
            "aggregatetype": "relations",
            "aggregateid": "test-id-123",
            # Missing "type" field
            "payload": {},
        }

        self.assertFalse(self.validator.validate_parsed_message(message))

    def test_validate_parsed_message_invalid_aggregatetype(self):
        """Test validation passes with any aggregatetype (validation removed)."""
        message = {
            "aggregatetype": "invalid_type",
            "aggregateid": "test-id-123",
            "type": "create_group",
            "payload": {},
        }

        self.assertTrue(self.validator.validate_parsed_message(message))

    def test_validate_parsed_message_empty_aggregateid(self):
        """Test validation passes with empty aggregateid (validation removed)."""
        message = {
            "aggregatetype": "relations",
            "aggregateid": "",
            "type": "create_group",
            "payload": {},
        }

        self.assertTrue(self.validator.validate_parsed_message(message))

    def test_validate_parsed_message_empty_event_type(self):
        """Test validation passes with empty event type (validation removed)."""
        message = {
            "aggregatetype": "relations",
            "aggregateid": "test-id-123",
            "type": "",
            "payload": {},
        }

        self.assertTrue(self.validator.validate_parsed_message(message))

    def test_validate_parsed_message_invalid_payload_type(self):
        """Test validation fails with non-dict payload."""
        message = {
            "aggregatetype": "relations",
            "aggregateid": "test-id-123",
            "type": "create_group",
            "payload": "invalid_payload",
        }

        self.assertFalse(self.validator.validate_parsed_message(message))

    def test_validate_replication_message_valid(self):
        """Test validation of valid replication message."""
        payload = {
            "relations_to_add": [
                {
                    "resource": {"type": "rbac", "id": "group1"},
                    "subject": {"type": "rbac", "id": "user1"},
                    "relation": "member",
                }
            ],
            "relations_to_remove": [],
            "resource_context": {
                "org_id": "12345",
                "event_type": "create_group",
            },
        }

        self.assertTrue(self.validator.validate_replication_message(payload))

    def test_validate_replication_message_missing_field(self):
        """Test validation passes when only one of the fields is present."""
        payload = {
            "relations_to_add": [
                {
                    "resource": {"type": "rbac", "id": "group1"},
                    "subject": {"type": "rbac", "id": "user1"},
                    "relation": "member",
                }
            ]
            # Missing "relations_to_remove" - but that's okay now
        }

        self.assertTrue(self.validator.validate_replication_message(payload))

    def test_validate_replication_message_invalid_type(self):
        """Test validation fails with invalid field types."""
        payload = {"relations_to_add": "not_a_list", "relations_to_remove": []}

        self.assertFalse(self.validator.validate_replication_message(payload))

    def test_validate_replication_message_empty_relations(self):
        """Test validation fails when both relations lists are empty."""
        payload = {"relations_to_add": [], "relations_to_remove": []}

        self.assertFalse(self.validator.validate_replication_message(payload))

    def test_validate_replication_message_invalid_relation_structure(self):
        """Test validation fails with invalid relation structure."""
        payload = {
            "relations_to_add": [
                {
                    "resource": {"type": "rbac", "id": "group1"},
                    # Missing "subject" field
                    "relation": "member",
                }
            ],
            "relations_to_remove": [],
        }

        self.assertFalse(self.validator.validate_replication_message(payload))

    def test_validate_replication_message_missing_resource_context(self):
        """Test validation passes when resource_context is missing (optional now)."""
        payload = {
            "relations_to_add": [
                {
                    "resource": {"type": "rbac", "id": "group1"},
                    "subject": {"type": "rbac", "id": "user1"},
                    "relation": "member",
                }
            ],
            "relations_to_remove": [],
            # Missing "resource_context" - but that's okay now
        }

        self.assertTrue(self.validator.validate_replication_message(payload))

    def test_validate_replication_message_invalid_resource_context_type(self):
        """Test validation passes when resource_context is not a dict (validation removed)."""
        payload = {
            "relations_to_add": [
                {
                    "resource": {"type": "rbac", "id": "group1"},
                    "subject": {"type": "rbac", "id": "user1"},
                    "relation": "member",
                }
            ],
            "relations_to_remove": [],
            "resource_context": "not_a_dict",
        }

        self.assertTrue(self.validator.validate_replication_message(payload))

    def test_validate_replication_message_valid_with_resource_context(self):
        """Test validation succeeds with complete resource_context (CREATE_WORKSPACE case)."""
        payload = {
            "relations_to_add": [
                {
                    "resource": {"type": "rbac", "id": "workspace1"},
                    "subject": {"type": "rbac", "id": "user1"},
                    "relation": "member",
                }
            ],
            "relations_to_remove": [],
            "resource_context": {
                "resource_type": "Workspace",
                "resource_id": "workspace1",
                "org_id": "12345",
                "event_type": "create_workspace",
            },
        }

        self.assertTrue(self.validator.validate_replication_message(payload))

    def test_validate_replication_message_valid_with_minimal_resource_context(self):
        """Test validation succeeds with minimal resource_context (org_id and event_type only)."""
        payload = {
            "relations_to_add": [
                {
                    "resource": {"type": "rbac", "id": "group1"},
                    "subject": {"type": "rbac", "id": "user1"},
                    "relation": "member",
                }
            ],
            "relations_to_remove": [],
            "resource_context": {
                "org_id": "12345",
                "event_type": "create_group",
            },
        }

        self.assertTrue(self.validator.validate_replication_message(payload))


class DebeziumMessageTests(TestCase):
    """Tests for DebeziumMessage class."""

    def test_from_kafka_message(self):
        """Test creating DebeziumMessage from Kafka message."""
        message_value = {
            "aggregatetype": "relations",
            "aggregateid": "test-id-123",
            "type": "create_group",
            "payload": {"test": "data"},
        }

        msg = DebeziumMessage.from_kafka_message(message_value)

        self.assertEqual(msg.aggregatetype, "relations")
        self.assertEqual(msg.aggregateid, "test-id-123")
        self.assertEqual(msg.event_type, "create_group")
        self.assertEqual(msg.payload, {"test": "data"})

    def test_from_kafka_message_missing_fields(self):
        """Test creating DebeziumMessage with missing fields."""
        message_value = {}

        msg = DebeziumMessage.from_kafka_message(message_value)

        self.assertEqual(msg.aggregatetype, "")
        self.assertEqual(msg.aggregateid, "")
        self.assertEqual(msg.event_type, "")
        self.assertEqual(msg.payload, {})


class ReplicationMessageTests(TestCase):
    """Tests for ReplicationMessage class."""

    def test_from_payload(self):
        """Test creating ReplicationMessage from payload."""
        payload = {
            "relations_to_add": [{"test": "add"}],
            "relations_to_remove": [{"test": "remove"}],
        }

        msg = ReplicationMessage.from_payload(payload)

        self.assertEqual(msg.relations_to_add, [{"test": "add"}])
        self.assertEqual(msg.relations_to_remove, [{"test": "remove"}])

    def test_from_payload_missing_fields(self):
        """Test creating ReplicationMessage with missing fields."""
        payload = {}

        msg = ReplicationMessage.from_payload(payload)

        self.assertEqual(msg.relations_to_add, [])
        self.assertEqual(msg.relations_to_remove, [])


class DebeziumMessageParsingTests(TestCase):
    """Tests for Debezium message parsing functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.liveness_file = Path(self.temp_dir) / "kubernetes-liveness"
        self.readiness_file = Path(self.temp_dir) / "kubernetes-readiness"

    @override_settings(KAFKA_ENABLED=True, RBAC_KAFKA_CONSUMER_TOPIC="test-topic")
    @patch("core.kafka_consumer.Path")
    def test_parse_standard_debezium_message_with_string_payload(self, mock_path):
        """Test parsing standard Debezium message with JSON string payload."""
        mock_path.return_value = self.liveness_file
        consumer = RBACKafkaConsumer()

        # Standard Debezium message format as provided in the user's question
        debezium_message = {
            "schema": {
                "type": "string",
                "optional": False,
                "name": "io.debezium.data.Json",
                "version": 1,
            },
            "payload": (
                '{"aggregatetype": "relations", "aggregateid": "test-aggregate-id", "type": "create_binding", '
                '"relations_to_add": [{"subject": {"subject": {"id": "09e360f0-ba13-48e2-a25a-76224b1f1717", '
                '"type": {"name": "group", "namespace": "rbac"}}, "relation": "member"}, "relation": "subject", '
                '"resource": {"id": "f2c095b1-b02d-4cf7-a71f-4dc06de0d9e1", "type": {"name": "role_binding", '
                '"namespace": "rbac"}}}], "relations_to_remove": [{"subject": {"subject": {"id": '
                '"09e360f0-ba13-48e2-a25a-76224b1f1717", "type": {"name": "group", "namespace": "rbac"}}, '
                '"relation": "member"}, "relation": "subject", "resource": {"id": '
                '"f2c095b1-b02d-4cf7-a71f-4dc06de0d9e1", "type": {"name": "role_binding", "namespace": "rbac"}}}]}'
            ),
        }

        result = consumer._parse_debezium_message(debezium_message)

        self.assertIsNotNone(result)
        self.assertEqual(result["aggregatetype"], "relations")
        self.assertEqual(result["aggregateid"], "test-aggregate-id")
        self.assertEqual(result["type"], "create_binding")

        # Verify the payload was correctly parsed
        payload = result["payload"]
        self.assertIn("relations_to_add", payload)
        self.assertIn("relations_to_remove", payload)
        self.assertEqual(len(payload["relations_to_add"]), 1)
        self.assertEqual(len(payload["relations_to_remove"]), 1)

    @override_settings(KAFKA_ENABLED=True, RBAC_KAFKA_CONSUMER_TOPIC="test-topic")
    @patch("core.kafka_consumer.Path")
    def test_parse_debezium_message_with_dict_payload(self, mock_path):
        """Test parsing Debezium message with dict payload (already parsed)."""
        mock_path.return_value = self.liveness_file
        consumer = RBACKafkaConsumer()

        debezium_message = {
            "schema": {
                "type": "string",
                "optional": False,
                "name": "io.debezium.data.Json",
                "version": 1,
            },
            "payload": {
                "aggregatetype": "relations",
                "aggregateid": "dict-test-id",
                "type": "add_member",
                "relations_to_add": [
                    {
                        "subject": {"id": "user-123", "type": "user"},
                        "relation": "member",
                        "resource": {"id": "group-456", "type": "group"},
                    }
                ],
                "relations_to_remove": [],
            },
        }

        result = consumer._parse_debezium_message(debezium_message)

        self.assertIsNotNone(result)
        self.assertEqual(result["aggregatetype"], "relations")
        self.assertEqual(result["aggregateid"], "dict-test-id")
        self.assertEqual(result["type"], "add_member")
        self.assertEqual(len(result["payload"]["relations_to_add"]), 1)
        self.assertEqual(len(result["payload"]["relations_to_remove"]), 0)

    @override_settings(KAFKA_ENABLED=True, RBAC_KAFKA_CONSUMER_TOPIC="test-topic")
    @patch("core.kafka_consumer.Path")
    def test_parse_non_debezium_message_rejected(self, mock_path):
        """Test that non-Debezium messages are rejected."""
        from core.kafka_consumer import ValidationError

        mock_path.return_value = self.liveness_file
        consumer = RBACKafkaConsumer()

        # Legacy format should now be rejected with ValidationError
        legacy_message = {
            "aggregatetype": "relations",
            "aggregateid": "group-123",
            "type": "create_group",
            "payload": {"relations_to_add": [], "relations_to_remove": []},
        }

        with self.assertRaises(ValidationError):
            consumer._parse_debezium_message(legacy_message)

    @override_settings(KAFKA_ENABLED=True, RBAC_KAFKA_CONSUMER_TOPIC="test-topic")
    @patch("core.kafka_consumer.Path")
    def test_parse_invalid_debezium_message(self, mock_path):
        """Test parsing invalid Debezium message."""
        from core.kafka_consumer import ValidationError

        mock_path.return_value = self.liveness_file
        consumer = RBACKafkaConsumer()

        # Test message without schema field - should raise ValidationError
        message_no_schema = {"payload": '{"relations_to_add": [], "relations_to_remove": []}'}
        with self.assertRaises(ValidationError):
            consumer._parse_debezium_message(message_no_schema)

        # Test message without payload field - should raise ValidationError
        message_no_payload = {"schema": {"type": "string"}}
        with self.assertRaises(ValidationError):
            consumer._parse_debezium_message(message_no_payload)

        # Test message with neither schema nor payload - should raise ValidationError
        invalid_message = {"some_field": "value"}
        with self.assertRaises(ValidationError):
            consumer._parse_debezium_message(invalid_message)

    @override_settings(KAFKA_ENABLED=True, RBAC_KAFKA_CONSUMER_TOPIC="test-topic")
    @patch("core.kafka_consumer.Path")
    def test_parse_debezium_message_invalid_json_payload(self, mock_path):
        """Test parsing Debezium message with invalid JSON in payload."""
        import json

        mock_path.return_value = self.liveness_file
        consumer = RBACKafkaConsumer()

        debezium_message = {"schema": {"type": "string"}, "payload": "invalid json {"}

        # Should raise JSONDecodeError for invalid JSON
        with self.assertRaises(json.JSONDecodeError):
            consumer._parse_debezium_message(debezium_message)

    @override_settings(KAFKA_ENABLED=True, RBAC_KAFKA_CONSUMER_TOPIC="test-topic")
    @patch("core.kafka_consumer.Path")
    def test_parse_debezium_message_unknown_payload_structure(self, mock_path):
        """Test parsing Debezium message with unknown payload structure."""
        from core.kafka_consumer import ValidationError

        mock_path.return_value = self.liveness_file
        consumer = RBACKafkaConsumer()

        debezium_message = {
            "schema": {"type": "string"},
            "payload": '{"unknown_field": "value"}',
        }

        # Should raise ValidationError for unknown payload structure
        with self.assertRaises(ValidationError):
            consumer._parse_debezium_message(debezium_message)


class RBACKafkaConsumerTests(TestCase):
    """Tests for RBACKafkaConsumer class."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.liveness_file = Path(self.temp_dir) / "kubernetes-liveness"
        self.readiness_file = Path(self.temp_dir) / "kubernetes-readiness"

    @override_settings(KAFKA_ENABLED=True, RBAC_KAFKA_CONSUMER_TOPIC="test-topic")
    @patch("core.kafka_consumer.Path")
    def test_init(self, mock_path):
        """Test consumer initialization."""
        mock_path.return_value = self.liveness_file

        consumer = RBACKafkaConsumer(topic="custom-topic", health_check_interval=10)

        self.assertEqual(consumer.topic, "custom-topic")
        self.assertIsNone(consumer.consumer)
        self.assertIsInstance(consumer.validator, MessageValidator)
        self.assertFalse(consumer.is_healthy)
        self.assertFalse(consumer.is_consuming)
        self.assertEqual(consumer.health_check_interval, 10)
        self.assertIsNone(consumer.health_check_thread)

    @override_settings(KAFKA_ENABLED=False)
    def test_create_consumer_kafka_disabled(self):
        """Test consumer creation fails when Kafka is disabled."""
        consumer = RBACKafkaConsumer()

        with self.assertRaises(RuntimeError) as cm:
            consumer._create_consumer()

        self.assertIn("Kafka must be enabled", str(cm.exception))

    @override_settings(KAFKA_ENABLED=True, RBAC_KAFKA_CONSUMER_TOPIC=None)
    def test_create_consumer_no_topic(self):
        """Test consumer creation fails when no topic is configured."""
        consumer = RBACKafkaConsumer(topic=None)

        with self.assertRaises(RuntimeError) as cm:
            consumer._create_consumer()

        self.assertIn("Consumer topic must be configured", str(cm.exception))

    @override_settings(
        KAFKA_ENABLED=True,
        RBAC_KAFKA_CONSUMER_TOPIC="test-topic",
        KAFKA_AUTH=None,
        KAFKA_SERVERS=["localhost:9092"],
    )
    @patch("core.kafka_consumer.KafkaConsumer")
    def test_create_consumer_success(self, mock_kafka_consumer):
        """Test successful consumer creation."""
        mock_consumer_instance = Mock()
        mock_kafka_consumer.return_value = mock_consumer_instance

        consumer = RBACKafkaConsumer()
        result = consumer._create_consumer()

        self.assertEqual(result, mock_consumer_instance)
        mock_kafka_consumer.assert_called_once()

    @patch("core.kafka_consumer.Path")
    def test_update_health_status_healthy(self, mock_path):
        """Test updating health status to healthy."""
        mock_liveness = Mock()
        mock_readiness = Mock()
        mock_path.side_effect = [mock_liveness, mock_readiness]

        consumer = RBACKafkaConsumer()
        consumer._update_health_status(True)

        self.assertTrue(consumer.is_healthy)
        mock_liveness.touch.assert_called_once()
        mock_readiness.touch.assert_called_once()

    @patch("core.kafka_consumer.Path")
    def test_update_health_status_unhealthy(self, mock_path):
        """Test updating health status to unhealthy."""
        mock_liveness = Mock()
        mock_readiness = Mock()
        mock_readiness.exists.return_value = True
        mock_path.side_effect = [mock_liveness, mock_readiness]

        consumer = RBACKafkaConsumer()
        consumer._update_health_status(False)

        self.assertFalse(consumer.is_healthy)
        mock_readiness.unlink.assert_called_once()

    def test_process_debezium_message_valid_relations(self):
        """Test processing valid relations message."""
        consumer = RBACKafkaConsumer()

        message_value = {
            "aggregatetype": "relations",
            "aggregateid": "test-id-123",
            "type": "create_group",
            "payload": {
                "relations_to_add": [
                    {
                        "resource": {"type": "rbac", "id": "group1"},
                        "subject": {"type": "rbac", "id": "user1"},
                        "relation": "member",
                    }
                ],
                "relations_to_remove": [],
            },
        }

        with patch.object(consumer, "_process_relations_message", return_value=True) as mock_process:
            result = consumer._process_debezium_message(message_value, 0, 0)

            self.assertTrue(result)
            mock_process.assert_called_once()

    def test_process_debezium_message_workspace_unknown(self):
        """Test processing workspace message without relations fields fails validation."""
        from core.kafka_consumer import ValidationError

        consumer = RBACKafkaConsumer()

        message_value = {
            "aggregatetype": "workspace",
            "aggregateid": "workspace-123",
            "type": "create_workspace",
            "payload": {
                "org_id": "12345",
                "workspace": {"id": "workspace-123", "name": "Test Workspace"},
            },
        }

        # Should raise ValidationError because payload doesn't have relations_to_add or relations_to_remove
        with self.assertRaises(ValidationError):
            consumer._process_debezium_message(message_value, 0, 0)

    def test_process_debezium_message_invalid(self):
        """Test processing invalid message."""
        from core.kafka_consumer import ValidationError

        consumer = RBACKafkaConsumer()

        message_value = {
            "aggregatetype": "invalid",
            # Missing required fields
        }

        # Should raise ValidationError for invalid message
        with self.assertRaises(ValidationError):
            consumer._process_debezium_message(message_value, 0, 0)

    @patch("core.kafka_consumer.json_format.ParseDict")
    @patch("core.kafka_consumer.relations_api_replication.write_relationships")
    @patch("core.kafka_consumer.relations_api_replication.delete_relationships")
    @patch("core.kafka_consumer.Tenant.objects.get")
    def test_process_relations_message_success(self, mock_tenant_get, mock_delete, mock_write, mock_parse_dict):
        """Test successful relations message processing."""
        # Mock tenant lookup
        mock_tenant = Mock()
        mock_tenant.org_id = "12345"
        mock_tenant_get.return_value = mock_tenant

        # Mock protobuf conversion
        mock_relationship_pb = Mock()
        mock_parse_dict.return_value = mock_relationship_pb

        # Mock API responses with consistency tokens
        mock_write_response = Mock()
        mock_write_response.consistency_token.token = "test-token-123"
        mock_write.return_value = mock_write_response

        mock_delete_response = Mock()
        mock_delete_response.consistency_token.token = "test-token-456"
        mock_delete.return_value = mock_delete_response

        consumer = RBACKafkaConsumer()

        # Set up lock token for fencing
        consumer.lock_id = "test-group/0"
        consumer.lock_token = "test-lock-token"

        debezium_msg = DebeziumMessage(
            aggregatetype="relations",
            aggregateid="test-id-123",
            event_type="create_group",
            payload={
                "relations_to_add": [
                    {
                        "resource": {"type": "rbac", "id": "group1"},
                        "subject": {"type": "rbac", "id": "user1"},
                        "relation": "member",
                    }
                ],
                "relations_to_remove": [],
                "resource_context": {
                    "resource_type": "Group",
                    "resource_id": "group1",
                    "org_id": "12345",
                    "event_type": "create_group",
                },
            },
        )

        result = consumer._process_relations_message(debezium_msg, 0, 0)

        self.assertTrue(result)
        mock_tenant_get.assert_called_once_with(org_id="12345")
        mock_write.assert_called_once()
        mock_delete.assert_called_once()

        # Verify fencing check was passed to write and delete operations
        write_call_kwargs = mock_write.call_args.kwargs
        self.assertIn("fencing_check", write_call_kwargs)
        write_fencing_check = write_call_kwargs["fencing_check"]
        self.assertIsNotNone(write_fencing_check)
        self.assertEqual(write_fencing_check.lock_id, "test-group/0")
        self.assertEqual(write_fencing_check.lock_token, "test-lock-token")

        delete_call_kwargs = mock_delete.call_args.kwargs
        self.assertIn("fencing_check", delete_call_kwargs)
        delete_fencing_check = delete_call_kwargs["fencing_check"]
        self.assertIsNotNone(delete_fencing_check)
        self.assertEqual(delete_fencing_check.lock_id, "test-group/0")
        self.assertEqual(delete_fencing_check.lock_token, "test-lock-token")

    @patch("core.kafka_consumer.REMOVE_LEGACY_ROOT_WORKSPACE_PARENT_NOTIFY_CHANNEL", "test_legacy_ch")
    @patch("core.kafka_consumer.connection.cursor")
    @patch("core.kafka_consumer.json_format.ParseDict")
    @patch("core.kafka_consumer.relations_api_replication.write_relationships")
    @patch("core.kafka_consumer.relations_api_replication.delete_relationships")
    @patch("core.kafka_consumer.Tenant.objects.get")
    def test_process_relations_message_remove_legacy_root_parent_sends_notify(
        self, mock_tenant_get, mock_delete, mock_write, mock_parse_dict, mock_conn_cursor
    ):
        """Consumer NOTIFYs after remove_root_parent_tenant_relationships batch replication."""
        from management.relation_replicator.relation_replicator import ReplicationEventType

        mock_tenant_get.return_value = Mock(org_id="12345")
        mock_parse_dict.return_value = Mock()
        mock_write.return_value = Mock(consistency_token=Mock(token="tok"))
        mock_delete.return_value = Mock(consistency_token=Mock(token=None))

        mock_cursor = Mock()
        mock_cm = Mock()
        mock_cm.__enter__ = Mock(return_value=mock_cursor)
        mock_cm.__exit__ = Mock(return_value=False)
        mock_conn_cursor.return_value = mock_cm

        consumer = RBACKafkaConsumer()
        consumer.lock_id = "test-group/0"
        consumer.lock_token = "test-lock-token"

        debezium_msg = DebeziumMessage(
            aggregatetype="relations",
            aggregateid="test-id",
            event_type=ReplicationEventType.REMOVE_ROOT_PARENT_TENANT_RELATIONSHIPS.value,
            payload={
                "relations_to_add": [],
                "relations_to_remove": [
                    {
                        "resource": {"type": "rbac", "id": "ws1"},
                        "subject": {"type": "rbac", "id": "t1"},
                        "relation": "parent",
                    }
                ],
                "resource_context": {
                    "org_id": "",
                    "event_type": ReplicationEventType.REMOVE_ROOT_PARENT_TENANT_RELATIONSHIPS.value,
                    "notify_token": "batch-ack-token",
                },
            },
        )

        self.assertTrue(consumer._process_relations_message(debezium_msg, 0, 0))

        notify_sql_calls = [c for c in mock_cursor.execute.call_args_list if "PG_NOTIFY" in str(c[0][0]).upper()]
        self.assertEqual(len(notify_sql_calls), 1)
        self.assertEqual(notify_sql_calls[0][0][1], ["test_legacy_ch", "batch-ack-token"])

    def test_process_relations_message_invalid_payload(self):
        """Test relations message processing with invalid payload raises ValidationError."""
        from core.kafka_consumer import ValidationError

        consumer = RBACKafkaConsumer()

        debezium_msg = DebeziumMessage(
            aggregatetype="relations",
            aggregateid="test-id-123",
            event_type="create_group",
            payload={
                "relations_to_add": [],
                "relations_to_remove": [],
                "resource_context": {
                    "org_id": "12345",
                    "event_type": "create_group",
                },
            },  # Both empty - invalid
        )

        # Should raise ValidationError for empty relations
        with self.assertRaises(ValidationError):
            consumer._process_relations_message(debezium_msg, 0, 0)

    @override_settings(
        KAFKA_ENABLED=True,
        RBAC_KAFKA_CONSUMER_TOPIC="test-topic",
        KAFKA_AUTH=None,
        KAFKA_SERVERS=["localhost:9092"],
    )
    @patch(
        "core.kafka_consumer.MessageValidator.validate_parsed_message",
        return_value=True,
    )
    @patch(
        "core.kafka_consumer.MessageValidator.validate_replication_message",
        return_value=True,
    )
    @patch("core.kafka_consumer.KafkaConsumer")
    @patch("core.kafka_consumer.Path")
    def test_start_consuming_success(
        self,
        mock_path,
        mock_kafka_consumer,
        mock_validate_replication,
        mock_validate_parsed,
    ):
        """Test successful message consumption."""
        from kafka import TopicPartition

        # Mock Kafka consumer
        mock_consumer_instance = Mock()
        mock_message = Mock()
        # Create proper Debezium message format
        message_dict = {
            "schema": {"type": "string"},
            "payload": {
                "relations_to_add": [
                    {
                        "resource": {"type": "rbac", "id": "group1"},
                        "subject": {"type": "rbac", "id": "user1"},
                        "relation": "member",
                    }
                ],
                "relations_to_remove": [],
            },
        }
        mock_message.value = json.dumps(message_dict).encode("utf-8")
        mock_message.partition = 0
        mock_message.offset = 123
        mock_message.topic = "test-topic"
        mock_message.leader_epoch = None
        mock_consumer_instance.__iter__ = Mock(return_value=iter([mock_message]))

        # Mock partition assignment for initial lock acquisition
        test_partition = TopicPartition("test-topic", 0)
        mock_consumer_instance.assignment.return_value = {test_partition}
        mock_consumer_instance.poll.return_value = {}
        mock_consumer_instance.config = {"group_id": "test-group"}
        mock_consumer_instance.committed.return_value = None
        # Mock partitions_for_topic to return a set of partition IDs
        mock_consumer_instance.partitions_for_topic.return_value = {0}

        mock_kafka_consumer.return_value = mock_consumer_instance

        # Mock health files
        mock_liveness = Mock()
        mock_readiness = Mock()
        mock_path.side_effect = [mock_liveness, mock_readiness]

        consumer = RBACKafkaConsumer()

        # Mock the message processing to avoid infinite loop
        with (
            patch.object(consumer, "_process_debezium_message", return_value=True) as mock_process,
            patch.object(consumer, "_acquire_lock_with_retry", return_value="test-token-123") as mock_acquire,
        ):
            # Use a side effect to break the loop after one iteration
            def side_effect(*args):
                consumer.stop_consuming()
                return True

            mock_process.side_effect = side_effect

            consumer.start_consuming()

            mock_process.assert_called_once()
            # Verify lock was acquired on startup
            mock_acquire.assert_called_once_with("test-group/0")

    @override_settings(
        KAFKA_ENABLED=True,
        RBAC_KAFKA_CUSTOM_CONSUMER_BROKER=None,
        KAFKA_AUTH=None,
        KAFKA_SERVERS=["localhost:9092"],
    )
    @patch("core.kafka_consumer.KafkaConsumer")
    def test_start_consuming_kafka_error(self, mock_kafka_consumer):
        """Test handling Kafka errors during consumption."""
        mock_kafka_consumer.side_effect = KafkaError("Connection failed")

        consumer = RBACKafkaConsumer(topic="test-topic")

        with self.assertRaises(KafkaError):
            consumer.start_consuming()

    @patch("core.kafka_consumer.Path")
    def test_stop_consuming(self, mock_path):
        """Test stopping consumer."""
        mock_consumer = Mock()
        mock_liveness = Mock()
        mock_readiness = Mock()
        mock_readiness.exists.return_value = True
        mock_path.side_effect = [mock_liveness, mock_readiness]

        consumer = RBACKafkaConsumer()
        consumer.consumer = mock_consumer

        consumer.stop_consuming()

        mock_consumer.close.assert_called_once()
        self.assertIsNone(consumer.consumer)
        self.assertFalse(consumer.is_healthy)

    @patch("core.kafka_consumer.Path")
    def test_is_ready(self, mock_path):
        """Test readiness check."""
        mock_readiness = Mock()
        mock_readiness.exists.return_value = True
        mock_path.return_value = mock_readiness

        consumer = RBACKafkaConsumer()

        result = consumer.is_ready()

        self.assertTrue(result)
        mock_readiness.exists.assert_called_once()

    @patch("core.kafka_consumer.Path")
    def test_is_alive(self, mock_path):
        """Test liveness check."""
        mock_liveness = Mock()
        mock_liveness.exists.return_value = True
        mock_path.return_value = mock_liveness

        consumer = RBACKafkaConsumer()

        result = consumer.is_alive()

        self.assertTrue(result)
        mock_liveness.exists.assert_called_once()

    @patch("core.kafka_consumer.Path")
    @patch("core.kafka_consumer.threading.Event")
    @patch("core.kafka_consumer.threading.Thread")
    def test_health_check_thread_management(self, mock_thread_class, mock_event_class, mock_path):
        """Test health check thread start and stop."""
        mock_thread = Mock()
        mock_thread_class.return_value = mock_thread
        mock_event = Mock()
        mock_event_class.return_value = mock_event
        mock_liveness = Mock()
        mock_readiness = Mock()
        mock_path.side_effect = [mock_liveness, mock_readiness]

        consumer = RBACKafkaConsumer(health_check_interval=5)

        # Test starting health check thread
        consumer._start_health_check_thread()

        mock_thread_class.assert_called_once()
        mock_thread.start.assert_called_once()
        self.assertIsNotNone(consumer.health_check_thread)

        # Test stopping health check thread
        consumer._stop_health_check_thread()

        mock_event.set.assert_called_once()
        mock_thread.join.assert_called_once_with(timeout=5)
        self.assertIsNone(consumer.health_check_thread)

    @patch("core.kafka_consumer.Path")
    @patch("core.kafka_consumer.time.time")
    def test_update_health_status_with_activity_tracking(self, mock_time, mock_path):
        """Test health status update includes activity tracking."""
        mock_time.return_value = 1234567890
        mock_liveness = Mock()
        mock_readiness = Mock()
        mock_path.side_effect = [mock_liveness, mock_readiness]

        consumer = RBACKafkaConsumer()

        consumer._update_health_status(True)

        self.assertTrue(consumer.is_healthy)
        self.assertEqual(consumer.last_activity, 1234567890)
        mock_liveness.touch.assert_called_once()
        mock_readiness.touch.assert_called_once()


class RetryConfigTests(TestCase):
    """Tests for RetryConfig class."""

    def test_default_config(self):
        """Test default retry configuration."""
        config = RetryConfig()

        self.assertEqual(config.operation_max_retries, 10)
        self.assertEqual(config.backoff_factor, 5)
        self.assertEqual(config.max_backoff_seconds, 30)
        self.assertEqual(config.base_delay, 0.3)
        self.assertEqual(config.jitter_factor, 0.1)

    def test_custom_config(self):
        """Test custom retry configuration."""
        config = RetryConfig(
            operation_max_retries=10,
            backoff_factor=3,
            max_backoff_seconds=60,
            base_delay=0.5,
            jitter_factor=0.2,
        )

        self.assertEqual(config.operation_max_retries, 10)
        self.assertEqual(config.backoff_factor, 3)
        self.assertEqual(config.max_backoff_seconds, 60)
        self.assertEqual(config.base_delay, 0.5)
        self.assertEqual(config.jitter_factor, 0.2)

    @patch("random.random")
    def test_calculate_delay_exponential_backoff(self, mock_random):
        """Test delay calculation with linear backoff.

        Formula: backoff = min(backoff_factor * attempt * base_delay, max_backoff_seconds)
        """
        mock_random.return_value = 0.5  # Fixed jitter for testing
        config = RetryConfig(
            backoff_factor=5,
            base_delay=0.3,
            max_backoff_seconds=30,
            jitter_factor=0.1,
        )

        # Test first few attempts
        # Formula: backoff_factor * (attempt+1) * base_delay + jitter
        delay_0 = config.calculate_delay(0)  # 5 * 1 * 0.3 + jitter = 1.5 + 0.075 = 1.575
        delay_1 = config.calculate_delay(1)  # 5 * 2 * 0.3 + jitter = 3.0 + 0.15 = 3.15
        delay_2 = config.calculate_delay(2)  # 5 * 3 * 0.3 + jitter = 4.5 + 0.225 = 4.725

        self.assertAlmostEqual(delay_0, 1.575, places=2)
        self.assertAlmostEqual(delay_1, 3.15, places=2)
        self.assertAlmostEqual(delay_2, 4.725, places=2)

    @patch("random.random")
    def test_calculate_delay_max_limit(self, mock_random):
        """Test delay calculation respects max limit."""
        mock_random.return_value = 0.0  # No jitter for simpler testing
        config = RetryConfig(backoff_factor=5, base_delay=0.3, max_backoff_seconds=10, jitter_factor=0.1)

        # High attempt number should be capped at max_backoff_seconds
        # Formula: backoff_factor * (attempt+1) * base_delay
        # For attempt 10: 5 * 11 * 0.3 = 16.5, but capped at 10
        delay = config.calculate_delay(10)

        self.assertLessEqual(delay, 10.0)

    def test_calculate_delay_with_jitter(self):
        """Test that jitter adds randomness to delays."""
        config = RetryConfig(backoff_factor=5, base_delay=0.3, max_backoff_seconds=100, jitter_factor=0.1)

        # Calculate delays multiple times to check they're different due to jitter
        # For attempt 1: 5 * (1+1) * 0.3 = 3.0, jitter up to 0.3
        delays = [config.calculate_delay(1) for _ in range(10)]

        # All delays should be around 3.0 but slightly different
        for delay in delays:
            self.assertGreater(delay, 3.0)
            self.assertLess(delay, 3.3)  # 3.0 + max jitter (0.3)

        # Check that we got some variation
        self.assertGreater(len(set(delays)), 5)  # Should have at least some different values


class RetryAllErrorsTests(TestCase):
    """Tests for the new 'retry all errors' policy."""

    def setUp(self):
        """Set up test fixtures."""
        from core.kafka_consumer import RetryConfig
        from kafka import TopicPartition

        # Use short operation_max_retries to avoid infinite loops in tests
        retry_config = RetryConfig(
            base_delay=0.01,
            max_backoff_seconds=1,
            operation_max_retries=2,
        )
        self.consumer = RBACKafkaConsumer(retry_config=retry_config)
        self.topic_partition = TopicPartition("test-topic", 0)

        # Mock the stop event's wait method to return False (not stopping)
        self.consumer._stop_health_check.wait = Mock(return_value=False)

    def test_should_retry_all_exceptions(self):
        """Test that ALL errors are now retried with new policy."""
        from core.kafka_consumer import RetryHelper

        json_error = json.JSONDecodeError("test", "doc", 0)
        unicode_error = UnicodeDecodeError("utf-8", b"", 0, 1, "test")
        connection_error = ConnectionError("Connection failed")
        timeout_error = TimeoutError("Request timed out")
        os_error = OSError("Network unreachable")
        validation_error = ValueError("validation failed")
        generic_value_error = ValueError("some other error")

        # Create RetryHelper to test retry logic
        retry_helper = RetryHelper(
            retry_config=self.consumer.retry_config,
            shutdown_event=self.consumer._stop_health_check,
        )

        # ALL errors should be retried with new policy
        self.assertTrue(retry_helper._should_retry(json_error))
        self.assertTrue(retry_helper._should_retry(unicode_error))
        self.assertTrue(retry_helper._should_retry(connection_error))
        self.assertTrue(retry_helper._should_retry(timeout_error))
        self.assertTrue(retry_helper._should_retry(os_error))
        self.assertTrue(retry_helper._should_retry(validation_error))
        self.assertTrue(retry_helper._should_retry(generic_value_error))

    @patch("core.kafka_consumer.logger")
    def test_process_message_with_retry_max_retries_exceeded(self, mock_logger):
        """Test that errors are retried until max_retries is hit, then raises RuntimeError."""
        # Use proper Debezium message format
        message_value = {
            "schema": {"type": "string"},
            "payload": {"relations_to_add": [], "relations_to_remove": []},
        }

        # Mock _process_debezium_message to always raise a JSON error
        with patch.object(
            self.consumer,
            "_process_debezium_message",
            side_effect=json.JSONDecodeError("test", "doc", 0),
        ):
            # Should raise RuntimeError after max retries are exceeded
            with self.assertRaises(RuntimeError) as ctx:
                self.consumer._process_message_with_retry(message_value, 1234, 0, self.topic_partition)

            # Verify the error message
            self.assertIn("Max operation retries", str(ctx.exception))

        # Should have retried operation_max_retries times (2)
        self.assertEqual(self.consumer._stop_health_check.wait.call_count, 2)
        # Should log max operation retries exceeded
        mock_logger.critical.assert_called()
        self.assertIn("CONSUMER STOPPING", str(mock_logger.critical.call_args))

    @patch("core.kafka_consumer.logger")
    def test_process_message_with_retry_processing_failure_retried(self, mock_logger):
        """Test that processing failures are retried, then raises RuntimeError when max retries exceeded."""
        # Use proper Debezium message format
        message_value = {
            "schema": {"type": "string"},
            "payload": {"relations_to_add": [], "relations_to_remove": []},
        }

        # Mock _process_debezium_message to return False (processing failed)
        with patch.object(self.consumer, "_process_debezium_message", return_value=False):
            # Should raise RuntimeError after max retries are exceeded
            with self.assertRaises(RuntimeError) as ctx:
                self.consumer._process_message_with_retry(message_value, 1234, 0, self.topic_partition)

            # Verify the error message
            self.assertIn("Max operation retries", str(ctx.exception))
            self.assertIn("Message processing failed", str(ctx.exception))

        # Should have retried operation_max_retries times (2)
        self.assertEqual(self.consumer._stop_health_check.wait.call_count, 2)
        # Should log max operation retries exceeded
        mock_logger.critical.assert_called()
        self.assertIn("CONSUMER STOPPING", str(mock_logger.critical.call_args))


class HealthCheckTests(TestCase):
    """Tests for health check functionality."""

    @override_settings(RBAC_KAFKA_CONSUMER_TOPIC="test-topic")
    def setUp(self):
        """Set up test fixtures."""
        self.consumer = RBACKafkaConsumer()

    @patch("core.kafka_consumer.threading.Thread")
    @patch("core.kafka_consumer.Path")
    def test_background_health_check_thread(self, mock_path, mock_thread):
        """Test that background health check thread is started."""
        mock_thread_instance = Mock()
        mock_thread.return_value = mock_thread_instance

        self.consumer._start_health_check_thread()

        mock_thread.assert_called_once()
        mock_thread_instance.start.assert_called_once()

    @patch.object(Path, "touch")
    def test_health_check_during_idle(self, mock_touch):
        """Test health check behavior during idle periods."""
        # Mock the health check logic
        self.consumer.is_consuming = True
        self.consumer.consumer = Mock()

        # Test that health check can run without active message processing
        self.consumer._update_health_status(True)

        # Verify health files are managed correctly - touch should be called twice (liveness + readiness)
        self.assertEqual(mock_touch.call_count, 2)


class RBACKafkaConsumerRetryTests(TestCase):
    """Tests for retry functionality in RBACKafkaConsumer."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        # Fast retries for testing
        self.retry_config = RetryConfig(
            base_delay=0.01,
            max_backoff_seconds=1,
            operation_max_retries=-1,
        )

    @patch("core.kafka_consumer.Path")
    def test_process_message_with_retry_success_after_failures(self, mock_path):
        """Test message processing succeeds after initial failures."""
        from kafka import TopicPartition

        mock_liveness = Mock()
        mock_readiness = Mock()
        mock_path.side_effect = [mock_liveness, mock_readiness]

        consumer = RBACKafkaConsumer(retry_config=self.retry_config)

        # Mock the stop event's wait method to return False (not stopping)
        consumer._stop_health_check.wait = Mock(return_value=False)

        # Mock _process_debezium_message to fail twice then succeed
        call_count = 0

        def mock_process_side_effect(*args):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                # Raise an exception that should be retried (network error)
                raise ConnectionError("Temporary network error")
            return True

        # Create a proper Debezium message format
        test_message = {
            "schema": {"type": "string"},
            "payload": {"relations_to_add": [], "relations_to_remove": []},
        }

        # Create a TopicPartition for the test
        topic_partition = TopicPartition("test-topic", 0)

        # Patch the method before calling
        original_method = consumer._process_debezium_message
        consumer._process_debezium_message = Mock(side_effect=mock_process_side_effect)

        try:
            result = consumer._process_message_with_retry(test_message, 123, 0, topic_partition)
        finally:
            # Restore original method
            consumer._process_debezium_message = original_method

        self.assertTrue(result)
        self.assertEqual(call_count, 3)  # Failed twice, succeeded on third attempt

    @patch("core.kafka_consumer.Path")
    def test_process_message_with_retry_json_error_retried(self, mock_path):
        """Test that JSON decode errors ARE now retried with new policy."""
        from kafka import TopicPartition
        from core.kafka_consumer import RetryConfig

        mock_liveness = Mock()
        mock_readiness = Mock()
        mock_path.side_effect = [mock_liveness, mock_readiness]

        # Set operation_max_retries to a small number to avoid infinite retries in test
        retry_config = RetryConfig(base_delay=0.01, max_backoff_seconds=0.1, operation_max_retries=2)
        consumer = RBACKafkaConsumer(retry_config=retry_config)

        # Mock the stop event's wait method to return False (not stopping)
        consumer._stop_health_check.wait = Mock(return_value=False)

        # Create a TopicPartition for the test
        topic_partition = TopicPartition("test-topic", 0)

        # Use valid Debezium message format
        message_value = {
            "schema": {"type": "string"},
            "payload": {"relations_to_add": [], "relations_to_remove": []},
        }

        # Mock _process_debezium_message to raise JSON decode error
        with patch.object(
            consumer,
            "_process_debezium_message",
            side_effect=json.JSONDecodeError("Invalid JSON", "", 0),
        ):
            # Should raise RuntimeError after max retries are exceeded
            with self.assertRaises(RuntimeError) as ctx:
                consumer._process_message_with_retry(message_value, 123, 0, topic_partition)

            # Verify the error message indicates max retries exceeded
            self.assertIn("Max operation retries", str(ctx.exception))
            self.assertIn("Invalid JSON", str(ctx.exception))

        # Should have waited (called wait max_retries times during retry)
        self.assertEqual(consumer._stop_health_check.wait.call_count, 2)

    @patch("core.kafka_consumer.Path")
    def test_consumer_init_with_retry_config(self, mock_path):
        """Test consumer initialization with custom retry config."""
        mock_liveness = Mock()
        mock_readiness = Mock()
        mock_path.side_effect = [mock_liveness, mock_readiness]

        custom_config = RetryConfig(
            base_delay=0.5,
            max_backoff_seconds=60,
            operation_max_retries=5,
        )
        consumer = RBACKafkaConsumer(retry_config=custom_config)

        self.assertEqual(consumer.retry_config.base_delay, 0.5)
        self.assertEqual(consumer.retry_config.max_backoff_seconds, 60)
        self.assertEqual(consumer.retry_config.operation_max_retries, 5)

    @patch("core.kafka_consumer.Path")
    def test_consumer_init_with_default_retry_config(self, mock_path):
        """Test consumer initialization with default retry config."""
        mock_liveness = Mock()
        mock_readiness = Mock()
        mock_path.side_effect = [mock_liveness, mock_readiness]

        consumer = RBACKafkaConsumer()

        self.assertEqual(consumer.retry_config.base_delay, 0.3)
        self.assertEqual(consumer.retry_config.max_backoff_seconds, 30)
        self.assertEqual(consumer.retry_config.operation_max_retries, 10)


class MockRpcError(grpc.RpcError, Exception):
    """Mock grpc.RpcError for testing.

    This class mimics the behavior of grpc.RpcError for unit tests,
    allowing proper exception handling without requiring a real gRPC connection.
    """

    def __init__(self, code_value, details_text="Mock error"):
        """Initialize mock RPC error.

        Args:
            code_value: The gRPC status code (e.g., grpc.StatusCode.UNAVAILABLE)
            details_text: Human-readable error details
        """
        super().__init__(details_text)
        self._code = code_value
        self._details = details_text

    def code(self):
        """Return the gRPC status code."""
        return self._code

    def details(self):
        """Return the error details string."""
        return self._details


def create_mock_grpc_error(code, details="Mock error"):
    """Create a proper mock grpc.RpcError for testing.

    Args:
        code: The gRPC status code
        details: Human-readable error message

    Returns:
        MockRpcError instance
    """
    return MockRpcError(code, details)


class FencingTokenTests(TestCase):
    """Tests for fencing token implementation."""

    def setUp(self):
        """Set up test fixtures."""
        self.consumer = RBACKafkaConsumer()
        self.consumer.consumer = Mock()
        self.consumer.consumer.config = {"group_id": "test-consumer-group"}

    @patch("core.kafka_consumer.relations_api_replication.acquire_lock")
    def test_acquire_lock_success(self, mock_acquire_lock):
        """Test successful lock acquisition."""
        mock_acquire_lock.return_value = "test-token-12345"

        lock_token = self.consumer._acquire_lock("test-group/0")

        self.assertEqual(lock_token, "test-token-12345")
        mock_acquire_lock.assert_called_once_with("test-group/0")

    @patch("core.kafka_consumer.relations_api_replication.acquire_lock")
    def test_acquire_lock_failure(self, mock_acquire_lock):
        """Test lock acquisition failure."""
        mock_error = create_mock_grpc_error(grpc.StatusCode.UNAVAILABLE, "Service unavailable")
        mock_acquire_lock.side_effect = mock_error

        with self.assertRaises(grpc.RpcError):
            self.consumer._acquire_lock("test-group/0")

    @patch("core.kafka_consumer.RBACKafkaConsumer._acquire_lock")
    @patch("time.sleep")
    def test_acquire_lock_with_retry_success_on_first_attempt(self, mock_sleep, mock_acquire):
        """Test lock acquisition succeeds on first try."""
        mock_acquire.return_value = "test-token-12345"

        lock_token = self.consumer._acquire_lock_with_retry("test-group/0")

        self.assertEqual(lock_token, "test-token-12345")
        mock_acquire.assert_called_once()
        mock_sleep.assert_not_called()

    @patch("core.kafka_consumer.RBACKafkaConsumer._acquire_lock")
    @patch("time.sleep")
    def test_acquire_lock_with_retry_success_after_retries(self, mock_sleep, mock_acquire):
        """Test lock acquisition succeeds after retries."""
        # Fail twice, then succeed
        mock_acquire.side_effect = [
            create_mock_grpc_error(grpc.StatusCode.UNAVAILABLE),
            create_mock_grpc_error(grpc.StatusCode.UNAVAILABLE),
            "test-token-12345",
        ]

        lock_token = self.consumer._acquire_lock_with_retry("test-group/0", max_retries=3)

        self.assertEqual(lock_token, "test-token-12345")
        self.assertEqual(mock_acquire.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("core.kafka_consumer.RBACKafkaConsumer._acquire_lock")
    @patch("time.sleep")
    def test_acquire_lock_with_retry_max_retries_exceeded(self, mock_sleep, mock_acquire):
        """Test lock acquisition fails after max retries."""
        mock_acquire.side_effect = create_mock_grpc_error(grpc.StatusCode.UNAVAILABLE)

        with self.assertRaises(RuntimeError) as ctx:
            self.consumer._acquire_lock_with_retry("test-group/0", max_retries=3)

        self.assertIn("Failed to acquire lock after 3 attempts", str(ctx.exception))
        self.assertEqual(mock_acquire.call_count, 3)


class FencingTokenRebalanceTests(TestCase):
    """Tests for fencing token in rebalance callbacks."""

    def setUp(self):
        """Set up test fixtures."""
        from kafka import TopicPartition

        self.consumer = RBACKafkaConsumer()
        self.consumer.consumer = Mock()
        self.consumer.consumer.config = {"group_id": "test-consumer-group"}
        self.consumer.consumer.assignment.return_value = set()
        self.consumer.offset_manager = Mock()

        self.rebalance_listener = Mock()
        self.rebalance_listener.consumer_instance = self.consumer

        self.partition = TopicPartition("test-topic", 0)

    @patch("core.kafka_consumer.RBACKafkaConsumer._acquire_lock_with_retry")
    def test_partition_assignment_acquires_lock(self, mock_acquire_lock):
        """Test that partition assignment acquires lock token and resets failure flag."""
        from core.kafka_consumer import RebalanceListener

        mock_acquire_lock.return_value = "test-token-12345"

        # Set failure flag to True to verify it gets reset
        self.consumer.lock_acquisition_failed = True

        listener = RebalanceListener(self.consumer)
        # Note: Kafka library passes a set of TopicPartition objects, not a list
        listener.on_partitions_assigned({self.partition})

        # Verify lock was acquired
        mock_acquire_lock.assert_called_once_with("test-consumer-group/0")

        # Verify lock token was stored
        self.assertEqual(self.consumer.lock_id, "test-consumer-group/0")
        self.assertEqual(self.consumer.lock_token, "test-token-12345")

        # Verify failure flag was reset
        self.assertFalse(self.consumer.lock_acquisition_failed)

    @patch("core.kafka_consumer.RBACKafkaConsumer._acquire_lock_with_retry")
    def test_partition_assignment_lock_failure(self, mock_acquire_lock):
        """Test that partition assignment failure clears lock state and sets failure flag."""
        from core.kafka_consumer import RebalanceListener

        mock_acquire_lock.side_effect = RuntimeError("Lock acquisition failed")

        listener = RebalanceListener(self.consumer)

        # Should NOT raise - instead sets a flag for later detection
        # Note: Kafka library passes a set of TopicPartition objects, not a list
        listener.on_partitions_assigned({self.partition})

        # Verify lock state was cleared
        self.assertIsNone(self.consumer.lock_id)
        self.assertIsNone(self.consumer.lock_token)

        # Verify failure flag was set
        self.assertTrue(self.consumer.lock_acquisition_failed)

    @patch("core.kafka_consumer.RBACKafkaConsumer._acquire_lock_with_retry")
    def test_partition_assignment_handles_set_not_list(self, mock_acquire_lock):
        """Test that on_partitions_assigned handles set of TopicPartition (not list).

        Regression test: The Kafka library's ConsumerRebalanceListener.on_partitions_assigned()
        callback receives a SET of TopicPartition objects, not a list. Previously the code
        tried to access assigned[0] which failed with "'set' object is not subscriptable".
        """
        from core.kafka_consumer import RebalanceListener

        mock_acquire_lock.return_value = "test-token-12345"

        listener = RebalanceListener(self.consumer)

        # Kafka library passes a SET, not a list - this must not raise TypeError
        assigned_partitions = {self.partition}
        self.assertIsInstance(assigned_partitions, set)

        # This should not raise "'set' object is not subscriptable"
        listener.on_partitions_assigned(assigned_partitions)

        # Verify lock was acquired successfully
        mock_acquire_lock.assert_called_once_with("test-consumer-group/0")
        self.assertEqual(self.consumer.lock_token, "test-token-12345")

    @patch("core.kafka_consumer.RBACKafkaConsumer._acquire_lock_with_retry")
    def test_startup_lock_acquisition_resets_failure_flag(self, mock_acquire_lock):
        """Test that startup lock acquisition sets lock fields and resets failure flag."""
        from kafka import TopicPartition

        # Arrange: simulate previous lock acquisition failure
        mock_acquire_lock.return_value = "startup-token-12345"
        self.consumer.lock_acquisition_failed = True

        # Setup consumer assignment to return a partition
        partition = TopicPartition("test-topic", 0)
        self.consumer.consumer.assignment.return_value = {partition}

        # Act: invoke the startup lock acquisition path
        result = self.consumer._ensure_lock_token_on_assignment()

        # Assert: lock fields are populated and failure flag is reset
        self.assertTrue(result)
        self.assertFalse(self.consumer.lock_acquisition_failed)
        self.assertEqual(self.consumer.lock_id, "test-consumer-group/0")
        self.assertEqual(self.consumer.lock_token, "startup-token-12345")
        mock_acquire_lock.assert_called_once_with("test-consumer-group/0")

    def test_message_processing_fails_fast_when_lock_acquisition_failed(self):
        """Test that message processing fails fast when lock_acquisition_failed is set.

        When lock_acquisition_failed is set, the message-processing loop should fail fast:
        - it must raise RuntimeError immediately
        - it must not process any messages
        - it must not commit any offsets
        """
        from unittest.mock import MagicMock

        # Arrange: Construct consumer in a "failed" state
        self.consumer.lock_acquisition_failed = True

        # Create a mock message
        mock_message = MagicMock()
        mock_message.partition = 0
        mock_message.offset = 100

        # Mock the consumer to return one message then stop
        self.consumer.consumer.__iter__ = MagicMock(return_value=iter([mock_message]))

        # Mock the offset manager commit method to track if it was called
        self.consumer.offset_manager.commit = MagicMock(return_value=(True, 0))

        # Act & Assert: The message loop should raise RuntimeError immediately
        with self.assertRaises(RuntimeError) as ctx:
            self.consumer._run_message_loop()

        # Verify the error message
        self.assertIn("Lock acquisition failed during rebalance", str(ctx.exception))
        self.assertIn("Cannot process messages without fencing token", str(ctx.exception))

        # Verify no offsets were committed (commit should not have been called)
        self.consumer.offset_manager.commit.assert_not_called()

    def test_partition_revocation_clears_lock(self):
        """Test that partition revocation clears lock token."""
        # Set up lock state
        self.consumer.lock_id = "test-consumer-group/0"
        self.consumer.lock_token = "test-token-12345"
        self.consumer.offset_manager.commit.return_value = (True, 5)  # Success, 5 offsets committed

        # Revoke partitions
        self.consumer._on_partitions_revoked([self.partition])

        # Verify lock was cleared
        self.assertIsNone(self.consumer.lock_id)
        self.assertIsNone(self.consumer.lock_token)

        # Verify offsets were committed
        self.consumer.offset_manager.commit.assert_called_once()


class FencingTokenProcessingTests(TestCase):
    """Tests for fencing token in message processing."""

    def setUp(self):
        """Set up test fixtures."""
        self.consumer = RBACKafkaConsumer()
        self.consumer.lock_id = "test-group/0"
        self.consumer.lock_token = "test-token-12345"

        self.payload = {
            "relations_to_add": [
                {
                    "resource": {
                        "type": {"namespace": "rbac", "name": "workspace"},
                        "id": "123",
                    },
                    "relation": "member",
                    "subject": {
                        "subject": {
                            "type": {"namespace": "rbac", "name": "user"},
                            "id": "456",
                        }
                    },
                }
            ],
            "relations_to_remove": [],
        }

    @patch("core.kafka_consumer.relations_api_replication.write_relationships")
    @patch("core.kafka_consumer.relations_api_replication.delete_relationships")
    def test_fencing_check_included_in_api_calls(self, mock_delete, mock_write):
        """Test that fencing check is included in Relations API calls."""
        from core.kafka_consumer import DebeziumMessage

        # Mock responses
        mock_write_response = Mock()
        mock_write_response.consistency_token.token = "consistency-token-123"
        mock_write.return_value = mock_write_response

        mock_delete_response = Mock()
        mock_delete_response.consistency_token.token = None
        mock_delete.return_value = mock_delete_response

        # Process message
        debezium_msg = DebeziumMessage(
            aggregatetype="relations",
            aggregateid="test-123",
            event_type="test",
            payload=self.payload,
        )

        result = self.consumer._process_relations_message(debezium_msg, 0, 0)

        self.assertTrue(result)

        # Verify fencing check was passed
        write_call_kwargs = mock_write.call_args.kwargs
        self.assertIn("fencing_check", write_call_kwargs)
        fencing_check = write_call_kwargs["fencing_check"]
        self.assertIsNotNone(fencing_check)
        self.assertEqual(fencing_check.lock_id, "test-group/0")
        self.assertEqual(fencing_check.lock_token, "test-token-12345")

    @patch("core.kafka_consumer.relations_api_replication.write_relationships")
    @patch("core.kafka_consumer.relations_api_replication.delete_relationships")
    def test_no_fencing_check_when_no_lock_token(self, mock_delete, mock_write):
        """Test that processing fails when lock token is not available."""
        from core.kafka_consumer import DebeziumMessage

        # Clear lock token
        self.consumer.lock_id = None
        self.consumer.lock_token = None

        # Process message
        debezium_msg = DebeziumMessage(
            aggregatetype="relations",
            aggregateid="test-123",
            event_type="test",
            payload=self.payload,
        )

        # Verify that processing raises RuntimeError when no lock token is available
        with self.assertRaises(RuntimeError) as ctx:
            self.consumer._process_relations_message(debezium_msg, 0, 0)

        # Verify error message
        self.assertIn("Lock token not available", str(ctx.exception))

        # Verify that write/delete were never called without a lock token
        mock_write.assert_not_called()
        mock_delete.assert_not_called()


class EnsureLockTokenOnAssignmentTests(TestCase):
    """Tests for _ensure_lock_token_on_assignment fallback behavior."""

    @override_settings(
        KAFKA_ENABLED=True,
        RBAC_KAFKA_CONSUMER_TOPIC="test-topic",
        KAFKA_AUTH=None,
        KAFKA_SERVERS=["localhost:9092"],
    )
    @patch("core.kafka_consumer.KafkaConsumer")
    @patch("core.kafka_consumer.RBACKafkaConsumer._acquire_lock_with_retry")
    def test_ensure_lock_token_acquires_on_first_message(self, mock_acquire_lock, mock_kafka_consumer):
        """Test that _ensure_lock_token_on_assignment acquires lock on first message."""
        from kafka import TopicPartition

        # Mock consumer creation
        mock_consumer_instance = Mock()
        mock_kafka_consumer.return_value = mock_consumer_instance
        mock_consumer_instance.config = {"group_id": "test-group"}

        # Simulate partition assignment without callback firing
        test_partition = TopicPartition("test-topic", 0)
        mock_consumer_instance.assignment.return_value = {test_partition}

        # Mock lock acquisition
        mock_acquire_lock.return_value = "test-token-12345"

        consumer = RBACKafkaConsumer()
        consumer.consumer = mock_consumer_instance

        # Ensure no lock token initially
        self.assertIsNone(consumer.lock_token)
        self.assertIsNone(consumer.lock_id)

        # Call _ensure_lock_token_on_assignment
        result = consumer._ensure_lock_token_on_assignment()

        # Verify lock was acquired
        self.assertTrue(result)
        mock_acquire_lock.assert_called_once_with("test-group/0")
        self.assertEqual(consumer.lock_token, "test-token-12345")
        self.assertEqual(consumer.lock_id, "test-group/0")

    @override_settings(
        KAFKA_ENABLED=True,
        RBAC_KAFKA_CONSUMER_TOPIC="test-topic",
        KAFKA_AUTH=None,
        KAFKA_SERVERS=["localhost:9092"],
    )
    @patch("core.kafka_consumer.KafkaConsumer")
    @patch("core.kafka_consumer.RBACKafkaConsumer._acquire_lock_with_retry")
    def test_ensure_lock_token_failure_raises_runtime_error(self, mock_acquire_lock, mock_kafka_consumer):
        """Test that _acquire_lock_with_retry failure raises RuntimeError."""
        from kafka import TopicPartition

        # Mock consumer creation
        mock_consumer_instance = Mock()
        mock_kafka_consumer.return_value = mock_consumer_instance
        mock_consumer_instance.config = {"group_id": "test-group"}

        # Simulate partition assignment
        test_partition = TopicPartition("test-topic", 0)
        mock_consumer_instance.assignment.return_value = {test_partition}

        # Mock lock acquisition failure
        mock_acquire_lock.side_effect = Exception("Lock service unavailable")

        consumer = RBACKafkaConsumer()
        consumer.consumer = mock_consumer_instance

        # Call _ensure_lock_token_on_assignment and expect RuntimeError
        with self.assertRaises(RuntimeError) as ctx:
            consumer._ensure_lock_token_on_assignment()

        # Verify error message
        self.assertIn("Failed to acquire lock token for partition 0", str(ctx.exception))

        # Verify lock token was not set
        self.assertIsNone(consumer.lock_token)
        self.assertIsNone(consumer.lock_id)

    @override_settings(
        KAFKA_ENABLED=True,
        RBAC_KAFKA_CONSUMER_TOPIC="test-topic",
        KAFKA_AUTH=None,
        KAFKA_SERVERS=["localhost:9092"],
    )
    @patch("core.kafka_consumer.KafkaConsumer")
    def test_ensure_lock_token_validates_existing_token(self, mock_kafka_consumer):
        """Test that existing token is validated against current assignment."""
        from kafka import TopicPartition

        # Mock consumer creation
        mock_consumer_instance = Mock()
        mock_kafka_consumer.return_value = mock_consumer_instance
        mock_consumer_instance.config = {"group_id": "test-group"}

        # Simulate partition assignment
        test_partition = TopicPartition("test-topic", 0)
        mock_consumer_instance.assignment.return_value = {test_partition}

        consumer = RBACKafkaConsumer()
        consumer.consumer = mock_consumer_instance

        # Set existing token for correct partition
        consumer.lock_id = "test-group/0"
        consumer.lock_token = "existing-token"

        # Call _ensure_lock_token_on_assignment
        result = consumer._ensure_lock_token_on_assignment()

        # Verify existing token was validated and kept
        self.assertTrue(result)
        self.assertEqual(consumer.lock_token, "existing-token")
        self.assertEqual(consumer.lock_id, "test-group/0")

    @override_settings(
        KAFKA_ENABLED=True,
        RBAC_KAFKA_CONSUMER_TOPIC="test-topic",
        KAFKA_AUTH=None,
        KAFKA_SERVERS=["localhost:9092"],
    )
    @patch("core.kafka_consumer.KafkaConsumer")
    @patch("core.kafka_consumer.RBACKafkaConsumer._acquire_lock_with_retry")
    def test_ensure_lock_token_clears_stale_token(self, mock_acquire_lock, mock_kafka_consumer):
        """Test that stale token for wrong partition is cleared and reacquired."""
        from kafka import TopicPartition

        # Mock consumer creation
        mock_consumer_instance = Mock()
        mock_kafka_consumer.return_value = mock_consumer_instance
        mock_consumer_instance.config = {"group_id": "test-group"}

        # Simulate partition assignment to partition 1
        test_partition = TopicPartition("test-topic", 1)
        mock_consumer_instance.assignment.return_value = {test_partition}

        # Mock lock acquisition
        mock_acquire_lock.return_value = "new-token-67890"

        consumer = RBACKafkaConsumer()
        consumer.consumer = mock_consumer_instance

        # Set stale token for partition 0 (but now assigned to partition 1)
        consumer.lock_id = "test-group/0"
        consumer.lock_token = "stale-token"

        # Call _ensure_lock_token_on_assignment
        result = consumer._ensure_lock_token_on_assignment()

        # Verify stale token was cleared and new token acquired
        self.assertTrue(result)
        mock_acquire_lock.assert_called_once_with("test-group/1")
        self.assertEqual(consumer.lock_token, "new-token-67890")
        self.assertEqual(consumer.lock_id, "test-group/1")


class FencingTokenErrorHandlingTests(TestCase):
    """Tests for fencing token error handling."""

    def setUp(self):
        """Set up test fixtures."""
        self.consumer = RBACKafkaConsumer()
        self.consumer.lock_id = "test-group/0"
        self.consumer.lock_token = "test-token-12345"

        self.payload = {
            "relations_to_add": [
                {
                    "resource": {
                        "type": {"namespace": "rbac", "name": "workspace"},
                        "id": "123",
                    },
                    "relation": "member",
                    "subject": {
                        "subject": {
                            "type": {"namespace": "rbac", "name": "user"},
                            "id": "456",
                        }
                    },
                }
            ],
            "relations_to_remove": [],
        }

    @patch("core.kafka_consumer.relations_api_replication.write_relationships")
    @patch("core.kafka_consumer.relations_api_replication.delete_relationships")
    def test_failed_precondition_raises_runtime_error(self, mock_delete, mock_write):
        """Test that FAILED_PRECONDITION error raises RuntimeError."""
        from core.kafka_consumer import DebeziumMessage

        # Mock delete to return success
        mock_delete_response = Mock()
        mock_delete_response.consistency_token.token = None
        mock_delete.return_value = mock_delete_response

        # Mock write to raise FAILED_PRECONDITION
        mock_write.side_effect = create_mock_grpc_error(grpc.StatusCode.FAILED_PRECONDITION, "Invalid fencing token")

        # Process message
        debezium_msg = DebeziumMessage(
            aggregatetype="relations",
            aggregateid="test-123",
            event_type="test",
            payload=self.payload,
        )

        with self.assertRaises(RuntimeError) as ctx:
            self.consumer._process_relations_message(debezium_msg, 0, 0)

        self.assertIn("Fencing token validation failed", str(ctx.exception))

    @patch("core.kafka_consumer.relations_api_replication.write_relationships")
    @patch("core.kafka_consumer.relations_api_replication.delete_relationships")
    def test_other_grpc_errors_are_reraised(self, mock_delete, mock_write):
        """Test that other gRPC errors are re-raised for retry."""
        from core.kafka_consumer import DebeziumMessage

        # Mock delete to return success
        mock_delete_response = Mock()
        mock_delete_response.consistency_token.token = None
        mock_delete.return_value = mock_delete_response

        # Mock write to raise UNAVAILABLE (retriable error)
        mock_write.side_effect = create_mock_grpc_error(grpc.StatusCode.UNAVAILABLE, "Service unavailable")

        # Process message
        debezium_msg = DebeziumMessage(
            aggregatetype="relations",
            aggregateid="test-123",
            event_type="test",
            payload=self.payload,
        )

        # Should re-raise the gRPC error (not RuntimeError)
        with self.assertRaises(grpc.RpcError):
            self.consumer._process_relations_message(debezium_msg, 0, 0)
