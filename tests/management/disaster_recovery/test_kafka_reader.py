"""Tests for disaster recovery Kafka reader."""

import json
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from management.disaster_recovery.kafka_reader import (
    DisasterRecoveryError,
    ParsedReplicationEvent,
    _parse_debezium_payload,
    _parse_event,
    read_events_in_window,
)


def _make_debezium_message(relations_to_add=None, relations_to_remove=None, event_type="create_workspace"):
    """Build a raw Debezium envelope as bytes."""
    inner = {
        "aggregatetype": "relations-replication-event",
        "aggregateid": "test-env",
        "type": event_type,
        "payload": {
            "relations_to_add": relations_to_add or [],
            "relations_to_remove": relations_to_remove or [],
            "resource_context": {"org_id": "test-org", "event_type": event_type},
        },
    }
    return json.dumps({"schema": {}, "payload": json.dumps(inner)}).encode()


def _make_relation_dict(resource_type="workspace", resource_id="ws-123", relation="parent"):
    return {
        "resource": {"type": {"namespace": "rbac", "name": resource_type}, "id": resource_id},
        "relation": relation,
        "subject": {
            "subject": {"type": {"namespace": "rbac", "name": "workspace"}, "id": "ws-parent"},
        },
    }


def _make_kafka_message(value_bytes, offset=0, partition=0, timestamp=1000000):
    msg = MagicMock()
    msg.value = value_bytes
    msg.offset = offset
    msg.partition = partition
    msg.timestamp = timestamp
    return msg


class ParseDebeziumPayloadTest(TestCase):
    def test_standard_envelope(self):
        inner = {"aggregatetype": "test", "payload": {"relations_to_add": []}}
        raw = json.dumps({"schema": {}, "payload": json.dumps(inner)}).encode()
        result = _parse_debezium_payload(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result["aggregatetype"], "test")

    def test_dict_payload(self):
        inner = {"aggregatetype": "test", "payload": {"relations_to_add": []}}
        raw = json.dumps({"schema": {}, "payload": inner}).encode()
        result = _parse_debezium_payload(raw)
        self.assertIsNotNone(result)

    def test_invalid_json(self):
        result = _parse_debezium_payload(b"not json")
        self.assertIsNone(result)

    def test_missing_payload(self):
        result = _parse_debezium_payload(json.dumps({"schema": {}}).encode())
        self.assertIsNone(result)


class ParseEventTest(TestCase):
    def test_parses_relations_to_add(self):
        rel = _make_relation_dict()
        payload = {
            "type": "create_workspace",
            "payload": {
                "relations_to_add": [rel],
                "relations_to_remove": [],
                "resource_context": {"event_type": "create_workspace"},
            },
        }
        event = _parse_event(payload, offset=5, partition=0, timestamp_ms=1000)
        self.assertEqual(len(event.relations_to_add), 1)
        self.assertEqual(event.relations_to_add[0].resource.type.name, "workspace")
        self.assertEqual(event.relations_to_add[0].resource.id, "ws-123")
        self.assertEqual(event.offset, 5)

    def test_parses_relations_to_remove(self):
        rel = _make_relation_dict()
        payload = {
            "type": "delete_workspace",
            "payload": {
                "relations_to_add": [],
                "relations_to_remove": [rel],
            },
        }
        event = _parse_event(payload, offset=10, partition=1, timestamp_ms=2000)
        self.assertEqual(len(event.relations_to_remove), 1)

    def test_skips_invalid_relation(self):
        payload = {
            "type": "test",
            "payload": {
                "relations_to_add": [{"invalid": "data"}],
                "relations_to_remove": [],
            },
        }
        event = _parse_event(payload, offset=0, partition=0, timestamp_ms=0)
        self.assertEqual(len(event.relations_to_add), 0)


