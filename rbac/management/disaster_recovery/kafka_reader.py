"""Read replication events from Kafka within a time window for disaster recovery."""

import json
import logging
from dataclasses import dataclass, field

from django.conf import settings
from kafka import KafkaConsumer, TopicPartition
from management.relation_replicator.types import RelationTuple

logger = logging.getLogger(__name__)


class DisasterRecoveryError(Exception):
    pass


@dataclass
class ParsedReplicationEvent:
    """A single replication event parsed from a Kafka message."""

    offset: int
    partition: int
    timestamp_ms: int
    event_type: str
    relations_to_add: list[RelationTuple] = field(default_factory=list)
    relations_to_remove: list[RelationTuple] = field(default_factory=list)


def _parse_debezium_payload(raw_value: bytes) -> dict | None:
    """Parse Debezium envelope and return the inner payload dict."""
    try:
        message = json.loads(raw_value)
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning("Failed to decode Kafka message JSON: %s", e)
        return None

    payload_field = message.get("payload")
    if payload_field is None:
        logger.warning("Kafka message missing 'payload' field")
        return None

    if isinstance(payload_field, str):
        try:
            payload_data = json.loads(payload_field)
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse Debezium payload string: %s", e)
            return None
    elif isinstance(payload_data := payload_field, dict):
        pass
    else:
        logger.warning("Unexpected payload type: %s", type(payload_field))
        return None

    return payload_data


def _parse_event(payload: dict, offset: int, partition: int, timestamp_ms: int) -> ParsedReplicationEvent:
    """Parse a Debezium payload into a ParsedReplicationEvent."""
    inner_payload = payload.get("payload", payload)

    relations_to_add = []
    for rel_dict in inner_payload.get("relations_to_add", []):
        try:
            relations_to_add.append(RelationTuple.from_message_dict(rel_dict))
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("Failed to parse relation_to_add at offset=%d: %s", offset, e)

    relations_to_remove = []
    for rel_dict in inner_payload.get("relations_to_remove", []):
        try:
            relations_to_remove.append(RelationTuple.from_message_dict(rel_dict))
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("Failed to parse relation_to_remove at offset=%d: %s", offset, e)

    resource_context = inner_payload.get("resource_context", {})
    event_type = payload.get("type", resource_context.get("event_type", "unknown"))

    return ParsedReplicationEvent(
        offset=offset,
        partition=partition,
        timestamp_ms=timestamp_ms,
        event_type=event_type,
        relations_to_add=relations_to_add,
        relations_to_remove=relations_to_remove,
    )


def read_events_in_window(
    start_timestamp_ms: int,
    end_timestamp_ms: int,
    topic: str | None = None,
) -> list[ParsedReplicationEvent]:
    """Read replication events from Kafka within a time window.

    Creates a temporary consumer with a DR-specific consumer group,
    seeks to start_timestamp_ms using offsets_for_times(), and reads
    messages until end_timestamp_ms or max events cap.
    """
    if not getattr(settings, "KAFKA_ENABLED", False):
        raise DisasterRecoveryError("Kafka is not enabled (KAFKA_ENABLED=False)")

    topic = topic or getattr(settings, "RBAC_KAFKA_CONSUMER_TOPIC", None)
    if not topic:
        raise DisasterRecoveryError("No Kafka topic configured (RBAC_KAFKA_CONSUMER_TOPIC)")

    max_events = getattr(settings, "DR_MAX_EVENTS_PER_RECONCILE", 10000)
    group_id = getattr(settings, "DR_KAFKA_CONSUMER_GROUP_ID", "rbac-dr-consumer-group")

    consumer_kwargs = {
        "bootstrap_servers": settings.KAFKA_SERVERS,
        "group_id": group_id,
        "enable_auto_commit": False,
        "auto_offset_reset": "earliest",
        "consumer_timeout_ms": 10000,
        "value_deserializer": None,
    }

    kafka_auth = getattr(settings, "KAFKA_AUTH", {})
    if kafka_auth:
        for key in ("sasl_plain_username", "sasl_plain_password", "sasl_mechanism", "security_protocol", "ssl_cafile"):
            if key in kafka_auth:
                consumer_kwargs[key] = kafka_auth[key]

    consumer = KafkaConsumer(**consumer_kwargs)
    events: list[ParsedReplicationEvent] = []

    try:
        partitions = consumer.partitions_for_topic(topic)
        if not partitions:
            logger.warning("No partitions found for topic '%s'", topic)
            return events

        topic_partitions = [TopicPartition(topic, p) for p in partitions]
        consumer.assign(topic_partitions)

        timestamps = {tp: start_timestamp_ms for tp in topic_partitions}
        offsets = consumer.offsets_for_times(timestamps)

        if not offsets:
            logger.info("No offsets found for timestamp %d", start_timestamp_ms)
            return events

        has_valid_offset = False
        for tp, offset_and_timestamp in offsets.items():
            if offset_and_timestamp is not None:
                consumer.seek(tp, offset_and_timestamp.offset)
                has_valid_offset = True
            else:
                consumer.seek_to_end(tp)

        if not has_valid_offset:
            logger.info("No messages found at or after timestamp %d", start_timestamp_ms)
            return events

        poll_timeout_ms = 5000
        empty_polls = 0
        max_empty_polls = 3

        while len(events) < max_events:
            records = consumer.poll(timeout_ms=poll_timeout_ms)

            if not records:
                empty_polls += 1
                if empty_polls >= max_empty_polls:
                    break
                continue

            empty_polls = 0
            past_window = False

            for tp, messages in records.items():
                for message in messages:
                    if message.timestamp > end_timestamp_ms:
                        past_window = True
                        break

                    payload = _parse_debezium_payload(message.value)
                    if payload is None:
                        continue

                    event = _parse_event(payload, message.offset, message.partition, message.timestamp)
                    if event.relations_to_add or event.relations_to_remove:
                        events.append(event)

                    if len(events) >= max_events:
                        break

                if past_window or len(events) >= max_events:
                    break

            if past_window:
                break

        logger.info(
            "DR Kafka reader: read %d events from topic '%s' in window [%d, %d]",
            len(events),
            topic,
            start_timestamp_ms,
            end_timestamp_ms,
        )
    finally:
        consumer.close()

    return events