class ReadEventsInWindowTest(TestCase):
    @override_settings(KAFKA_ENABLED=False)
    def test_kafka_disabled_raises(self):
        with self.assertRaises(DisasterRecoveryError) as ctx:
            read_events_in_window(1000, 2000)
        self.assertIn("not enabled", str(ctx.exception))

    @override_settings(KAFKA_ENABLED=True, RBAC_KAFKA_CONSUMER_TOPIC=None)
    def test_no_topic_raises(self):
        with self.assertRaises(DisasterRecoveryError) as ctx:
            read_events_in_window(1000, 2000)
        self.assertIn("topic", str(ctx.exception))

    @override_settings(
        KAFKA_ENABLED=True,
        RBAC_KAFKA_CONSUMER_TOPIC="test-topic",
        KAFKA_SERVERS=["localhost:9092"],
        KAFKA_AUTH={},
        DR_KAFKA_CONSUMER_GROUP_ID="test-dr-group",
        DR_MAX_EVENTS_PER_RECONCILE=100,
    )
    @patch("management.disaster_recovery.kafka_reader.KafkaConsumer")
    def test_reads_events_within_window(self, mock_consumer_cls):
        consumer = MagicMock()
        mock_consumer_cls.return_value = consumer

        consumer.partitions_for_topic.return_value = {0}

        from kafka import TopicPartition

        tp = TopicPartition("test-topic", 0)

        offset_result = MagicMock()
        offset_result.offset = 0
        consumer.offsets_for_times.return_value = {tp: offset_result}

        rel = _make_relation_dict()
        msg_in_window = _make_kafka_message(
            _make_debezium_message(relations_to_add=[rel]),
            offset=0,
            partition=0,
            timestamp=1500,
        )
        msg_past_window = _make_kafka_message(
            _make_debezium_message(relations_to_add=[rel]),
            offset=1,
            partition=0,
            timestamp=2500,
        )

        consumer.poll.side_effect = [
            {tp: [msg_in_window, msg_past_window]},
            {},
            {},
            {},
        ]

        events = read_events_in_window(1000, 2000)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].timestamp_ms, 1500)
        self.assertEqual(len(events[0].relations_to_add), 1)
        consumer.close.assert_called_once()

    @override_settings(
        KAFKA_ENABLED=True,
        RBAC_KAFKA_CONSUMER_TOPIC="test-topic",
        KAFKA_SERVERS=["localhost:9092"],
        KAFKA_AUTH={},
        DR_KAFKA_CONSUMER_GROUP_ID="test-dr-group",
        DR_MAX_EVENTS_PER_RECONCILE=100,
    )
    @patch("management.disaster_recovery.kafka_reader.KafkaConsumer")
    def test_empty_window_no_offsets(self, mock_consumer_cls):
        consumer = MagicMock()
        mock_consumer_cls.return_value = consumer
        consumer.partitions_for_topic.return_value = {0}

        from kafka import TopicPartition

        tp = TopicPartition("test-topic", 0)
        consumer.offsets_for_times.return_value = {tp: None}

        events = read_events_in_window(1000, 2000)
        self.assertEqual(events, [])
        consumer.poll.assert_not_called()

    @override_settings(
        KAFKA_ENABLED=True,
        RBAC_KAFKA_CONSUMER_TOPIC="test-topic",
        KAFKA_SERVERS=["localhost:9092"],
        KAFKA_AUTH={},
        DR_KAFKA_CONSUMER_GROUP_ID="test-dr-group",
        DR_MAX_EVENTS_PER_RECONCILE=100,
    )
    @patch("management.disaster_recovery.kafka_reader.KafkaConsumer")
    def test_empty_offsets_dict(self, mock_consumer_cls):
        consumer = MagicMock()
        mock_consumer_cls.return_value = consumer
        consumer.partitions_for_topic.return_value = {0}
        consumer.offsets_for_times.return_value = {}

        events = read_events_in_window(1000, 2000)
        self.assertEqual(events, [])
        consumer.poll.assert_not_called()

    @override_settings(
        KAFKA_ENABLED=True,
        RBAC_KAFKA_CONSUMER_TOPIC="test-topic",
        KAFKA_SERVERS=["localhost:9092"],
        KAFKA_AUTH={},
        DR_KAFKA_CONSUMER_GROUP_ID="test-dr-group",
        DR_MAX_EVENTS_PER_RECONCILE=2,
    )
    @patch("management.disaster_recovery.kafka_reader.KafkaConsumer")
    def test_max_events_cap(self, mock_consumer_cls):
        consumer = MagicMock()
        mock_consumer_cls.return_value = consumer
        consumer.partitions_for_topic.return_value = {0}

        from kafka import TopicPartition

        tp = TopicPartition("test-topic", 0)
        offset_result = MagicMock()
        offset_result.offset = 0
        consumer.offsets_for_times.return_value = {tp: offset_result}

        rel = _make_relation_dict()
        messages = [
            _make_kafka_message(_make_debezium_message(relations_to_add=[rel]), offset=i, timestamp=1000 + i)
            for i in range(5)
        ]
        consumer.poll.side_effect = [{tp: messages}, {}, {}, {}]

        events = read_events_in_window(900, 2000)
        self.assertEqual(len(events), 2)

    @override_settings(
        KAFKA_ENABLED=True,
        RBAC_KAFKA_CONSUMER_TOPIC="test-topic",
        KAFKA_SERVERS=["localhost:9092"],
        KAFKA_AUTH={},
        DR_KAFKA_CONSUMER_GROUP_ID="test-dr-group",
        DR_MAX_EVENTS_PER_RECONCILE=100,
    )
    @patch("management.disaster_recovery.kafka_reader.KafkaConsumer")
    def test_no_partitions(self, mock_consumer_cls):
        consumer = MagicMock()
        mock_consumer_cls.return_value = consumer
        consumer.partitions_for_topic.return_value = set()

        events = read_events_in_window(1000, 2000)
        self.assertEqual(events, [])
